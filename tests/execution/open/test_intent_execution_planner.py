from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from strategy.intent_execution_planner import (  # noqa: E402
    InstrumentSpec,
    OrderDenied,
    OrderPlan,
    PlannerContext,
    PositionSnapshot,
    decode_client_order_id,
    encode_client_order_id,
    plan_intent_execution,
)


ACCOUNT_ID = "account-a"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
NOW = datetime(2026, 6, 19, 12, tzinfo=timezone.utc)


class IntentExecutionPlannerTest(unittest.TestCase):
    def test_client_order_id_round_trips_intent_trace(self) -> None:
        intent_id = uuid4()

        client_order_id = encode_client_order_id(intent_id, sequence=7)

        self.assertEqual(len(client_order_id), 35)
        decoded = decode_client_order_id(client_order_id)
        self.assertEqual(decoded.intent_id, intent_id)
        self.assertEqual(decoded.sequence, 7)

    def test_market_open_maps_to_market_order_with_rounded_quantity_and_trace_tags(
        self,
    ) -> None:
        intent = _intent(
            action="open_position",
            order_plan={"type": "market", "side": "buy", "quantity": "0.1236"},
        )

        result = plan_intent_execution(intent, _context(position=None))

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.order_type, "MARKET")
        self.assertEqual(result.side, "BUY")
        self.assertEqual(result.quantity, "0.124")
        self.assertIsNone(result.price)
        self.assertEqual(result.time_in_force, "IOC")
        self.assertEqual(decode_client_order_id(result.client_order_id).intent_id, intent.intent_id)
        self.assertIn(f"intent_id={intent.intent_id}", result.tags)
        self.assertIn(f"decision_id={intent.decision_id}", result.tags)
        self.assertIn(f"risk_decision_id={intent.risk_decision_id}", result.tags)
        self.assertIn(f"idempotency_key={intent.idempotency_key}", result.tags)

    def test_limit_add_maps_to_limit_order_when_position_matches_side(self) -> None:
        intent = _intent(
            action="add_position",
            order_plan={
                "type": "limit",
                "side": "buy",
                "quantity": "0.1236",
                "price": "27123.457",
                "time_in_force": "GTC",
            },
        )

        result = plan_intent_execution(intent, _context(position=_position("LONG")))

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.order_type, "LIMIT")
        self.assertEqual(result.side, "BUY")
        self.assertEqual(result.quantity, "0.124")
        self.assertEqual(result.price, "27123.46")
        self.assertEqual(result.time_in_force, "GTC")

    def test_zone_maps_to_boundary_limit_order(self) -> None:
        buy_intent = _intent(
            order_plan={
                "type": "zone",
                "side": "buy",
                "quantity": "0.25",
                "price_min": "27000",
                "price_max": "27123.457",
            },
        )
        sell_intent = _intent(
            order_plan={
                "type": "zone",
                "side": "sell",
                "quantity": "0.25",
                "price_min": "27000.114",
                "price_max": "27123",
            },
        )

        buy_order = plan_intent_execution(buy_intent, _context(position=None))
        sell_order = plan_intent_execution(sell_intent, _context(position=None))

        self.assertIsInstance(buy_order, OrderPlan)
        self.assertIsInstance(sell_order, OrderPlan)
        assert isinstance(buy_order, OrderPlan)
        assert isinstance(sell_order, OrderPlan)
        self.assertEqual(buy_order.order_type, "LIMIT")
        self.assertEqual(buy_order.price, "27123.46")
        self.assertEqual(sell_order.order_type, "LIMIT")
        self.assertEqual(sell_order.price, "27000.11")

    def test_halted_or_reducing_node_denies_open_and_add(self) -> None:
        for state in ("HALTED", "REDUCING"):
            with self.subTest(state=state):
                result = plan_intent_execution(_intent(), _context(trading_state=state))

                self.assertEqual(result, OrderDenied(reason="trading_not_active", detail=state))

    def test_duplicate_intent_denies_without_second_order(self) -> None:
        intent = _intent()

        result = plan_intent_execution(
            intent,
            _context(existing_intent_ids=frozenset({str(intent.intent_id)})),
        )

        self.assertEqual(
            result,
            OrderDenied(reason="duplicate_intent", detail=str(intent.intent_id)),
        )

    def test_validates_instrument_account_expiry_and_position_before_ordering(self) -> None:
        expired = _intent(valid_until=NOW - timedelta(seconds=1))
        wrong_account = _intent(account_id="account-b")
        open_with_position = _intent(action="open_position")
        add_without_position = _intent(action="add_position")
        add_wrong_side = _intent(
            action="add_position",
            order_plan={"type": "market", "side": "sell", "quantity": "1"},
        )

        self.assertEqual(
            plan_intent_execution(expired, _context()),
            OrderDenied(reason="expired", detail=expired.valid_until.isoformat()),
        )
        self.assertEqual(
            plan_intent_execution(wrong_account, _context()),
            OrderDenied(reason="wrong_account", detail="account-b"),
        )
        self.assertEqual(
            plan_intent_execution(_intent(), _context(instrument=None)),
            OrderDenied(reason="instrument_not_found", detail=INSTRUMENT_ID),
        )
        self.assertEqual(
            plan_intent_execution(open_with_position, _context(position=_position("LONG"))),
            OrderDenied(reason="position_exists", detail=INSTRUMENT_ID),
        )
        self.assertEqual(
            plan_intent_execution(add_without_position, _context(position=None)),
            OrderDenied(reason="position_required", detail=INSTRUMENT_ID),
        )
        self.assertEqual(
            plan_intent_execution(add_wrong_side, _context(position=_position("LONG"))),
            OrderDenied(reason="position_side_mismatch", detail="LONG"),
        )

    def test_unsupported_order_spec_fails_closed(self) -> None:
        unsupported_type = _intent(order_plan={"type": "stop", "side": "buy", "quantity": "1"})
        missing_price = _intent(order_plan={"type": "limit", "side": "buy", "quantity": "1"})
        invalid_zone = _intent(
            order_plan={
                "type": "zone",
                "side": "buy",
                "quantity": "1",
                "price_min": "11",
                "price_max": "10",
            }
        )

        self.assertEqual(
            plan_intent_execution(unsupported_type, _context(position=None)),
            OrderDenied(reason="unsupported_order_spec", detail="type=stop"),
        )
        self.assertEqual(
            plan_intent_execution(missing_price, _context(position=None)),
            OrderDenied(reason="unsupported_order_spec", detail="limit.price"),
        )
        self.assertEqual(
            plan_intent_execution(invalid_zone, _context(position=None)),
            OrderDenied(reason="unsupported_order_spec", detail="zone.price_min_gt_price_max"),
        )


