from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Optional
from uuid import UUID


OPEN_POSITION = "open_position"
ADD_POSITION = "add_position"
PARTIAL_CLOSE = "partial_close"
CLOSE_POSITION = "close_position"
MOVE_STOP_LOSS = "move_stop_loss"
MOVE_STOP_TO_ENTRY = "move_stop_to_entry"
REPLACE_TAKE_PROFITS = "replace_take_profits"
CANCEL_ORDER = "cancel"
CANCEL_ORDER_ALIASES = frozenset({CANCEL_ORDER, "cancel_order"})
ENTRY_ACTIONS = frozenset({OPEN_POSITION, ADD_POSITION})
EXIT_ACTIONS = frozenset({PARTIAL_CLOSE, CLOSE_POSITION})
POSITION_REQUIRED_ACTIONS = frozenset(
    {
        PARTIAL_CLOSE,
        CLOSE_POSITION,
        MOVE_STOP_LOSS,
        MOVE_STOP_TO_ENTRY,
        REPLACE_TAKE_PROFITS,
    }
)
MANAGEMENT_ACTIONS = EXIT_ACTIONS | POSITION_REQUIRED_ACTIONS | frozenset({CANCEL_ORDER})
SUPPORTED_TIF = frozenset({"GTC", "IOC", "FOK", "GTD"})
ACTION_PERMISSION_MATRIX = {
    "ACTIVE": ENTRY_ACTIONS | MANAGEMENT_ACTIONS,
    "REDUCING": MANAGEMENT_ACTIONS,
    "HALTED": MANAGEMENT_ACTIONS,
}


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
    position_id: Optional[str] = None
    entry_price: Optional[str] = None


@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str
    instrument_id: str
    order_type: str
    side: str
    quantity: str
    price: Optional[str]
    trigger_price: Optional[str]
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannerContext:
    account_id: str
    trading_state: str
    now: datetime
    instrument: Optional[InstrumentSpec]
    position: Optional[PositionSnapshot] = None
    positions: tuple[PositionSnapshot, ...] = ()
    existing_orders: tuple[OrderSnapshot, ...] = ()
    existing_intent_ids: frozenset[str] = frozenset()
    effective_settings: Mapping[str, Any] | None = None


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
    reduce_only: bool = False
    trigger_price: Optional[str] = None
    post_only: bool = False
    max_slippage_bps: Optional[str] = None
    guard_price: Optional[str] = None


@dataclass(frozen=True)
class ManagementPlan:
    intent_id: UUID
    action: str
    instrument_id: str
    target_position_id: Optional[str]
    target_position_side: Optional[str]
    cancel_order_ids: tuple[str, ...]
    orders: tuple[OrderPlan, ...]
    cancel_after_submit: bool = False
    authorization: Mapping[str, str] | None = None
    disable_take_profits: bool = False


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


def plan_intent_execution(intent: Any, context: PlannerContext) -> OrderPlan | ManagementPlan | OrderDenied:
    action = _enum_value(getattr(intent, "action", ""))
    if action in CANCEL_ORDER_ALIASES:
        action = CANCEL_ORDER
    if action not in ENTRY_ACTIONS and action not in MANAGEMENT_ACTIONS:
        return OrderDenied("unsupported_action", action)
    authorization = _authorization_source(intent)
    if isinstance(authorization, OrderDenied):
        return authorization

    trading_state = _enum_value(context.trading_state).upper()
    if not halted_action_allowed(action, trading_state):
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

    if action == CANCEL_ORDER:
        return _plan_cancel_order_intent(intent, context)
    if action in MANAGEMENT_ACTIONS:
        return _plan_management_intent(intent, action, context)

    entry_order_plan = _entry_order_plan(intent, context)
    if isinstance(entry_order_plan, OrderDenied):
        return entry_order_plan

    side_result = _parse_side(entry_order_plan.get("side"))
    if isinstance(side_result, OrderDenied):
        return side_result
    side = side_result

    position_denial = _validate_position(action, side, context.position, instrument_id)
    if position_denial is not None:
        return position_denial

    order_spec = _build_order_spec(entry_order_plan, context.instrument)
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
        post_only=bool(entry_order_plan.get("post_only") or False),
        max_slippage_bps=_optional_decimal_string(entry_order_plan.get("max_slippage_bps")),
        guard_price=_guard_price(entry_order_plan, context.instrument),
    )


