from __future__ import annotations

import sys
import unittest
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from strategy.intent_execution_strategy import (  # noqa: E402
    _book_id_quantity,
    _hedge_cache_blocks_reduce_only,
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)
from strategy.intent_execution_planner import OrderPlan, encode_client_order_id
from runtime.intent_execution_inbox import IntentExecutionIdentity
from nautilus_trader.model.enums import OmsType


INSTRUMENT_ID = "ALGOUSDT-PERP.BINANCE"


def _pos(position_id: str, quantity: str, side: str = "LONG") -> SimpleNamespace:
    return SimpleNamespace(
        id=position_id,
        position_id=position_id,
        quantity=quantity,
        side=side,
    )


class HedgeReduceOnlySubmissionTest(unittest.TestCase):
    def test_actual_submit_paths_require_fresh_authorized_reduction(self) -> None:
        for mode in ("sync", "prepared"):
            for case in ("empty", "undersized", "external", "sufficient", "unknown", "stale",
                         "oversize", "authority_oversize", "account_mismatch", "book_mismatch", "manual_unowned", "first_protection", "short"):
                with self.subTest(mode=mode, case=case), tempfile.TemporaryDirectory() as directory:
                    positions = []
                    if case == "undersized":
                        positions = [_pos(f"{INSTRUMENT_ID}-LONG", "0.2")]
                    elif case == "external":
                        positions = [_pos(f"{INSTRUMENT_ID}-EXTERNAL", "3")]
                    elif case == "sufficient":
                        positions = [_pos(f"{INSTRUMENT_ID}-LONG", "3")]
                    strategy = _SubmissionHarness(directory, positions)
                    book = "SHORT" if case == "short" else "LONG"
                    intent_id = uuid4()
                    actor_type = "channel" if case in {"manual_unowned", "first_protection"} else "user"
                    auth = {"authorized_by_type": actor_type, "authorized_by_id": "operator",
                            "source_message_id": "approved-close", "parent_intent_id": str(intent_id)}
                    identity = IntentExecutionIdentity(
                        account_id="account-a", intent_id=str(intent_id),
                        idempotency_key=sha256(str(intent_id).encode()).hexdigest(),
                        instrument_id=INSTRUMENT_ID, action="close_position",
                    )
                    order_id = encode_client_order_id(intent_id, sequence=11 if case == "first_protection" else 1)
                    payload = {"action": "close_position", "order_plan": {
                        "authorization": auth, "position_side": "SHORT" if case == "book_mismatch" else book,
                        "quantity": "1" if case == "authority_oversize" else "3",
                    }}
                    if case != "first_protection":
                        strategy._intent_execution_inbox.register_received(identity, payload)
                        strategy._intent_execution_inbox.begin_dispatch(identity, (order_id,))
                    account_tag = "account-b" if case == "account_mismatch" else "account-a"
                    plan = OrderPlan(
                        intent_id=intent_id, client_order_id=order_id,
                        tags=tuple(f"{key}={value}" for key, value in auth.items()) + (
                            f"account_id={account_tag}", f"position_id={INSTRUMENT_ID}-{book}", "action=close_position",
                        ), instrument_id=INSTRUMENT_ID, side="BUY" if case == "short" else "SELL", order_type="MARKET",
                        quantity="4" if case == "oversize" else "1.5", price=None, time_in_force="IOC", reduce_only=True,
                    )
                    if case == "first_protection":
                        plan = replace(plan, quantity="0.5", order_type="STOP_MARKET", trigger_price="100")
                        strategy._entry_protection_stash[str(intent_id)] = {
                            "instrument_id": INSTRUMENT_ID, "entry_side": "BUY", "protected_quantity": None,
                            "entry_tags": tuple(f"{key}={value}" for key, value in auth.items()),
                        }
                        strategy.orders = [SimpleNamespace(
                            client_order_id=encode_client_order_id(intent_id), instrument_id=INSTRUMENT_ID,
                            filled_qty="0.8", status="FILLED",
                        )]
                    fetched = datetime.now(timezone.utc)
                    if case == "stale":
                        fetched -= timedelta(seconds=60)
                    if case != "unknown":
                        strategy._exchange_evidence_provider = SimpleNamespace(cached_snapshot=lambda **_kwargs: {
                            "fetched_at": fetched,
                            "positions": [{"symbol": "ALGOUSDT", "position_side": book, "quantity": "3"}],
                            "regular_orders": [], "algo_orders": [],
                        })
                    try:
                        if mode == "sync":
                            submitted = strategy._submit_order_plan(plan)
                        else:
                            # A prebuilt order must not bypass the same final evidence check.
                            prepared = SimpleNamespace(instrument_id=INSTRUMENT_ID, client_order_id=order_id, plan=plan)
                            submitted = strategy._submit_order_plan_after_durable_prepare(
                                plan, live_canary_execution=False, prepared_order=prepared,
                            )
                        success = case in {"empty", "undersized", "external", "sufficient", "first_protection", "short"}
                        self.assertEqual(submitted, success)
                        self.assertEqual(len(strategy.sent), int(success))
                        if success:
                            order, position_id = strategy.sent[0]
                            self.assertEqual(str(position_id), f"{INSTRUMENT_ID}-{book}")
                            self.assertEqual(order.plan.reduce_only, case == "sufficient")
                            self.assertTrue(plan.reduce_only)
                            self.assertEqual(strategy.positions, positions)
                        else:
                            self.assertTrue(strategy.denials[-1].reason.startswith("reduction_"))
                    finally:
                        strategy.on_stop()

    def test_empty_cache_blocks_reduce_only(self) -> None:
        self.assertTrue(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "SELL", "2374.2", (),
            )
        )

    def test_undersized_book_id_blocks_reduce_only(self) -> None:
        positions = (
            _pos(f"{INSTRUMENT_ID}-LONG", "1302.1"),
        )
        self.assertTrue(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "SELL", "2374.2", positions,
            )
        )
        self.assertEqual(
            _book_id_quantity(INSTRUMENT_ID, "LONG", positions),
            __import__("decimal").Decimal("1302.1"),
        )

    def test_external_only_cache_blocks_reduce_only(self) -> None:
        positions = (
            _pos(f"{INSTRUMENT_ID}-EXTERNAL", "11871.2"),
        )
        self.assertTrue(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "SELL", "2374.2", positions,
            )
        )
        self.assertEqual(
            _book_id_quantity(INSTRUMENT_ID, "LONG", positions),
            __import__("decimal").Decimal("0"),
        )

    def test_sufficient_book_id_keeps_reduce_only(self) -> None:
        positions = (
            _pos(f"{INSTRUMENT_ID}-LONG", "11871.2"),
        )
        self.assertFalse(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "SELL", "2374.2", positions,
            )
        )

    def test_buy_reduce_uses_short_book(self) -> None:
        positions = (
            _pos(f"{INSTRUMENT_ID}-SHORT", "10", side="SHORT"),
        )
        self.assertFalse(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "BUY", "4", positions,
            )
        )
        self.assertTrue(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "BUY", "11", positions,
            )
        )


class _SubmissionHarness(IntentExecutionStrategy):
    def __init__(self, directory, positions):
        super().__init__(IntentExecutionStrategyConfig(
            account_id="account-a", trading_state="HALTED", oms_type=OmsType.HEDGING,
            intent_execution_inbox_path=str(Path(directory) / "inbox.json"),
        ))
        self.positions = positions
        self.orders = []
        self.sent = []

    def _cache_positions(self, _instrument_id):
        return self.positions

    def _cache_orders(self, _instrument_id):
        return self.orders

    def _cache_instrument(self, _instrument_id):
        return SimpleNamespace(id=INSTRUMENT_ID)

    def _build_nautilus_order(self, plan, _instrument):
        return SimpleNamespace(instrument_id=plan.instrument_id, client_order_id=plan.client_order_id, plan=plan)

    def submit_order(self, order, position_id=None):
        self.sent.append((order, position_id))


if __name__ == "__main__":
    unittest.main()
