from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

from strategy.intent_execution_planner import InstrumentSpec, OrderPlan, encode_client_order_id


@dataclass(frozen=True)
class PositionProtectionSnapshot:
    position_key: str
    side: str
    quantity: Decimal
    entry_price: Decimal | None = None


@dataclass(frozen=True)
class StopProtectionSpec:
    stop_type: str = "stop_market"
    trigger_price: Decimal | None = None
    distance_pct: Decimal | None = None
    limit_price: Decimal | None = None
    trigger_basis: str = "mark"


def build_stop_order_plan(
    *,
    intent_id: str | UUID,
    account_id: str,
    instrument_id: str,
    position: PositionProtectionSnapshot,
    stop: StopProtectionSpec,
    instrument: InstrumentSpec,
    sequence: int = 1,
) -> OrderPlan:
    stop_type = stop.stop_type.lower()
    if stop_type not in {"stop_market", "stop_limit"}:
        raise ValueError(f"unsupported stop type {stop.stop_type!r}")

    trigger_price = _trigger_price(position, stop)
    order_type = "STOP_MARKET"
    limit_price: Optional[str] = None
    if stop_type == "stop_limit":
        if stop.limit_price is None:
            raise ValueError("stop_limit requires limit_price")
        limit_price = _round_to_increment(stop.limit_price, instrument.price_increment)
        order_type = "STOP_LIMIT"

    intent_uuid = UUID(str(intent_id))
    side = _exit_side(position.side)
    return OrderPlan(
        intent_id=intent_uuid,
        client_order_id=encode_client_order_id(intent_uuid, sequence=sequence),
        tags=(
            f"intent_id={intent_uuid}",
            f"action=install_protection",
            f"account_id={account_id}",
            "lifecycle_role=stop_loss",
            f"position_id={position.position_key}",
            f"trigger_basis={stop.trigger_basis}",
        ),
        instrument_id=instrument_id,
        side=side,
        order_type=order_type,
        quantity=_round_to_increment(abs(position.quantity), instrument.quantity_increment),
        price=limit_price,
        time_in_force="GTC",
        reduce_only=True,
        trigger_price=_round_to_increment(trigger_price, instrument.price_increment),
    )


def _trigger_price(position: PositionProtectionSnapshot, stop: StopProtectionSpec) -> Decimal:
    if stop.trigger_price is not None:
        return Decimal(str(stop.trigger_price))
    if stop.distance_pct is None or position.entry_price is None:
        raise ValueError("stop requires trigger_price or entry_price plus distance_pct")
    distance = Decimal(str(stop.distance_pct)) / Decimal("100")
    entry = Decimal(str(position.entry_price))
    if _is_long(position.side):
        return entry * (Decimal("1") - distance)
    return entry * (Decimal("1") + distance)


def _exit_side(side: str) -> str:
    return "SELL" if _is_long(side) else "BUY"


def _is_long(side: str) -> bool:
    return str(side).lower() in {"long", "buy"}


def _round_to_increment(value: Decimal, increment: str) -> str:
    try:
        step = Decimal(str(increment))
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal value") from exc
    if amount <= 0 or step <= 0:
        raise ValueError("decimal values must be positive")
    snapped = (amount / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    return format(snapped.quantize(step), "f")