def _plan_cancel_order_intent(
    intent: Any,
    context: PlannerContext,
) -> ManagementPlan | OrderDenied:
    instrument_id = str(getattr(intent, "instrument_id"))
    order_plan = getattr(intent, "order_plan", {}) or {}
    target = str(order_plan.get("cancel_client_order_id") or "").strip()
    if not target:
        return OrderDenied("unsupported_order_spec", "cancel_client_order_id")
    matches = tuple(
        order
        for order in context.existing_orders
        if order.instrument_id == instrument_id and order.client_order_id == target
    )
    if len(matches) == 0:
        return OrderDenied("order_not_found", target)
    if len(matches) > 1:
        return OrderDenied("order_not_unique", target)
    ownership_denial = _validate_cancel_order_ownership(matches[0], context)
    if ownership_denial is not None:
        return ownership_denial
    authorization = _authorization_source(intent)
    if isinstance(authorization, OrderDenied):
        return authorization
    return ManagementPlan(
        intent_id=getattr(intent, "intent_id"),
        action=CANCEL_ORDER,
        instrument_id=instrument_id,
        target_position_id=None,
        target_position_side=None,
        cancel_order_ids=(target,),
        orders=(),
        authorization=authorization,
    )


def halted_action_allowed(action: str, mode: str) -> bool:
    allowed_actions = ACTION_PERMISSION_MATRIX.get(str(mode).upper(), frozenset())
    return action in allowed_actions


def _plan_management_intent(
    intent: Any,
    action: str,
    context: PlannerContext,
) -> ManagementPlan | OrderDenied:
    assert context.instrument is not None
    instrument_id = str(getattr(intent, "instrument_id"))
    position = _select_target_position(intent, context, instrument_id)
    if isinstance(position, OrderDenied):
        return position

    order_plan = getattr(intent, "order_plan", {}) or {}
    authorization = _authorization_source(intent)
    if isinstance(authorization, OrderDenied):
        return authorization
    side = _exit_side(position)
    cancel_role: Optional[str] = None
    cancel_after_submit = False
    disable_take_profits = False
    orders: tuple[OrderPlan, ...]

    if action == PARTIAL_CLOSE:
        order = _build_exit_order(intent, action, order_plan, context.instrument, position, side)
        if isinstance(order, OrderDenied):
            return order
        orders = (order,)
    elif action == CLOSE_POSITION:
        close_plan = dict(order_plan)
        close_plan["quantity"] = position.quantity
        order = _build_exit_order(intent, action, close_plan, context.instrument, position, side)
        if isinstance(order, OrderDenied):
            return order
        orders = (order,)
    elif action == MOVE_STOP_LOSS:
        order = _build_stop_order(intent, action, order_plan, context.instrument, position, side)
        if isinstance(order, OrderDenied):
            return order
        cancel_role = "stop_loss"
        cancel_after_submit = True
        orders = (order,)
    elif action == MOVE_STOP_TO_ENTRY:
        entry_price = position.entry_price or order_plan.get("entry_price")
        if entry_price is None:
            return OrderDenied("position_entry_price_required", _position_detail(position, instrument_id))
        stop_plan = dict(order_plan)
        stop_plan["stop_price"] = entry_price
        order = _build_stop_order(intent, action, stop_plan, context.instrument, position, side)
        if isinstance(order, OrderDenied):
            return order
        cancel_role = "stop_loss"
        cancel_after_submit = True
        orders = (order,)
    elif action == REPLACE_TAKE_PROFITS:
        disable_take_profits = order_plan.get("disable_take_profits") is True
        take_profit_orders = _build_take_profit_orders(
            intent,
            order_plan,
            context.instrument,
            position,
            side,
        )
        if isinstance(take_profit_orders, OrderDenied):
            return take_profit_orders
        cancel_role = "take_profit"
        orders = take_profit_orders
    else:  # pragma: no cover - guarded by MANAGEMENT_ACTIONS.
        return OrderDenied("unsupported_action", action)

    cancel_order_ids = (
        _matching_lifecycle_order_ids(
            context.existing_orders,
            role=cancel_role,
            position_id=position.position_id,
            instrument_id=instrument_id,
        )
        if cancel_role is not None
        else ()
    )
    return ManagementPlan(
        intent_id=getattr(intent, "intent_id"),
        action=action,
        instrument_id=instrument_id,
        target_position_id=position.position_id,
        target_position_side=position.side,
        cancel_order_ids=cancel_order_ids,
        orders=orders,
        cancel_after_submit=cancel_after_submit,
        authorization=authorization,
        disable_take_profits=disable_take_profits,
    )


