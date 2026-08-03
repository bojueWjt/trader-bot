from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from strategy.intent_execution_planner import encode_client_order_id  # noqa: E402
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)


class StrategyShellTest(unittest.TestCase):
    def test_active_intent_ids_reads_client_order_id_and_tags(self) -> None:
        order_intent_id = uuid4()
        tagged_intent_id = uuid4()

        class _CacheStubStrategy(IntentExecutionStrategy):
            # Nautilus Actor.cache is read-only; override the cache-read helpers to
            # exercise _active_intent_ids without touching the real cache property.
            def _cache_orders(self, _instrument_id):
                return [
                    SimpleNamespace(client_order_id=encode_client_order_id(order_intent_id)),
                    SimpleNamespace(tags=[f"intent_id={tagged_intent_id}"]),
                ]

            def _cache_positions(self, _instrument_id):
                return ()

        strategy = _CacheStubStrategy(
            IntentExecutionStrategyConfig(account_id="account-a", trading_state="ACTIVE")
        )

        self.assertEqual(
            strategy._active_intent_ids("BTCUSDT-PERP.BINANCE"),
            {str(order_intent_id), str(tagged_intent_id)},
        )

    def test_trading_state_getter_accepts_enum_values(self) -> None:
        class TradingState(Enum):
            ACTIVE = "ACTIVE"

        strategy = IntentExecutionStrategy(
            IntentExecutionStrategyConfig(account_id="account-a")
        )
        strategy.set_trading_state_getter(lambda: TradingState.ACTIVE)

        self.assertEqual(strategy._trading_state(), "ACTIVE")

    def test_protection_rejection_is_persisted_before_retry_without_advancing_revision(self) -> None:
        intent_id = uuid4()
        client_order_id = encode_client_order_id(intent_id, sequence=22)
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _ProtectionTerminalStrategy(Path(state_dir))
            strategy._entry_protection_stash[str(intent_id)] = {
                "instrument_id": "ATOMUSDT-PERP.BINANCE",
                "protection_sequence_start": 11,
                "protection_revision": 2,
                "protection_ids": (client_order_id,),
                "protection_roles": {
                    client_order_id: {
                        "role": "take_profit",
                        "tp_price": "6.75",
                        "quantity": "144.17",
                        "submitted_at": "",
                    }
                },
                "pending_cancel_ids": (),
            }
            event = SimpleNamespace(
                event_type="OrderRejected",
                client_order_id=client_order_id,
                instrument_id="ATOMUSDT-PERP.BINANCE",
                reason="Filter failure: PERCENT_PRICE (-4131)",
                ts_event=1785312000000000000,
            )

            strategy.on_order_rejected(event)

            terminal = strategy._entry_protection_stash[str(intent_id)][
                "last_protection_terminal_event"
            ]
            self.assertEqual(terminal["event_type"], "OrderRejected")
            self.assertEqual(terminal["reason"], "Filter failure: PERCENT_PRICE (-4131)")
            self.assertEqual(terminal["error_code"], "-4131")
            self.assertEqual(terminal["role"], "take_profit")
            self.assertEqual(terminal["tp_price"], "6.75")
            self.assertEqual(terminal["protection_revision"], 2)
            self.assertEqual(terminal["ts_event"], "1785312000000000000")
            self.assertEqual(
                strategy._entry_protection_stash[str(intent_id)]["protection_revision"],
                2,
            )
            self.assertEqual(
                strategy._entry_protection_stash[str(intent_id)]["sync_retries"],
                1,
            )
            self.assertEqual(
                strategy.scheduled_delays,
                [strategy._PROTECTION_SYNC_DELAY_S],
            )
            self.assertEqual(strategy.submitted_plans, [])
            persisted = strategy.persisted_before_schedule
            persisted_terminal = persisted[str(intent_id)][
                "last_protection_terminal_event"
            ]
            self.assertEqual(persisted_terminal, terminal)

    def test_mit_immediate_trigger_rejection_submits_reduce_only_market_once(self) -> None:
        intent_id = uuid4()
        source_client_order_id = encode_client_order_id(intent_id, sequence=22)
        tags = (
            f"intent_id={intent_id}",
            "lifecycle_role=take_profit",
            "position_id=ATOMUSDT-PERP.BINANCE-LONG",
        )
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _ProtectionTerminalStrategy(Path(state_dir))
            reported_events: list[dict] = []
            strategy.set_protection_event_reporter(
                lambda event: reported_events.append(event) is None or True
            )
            strategy._entry_protection_stash[str(intent_id)] = {
                "instrument_id": "ATOMUSDT-PERP.BINANCE",
                "entry_side": "BUY",
                "entry_tags": tags,
                "take_profits": ({"price": "6.75"},),
                "take_profit_quantities": ("144.17",),
                "tp_consumed": {},
                "protection_sequence_start": 11,
                "protection_revision": 2,
                "protection_ids": (source_client_order_id,),
                "protection_roles": {
                    source_client_order_id: {
                        "role": "take_profit",
                        "tp_price": "6.75",
                        "quantity": "144.17",
                        "submitted_at": "",
                        "order_type": "MARKET_IF_TOUCHED",
                        "side": "SELL",
                        "tags": tags,
                    }
                },
                "pending_cancel_ids": (),
            }
            event = SimpleNamespace(
                event_type="OrderRejected",
                client_order_id=source_client_order_id,
                instrument_id="ATOMUSDT-PERP.BINANCE",
                order_type="MARKET_IF_TOUCHED",
                side="SELL",
                tags=tags,
                reason="Order would immediately trigger. (-2021)",
            )

            strategy.on_order_rejected(event)
            strategy.on_order_rejected(event)

            self.assertEqual(len(strategy.submitted_plans), 1)
            fallback_plan = strategy.submitted_plans[0]
            self.assertEqual(fallback_plan.order_type, "MARKET")
            self.assertEqual(fallback_plan.quantity, "144.17")
            self.assertEqual(fallback_plan.side, "SELL")
            self.assertTrue(fallback_plan.reduce_only)
            self.assertEqual(fallback_plan.tags, tags)
            self.assertTrue(fallback_plan.client_order_id.startswith("M"))
            self.assertEqual(
                fallback_plan.client_order_id[1:],
                source_client_order_id[1:],
            )
            self.assertEqual(strategy.scheduled_delays, [])
            self.assertEqual(len(reported_events), 1)
            self.assertEqual(
                reported_events[0]["event_type"],
                "TakeProfitImmediateMarketFallback",
            )
            remaining = strategy._take_profit_remaining_quantities(
                strategy._entry_protection_stash[str(intent_id)],
                ({"price": "6.75"},),
                "144.17",
                "0.01",
            )
            self.assertEqual(remaining, (None,))

            strategy.on_order_filled(
                SimpleNamespace(
                    client_order_id=fallback_plan.client_order_id,
                    instrument_id="ATOMUSDT-PERP.BINANCE",
                    last_qty="144.17",
                )
            )

            stash = strategy._entry_protection_stash[str(intent_id)]
            fallback_state = stash["tp_market_fallbacks"][
                fallback_plan.client_order_id
            ]
            self.assertEqual(fallback_state["status"], "filled")
            self.assertEqual(fallback_state["remaining_quantity"], "0")
            self.assertEqual(stash["tp_consumed"]["6.75"], "144.17")

    def test_revisions_exhausted_emits_one_protection_frozen_event(self) -> None:
        intent_id = uuid4()
        strategy = IntentExecutionStrategy(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                trading_state="ACTIVE",
            )
        )
        reported_events: list[dict] = []
        strategy.set_protection_event_reporter(
            lambda event: reported_events.append(event) is None or True
        )
        stash = {
            "instrument_id": "ATOMUSDT-PERP.BINANCE",
            "protection_revision": strategy._PROTECTION_MAX_REVISION,
        }

        strategy._normalize_protection_stash(str(intent_id), stash)
        strategy._normalize_protection_stash(str(intent_id), stash)

        self.assertEqual(stash["protection_frozen"], "revisions_exhausted")
        self.assertEqual(
            stash["protection_freeze_denial_reason"],
            "protection_revisions_exhausted",
        )
        self.assertEqual(len(reported_events), 1)
        self.assertEqual(reported_events[0]["event_type"], "ProtectionFrozen")
        self.assertEqual(
            reported_events[0]["payload"]["reason"],
            "revisions_exhausted",
        )


class _ProtectionTerminalStrategy(IntentExecutionStrategy):
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self.scheduled_delays: list[float | None] = []
        self.persisted_before_schedule: dict = {}
        self.submitted_plans: list = []
        super().__init__(
            IntentExecutionStrategyConfig(account_id="account-a", trading_state="ACTIVE")
        )

    def _protection_stash_path(self) -> str:
        return str(self._state_dir / self._PROTECTION_STASH_FILENAME)

    def _now(self) -> datetime:
        return datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    def _schedule_protection_sync(
        self,
        _intent_key: str,
        delay_seconds: float | None = None,
    ) -> None:
        with open(self._protection_stash_path(), "r") as fh:
            self.persisted_before_schedule = json.load(fh)
        self.scheduled_delays.append(delay_seconds)

    def _submit_order_plan(self, plan) -> bool:
        self.submitted_plans.append(plan)
        return True


if __name__ == "__main__":
    unittest.main()
