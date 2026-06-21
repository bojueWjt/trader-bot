from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from strategy.intent_execution_planner import (
    InstrumentSpec,
    ManagementPlan,
    OrderDenied,
    OrderSnapshot,
    PlannerContext,
    PositionSnapshot,
    plan_intent_execution,
)


ACCOUNT_ID = "acct-om5-plan"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
POSITION_ID = "acct-om5-plan:BTCUSDT"
NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_move_stop_loss_uses_stop_order_with_trigger_and_submit_before_cancel() -> None:
    result = plan_intent_execution(
        _intent(action="move_stop_loss", order_plan={"stop_price": "64000.111"}),
        _context(existing_orders=(_order("old-stop", "stop_loss"),)),
    )

    assert isinstance(result, ManagementPlan)
    assert result.cancel_after_submit is True
    assert result.cancel_order_ids == ("old-stop",)
    assert len(result.orders) == 1
    assert result.orders[0].order_type == "STOP_MARKET"
    assert result.orders[0].trigger_price == "64000.11"
    assert result.orders[0].order_type != "MARKET"


def test_move_stop_loss_rejects_plain_market_order_translation() -> None:
    result = plan_intent_execution(
        _intent(action="move_stop_loss", order_plan={"type": "market", "stop_price": "64000"}),
        _context(),
    )

    assert result == OrderDenied("unsupported_order_spec", "type=market")


def test_partial_close_fraction_creates_reduce_only_exit_quantity_from_actual_position() -> None:
    result = plan_intent_execution(
        _intent(action="partial_close", order_plan={"type": "market", "fraction": "0.25"}),
        _context(position=_position(quantity="0.8"), positions=(_position(quantity="0.8"),)),
    )

    assert isinstance(result, ManagementPlan)
    assert result.orders[0].order_type == "MARKET"
    assert result.orders[0].quantity == "0.200"
    assert result.orders[0].reduce_only is True


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
    return PlannerContext(**values)


def _position(quantity: str = "1.0") -> PositionSnapshot:
    return PositionSnapshot(
        instrument_id=INSTRUMENT_ID,
        side="LONG",
        quantity=quantity,
        position_id=POSITION_ID,
        entry_price="65000",
    )


def _order(client_order_id: str, role: str) -> OrderSnapshot:
    return OrderSnapshot(
        client_order_id=client_order_id,
        instrument_id=INSTRUMENT_ID,
        order_type="STOP_MARKET",
        side="SELL",
        quantity="1",
        price=None,
        trigger_price="63000",
        tags=(f"position_id={POSITION_ID}", f"lifecycle_role={role}"),
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
    target_position_id: str | None = POSITION_ID


def _intent(**overrides: Any) -> _Intent:
    intent_id = overrides.get("intent_id", uuid4())
    values: dict[str, Any] = {
        "schema_version": "1.0",
        "intent_id": intent_id,
        "decision_id": uuid4(),
        "risk_decision_id": uuid4(),
        "account_id": ACCOUNT_ID,
        "instrument_id": INSTRUMENT_ID,
        "action": "move_stop_loss",
        "order_plan": {"stop_price": "64000"},
        "target_position_id": POSITION_ID,
        "valid_until": NOW + timedelta(minutes=5),
        "idempotency_key": sha256(str(intent_id).encode("ascii")).hexdigest(),
        "approved_at": NOW - timedelta(seconds=5),
    }
    values.update(overrides)
    return _Intent(**values)