@dataclass(frozen=True)
class _OrderSpec:
    order_type: str
    quantity: str
    price: Optional[str]
    time_in_force: str


def _entry_order_plan(intent: Any, context: PlannerContext) -> dict[str, Any] | OrderDenied:
    order_plan = dict(getattr(intent, "order_plan", {}) or {})
    execution = getattr(intent, "execution", None)
    sizing = getattr(intent, "sizing", None)
    settings = dict(context.effective_settings or {})

    if execution is None and sizing is None:
        return order_plan

    if _get(execution, "reduce_only") is True:
        return OrderDenied("unsupported_order_spec", "entry.reduce_only")

    order_type = _first_present(
        _get(execution, "order_type"),
        order_plan.get("type"),
        settings.get("order_type"),
    )
    if order_type is not None:
        order_plan["type"] = str(order_type)

    quantity = _first_present(_get(sizing, "quantity"), order_plan.get("quantity"))
    if quantity is None and _get(sizing, "fraction") is not None:
        return OrderDenied("unsupported_order_spec", "sizing.fraction")
    if quantity is not None:
        order_plan["quantity"] = quantity

    tif = _first_present(
        _get(execution, "time_in_force"),
        order_plan.get("time_in_force"),
        settings.get("time_in_force"),
        settings.get(f"{str(order_plan.get('type', '')).lower()}_time_in_force"),
    )
    if tif is not None:
        order_plan["time_in_force"] = tif

    post_only = _first_present(_get(execution, "post_only"), order_plan.get("post_only"), settings.get("post_only"))
    if post_only is not None:
        order_plan["post_only"] = bool(post_only)

    max_slippage = _first_present(
        _get(execution, "max_slippage_bps"),
        order_plan.get("max_slippage_bps"),
        settings.get("max_slippage_bps"),
    )
    if max_slippage is not None:
        order_plan["max_slippage_bps"] = max_slippage

    order_type_text = str(order_plan.get("type", "")).lower()
    if order_type_text == "limit":
        price = _first_present(_get(execution, "limit_price"), order_plan.get("price"))
        if price is not None:
            order_plan["price"] = price
    if order_type_text == "zone":
        zone = _get(execution, "zone")
        low = _first_present(_get(zone, "low"), order_plan.get("price_min"))
        high = _first_present(_get(zone, "high"), order_plan.get("price_max"))
        if low is not None:
            order_plan["price_min"] = low
        if high is not None:
            order_plan["price_max"] = high
    return order_plan


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


def _build_exit_order(
    intent: Any,
    action: str,
    order_plan: dict[str, Any],
    instrument: InstrumentSpec,
    position: PositionSnapshot,
    side: str,
) -> OrderPlan | OrderDenied:
    normalized = dict(order_plan)
    normalized["side"] = side.lower()
    if "quantity" not in normalized and "fraction" in normalized:
        fraction = _decimal(normalized.get("fraction"), "fraction")
        if isinstance(fraction, OrderDenied):
            return fraction
        if fraction <= Decimal("0") or fraction > Decimal("1"):
            return OrderDenied("unsupported_order_spec", "fraction")
        normalized["quantity"] = Decimal(str(position.quantity)) * fraction
    order_spec = _build_order_spec(normalized, instrument)
    if isinstance(order_spec, OrderDenied):
        return order_spec
    quantity_denial = _deny_if_quantity_exceeds_position(order_spec.quantity, position.quantity)
    if quantity_denial is not None:
        return quantity_denial
    return OrderPlan(
        intent_id=getattr(intent, "intent_id"),
        client_order_id=encode_client_order_id(getattr(intent, "intent_id"), sequence=1),
        tags=_intent_tags(intent, action, lifecycle_role="exit", position_id=position.position_id),
        instrument_id=str(getattr(intent, "instrument_id")),
        side=side,
        order_type=order_spec.order_type,
        quantity=order_spec.quantity,
        price=order_spec.price,
        time_in_force=order_spec.time_in_force,
        reduce_only=True,
    )