def _context(**overrides: Any) -> PlannerContext:
    values = {
        "account_id": ACCOUNT_ID,
        "trading_state": "ACTIVE",
        "now": NOW,
        "instrument": InstrumentSpec(
            instrument_id=INSTRUMENT_ID,
            price_increment="0.01",
            quantity_increment="0.001",
        ),
        "position": None,
        "existing_intent_ids": frozenset(),
    }
    values.update(overrides)
    return PlannerContext(**values)


def _position(side: str) -> PositionSnapshot:
    return PositionSnapshot(instrument_id=INSTRUMENT_ID, side=side, quantity="0.5")


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


def _intent(**overrides: Any) -> _Intent:
    intent_id = overrides.get("intent_id", uuid4())
    values: dict[str, Any] = {
        "schema_version": "1.0",
        "intent_id": intent_id,
        "decision_id": uuid4(),
        "risk_decision_id": uuid4(),
        "account_id": ACCOUNT_ID,
        "instrument_id": INSTRUMENT_ID,
        "action": "open_position",
        "order_plan": {"type": "market", "side": "buy", "quantity": "1"},
        "target_position_id": None,
        "valid_until": NOW + timedelta(minutes=5),
        "idempotency_key": sha256(str(intent_id).encode("ascii")).hexdigest(),
        "approved_at": NOW - timedelta(seconds=5),
    }
    values.update(overrides)
    order_plan = dict(values["order_plan"])
    order_plan.setdefault(
        "authorization",
        {
            "authorized_by_type": "user",
            "authorized_by_id": "planner-test",
            "source_message_id": f"planner-test-{intent_id}",
        },
    )
    values["order_plan"] = order_plan
    return _Intent(**values)


if __name__ == "__main__":
    unittest.main()
