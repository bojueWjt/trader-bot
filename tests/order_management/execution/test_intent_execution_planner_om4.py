from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from strategy.intent_execution_planner import (
    InstrumentSpec,
    OrderDenied,
    OrderPlan,
    PlannerContext,
    plan_intent_execution,
)


ACCOUNT_ID = "acct-om4"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_execution_market_uses_sizing_quantity_and_explicit_tif() -> None:
    intent = _intent(
        order_plan={"side": "buy", "entry": "100.014"},
        execution=_Execution(order_type="market", time_in_force="FOK", max_slippage_bps=Decimal("10")),
        sizing=_Sizing(quantity=Decimal("0.1236")),
    )

    result = plan_intent_execution(intent, _context())

    assert isinstance(result, OrderPlan)
    assert result.order_type == "MARKET"
    assert result.quantity == "0.124"
    assert result.time_in_force == "FOK"
    assert result.max_slippage_bps == "10"
    assert result.guard_price == "100.01"


def test_execution_limit_uses_only_approved_limit_price() -> None:
    intent = _intent(
        order_plan={"side": "buy", "price": "1"},
        execution=_Execution(order_type="limit", time_in_force="GTC", limit_price=Decimal("27123.457")),
        sizing=_Sizing(quantity=Decimal("0.25")),
    )

    result = plan_intent_execution(intent, _context())

    assert isinstance(result, OrderPlan)
    assert result.order_type == "LIMIT"
    assert result.price == "27123.46"
    assert result.quantity == "0.250"


def test_execution_zone_boundary_is_deterministic_per_side() -> None:
    zone = _Zone(low=Decimal("27000.114"), high=Decimal("27123.457"))
    buy = _intent(order_plan={"side": "buy"}, execution=_Execution(order_type="zone", zone=zone), sizing=_Sizing(quantity=Decimal("1")))
    sell = _intent(order_plan={"side": "sell"}, execution=_Execution(order_type="zone", zone=zone), sizing=_Sizing(quantity=Decimal("1")))

    buy_order = plan_intent_execution(buy, _context())
    sell_order = plan_intent_execution(sell, _context())

    assert isinstance(buy_order, OrderPlan)
    assert isinstance(sell_order, OrderPlan)
    assert buy_order.price == "27123.46"
    assert sell_order.price == "27000.11"


def test_entry_execution_rejects_reduce_only() -> None:
    intent = _intent(
        order_plan={"side": "buy"},
        execution=_Execution(order_type="market", reduce_only=True),
        sizing=_Sizing(quantity=Decimal("1")),
    )

    result = plan_intent_execution(intent, _context())

    assert result == OrderDenied("unsupported_order_spec", "entry.reduce_only")


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


@dataclass(frozen=True)
class _Zone:
    low: Decimal
    high: Decimal


@dataclass(frozen=True)
class _Execution:
    order_type: str | None = None
    time_in_force: str | None = None
    post_only: bool | None = None
    reduce_only: bool | None = None
    max_slippage_bps: Decimal | None = None
    limit_price: Decimal | None = None
    zone: _Zone | None = None


@dataclass(frozen=True)
class _Sizing:
    quantity: Decimal | None = None
    fraction: Decimal | None = None


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
    execution: _Execution | None
    sizing: _Sizing | None
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
        "order_plan": {"side": "buy"},
        "execution": None,
        "sizing": None,
        "target_position_id": None,
        "valid_until": NOW + timedelta(minutes=5),
        "idempotency_key": sha256(str(intent_id).encode("ascii")).hexdigest(),
        "approved_at": NOW - timedelta(seconds=5),
    }
    values.update(overrides)
    return _Intent(**values)