def _build_stop_order(
    intent: Any,
    action: str,
    order_plan: dict[str, Any],
    instrument: InstrumentSpec,
    position: PositionSnapshot,
    side: str,
) -> OrderPlan | OrderDenied:
    order_type = str(order_plan.get("type", "stop_market")).lower()
    if order_type not in {"stop_market", "stop_limit"}:
        return OrderDenied("unsupported_order_spec", f"type={order_type}")

    raw_trigger = order_plan.get("stop_price", order_plan.get("trigger_price"))
    trigger_price = _rounded_positive(raw_trigger, instrument.price_increment, "stop_price")
    if isinstance(trigger_price, OrderDenied):
        return trigger_price

    price: Optional[str] = None
    planned_order_type = "STOP_MARKET"
    if order_type == "stop_limit":
        raw_price = order_plan.get("limit_price", order_plan.get("price"))
        price = _rounded_positive(raw_price, instrument.price_increment, "limit_price")
        if isinstance(price, OrderDenied):
            return price
        planned_order_type = "STOP_LIMIT"

    quantity = _rounded_positive(position.quantity, instrument.quantity_increment, "quantity")
    if isinstance(quantity, OrderDenied):
        return quantity
    tif = _parse_tif(order_plan.get("time_in_force"), default="GTC")
    if isinstance(tif, OrderDenied):
        return tif
    return OrderPlan(
        intent_id=getattr(intent, "intent_id"),
        client_order_id=encode_client_order_id(getattr(intent, "intent_id"), sequence=1),
        tags=_intent_tags(intent, action, lifecycle_role="stop_loss", position_id=position.position_id),
        instrument_id=str(getattr(intent, "instrument_id")),
        side=side,
        order_type=planned_order_type,
        quantity=quantity,
        price=price,
        time_in_force=tif,
        reduce_only=True,
        trigger_price=trigger_price,
    )


def _build_take_profit_orders(
    intent: Any,
    order_plan: dict[str, Any],
    instrument: InstrumentSpec,
    position: PositionSnapshot,
    side: str,
) -> tuple[OrderPlan, ...] | OrderDenied:
    raw_targets = order_plan.get("take_profits", order_plan.get("targets"))
    if not isinstance(raw_targets, list):
        return OrderDenied("unsupported_order_spec", "take_profits")
    if len(raw_targets) == 0:
        if order_plan.get("disable_take_profits") is not True:
            return OrderDenied(
                "unsupported_order_spec",
                "disable_take_profits=true required for empty take_profits",
            )
        return ()

    orders: list[OrderPlan] = []
    total = Decimal("0")
    for index, raw_target in enumerate(raw_targets, start=1):
        if not isinstance(raw_target, dict):
            return OrderDenied("unsupported_order_spec", f"take_profits[{index - 1}]")
        quantity = _rounded_positive(
            raw_target.get("quantity"),
            instrument.quantity_increment,
            f"take_profits[{index - 1}].quantity",
        )
        if isinstance(quantity, OrderDenied):
            return quantity
        total += Decimal(quantity)
        trigger_price = _rounded_positive(
            raw_target.get("trigger_price", raw_target.get("price")),
            instrument.price_increment,
            f"take_profits[{index - 1}].price",
        )
        if isinstance(trigger_price, OrderDenied):
            return trigger_price
        tif = _parse_tif(raw_target.get("time_in_force", order_plan.get("time_in_force")), default="GTC")
        if isinstance(tif, OrderDenied):
            return tif
        orders.append(
            OrderPlan(
                intent_id=getattr(intent, "intent_id"),
                client_order_id=encode_client_order_id(getattr(intent, "intent_id"), sequence=index),
                tags=_intent_tags(
                    intent,
                    REPLACE_TAKE_PROFITS,
                    lifecycle_role="take_profit",
                    position_id=position.position_id,
                )
                + (f"take_profit_index={index}",),
                instrument_id=str(getattr(intent, "instrument_id")),
                side=side,
                order_type="MARKET_IF_TOUCHED",
                quantity=quantity,
                price=None,
                time_in_force=tif,
                reduce_only=True,
                trigger_price=trigger_price,
            )
        )

    position_quantity = Decimal(str(position.quantity))
    if total > position_quantity:
        return OrderDenied("quantity_exceeds_position", f"{format(total, 'f')}>{position.quantity}")
    return tuple(orders)


