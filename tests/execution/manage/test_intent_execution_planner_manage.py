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
    ManagementPlan,
    OrderDenied,
    OrderPlan,
    OrderSnapshot,
    PlannerContext,
    PositionSnapshot,
    encode_client_order_id,
    plan_intent_execution,
)


ACCOUNT_ID = "account-a"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
POSITION_ID = "P-1"
NOW = datetime(2026, 6, 19, 12, tzinfo=timezone.utc)


class IntentExecutionPlannerManageTest(unittest.TestCase):
    def test_partial_close_creates_reduce_only_exit_for_target_position(self) -> None:
        intent = _intent(
            action="partial_close",
            target_position_id=POSITION_ID,
            order_plan={"type": "market", "quantity": "0.1244"},
        )

        result = plan_intent_execution(intent, _context())

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(len(result.orders), 1)
        order = result.orders[0]
        self.assertEqual(order.order_type, "MARKET")
        self.assertEqual(order.side, "SELL")
        self.assertEqual(order.quantity, "0.124")
        self.assertTrue(order.reduce_only)
        self.assertIn("lifecycle_role=exit", order.tags)
        self.assertIn(f"position_id={POSITION_ID}", order.tags)

    def test_close_position_uses_full_position_quantity_and_reduce_only(self) -> None:
        intent = _intent(
            action="close_position",
            target_position_id=POSITION_ID,
            order_plan={"type": "market"},
        )

        result = plan_intent_execution(intent, _context())

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.orders[0].quantity, "0.500")
        self.assertEqual(result.orders[0].side, "SELL")
        self.assertTrue(result.orders[0].reduce_only)

    def test_move_stop_loss_replaces_existing_stop_with_reduce_only_stop_market(self) -> None:
        intent = _intent(
            action="move_stop_loss",
            target_position_id=POSITION_ID,
            order_plan={"stop_price": "26000.116"},
        )

        result = plan_intent_execution(
            intent,
            _context(
                existing_orders=(
                    _order("old-stop", "stop_loss"),
                    _order("old-tp", "take_profit"),
                )
            ),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.cancel_order_ids, ("old-stop",))
        self.assertEqual(len(result.orders), 1)
        order = result.orders[0]
        self.assertEqual(order.order_type, "STOP_MARKET")
        self.assertEqual(order.trigger_price, "26000.12")
        self.assertIsNone(order.price)
        self.assertTrue(order.reduce_only)
        self.assertIn("lifecycle_role=stop_loss", order.tags)

    def test_move_stop_to_entry_uses_position_entry_price(self) -> None:
        intent = _intent(
            action="move_stop_to_entry",
            target_position_id=POSITION_ID,
            order_plan={},
        )

        result = plan_intent_execution(intent, _context())

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.orders[0].order_type, "STOP_MARKET")
        self.assertEqual(result.orders[0].trigger_price, "27123.46")
        self.assertTrue(result.orders[0].reduce_only)

    def test_replace_take_profits_uses_market_if_touched_triggers(self) -> None:
        intent = _intent(
            action="replace_take_profits",
            target_position_id=POSITION_ID,
            order_plan={
                "take_profits": [
                    {"quantity": "0.1", "price": "28000.111"},
                    {"quantity": "0.2", "trigger_price": "29000", "limit_price": "28999.995"},
                ]
            },
        )

        result = plan_intent_execution(
            intent,
            _context(existing_orders=(_order("old-tp", "take_profit"),)),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.cancel_order_ids, ("old-tp",))
        self.assertEqual(
            [order.order_type for order in result.orders],
            ["MARKET_IF_TOUCHED", "MARKET_IF_TOUCHED"],
        )
        self.assertEqual([order.quantity for order in result.orders], ["0.100", "0.200"])
        self.assertEqual(
            [order.trigger_price for order in result.orders],
            ["28000.11", "29000.00"],
        )
        self.assertTrue(all(order.price is None for order in result.orders))
        self.assertTrue(all(order.reduce_only for order in result.orders))
        self.assertTrue(all("lifecycle_role=take_profit" in order.tags for order in result.orders))

    def test_position_required_actions_fail_closed_without_unique_target(self) -> None:
        intent = _intent(
            action="close_position",
            target_position_id=None,
            order_plan={"type": "market"},
        )

        self.assertEqual(
            plan_intent_execution(intent, _context(positions=())),
            OrderDenied(reason="position_required", detail=INSTRUMENT_ID),
        )
        self.assertEqual(
            plan_intent_execution(
                intent,
                _context(
                    positions=(
                        _position(position_id="P-LONG", side="LONG"),
                        _position(position_id="P-SHORT", side="SHORT"),
                    )
                ),
            ),
            OrderDenied(reason="position_not_unique", detail=INSTRUMENT_ID),
        )
        self.assertEqual(
            plan_intent_execution(
                _intent(
                    action="close_position",
                    target_position_id="missing",
                    order_plan={"type": "market"},
                ),
                _context(),
            ),
            OrderDenied(reason="position_required", detail="missing"),
        )

    def test_management_selects_position_from_order_plan_position_side(self) -> None:
        long_position = _position(position_id="P-LONG", side="LONG")
        short_position = _position(position_id="P-SHORT", side="SHORT")
        intent = _intent(
            action="close_position",
            target_position_id=None,
            order_plan={"type": "market", "position_side": "short"},
        )

        result = plan_intent_execution(
            intent,
            _context(
                position=long_position,
                positions=(long_position, short_position),
            ),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.target_position_id, "P-SHORT")
        self.assertEqual(result.target_position_side, "SHORT")
        self.assertEqual(result.orders[0].side, "BUY")

    def test_management_without_position_side_uses_exact_target_position_id(self) -> None:
        long_position = _position(position_id="P-LONG", side="LONG")
        short_position = _position(position_id="P-SHORT", side="SHORT")
        intent = _intent(
            action="close_position",
            target_position_id="P-SHORT",
            order_plan={"type": "market"},
        )

        result = plan_intent_execution(
            intent,
            _context(
                position=long_position,
                positions=(long_position, short_position),
            ),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.target_position_id, "P-SHORT")
        self.assertEqual(result.target_position_side, "SHORT")
        self.assertEqual(result.orders[0].side, "BUY")

    def test_management_without_position_side_uses_single_live_position(self) -> None:
        intent = _intent(
            action="close_position",
            target_position_id=None,
            order_plan={"type": "market"},
        )

        result = plan_intent_execution(intent, _context())

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.target_position_id, POSITION_ID)
        self.assertEqual(result.target_position_side, "LONG")
        self.assertEqual(result.orders[0].side, "SELL")

    def test_management_fails_closed_when_target_position_conflicts_with_position_side(self) -> None:
        short_position = _position(position_id="P-SHORT", side="SHORT")
        intent = _intent(
            action="close_position",
            target_position_id="P-SHORT",
            order_plan={"type": "market", "position_side": "long"},
        )

        result = plan_intent_execution(
            intent,
            _context(position=short_position, positions=(short_position,)),
        )

        self.assertEqual(
            result,
            OrderDenied(
                reason="position_side_mismatch",
                detail="requested=LONG,actual=SHORT",
            ),
        )

    def test_management_fails_closed_when_requested_position_side_has_no_position(self) -> None:
        short_position = _position(position_id="P-SHORT", side="SHORT")
        intent = _intent(
            action="close_position",
            target_position_id=None,
            order_plan={"type": "market", "position_side": "long"},
        )

        result = plan_intent_execution(
            intent,
            _context(position=short_position, positions=(short_position,)),
        )

        self.assertEqual(
            result,
            OrderDenied(reason="position_required", detail=f"{INSTRUMENT_ID}:LONG"),
        )

    def test_cancel_order_requires_account_instrument_and_intent_owned_live_order(self) -> None:
        owner_intent_id = uuid4()
        target = encode_client_order_id(owner_intent_id)
        intent = _intent(
            action="cancel_order",
            target_position_id=None,
            order_plan={"cancel_client_order_id": target},
        )

        result = plan_intent_execution(
            intent,
            _context(
                existing_orders=(_owned_order(target, owner_intent_id),),
                existing_intent_ids=frozenset({str(owner_intent_id)}),
            ),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.cancel_order_ids, (target,))

    def test_cancel_order_accepts_durable_system_id_when_snapshot_tags_are_missing(self) -> None:
        owner_intent_id = uuid4()
        target = encode_client_order_id(owner_intent_id)
        intent = _intent(
            action="cancel_order",
            target_position_id=None,
            order_plan={"cancel_client_order_id": target},
        )

        result = plan_intent_execution(
            intent,
            _context(
                existing_orders=(_order(target, "stop_loss"),),
                existing_intent_ids=frozenset(),
            ),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.cancel_order_ids, (target,))

    def test_cancel_order_rejects_order_owned_by_another_account(self) -> None:
        owner_intent_id = uuid4()
        target = encode_client_order_id(owner_intent_id)
        intent = _intent(
            action="cancel_order",
            target_position_id=None,
            order_plan={"cancel_client_order_id": target},
        )

        result = plan_intent_execution(
            intent,
            _context(
                existing_orders=(
                    _owned_order(
                        target,
                        owner_intent_id,
                        account_id="account-b",
                    ),
                ),
                existing_intent_ids=frozenset({str(owner_intent_id)}),
            ),
        )

        self.assertEqual(
            result,
            OrderDenied(reason="order_ownership_unverified", detail=target),
        )

    def test_cancel_order_accepts_matching_tags_without_processed_intent_history(self) -> None:
        owner_intent_id = uuid4()
        target = encode_client_order_id(owner_intent_id)
        intent = _intent(
            action="cancel_order",
            target_position_id=None,
            order_plan={"cancel_client_order_id": target},
        )

        result = plan_intent_execution(
            intent,
            _context(
                existing_orders=(_owned_order(target, owner_intent_id),),
                existing_intent_ids=frozenset(),
            ),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.cancel_order_ids, (target,))

    def test_cancel_order_rejects_order_with_conflicting_intent_tag(self) -> None:
        owner_intent_id = uuid4()
        target = encode_client_order_id(owner_intent_id)
        intent = _intent(
            action="cancel_order",
            target_position_id=None,
            order_plan={"cancel_client_order_id": target},
        )

        result = plan_intent_execution(
            intent,
            _context(
                existing_orders=(_owned_order(target, uuid4()),),
                existing_intent_ids=frozenset({str(owner_intent_id)}),
            ),
        )

        self.assertEqual(
            result,
            OrderDenied(reason="order_ownership_unverified", detail=target),
        )

    def test_management_quantity_cannot_exceed_position(self) -> None:
        intent = _intent(
            action="partial_close",
            target_position_id=POSITION_ID,
            order_plan={"type": "market", "quantity": "0.6"},
        )

        result = plan_intent_execution(intent, _context())

        self.assertEqual(
            result,
            OrderDenied(reason="quantity_exceeds_position", detail="0.600>0.5"),
        )


