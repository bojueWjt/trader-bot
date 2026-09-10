from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Protocol

from execution_domain.order_ownership import is_robot_client_order_id
from runtime.exchange_cancel_adapter import BinanceApiError


TERMINAL_LOCAL_ORDER_STATUSES = frozenset(
    {
        "CANCELED",
        "CANCELLED",
        "CLOSED",
        "DENIED",
        "EXPIRED",
        "FILLED",
        "REJECTED",
    }
)
EXECUTED_ALGO_STATUSES = frozenset(
    {
        "FINISHED",
        "TRIGGERED",
        "FILLED",
    }
)
NON_EXECUTED_TERMINAL_ALGO_STATUSES = frozenset(
    {
        "CANCELED",
        "CANCELLED",
        "EXPIRED",
        "REJECTED",
    }
)
VENUE_TERMINAL_EVENT_TYPES = {
    "CANCELED": "OrderCanceled",
    "CANCELLED": "OrderCanceled",
    "EXPIRED": "OrderExpired",
    "EXPIRED_IN_MATCH": "OrderExpired",
    "REJECTED": "OrderRejected",
}
VENUE_ORDER_STATUSES = frozenset(
    {
        "CANCELED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
        "FILLED",
        "NEW",
        "PARTIALLY_FILLED",
        "REJECTED",
    }
)
RECOVERY_SOURCE = "exchange_reconciliation"
USER_TRADES_LIMIT = 1000


class ExchangeTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any: ...


class OwnedOrderRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnedOrderCandidate:
    client_order_id: str
    venue_order_id: str
    instrument_id: str
    symbol: str
    side: str
    position_side: str
    order_type: str
    time_in_force: str
    reduce_only: bool
    tags: tuple[str, ...]


@dataclass(frozen=True)
class RecoveredOrderEvent:
    event_type: str
    ts_event: int
    client_order_id: str
    venue_order_id: str
    instrument_id: str
    trade_id: str | None = None
    order_side: str = ""
    side: str = ""
    position_side: str = ""
    order_type: str = ""
    time_in_force: str = ""
    quantity: str = ""
    filled_qty: str = ""
    leaves_qty: str = ""
    price: str = ""
    avg_px: str = ""
    last_qty: str = ""
    last_px: str = ""
    quote_qty: str = ""
    commission: str = ""
    commission_asset: str = ""
    realized_pnl: str = ""
    reduce_only: bool = False
    maker: bool = False
    buyer: bool = False
    status: str = ""
    reason: str = ""
    recovered: bool = True
    source: str = RECOVERY_SOURCE
    tags: tuple[str, ...] = ()