def _select_target_position(
    intent: Any,
    context: PlannerContext,
    instrument_id: str,
) -> PositionSnapshot | OrderDenied:
    order_plan = getattr(intent, "order_plan", {}) or {}
    requested_side = str(order_plan.get("position_side") or "").strip().upper()
    if requested_side and requested_side not in {"LONG", "SHORT"}:
        return OrderDenied("unsupported_order_spec", "position_side")
    positions = tuple(
        position
        for position in _context_positions(context)
        if position.instrument_id == instrument_id and Decimal(str(position.quantity)) != Decimal("0")
    )
    target_position_id = getattr(intent, "target_position_id", None)
    if target_position_id:
        matches = tuple(position for position in positions if position.position_id == target_position_id)
        if len(matches) == 0:
            return OrderDenied("position_required", str(target_position_id))
        if len(matches) > 1:
            return OrderDenied("position_not_unique", str(target_position_id))
        selected = matches[0]
        if requested_side:
            actual_side = str(selected.side).upper()
            if actual_side != requested_side:
                return OrderDenied(
                    "position_side_mismatch",
                    f"requested={requested_side},actual={actual_side}",
                )
        return selected
    if not requested_side:
        if len(positions) == 0:
            return OrderDenied("position_required", instrument_id)
        if len(positions) > 1:
            return OrderDenied("position_not_unique", instrument_id)
        return positions[0]
    side_matches = tuple(
        position
        for position in positions
        if str(position.side).upper() == requested_side
    )
    side_detail = f"{instrument_id}:{requested_side}"
    if len(side_matches) == 0:
        return OrderDenied("position_required", side_detail)
    if len(side_matches) > 1:
        return OrderDenied("position_not_unique", side_detail)
    return side_matches[0]


def _context_positions(context: PlannerContext) -> tuple[PositionSnapshot, ...]:
    if context.positions:
        return context.positions
    if context.position is not None:
        return (context.position,)
    return ()


def _exit_side(position: PositionSnapshot) -> str:
    return "SELL" if position.side.upper() == "LONG" else "BUY"


def _deny_if_quantity_exceeds_position(
    quantity: str,
    position_quantity: str,
) -> Optional[OrderDenied]:
    if Decimal(quantity) > Decimal(str(position_quantity)):
        return OrderDenied("quantity_exceeds_position", f"{quantity}>{position_quantity}")
    return None


def _matching_lifecycle_order_ids(
    orders: tuple[OrderSnapshot, ...],
    role: Optional[str],
    position_id: Optional[str],
    instrument_id: str,
) -> tuple[str, ...]:
    if role is None:
        return ()
    result: list[str] = []
    for order in orders:
        if order.instrument_id != instrument_id:
            continue
        tags = set(order.tags)
        if f"lifecycle_role={role}" not in tags:
            continue
        if position_id is not None and f"position_id={position_id}" not in tags:
            continue
        result.append(order.client_order_id)
    return tuple(result)


def _validate_cancel_order_ownership(
    order: OrderSnapshot,
    context: PlannerContext,
) -> Optional[OrderDenied]:
    target = order.client_order_id
    try:
        trace = decode_client_order_id(target)
    except ValueError:
        return OrderDenied("order_ownership_unverified", target)
    owner_intent_id = str(trace.intent_id)
    # Exchange snapshots can lose Nautilus tags and the node's processed-intent
    # set across restarts. The reversible B+UUID id remains durable ownership.
    if not order.tags:
        return None
    account_ids = _tag_values(order.tags, "account_id")
    if account_ids and account_ids != {context.account_id}:
        return OrderDenied("order_ownership_unverified", target)
    intent_ids = _tag_values(order.tags, "intent_id")
    if intent_ids and intent_ids != {owner_intent_id}:
        return OrderDenied("order_ownership_unverified", target)
    return None


