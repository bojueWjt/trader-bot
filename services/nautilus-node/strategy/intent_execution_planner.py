from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional
from uuid import UUID


OPEN_POSITION = "open_position"
ADD_POSITION = "add_position"
ENTRY_ACTIONS = frozenset({OPEN_POSITION, ADD_POSITION})
SUPPORTED_TIF = frozenset({"GTC", "IOC", "FOK", "GTD"})


@dataclass(frozen=True)
class InstrumentSpec:
    instrument_id: str
    price_increment: str
    quantity_increment: str


@dataclass(frozen=True)
class PositionSnapshot:
    instrument_id: str
    side: str
    quantity: str


@dataclass(frozen=True)
class PlannerContext:
    account_id: str
    trading_state: str
    now: datetime
    instrument: Optional[InstrumentSpec]
    position: Optional[PositionSnapshot] = None
    existing_intent_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ClientOrderTrace:
    intent_id: UUID
    sequence: int


@dataclass(frozen=True)
class OrderPlan:
    intent_id: UUID
    client_order_id: str
    tags: tuple[str, ...]
    instrument_id: str
    side: str
    order_type: str
    quantity: str
    price: Optional[str]
    time_in_force: str


@dataclass(frozen=True)
class OrderDenied:
    reason: str
    detail: str


def encode_client_order_id(intent_id: UUID, sequence: int = 1) -> str:
    """Encode a full intent UUID into a Binance-safe client order id.

    Scheme: ``B{intent_uuid_hex}{sequence:02d}``.
    The result is 35 ASCII characters: 1 prefix + 32 UUID hex chars + 2 decimal
    sequence digits. This keeps the full intent id recoverable from venue/order
    history while staying below Binance's 36-character client id ceiling.
    """

    if sequence < 0 or sequence > 99:
        raise ValueError("sequence must be between 0 and 99")
    return f"B{intent_id.hex}{sequence:02d}"


def decode_client_order_id(client_order_id: str) -> ClientOrderTrace:
    if len(client_order_id) != 35 or not client_order_id.startswith("B"):
        raise ValueError("unsupported client_order_id format")
    intent_hex = client_order_id[1:33]
    sequence_raw = client_order_id[33:35]
    if not sequence_raw.isdigit():
        raise ValueError("client_order_id sequence is not numeric")
    return ClientOrderTrace(intent_id=UUID(hex=intent_hex), sequence=int(sequence_raw))


def plan_intent_execution(intent: Any, context: PlannerContext) -> OrderPlan | OrderDenied:
    action = _enum_value(getattr(intent, "action", ""))
    if action not in ENTRY_ACTIONS:
        return OrderDenied("unsupported_action", action)

    trading_state = _enum_value(context.trading_state).upper()
    if trading_state != "ACTIVE":
        return OrderDenied("trading_not_active", trading_state)

    intent_id = getattr(intent, "intent_id")
    if str(intent_id) in context.existing_intent_ids:
        return OrderDenied("duplicate_intent", str(intent_id))

    if getattr(intent, "account_id") != context.account_id:
        return OrderDenied("wrong_account", str(getattr(intent, "account_id")))

    instrument_id = str(getattr(intent, "instrument_id"))
    if context.instrument is None:
        return OrderDenied("instrument_not_found", instrument_id)
    if context.instrument.instrument_id != instrument_id:
        return OrderDenied("instrument_not_found", instrument_id)

    valid_until = _ensure_aware(getattr(intent, "valid_until"))
    if valid_until <= _ensure_aware(context.now):
        return OrderDenied("expired", valid_until.isoformat())

    side_result = _parse_side(getattr(intent, "order_plan", {}).get("side"))
    if isinstance(side_result, OrderDenied):
        return side_result
    side = side_result

    position_denial = _validate_position(action, side, context.position, instrument_id)
    if position_denial is not None:
        return position_denial

    order_spec = _build_order_spec(getattr(intent, "order_plan", {}), context.instrument)
    if isinstance(order_spec, OrderDenied):
        return order_spec

    return OrderPlan(
        intent_id=intent_id,
        client_order_id=encode_client_order_id(intent_id, sequence=1),
        tags=_intent_tags(intent, action),
        instrument_id=instrument_id,
        side=side,
        order_type=order_spec.order_type,
        quantity=order_spec.quantity,
        price=order_spec.price,
        time_in_force=order_spec.time_in_force,
    )


@dataclass(frozen=True)
class _OrderSpec:
    order_type: str
    quantity: str
    price: Optional[str]
    time_in_force: str