class BinanceOwnedOrderReconciler:
    def __init__(
        self,
        *,
        transport: ExchangeTransport,
        monotonic: Any = time.monotonic,
    ) -> None:
        self._transport = transport
        self._monotonic = monotonic

    def capture(
        self,
        orders: Iterable[Any],
    ) -> tuple[OwnedOrderCandidate, ...]:
        candidates = []
        for order in orders:
            candidate = _owned_order_candidate(order)
            if candidate is not None:
                candidates.append(candidate)
        return tuple(candidates)

    def recover(
        self,
        orders: Iterable[OwnedOrderCandidate | Any],
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[RecoveredOrderEvent, ...]:
        candidates = []
        for order in orders:
            candidate = order
            if not isinstance(candidate, OwnedOrderCandidate):
                candidate = _owned_order_candidate(order)
            if candidate is not None:
                candidates.append(candidate)

        events = []
        for candidate in candidates:
            try:
                order_snapshot = self._query_order(
                    candidate,
                    deadline_monotonic=deadline_monotonic,
                )
            except BinanceApiError as exc:
                if exc.code != -2013:
                    raise
                try:
                    algo_snapshot = self._query_algo_order(
                        candidate,
                        deadline_monotonic=deadline_monotonic,
                    )
                except BinanceApiError as algo_exc:
                    if algo_exc.code != -2013:
                        raise
                    events.extend(
                        self._recover_missing_order(
                            candidate,
                            deadline_monotonic=deadline_monotonic,
                        )
                    )
                    continue
                _validate_algo_order_identity(candidate, algo_snapshot)
                algo_status = _text_value(
                    algo_snapshot.get("algoStatus")
                ).upper()
                if algo_status in EXECUTED_ALGO_STATUSES:
                    actual_order_id = _text_value(
                        algo_snapshot.get("actualOrderId")
                    )
                    if not actual_order_id:
                        continue
                    order_snapshot = self._query_actual_order(
                        candidate,
                        actual_order_id,
                        deadline_monotonic=deadline_monotonic,
                    )
                    trades = self._query_trades(
                        candidate,
                        order_snapshot,
                        deadline_monotonic=deadline_monotonic,
                    )
                    events.extend(
                        _recovered_events(
                            candidate,
                            order_snapshot,
                            trades,
                        )
                    )
                    continue
                if algo_status in NON_EXECUTED_TERMINAL_ALGO_STATUSES:
                    events.extend(
                        _recovered_missing_order_events(
                            candidate,
                            (),
                            reason=f"algo_order_terminal({algo_status})",
                        )
                    )
                    continue
                continue
            trades = self._query_trades(
                candidate,
                order_snapshot,
                deadline_monotonic=deadline_monotonic,
            )
            events.extend(
                _recovered_events(
                    candidate,
                    order_snapshot,
                    trades,
                )
            )
        return tuple(events)

    def _query_actual_order(
        self,
        candidate: OwnedOrderCandidate,
        actual_order_id: str,
        *,
        deadline_monotonic: float | None,
    ) -> Mapping[str, Any]:
        payload = self._request(
            "/fapi/v1/order",
            {
                "symbol": candidate.symbol,
                "orderId": actual_order_id,
            },
            deadline_monotonic=deadline_monotonic,
        )
        if not isinstance(payload, Mapping):
            raise OwnedOrderRecoveryError(
                "Binance actual order query returned an invalid object"
            )
        _validate_order_identity(
            candidate,
            payload,
            expected_venue_order_id=actual_order_id,
            allow_client_order_id_mismatch=True,
        )
        return payload

    def _query_algo_order(
        self,
        candidate: OwnedOrderCandidate,
        *,
        deadline_monotonic: float | None,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {"symbol": candidate.symbol}
        if candidate.venue_order_id:
            params["algoId"] = candidate.venue_order_id
        else:
            params["clientAlgoId"] = candidate.client_order_id
        payload = self._request(
            "/fapi/v1/algoOrder",
            params,
            deadline_monotonic=deadline_monotonic,
        )
        if not isinstance(payload, Mapping):
            raise OwnedOrderRecoveryError(
                "Binance algo order query returned an invalid object"
            )
        return payload

    def _recover_missing_order(
        self,
        candidate: OwnedOrderCandidate,
        *,
        deadline_monotonic: float | None,
    ) -> tuple[RecoveredOrderEvent, ...]:
        trades = ()
        if candidate.venue_order_id:
            trades = self._query_trades_by_venue_order_id(
                candidate,
                candidate.venue_order_id,
                deadline_monotonic=deadline_monotonic,
            )
        return _recovered_missing_order_events(
            candidate,
            trades,
        )

    def _query_order(
        self,
        candidate: OwnedOrderCandidate,
        *,
        deadline_monotonic: float | None,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {"symbol": candidate.symbol}
        if candidate.venue_order_id:
            params["orderId"] = candidate.venue_order_id
        else:
            params["origClientOrderId"] = candidate.client_order_id
        payload = self._request(
            "/fapi/v1/order",
            params,
            deadline_monotonic=deadline_monotonic,
        )
        if not isinstance(payload, Mapping):
            raise OwnedOrderRecoveryError(
                "Binance order query returned an invalid object"
            )
        _validate_order_identity(candidate, payload)
        return payload

    def _query_trades(
        self,
        candidate: OwnedOrderCandidate,
        order_snapshot: Mapping[str, Any],
        *,
        deadline_monotonic: float | None,
    ) -> tuple[Mapping[str, Any], ...]:
        venue_order_id = _required_text(
            order_snapshot.get("orderId"),
            "Binance order query orderId",
        )
        return self._query_trades_by_venue_order_id(
            candidate,
            venue_order_id,
            deadline_monotonic=deadline_monotonic,
        )

    def _query_trades_by_venue_order_id(
        self,
        candidate: OwnedOrderCandidate,
        venue_order_id: str,
        *,
        deadline_monotonic: float | None,
    ) -> tuple[Mapping[str, Any], ...]:
        payload = self._request(
            "/fapi/v1/userTrades",
            {
                "symbol": candidate.symbol,
                "orderId": venue_order_id,
                "limit": USER_TRADES_LIMIT,
            },
            deadline_monotonic=deadline_monotonic,
        )
        if not isinstance(payload, list):
            raise OwnedOrderRecoveryError(
                "Binance userTrades query returned an invalid collection"
            )
        trades = []
        trade_ids = set()
        for raw_trade in payload:
            if not isinstance(raw_trade, Mapping):
                raise OwnedOrderRecoveryError(
                    "Binance userTrades query returned an invalid trade"
                )
            _validate_trade_identity(
                candidate,
                venue_order_id,
                raw_trade,
            )
            trade_id = _required_text(
                raw_trade.get("id"),
                "Binance userTrades trade id",
            )
            if trade_id in trade_ids:
                raise OwnedOrderRecoveryError(
                    "Binance userTrades contains a duplicate trade id"
                )
            trade_ids.add(trade_id)
            trades.append(raw_trade)
        trades.sort(key=_trade_sort_key)
        return tuple(trades)

    def _request(
        self,
        path: str,
        params: dict[str, Any],
        *,
        deadline_monotonic: float | None,
    ) -> Any:
        timeout_seconds = None
        if deadline_monotonic is not None:
            timeout_seconds = (
                float(deadline_monotonic) - self._monotonic()
            )
            if timeout_seconds <= 0:
                raise TimeoutError(
                    "owned order recovery deadline exceeded"
                )
        return self._transport.request(
            "GET",
            path,
            params,
            timeout_seconds=timeout_seconds,
        )


def _owned_order_candidate(order: Any) -> OwnedOrderCandidate | None:
    client_order_id = _text_attr(
        order,
        "client_order_id",
        "clientOrderId",
    )
    if not is_robot_client_order_id(client_order_id):
        return None
    status = _enum_text(_attr(order, "status")).upper()
    if status in TERMINAL_LOCAL_ORDER_STATUSES:
        return None
    instrument_id = _text_attr(order, "instrument_id")
    if not instrument_id:
        raise OwnedOrderRecoveryError(
            f"robot order {client_order_id} is missing instrument_id"
        )
    symbol = _binance_symbol(instrument_id)
    tags_raw = _attr(order, "tags")
    tags = ()
    if isinstance(tags_raw, (list, tuple)):
        tags = tuple(str(tag) for tag in tags_raw)
    position_side = _enum_text(
        _attr(order, "position_side")
    ).upper()
    return OwnedOrderCandidate(
        client_order_id=str(client_order_id),
        venue_order_id=_text_attr(
            order,
            "venue_order_id",
            "order_id",
        ),
        instrument_id=instrument_id,
        symbol=symbol,
        side=_normalized_order_side(
            _attr(order, "side", "order_side")
        ),
        position_side=position_side,
        order_type=_enum_text(_attr(order, "order_type")).upper(),
        time_in_force=_enum_text(
            _attr(order, "time_in_force")
        ).upper(),
        reduce_only=bool(_attr(order, "reduce_only") or False),
        tags=tags,
    )


def _validate_order_identity(
    candidate: OwnedOrderCandidate,
    payload: Mapping[str, Any],
    *,
    expected_venue_order_id: str = "",
    allow_client_order_id_mismatch: bool = False,
) -> None:
    symbol = _required_text(
        payload.get("symbol"),
        "Binance order query symbol",
    ).upper()
    if symbol != candidate.symbol:
        raise OwnedOrderRecoveryError(
            "Binance order query symbol does not match candidate"
        )
    client_order_id = _required_text(
        payload.get("clientOrderId"),
        "Binance order query clientOrderId",
    )
    if (
        not allow_client_order_id_mismatch
        and client_order_id != candidate.client_order_id
    ):
        raise OwnedOrderRecoveryError(
            "Binance order query clientOrderId does not match candidate"
        )
    venue_order_id = _required_text(
        payload.get("orderId"),
        "Binance order query orderId",
    )
    expected_order_id = expected_venue_order_id
    if not expected_order_id:
        expected_order_id = candidate.venue_order_id
    if expected_order_id and venue_order_id != expected_order_id:
        raise OwnedOrderRecoveryError(
            "Binance order query orderId does not match candidate"
        )
    status = _required_text(
        payload.get("status"),
        "Binance order query status",
    ).upper()
    if status not in VENUE_ORDER_STATUSES:
        raise OwnedOrderRecoveryError(
            f"unsupported Binance order status: {status}"
        )
    side = _required_text(
        payload.get("side"),
        "Binance order query side",
    ).upper()
    if candidate.side and side != candidate.side:
        raise OwnedOrderRecoveryError(
            "Binance order query side does not match candidate"
        )
    position_side = _text_value(
        payload.get("positionSide")
    ).upper()
    if (
        candidate.position_side
        and position_side
        and position_side != candidate.position_side
    ):
        raise OwnedOrderRecoveryError(
            "Binance order query positionSide does not match candidate"
        )


def _validate_algo_order_identity(
    candidate: OwnedOrderCandidate,
    payload: Mapping[str, Any],
) -> None:
    symbol = _required_text(
        payload.get("symbol"),
        "Binance algo order query symbol",
    ).upper()
    if symbol != candidate.symbol:
        raise OwnedOrderRecoveryError(
            "Binance algo order query symbol does not match candidate"
        )
    algo_order_id = _text_value(payload.get("algoId"))
    client_algo_id = _text_value(payload.get("clientAlgoId"))
    if candidate.venue_order_id:
        if not algo_order_id:
            raise OwnedOrderRecoveryError(
                "Binance algo order query is missing algoId"
            )
        if algo_order_id != candidate.venue_order_id:
            raise OwnedOrderRecoveryError(
                "Binance algo order query algoId does not match candidate"
            )
        if client_algo_id and client_algo_id != candidate.client_order_id:
            raise OwnedOrderRecoveryError(
                "Binance algo order query clientAlgoId does not match candidate"
            )
        return
    if not client_algo_id:
        raise OwnedOrderRecoveryError(
            "Binance algo order query is missing clientAlgoId"
        )
    if client_algo_id != candidate.client_order_id:
        raise OwnedOrderRecoveryError(
            "Binance algo order query clientAlgoId does not match candidate"
        )


def _validate_trade_identity(
    candidate: OwnedOrderCandidate,
    venue_order_id: str,
    payload: Mapping[str, Any],
) -> None:
    symbol = _required_text(
        payload.get("symbol"),
        "Binance userTrades symbol",
    ).upper()
    if symbol != candidate.symbol:
        raise OwnedOrderRecoveryError(
            "Binance userTrades symbol does not match order"
        )
    trade_order_id = _required_text(
        payload.get("orderId"),
        "Binance userTrades orderId",
    )
    if trade_order_id != venue_order_id:
        raise OwnedOrderRecoveryError(
            "Binance userTrades orderId does not match order"
        )
    _required_text(
        payload.get("id"),
        "Binance userTrades trade id",
    )
    order_side = _text_value(
        payload.get("side")
    ).upper()
    if candidate.side and order_side and order_side != candidate.side:
        raise OwnedOrderRecoveryError(
            "Binance userTrades side does not match order"
        )
    trade_position_side = _text_value(
        payload.get("positionSide")
    ).upper()
    if (
        candidate.position_side
        and trade_position_side
        and trade_position_side != candidate.position_side
    ):
        raise OwnedOrderRecoveryError(
            "Binance userTrades positionSide does not match order"
        )


def _recovered_events(
    candidate: OwnedOrderCandidate,
    order_snapshot: Mapping[str, Any],
    trades: tuple[Mapping[str, Any], ...],
) -> tuple[RecoveredOrderEvent, ...]:
    venue_order_id = _required_text(
        order_snapshot.get("orderId"),
        "Binance order query orderId",
    )
    status = _required_text(
        order_snapshot.get("status"),
        "Binance order query status",
    ).upper()
    quantity = _positive_decimal(
        order_snapshot.get("origQty"),
        "Binance order query origQty",
    )
    executed_quantity = _nonnegative_decimal(
        order_snapshot.get("executedQty"),
        "Binance order query executedQty",
    )
    trade_quantities = [
        _positive_decimal(
            trade.get("qty"),
            "Binance userTrades qty",
        )
        for trade in trades
    ]
    recovered_quantity = sum(trade_quantities, Decimal("0"))
    if recovered_quantity != executed_quantity:
        raise OwnedOrderRecoveryError(
            "Binance userTrades quantity does not match order executedQty"
        )
    if status == "FILLED" and not trades:
        raise OwnedOrderRecoveryError(
            "filled Binance order has no attributable userTrades"
        )

    side = _text_value(
        order_snapshot.get("side")
    ).upper() or candidate.side
    position_side = _text_value(
        order_snapshot.get("positionSide")
    ).upper() or candidate.position_side
    order_type = _text_value(
        order_snapshot.get("type")
    ).upper() or candidate.order_type
    time_in_force = _text_value(
        order_snapshot.get("timeInForce")
    ).upper() or candidate.time_in_force
    avg_price = _decimal_text(order_snapshot.get("avgPrice"))
    order_price = _decimal_text(order_snapshot.get("price"))
    reduce_only = bool(
        order_snapshot.get("reduceOnly", candidate.reduce_only)
    )

    events = []
    cumulative = Decimal("0")
    for trade, trade_quantity in zip(
        trades,
        trade_quantities,
        strict=True,
    ):
        cumulative += trade_quantity
        leaves = max(quantity - cumulative, Decimal("0"))
        trade_side = _text_value(trade.get("side")).upper()
        if not trade_side:
            trade_side = side
        trade_position_side = _text_value(
            trade.get("positionSide")
        ).upper()
        if not trade_position_side:
            trade_position_side = position_side
        events.append(
            RecoveredOrderEvent(
                event_type="OrderFilled",
                ts_event=_binance_time_ns(
                    trade.get("time"),
                    "Binance userTrades time",
                ),
                client_order_id=candidate.client_order_id,
                venue_order_id=venue_order_id,
                trade_id=_required_text(
                    trade.get("id"),
                    "Binance userTrades trade id",
                ),
                instrument_id=candidate.instrument_id,
                order_side=trade_side,
                side=trade_side,
                position_side=trade_position_side,
                order_type=order_type,
                time_in_force=time_in_force,
                quantity=_format_decimal(quantity),
                filled_qty=_format_decimal(cumulative),
                leaves_qty=_format_decimal(leaves),
                price=order_price,
                avg_px=avg_price,
                last_qty=_format_decimal(trade_quantity),
                last_px=_required_decimal_text(
                    trade.get("price"),
                    "Binance userTrades price",
                ),
                quote_qty=_required_decimal_text(
                    trade.get("quoteQty"),
                    "Binance userTrades quoteQty",
                ),
                commission=_required_nonnegative_decimal_text(
                    trade.get("commission"),
                    "Binance userTrades commission",
                ),
                commission_asset=_required_text(
                    trade.get("commissionAsset"),
                    "Binance userTrades commissionAsset",
                ).upper(),
                realized_pnl=_required_finite_decimal_text(
                    trade.get("realizedPnl"),
                    "Binance userTrades realizedPnl",
                ),
                reduce_only=reduce_only,
                maker=bool(trade.get("maker", False)),
                buyer=bool(trade.get("buyer", False)),
                status=status,
                tags=candidate.tags,
            )
        )

    terminal_event_type = VENUE_TERMINAL_EVENT_TYPES.get(status)
    if terminal_event_type is not None:
        events.append(
            RecoveredOrderEvent(
                event_type=terminal_event_type,
                ts_event=_order_event_time(order_snapshot),
                client_order_id=candidate.client_order_id,
                venue_order_id=venue_order_id,
                instrument_id=candidate.instrument_id,
                order_side=side,
                side=side,
                position_side=position_side,
                order_type=order_type,
                time_in_force=time_in_force,
                quantity=_format_decimal(quantity),
                filled_qty=_format_decimal(executed_quantity),
                leaves_qty=_format_decimal(
                    max(quantity - executed_quantity, Decimal("0"))
                ),
                price=order_price,
                avg_px=avg_price,
                reduce_only=reduce_only,
                status=status,
                reason=f"venue_status={status}",
                tags=candidate.tags,
            )
        )
    return tuple(events)


def _recovered_missing_order_events(
    candidate: OwnedOrderCandidate,
    trades: tuple[Mapping[str, Any], ...],
    *,
    reason: str = "venue_order_missing(-2013)",
) -> tuple[RecoveredOrderEvent, ...]:
    tags = _order_vanished_tags(candidate.tags)
    tagged_candidate = replace(candidate, tags=tags)
    events = []
    filled_quantity = ""
    trades_were_checked = reason == "venue_order_missing(-2013)"

    if candidate.venue_order_id and trades_were_checked:
        reason = (
            f"{reason}; user_trades_checked; "
            f"attributed_trade_count={len(trades)}"
        )
        if trades:
            trade_quantities = [
                _positive_decimal(
                    trade.get("qty"),
                    "Binance userTrades qty",
                )
                for trade in trades
            ]
            recovered_quantity = sum(
                trade_quantities,
                Decimal("0"),
            )
            filled_quantity = _format_decimal(recovered_quantity)
            reason = (
                f"{reason}; "
                f"attributed_filled_qty={filled_quantity}"
            )
            synthetic_snapshot = {
                "orderId": candidate.venue_order_id,
                "status": "PARTIALLY_FILLED",
                "origQty": filled_quantity,
                "executedQty": filled_quantity,
                "side": candidate.side,
                "positionSide": candidate.position_side,
                "type": candidate.order_type,
                "timeInForce": candidate.time_in_force,
                "reduceOnly": candidate.reduce_only,
            }
            recovered_fills = _recovered_events(
                tagged_candidate,
                synthetic_snapshot,
                trades,
            )
            for fill in recovered_fills:
                events.append(
                    replace(
                        fill,
                        quantity="",
                        leaves_qty="",
                        reason=reason,
                    )
                )
    elif not candidate.venue_order_id and trades_were_checked:
        reason = (
            f"{reason}; "
            "fill_attribution_unverifiable_without_venue_order_id"
        )

    events.append(
        RecoveredOrderEvent(
            event_type="OrderCanceled",
            ts_event=_missing_order_event_time(trades),
            client_order_id=candidate.client_order_id,
            venue_order_id=candidate.venue_order_id,
            instrument_id=candidate.instrument_id,
            order_side=candidate.side,
            side=candidate.side,
            position_side=candidate.position_side,
            order_type=candidate.order_type,
            time_in_force=candidate.time_in_force,
            filled_qty=filled_quantity,
            reduce_only=candidate.reduce_only,
            status="CANCELED",
            reason=reason,
            recovered=True,
            source=RECOVERY_SOURCE,
            tags=tags,
        )
    )
    return tuple(events)


def _order_vanished_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    if "order_vanished" in tags:
        return tags
    return (*tags, "order_vanished")


def _missing_order_event_time(
    trades: tuple[Mapping[str, Any], ...],
) -> int:
    if not trades:
        return 0
    return _binance_time_ns(
        trades[-1].get("time"),
        "Binance userTrades time",
    )


def _trade_sort_key(payload: Mapping[str, Any]) -> tuple[int, int]:
    return (
        _positive_int(
            payload.get("time"),
            "Binance userTrades time",
        ),
        _positive_int(
            payload.get("id"),
            "Binance userTrades trade id",
        ),
    )


def _order_event_time(payload: Mapping[str, Any]) -> int:
    for key in ("updateTime", "time"):
        value = payload.get(key)
        if value is not None:
            return _binance_time_ns(
                value,
                f"Binance order query {key}",
            )
    raise OwnedOrderRecoveryError(
        "Binance terminal order is missing event time"
    )


def _binance_symbol(instrument_id: str) -> str:
    normalized = str(instrument_id).strip().upper()
    match = re.fullmatch(
        r"([A-Z0-9]+?)(?:-PERP)?(?:\.BINANCE)?",
        normalized,
    )
    if match is None:
        raise OwnedOrderRecoveryError(
            f"unsupported Binance instrument_id: {instrument_id}"
        )
    return match.group(1)


def _attr(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        attribute = getattr(value, name, None)
        if attribute is not None:
            return attribute
    return None


def _text_attr(value: Any, *names: str) -> str:
    return _text_value(_attr(value, *names))


def _enum_text(value: Any) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name:
        return str(name)
    return _text_value(value)


def _normalized_order_side(value: Any) -> str:
    side = _enum_text(value).upper()
    if side == "LONG":
        return "BUY"
    if side == "SHORT":
        return "SELL"
    return side


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw).strip()


def _required_text(value: Any, label: str) -> str:
    text = _text_value(value)
    if not text:
        raise OwnedOrderRecoveryError(f"{label} is required")
    return text


def _decimal_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if not decimal_value.is_finite():
        return ""
    return _format_decimal(decimal_value)


def _required_decimal_text(value: Any, label: str) -> str:
    text = _decimal_text(value)
    if not text:
        raise OwnedOrderRecoveryError(f"{label} is invalid")
    decimal_value = Decimal(text)
    if decimal_value <= 0:
        raise OwnedOrderRecoveryError(f"{label} must be positive")
    return text


def _required_nonnegative_decimal_text(
    value: Any,
    label: str,
) -> str:
    text = _required_finite_decimal_text(value, label)
    if Decimal(text) < 0:
        raise OwnedOrderRecoveryError(
            f"{label} must be non-negative"
        )
    return text


def _required_finite_decimal_text(
    value: Any,
    label: str,
) -> str:
    text = _decimal_text(value)
    if not text:
        raise OwnedOrderRecoveryError(f"{label} is invalid")
    return text


def _positive_decimal(value: Any, label: str) -> Decimal:
    decimal_value = _nonnegative_decimal(value, label)
    if decimal_value <= 0:
        raise OwnedOrderRecoveryError(f"{label} must be positive")
    return decimal_value


def _nonnegative_decimal(value: Any, label: str) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OwnedOrderRecoveryError(f"{label} is invalid") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise OwnedOrderRecoveryError(f"{label} is invalid")
    return decimal_value


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OwnedOrderRecoveryError(f"{label} is invalid") from exc
    if parsed <= 0:
        raise OwnedOrderRecoveryError(f"{label} must be positive")
    return parsed


def _binance_time_ns(value: Any, label: str) -> int:
    return _positive_int(value, label) * 1_000_000


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")