def _context(**overrides: Any) -> PlannerContext:
    position = _position()
    values = {
        "account_id": ACCOUNT_ID,
        "trading_state": "ACTIVE",
        "now": NOW,
        "instrument": InstrumentSpec(
            instrument_id=INSTRUMENT_ID,
            price_increment="0.01",
            quantity_increment="0.001",
        ),
        "position": position,
        "positions": (position,),
        "existing_orders": (),
        "existing_intent_ids": frozenset(),
    }
    values.update(overrides)
    if "positions" in overrides and "position" not in overrides:
        positions = overrides["positions"]
        values["position"] = positions[0] if positions else None
    return PlannerContext(**values)


def _position(position_id: str = POSITION_ID, side: str = "LONG") -> PositionSnapshot:
    return PositionSnapshot(
        instrument_id=INSTRUMENT_ID,
        side=side,
        quantity="0.5",
        position_id=position_id,
        entry_price="27123.456",
    )


def _order(client_order_id: str, role: str) -> OrderSnapshot:
    return OrderSnapshot(
        client_order_id=client_order_id,
        instrument_id=INSTRUMENT_ID,
        order_type="STOP_MARKET" if role == "stop_loss" else "MARKET_IF_TOUCHED",
        side="SELL",
        quantity="0.5",
        price=None,
        trigger_price="26000",
        tags=(f"position_id={POSITION_ID}", f"lifecycle_role={role}"),
    )


