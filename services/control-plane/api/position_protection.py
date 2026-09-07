"""Protection status for GET /v1/positions (M1a) and later mirror (M1b).

Rules (design §4.1d / contracts §2):
- order set = open_orders ∪ algo_orders
- hedge attribution by exit side (LONG→SELL, SHORT→BUY)
- protected: SL quantity covers the full position
- partial: SL under-covers, or TP exists with no SL
- unprotected: no SL
- no protection_policy field
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from execution_domain.order_ownership import row_is_robot_order

_SL_TYPES = frozenset(
    {
        "STOP",
        "STOP_MARKET",
        "STOP_LOSS",
        "STOP_LOSS_LIMIT",
        "TRAILING_STOP_MARKET",
    }
)
_TP_TYPES = frozenset(
    {
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TAKE_PROFIT_LIMIT",
    }
)


def _dec(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not number.is_finite():
        return Decimal("0")
    return number


def _dec_text(value: Any) -> str:
    return format(_dec(value), "f")


def _order_type(order: Mapping[str, Any]) -> str:
    raw = order.get("type")
    if raw is None:
        raw = order.get("order_type") or order.get("orderType")
    return str(raw or "").strip().upper()


def _order_side(order: Mapping[str, Any]) -> str:
    return str(order.get("side") or "").strip().upper()


def _order_symbol(order: Mapping[str, Any]) -> str:
    raw = str(order.get("symbol") or order.get("instrument_symbol") or "")
    return raw.split("-", 1)[0]


def _order_id(order: Mapping[str, Any]) -> str:
    for key in (
        "order_id",
        "orderId",
        "algo_id",
        "algoId",
        "client_order_id",
        "clientOrderId",
        "client_algo_id",
        "clientAlgoId",
    ):
        value = order.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _trigger_price(order: Mapping[str, Any]) -> str:
    for key in ("trigger_price", "triggerPrice", "stop_price", "stopPrice", "price"):
        value = order.get(key)
        if value is None or str(value).strip() in ("", "0", "0.0"):
            continue
        return _dec_text(value)
    return _dec_text(0)


def _quantity(order: Mapping[str, Any]) -> Decimal:
    for key in ("quantity", "origQty", "qty"):
        if order.get(key) is not None:
            return _dec(order.get(key)).copy_abs()
    return Decimal("0")


def _exit_side(position_side: str | None) -> str:
    side = str(position_side or "").strip().upper()
    if side in ("LONG", "BUY"):
        return "SELL"
    if side in ("SHORT", "SELL"):
        return "BUY"
    return ""


def _position_side_token(position_side: str | None) -> str:
    side = str(position_side or "").strip().upper()
    if side in ("LONG", "BUY"):
        return "LONG"
    if side in ("SHORT", "SELL"):
        return "SHORT"
    return side


def _belongs_to_position(order: Mapping[str, Any], *, symbol: str, exit_side: str, position_side: str) -> bool:
    if _order_symbol(order) != symbol:
        return False
    order_side = _order_side(order)
    if order_side:
        return order_side == exit_side
    raw_ps = str(order.get("position_side") or order.get("positionSide") or "").strip().upper()
    if raw_ps in ("LONG", "SHORT"):
        return raw_ps == position_side
    return False


def _as_protection_row(order: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "order_id": _order_id(order),
        "trigger_price": _trigger_price(order),
        "quantity": _dec_text(_quantity(order)),
        "is_bot_order": row_is_robot_order(order),
    }


def protection_status(
    *,
    symbol: str | None,
    position_side: str | None,
    quantity: Any,
    open_orders: Iterable[Mapping[str, Any]] | None,
    algo_orders: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    venue_symbol = str(symbol or "").split("-", 1)[0]
    exit_side = _exit_side(position_side)
    pos_side = _position_side_token(position_side)
    combined = list(open_orders or []) + list(algo_orders or [])
    matching = [
        order
        for order in combined
        if isinstance(order, Mapping)
        and _belongs_to_position(
            order, symbol=venue_symbol, exit_side=exit_side, position_side=pos_side
        )
    ]
    stop_loss = [order for order in matching if _order_type(order) in _SL_TYPES]
    take_profits = [order for order in matching if _order_type(order) in _TP_TYPES]
    sl_qty = sum((_quantity(order) for order in stop_loss), Decimal("0"))
    pos_qty = _dec(quantity).copy_abs()
    if not stop_loss:
        status = "partial" if take_profits else "unprotected"
    elif pos_qty > 0 and sl_qty >= pos_qty:
        status = "protected"
    else:
        status = "partial"
    return {
        "status": status,
        "stop_loss": [_as_protection_row(order) for order in stop_loss],
        "take_profits": [_as_protection_row(order) for order in take_profits],
    }
