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
            persisted = strategy.persisted_before_schedule
            persisted_terminal = persisted[str(intent_id)][
                "last_protection_terminal_event"
            ]
            self.assertEqual(persisted_terminal, terminal)


class _ProtectionTerminalStrategy(IntentExecutionStrategy):
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self.scheduled_delays: list[float | None] = []
        self.persisted_before_schedule: dict = {}
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


if __name__ == "__main__":
    unittest.main()
