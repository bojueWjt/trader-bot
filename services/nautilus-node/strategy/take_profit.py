from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterator
from uuid import UUID, uuid4

from psycopg2.extras import Json

from strategy.intent_execution_planner import InstrumentSpec, OrderPlan, encode_client_order_id
from strategy.protection import PositionProtectionSnapshot


@dataclass(frozen=True)
class TakeProfitTarget:
    price: Decimal | None = None
    r_multiple: Decimal | None = None
    fraction: Decimal | None = None
    quantity: Decimal | None = None
    time_in_force: str = "GTC"


@dataclass(frozen=True)
class TakeProfitDenied:
    reason: str
    detail: str


@dataclass(frozen=True)
class RecomputedProtectionQuantities:
    stop_quantity: Decimal
    take_profit_quantities: tuple[Decimal, ...]


def build_take_profit_ladder(
    *,
    intent_id: str | UUID,
    account_id: str,
    instrument_id: str,
    position: PositionProtectionSnapshot,
    initial_stop_price: Decimal,
    targets: tuple[TakeProfitTarget, ...],
    instrument: InstrumentSpec,
) -> tuple[OrderPlan, ...] | TakeProfitDenied:
    if not targets:
        return TakeProfitDenied("take_profit_required", "targets")

    intent_uuid = UUID(str(intent_id))
    plans: list[OrderPlan] = []
    total = Decimal("0")
    for index, target in enumerate(targets, start=1):
        quantity = _target_quantity(position.quantity, target, instrument.quantity_increment)
        if isinstance(quantity, TakeProfitDenied):
            return quantity
        total += Decimal(quantity)
        trigger = _target_price(position, initial_stop_price, target)
        if isinstance(trigger, TakeProfitDenied):
            return trigger
        trigger_price = _round_to_increment(trigger, instrument.price_increment)
        plans.append(
            OrderPlan(
                intent_id=intent_uuid,
                client_order_id=encode_client_order_id(intent_uuid, sequence=index),
                tags=(
                    f"intent_id={intent_uuid}",
                    "action=install_take_profit",
                    f"account_id={account_id}",
                    "lifecycle_role=take_profit",
                    f"position_id={position.position_key}",
                    f"take_profit_index={index}",
                ),
                instrument_id=instrument_id,
                side=_exit_side(position.side),
                order_type="LIMIT_IF_TOUCHED",
                quantity=quantity,
                price=trigger_price,
                time_in_force=target.time_in_force,
                reduce_only=True,
                trigger_price=trigger_price,
            )
        )

    if total > position.quantity:
        return TakeProfitDenied(
            "quantity_exceeds_position",
            f"{format(total, 'f')}>{_format_decimal(position.quantity)}",
        )
    return tuple(plans)


def record_take_profit_links(
    conn,
    *,
    account_id: str,
    position_key: str,
    parent_order_projection_id: str,
    child_order_projection_ids: tuple[str, ...],
    levels: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    if len(child_order_projection_ids) != len(levels):
        raise ValueError("child_order_projection_ids and levels length mismatch")
    link_ids: list[str] = []
    with _transaction(conn), conn.cursor() as cur:
        for child_id, level in zip(child_order_projection_ids, levels):
            link_id = str(uuid4())
            level_index = level.get("index")
            payload = {**level, "level_index": level_index}
            cur.execute(
                """
                INSERT INTO order_links (
                    order_link_id, account_id, parent_order_projection_id,
                    child_order_projection_id, link_type, lifecycle_role,
                    position_key, payload
                )
                VALUES (%s,%s,%s,%s,'protection','take_profit',%s,%s)
                ON CONFLICT (parent_order_projection_id, child_order_projection_id, link_type)
                DO UPDATE SET payload=order_links.payload || EXCLUDED.payload
                RETURNING order_link_id::text
                """,
                (
                    link_id,
                    account_id,
                    parent_order_projection_id,
                    child_id,
                    position_key,
                    Json(payload),
                ),
            )
            link_ids.append(cur.fetchone()[0])
    return tuple(link_ids)


def recompute_remaining_quantities(
    *,
    position_quantity: Decimal,
    stop_quantity: Decimal,
    take_profit_quantities: tuple[Decimal, ...],
) -> RecomputedProtectionQuantities:
    remaining = max(Decimal("0"), Decimal(str(position_quantity)))
    stop = min(Decimal(str(stop_quantity)), remaining)
    allocated = Decimal("0")
    adjusted: list[Decimal] = []
    for quantity in take_profit_quantities:
        available = max(Decimal("0"), remaining - allocated)
        capped = min(Decimal(str(quantity)), available)
        adjusted.append(capped)
        allocated += capped
    return RecomputedProtectionQuantities(
        stop_quantity=stop,
        take_profit_quantities=tuple(adjusted),
    )


def _target_quantity(
    position_quantity: Decimal,
    target: TakeProfitTarget,
    increment: str,
) -> str | TakeProfitDenied:
    if (target.fraction is None) == (target.quantity is None):
        return TakeProfitDenied("unsupported_order_spec", "fraction_xor_quantity")
    if target.fraction is not None:
        raw = Decimal(str(position_quantity)) * Decimal(str(target.fraction))
    else:
        raw = Decimal(str(target.quantity))
    if raw <= 0:
        return TakeProfitDenied("unsupported_order_spec", "quantity")
    return _round_to_increment(raw, increment)


def _target_price(
    position: PositionProtectionSnapshot,
    initial_stop_price: Decimal,
    target: TakeProfitTarget,
) -> Decimal | TakeProfitDenied:
    if (target.price is None) == (target.r_multiple is None):
        return TakeProfitDenied("unsupported_order_spec", "price_xor_r_multiple")
    if target.price is not None:
        return Decimal(str(target.price))
    if position.entry_price is None:
        return TakeProfitDenied("unsupported_order_spec", "entry_price_required")
    entry = Decimal(str(position.entry_price))
    risk = abs(entry - Decimal(str(initial_stop_price)))
    multiple = Decimal(str(target.r_multiple))
    return entry + risk * multiple if _is_long(position.side) else entry - risk * multiple


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


def _format_decimal(value: Decimal) -> str:
    return format(Decimal(str(value)), "f").rstrip("0").rstrip(".") or "0"


def _exit_side(side: str) -> str:
    return "SELL" if _is_long(side) else "BUY"


def _is_long(side: str) -> bool:
    return str(side).lower() in {"long", "buy"}


@contextmanager
def _transaction(conn) -> Iterator[None]:
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