def _owned_order(
    client_order_id: str,
    intent_id: UUID,
    *,
    account_id: str = ACCOUNT_ID,
) -> OrderSnapshot:
    return OrderSnapshot(
        client_order_id=client_order_id,
        instrument_id=INSTRUMENT_ID,
        order_type="STOP_MARKET",
        side="SELL",
        quantity="0.5",
        price=None,
        trigger_price="26000",
        tags=(
            f"account_id={account_id}",
            f"intent_id={intent_id}",
            f"position_id={POSITION_ID}",
            "lifecycle_role=stop_loss",
        ),
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


def _intent(**overrides: Any) -> _Intent:
    intent_id = overrides.get("intent_id", uuid4())
    values: dict[str, Any] = {
        "schema_version": "1.0",
        "intent_id": intent_id,
        "decision_id": uuid4(),
        "risk_decision_id": uuid4(),
        "account_id": ACCOUNT_ID,
        "instrument_id": INSTRUMENT_ID,
        "action": "partial_close",
        "order_plan": {"type": "market", "quantity": "0.1"},
        "target_position_id": POSITION_ID,
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
            "authorized_by_id": "manage-planner-test",
            "source_message_id": f"manage-planner-test-{intent_id}",
        },
    )
    values["order_plan"] = order_plan
    return _Intent(**values)


if __name__ == "__main__":
    unittest.main()