def _build_order_spec(order_plan: dict[str, Any], instrument: InstrumentSpec) -> _OrderSpec | OrderDenied:
    order_type = str(order_plan.get("type", "")).lower()
    quantity = _rounded_positive(
        order_plan.get("quantity"),
        instrument.quantity_increment,
        "quantity",
    )
    if isinstance(quantity, OrderDenied):
        return quantity

    if order_type == "market":
        tif = _parse_tif(order_plan.get("time_in_force"), default="IOC")
        if isinstance(tif, OrderDenied):
            return tif
        return _OrderSpec("MARKET", quantity=quantity, price=None, time_in_force=tif)

    if order_type == "limit":
        if "price" not in order_plan:
            return OrderDenied("unsupported_order_spec", "limit.price")
        price = _rounded_positive(order_plan.get("price"), instrument.price_increment, "price")
        if isinstance(price, OrderDenied):
            return price
        tif = _parse_tif(order_plan.get("time_in_force"), default="GTC")
        if isinstance(tif, OrderDenied):
            return tif
        return _OrderSpec("LIMIT", quantity=quantity, price=price, time_in_force=tif)

    if order_type == "zone":
        zone_price = _zone_boundary_price(order_plan, instrument)
        if isinstance(zone_price, OrderDenied):
            return zone_price
        tif = _parse_tif(order_plan.get("time_in_force"), default="GTC")
        if isinstance(tif, OrderDenied):
            return tif
        return _OrderSpec("LIMIT", quantity=quantity, price=zone_price, time_in_force=tif)

    return OrderDenied("unsupported_order_spec", f"type={order_type}")


def _zone_boundary_price(order_plan: dict[str, Any], instrument: InstrumentSpec) -> str | OrderDenied:
    if "price_min" not in order_plan:
        return OrderDenied("unsupported_order_spec", "zone.price_min")
    if "price_max" not in order_plan:
        return OrderDenied("unsupported_order_spec", "zone.price_max")

    price_min = _decimal(order_plan.get("price_min"), "zone.price_min")
    if isinstance(price_min, OrderDenied):
        return price_min
    price_max = _decimal(order_plan.get("price_max"), "zone.price_max")
    if isinstance(price_max, OrderDenied):
        return price_max
    if price_min > price_max:
        return OrderDenied("unsupported_order_spec", "zone.price_min_gt_price_max")

    side = str(order_plan.get("side", "")).lower()
    boundary = price_max if side == "buy" else price_min
    return _round_to_increment(boundary, instrument.price_increment, "price")


def _validate_position(
    action: str,
    side: str,
    position: Optional[PositionSnapshot],
    instrument_id: str,
) -> Optional[OrderDenied]:
    if action == OPEN_POSITION:
        if position is not None and Decimal(str(position.quantity)) != Decimal("0"):
            return OrderDenied("position_exists", instrument_id)
        return None

    if position is None or Decimal(str(position.quantity)) == Decimal("0"):
        return OrderDenied("position_required", instrument_id)

    position_side = position.side.upper()
    expected = "LONG" if side == "BUY" else "SHORT"
    if position_side != expected:
        return OrderDenied("position_side_mismatch", position_side)
    return None


def _parse_side(raw_side: Any) -> str | OrderDenied:
    side = str(raw_side or "").lower()
    if side == "buy":
        return "BUY"
    if side == "sell":
        return "SELL"
    return OrderDenied("unsupported_order_spec", f"side={side}")


def _parse_tif(raw_tif: Any, default: str) -> str | OrderDenied:
    tif = str(raw_tif or default).upper()
    if tif not in SUPPORTED_TIF:
        return OrderDenied("unsupported_order_spec", f"time_in_force={tif}")
    return tif


def _rounded_positive(raw: Any, increment: str, field: str) -> str | OrderDenied:
    value = _decimal(raw, field)
    if isinstance(value, OrderDenied):
        return value
    if value <= Decimal("0"):
        return OrderDenied("unsupported_order_spec", field)
    return _round_to_increment(value, increment, field)


def _round_to_increment(value: Decimal, increment: str, field: str) -> str | OrderDenied:
    step = _decimal(increment, f"{field}.increment")
    if isinstance(step, OrderDenied):
        return step
    if step <= Decimal("0"):
        return OrderDenied("unsupported_order_spec", f"{field}.increment")
    snapped = (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    return format(snapped.quantize(step), "f")


def _decimal(raw: Any, field: str) -> Decimal | OrderDenied:
    if raw is None:
        return OrderDenied("unsupported_order_spec", field)
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return OrderDenied("unsupported_order_spec", field)


def _intent_tags(intent: Any, action: str) -> tuple[str, ...]:
    return (
        f"intent_id={getattr(intent, 'intent_id')}",
        f"decision_id={getattr(intent, 'decision_id')}",
        f"risk_decision_id={getattr(intent, 'risk_decision_id')}",
        f"idempotency_key={getattr(intent, 'idempotency_key')}",
        f"action={action}",
        f"account_id={getattr(intent, 'account_id')}",
    )


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