def _tag_values(tags: tuple[str, ...], key: str) -> set[str]:
    prefix = f"{key}="
    values: set[str] = set()
    for tag in tags:
        text = str(tag)
        if not text.startswith(prefix):
            continue
        value = text.split("=", 1)[1].strip()
        if value:
            values.add(value)
    return values


def _position_detail(position: PositionSnapshot, instrument_id: str) -> str:
    return position.position_id or instrument_id


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


def _intent_tags(
    intent: Any,
    action: str,
    lifecycle_role: Optional[str] = None,
    position_id: Optional[str] = None,
) -> tuple[str, ...]:
    authorization = _authorization_source(intent)
    tags = (
        f"intent_id={getattr(intent, 'intent_id')}",
        f"decision_id={getattr(intent, 'decision_id')}",
        f"risk_decision_id={getattr(intent, 'risk_decision_id')}",
        f"idempotency_key={getattr(intent, 'idempotency_key')}",
        f"action={action}",
        f"account_id={getattr(intent, 'account_id')}",
    )
    if isinstance(authorization, OrderDenied):
        return tags
    tags += (
        f"parent_intent_id={authorization['parent_intent_id']}",
        f"authorized_by_type={authorization['authorized_by_type']}",
        f"authorized_by_id={authorization['authorized_by_id']}",
    )
    source_message_id = authorization.get("source_message_id")
    if source_message_id:
        tags += (f"source_message_id={source_message_id}",)
    channel_id = authorization.get("channel_id")
    if channel_id:
        tags += (f"channel_id={channel_id}",)
    if lifecycle_role is not None:
        tags += (f"lifecycle_role={lifecycle_role}",)
    if position_id is not None:
        tags += (f"position_id={position_id}",)
    return tags


def _authorization_source(intent: Any) -> dict[str, str] | OrderDenied:
    order_plan = getattr(intent, "order_plan", {}) or {}
    raw = order_plan.get("authorization")
    supplied = raw if isinstance(raw, Mapping) else {}
    intent_id = str(getattr(intent, "intent_id"))
    supplied_parent = str(supplied.get("parent_intent_id") or "").strip()
    parent_intent_id = intent_id
    try:
        UUID(supplied_parent)
        parent_intent_id = supplied_parent
    except (TypeError, ValueError):
        pass
    authorized_by_type = str(supplied.get("authorized_by_type") or "").strip()
    authorized_by_id = str(supplied.get("authorized_by_id") or "").strip()
    source_message_id = str(supplied.get("source_message_id") or "").strip()
    if authorized_by_type not in {"user", "channel"}:
        return OrderDenied(
            "authorization_source_required",
            "authorized_by_type must be user or channel",
        )
    if not authorized_by_id:
        return OrderDenied(
            "authorization_source_required",
            "authorized_by_id",
        )
    if not source_message_id:
        return OrderDenied(
            "authorization_source_required",
            "source_message_id",
        )
    source = {
        "authorized_by_type": authorized_by_type,
        "authorized_by_id": authorized_by_id,
        "source_message_id": source_message_id,
        "parent_intent_id": parent_intent_id,
        "current_intent_id": intent_id,
        "decision_id": str(getattr(intent, "decision_id")),
        "risk_decision_id": str(getattr(intent, "risk_decision_id")),
        "idempotency_key": str(getattr(intent, "idempotency_key")),
    }
    channel_id = supplied.get("channel_id")
    if channel_id:
        source["channel_id"] = str(channel_id)
    return source


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _optional_decimal_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    parsed = _decimal(value, "max_slippage_bps")
    if isinstance(parsed, OrderDenied):
        return None
    return format(parsed.normalize(), "f")


def _guard_price(order_plan: dict[str, Any], instrument: InstrumentSpec) -> Optional[str]:
    raw = _first_present(
        order_plan.get("guard_price"),
        order_plan.get("reference_price"),
        order_plan.get("entry_price"),
        order_plan.get("entry"),
        order_plan.get("price"),
    )
    if raw is None:
        return None
    rounded = _rounded_positive(raw, instrument.price_increment, "guard_price")
    if isinstance(rounded, OrderDenied):
        return None
    return rounded


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
