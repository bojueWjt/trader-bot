from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)


ACCOUNT_ID = "account-a"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
POSITION_ID = "P-1"
NOW = datetime(2026, 6, 19, 12, tzinfo=timezone.utc)


class StrategyManageShellTest(unittest.TestCase):
    def test_on_data_replaces_stop_by_cancelling_cached_stop_and_submitting_new_one(self) -> None:
        intent = _intent(
            action="move_stop_loss",
            order_plan={"stop_price": "26000.114"},
        )
        old_stop = SimpleNamespace(
            client_order_id="old-stop",
            instrument_id=INSTRUMENT_ID,
            order_type="STOP_MARKET",
            side="SELL",
            quantity="0.5",
            trigger_price="25500",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=stop_loss"),
        )
        strategy = _HarnessStrategy(orders=[old_stop])

        strategy._handle_intent(intent)

        self.assertEqual(strategy.cancelled_client_order_ids, ["old-stop"])
        self.assertEqual(len(strategy.submitted_plans), 1)
        submitted = strategy.submitted_plans[0]
        self.assertEqual(submitted.order_type, "STOP_MARKET")
        self.assertEqual(submitted.trigger_price, "26000.11")
        self.assertTrue(submitted.reduce_only)
        self.assertIn("lifecycle_role=stop_loss", submitted.tags)

    def test_on_data_rejects_management_when_target_position_is_not_unique(self) -> None:
        intent = _intent(
            action="close_position",
            target_position_id=None,
            order_plan={"type": "market"},
        )
        strategy = _HarnessStrategy(
            positions=[
                _position(position_id="P-1"),
                _position(position_id="P-2"),
            ]
        )

        strategy._handle_intent(intent)

        self.assertEqual(strategy.submitted_plans, [])
        self.assertEqual(strategy.denials[-1].reason, "position_not_unique")

    def test_replace_take_profits_recovers_existing_lifecycle_from_cache(self) -> None:
        intent = _intent(
            action="replace_take_profits",
            order_plan={
                "take_profits": [
                    {"quantity": "0.2", "price": "28000"},
                    {"quantity": "0.3", "price": "29000"},
                ]
            },
        )
        old_tp = SimpleNamespace(
            client_order_id="old-tp",
            instrument_id=INSTRUMENT_ID,
            order_type="LIMIT_IF_TOUCHED",
            side="SELL",
            quantity="0.5",
            price="27500",
            trigger_price="27500",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=take_profit"),
        )
        strategy = _HarnessStrategy(orders=[old_tp])

        strategy._handle_intent(intent)

        self.assertEqual(strategy.cancelled_client_order_ids, ["old-tp"])
        self.assertEqual([plan.order_type for plan in strategy.submitted_plans], ["LIMIT_IF_TOUCHED", "LIMIT_IF_TOUCHED"])
        self.assertTrue(all(plan.reduce_only for plan in strategy.submitted_plans))
        self.assertTrue(all("lifecycle_role=take_profit" in plan.tags for plan in strategy.submitted_plans))

    def test_cancel_failure_does_not_submit_replacement_or_mark_processed(self) -> None:
        intent = _intent(
            action="move_stop_loss",
            order_plan={"stop_price": "26000"},
        )
        old_stop = SimpleNamespace(
            client_order_id="old-stop",
            instrument_id=INSTRUMENT_ID,
            order_type="STOP_MARKET",
            side="SELL",
            quantity="0.5",
            trigger_price="25500",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=stop_loss"),
        )
        strategy = _HarnessStrategy(orders=[old_stop], fail_cancel=True)

        strategy._handle_intent(intent)

        self.assertEqual(strategy.submitted_plans, [])
        self.assertEqual(strategy.denials[-1].reason, "order_cancel_failed")
        self.assertNotIn(str(intent.intent_id), strategy._processed_intent_ids)


class _HarnessStrategy(IntentExecutionStrategy):
    def __init__(self, positions=None, orders=None, fail_cancel=False):
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id=ACCOUNT_ID,
                trading_state="ACTIVE",
            )
        )
        self._positions = list(positions or [_position()])
        self._orders = list(orders or [])
        self.cancelled_client_order_ids: list[str] = []
        self.submitted_plans = []
        self._fail_cancel = fail_cancel

    def _now(self):
        return NOW

    def _cache_instrument(self, _instrument_id):
        return SimpleNamespace(
            id=INSTRUMENT_ID,
            price_increment="0.01",
            size_increment="0.001",
        )

    def _cache_positions(self, _instrument_id):
        return self._positions

    def _cache_orders(self, _instrument_id):
        return self._orders

    def _build_nautilus_order(self, plan, _instrument):
        self.submitted_plans.append(plan)
        return SimpleNamespace(client_order_id=plan.client_order_id, plan=plan)

    def submit_order(self, _order):
        return None

    def cancel_order(self, order):
        if self._fail_cancel:
            raise RuntimeError("cancel rejected")
        self.cancelled_client_order_ids.append(str(order.client_order_id))


def _position(position_id: str = POSITION_ID):
    return SimpleNamespace(
        id=position_id,
        instrument_id=INSTRUMENT_ID,
        side="LONG",
        quantity="0.5",
        entry_price="27123.456",
    )


@dataclass(frozen=True)
class _Intent:
    schema_version: str
    intent_id: UUID
    decision_id: UUID
    risk_decision_id: UUID
    account_id: str
    instrument_id: str
    action: str
    order_plan: dict[str, Any]
    valid_until: datetime
    idempotency_key: str
    approved_at: datetime
    target_position_id: str | None = None


def _intent(**overrides):
    intent_id = overrides.get("intent_id", uuid4())
    values = {
        "schema_version": "1.0",
        "intent_id": intent_id,
        "decision_id": uuid4(),
        "risk_decision_id": uuid4(),
        "account_id": ACCOUNT_ID,
        "instrument_id": INSTRUMENT_ID,
        "action": "move_stop_loss",
        "order_plan": {"stop_price": "26000"},
        "target_position_id": POSITION_ID,
        "valid_until": NOW + timedelta(minutes=5),
        "idempotency_key": sha256(str(intent_id).encode("ascii")).hexdigest(),
        "approved_at": NOW - timedelta(seconds=5),
    }
    values.update(overrides)
    return _Intent(**values)


if __name__ == "__main__":
    unittest.main()
