from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable
from uuid import UUID

from execution_domain.order_ownership import is_robot_client_order_id

from .contracts import ExecutionEventEnvelopeV1


ORDER_EVENT_TYPES = frozenset(
    {
        "OrderSubmitted",
        "OrderAccepted",
        "OrderFilled",
        "OrderCanceled",
        "OrderRejected",
        "OrderExpired",
        "OrderUpdated",
        "OrderPendingUpdate",
        "OrderPendingCancel",
    }
)
POSITION_EVENT_TYPES = frozenset(
    {
        "PositionOpened",
        "PositionChanged",
        "PositionClosed",
    }
)
ACCOUNT_EVENT_TYPES = frozenset(
    {
        "AccountState",
        "AccountBalance",
        "AccountMargin",
    }
)

EVENT_MAPPING_CATALOG = {
    **{event_type: "order" for event_type in sorted(ORDER_EVENT_TYPES)},
    **{event_type: "position" for event_type in sorted(POSITION_EVENT_TYPES)},
    **{event_type: "account" for event_type in sorted(ACCOUNT_EVENT_TYPES)},
}


@dataclass(frozen=True)
class ProjectionConfig:
    node_id: str
    account_id: str
    lag_degrade_threshold_ms: int = 30_000
    max_flush_batch_size: int = 100
    allowed_instrument_ids: frozenset[str] = frozenset()
    require_robot_order_ownership: bool = False


class ProjectionEventMapper:
    """Maps Nautilus execution/account events into contracts-v1 envelopes.

    TODO(host-verify): Confirm exact Nautilus 1.227.0 account event class names
    and whether account updates arrive as ``AccountState`` or a more granular
    balance/margin event.
    """

    def __init__(
        self,
        config: ProjectionConfig,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._now = now or (lambda: datetime.now(timezone.utc))

    def to_envelope(self, event: Any) -> ExecutionEventEnvelopeV1 | None:
        event_type = _event_type(event)
        if event_type not in EVENT_MAPPING_CATALOG:
            return None

        ts_event_raw = _attr(event, "ts_event", "timestamp", "event_time")
        ts_event = _timestamp_to_datetime(ts_event_raw)
        ts_ingest = _ensure_aware(self._now())
        # PATCH 2026-07-10 (hk): events occasionally carry no ts_event (raw
        # None/0 -> 1970 epoch); the projection actor then computes a ~56-year
        # lag and silently halts trading (lag guard misfire). A missing event
        # time is not "infinitely stale" - stamp it with ingest time.
        if ts_event < datetime(2020, 1, 1, tzinfo=timezone.utc):
            ts_event = ts_ingest
        client_order_id = _optional_str(_attr(event, "client_order_id"))
        venue_order_id = _optional_str(_attr(event, "venue_order_id", "order_id"))
        trade_id = _optional_str(_attr(event, "trade_id", "venue_trade_id"))
        instrument_id = _optional_str(_attr(event, "instrument_id"))
        position_id = _optional_str(_attr(event, "position_id"))
        if not _event_is_in_scope(
            self._config,
            event_type=event_type,
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            position_id=position_id,
        ):
            return None
        intent_id = _intent_id(event, client_order_id)
        payload = _payload(event, instrument_id=instrument_id)
        event_id = stable_event_id(
            account_id=self._config.account_id,
            event_type=event_type,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            trade_id=trade_id,
            ts_event_raw=ts_event_raw,
        )
        return ExecutionEventEnvelopeV1(
            schema_version="1.0",
            event_id=event_id,
            node_id=self._config.node_id,
            account_id=self._config.account_id,
            intent_id=intent_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            trade_id=trade_id,
            event_type=event_type,
            ts_event=ts_event,
            ts_ingest=ts_ingest,
            payload=payload,
        )

    def is_expected_ownership_ignore(self, event: Any) -> bool:
        event_type = _event_type(event)
        if event_type not in EVENT_MAPPING_CATALOG:
            return False
        client_order_id = _optional_str(_attr(event, "client_order_id"))
        instrument_id = _optional_str(_attr(event, "instrument_id"))
        position_id = _optional_str(_attr(event, "position_id"))
        return not _event_is_in_scope(
            self._config,
            event_type=event_type,
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            position_id=position_id,
        )


def stable_event_id(
    *,
    account_id: str,
    event_type: str,
    client_order_id: str | None,
    venue_order_id: str | None,
    trade_id: str | None,
    ts_event_raw: Any,
) -> str:
    material = {
        "account_id": account_id,
        "event_type": event_type,
        "client_order_id": client_order_id or "",
        "venue_order_id": venue_order_id or "",
        "trade_id": trade_id or "",
        "ts_event": _stable_timestamp_part(ts_event_raw),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return sha256(encoded).hexdigest()


def _event_type(event: Any) -> str:
    event_type = _attr(event, "event_type", "type")
    if event_type is not None:
        text = str(getattr(event_type, "value", event_type))
        if text:
            return text
    return event.__class__.__name__


def _payload(event: Any, *, instrument_id: str | None) -> dict[str, Any]:
    keys = (
        "instrument_id",
        "order_side",
        "side",
        "order_type",
        "time_in_force",
        "quantity",
        "qty",
        "filled_qty",
        "leaves_qty",
        "price",
        "avg_px",
        "last_px",
        "last_qty",
        "quote_qty",
        "commission",
        "commission_asset",
        "position_side",
        "reduce_only",
        "maker",
        "buyer",
        "status",
        "recovered",
        "source",
        "position_id",
        "realized_pnl",
        "currency",
        "balance",
        "margin_balance",
        "total",
        "free",
        "locked",
        "reason",
    )
    payload: dict[str, Any] = {}
    # Enriched order fields attached upstream (mounted actor wrapper): price,
    # trigger_price, quantity, side, order_type, reduce_only, position_id. The
    # thin native event payloads left orders_projection without prices.
    extra = getattr(event, "_projection_payload_extra", None)
    if isinstance(extra, dict):
        for key, value in extra.items():
            payload[key] = _jsonable(value)
    if instrument_id is not None:
        payload["instrument_id"] = instrument_id
    for key in keys:
        value = _attr(event, key)
        if value is not None:
            payload[key] = _jsonable(value)
    tags = _attr(event, "tags")
    if tags:
        payload["tags"] = [str(tag) for tag in tags]
    return payload


def _intent_id(event: Any, client_order_id: str | None) -> UUID | None:
    direct = _optional_str(_attr(event, "intent_id"))
    if direct is not None:
        try:
            return UUID(direct)
        except ValueError:
            return None
    for tag in _attr(event, "tags") or ():
        text = str(tag)
        if text.startswith("intent_id="):
            try:
                return UUID(text.split("=", 1)[1])
            except ValueError:
                return None
    if client_order_id is not None:
        decoded = _intent_from_client_order_id(client_order_id)
        if decoded is not None:
            return decoded
    return None


def _intent_from_client_order_id(client_order_id: str) -> UUID | None:
    if len(client_order_id) != 35 or not client_order_id.startswith("B"):
        return None
    try:
        return UUID(hex=client_order_id[1:33])
    except ValueError:
        return None


def _event_is_in_scope(
    config: ProjectionConfig,
    *,
    event_type: str,
    client_order_id: str | None,
    instrument_id: str | None,
    position_id: str | None,
) -> bool:
    family = EVENT_MAPPING_CATALOG[event_type]
    if family == "account":
        return True
    allowed_instrument_ids = config.allowed_instrument_ids
    if allowed_instrument_ids:
        if instrument_id not in allowed_instrument_ids:
            return False
    if not config.require_robot_order_ownership:
        return True
    if family == "order":
        return is_robot_client_order_id(client_order_id)
    if position_id is None:
        return False
    return not position_id.upper().endswith("-EXTERNAL")


def _timestamp_to_datetime(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return _ensure_aware(raw)
    if raw is None:
        return datetime.fromtimestamp(0, timezone.utc)
    value = int(raw)
    if abs(value) > 10_000_000_000_000:
        return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc)
    if abs(value) > 10_000_000_000:
        return datetime.fromtimestamp(value / 1_000, timezone.utc)
    return datetime.fromtimestamp(value, timezone.utc)


def _stable_timestamp_part(raw: Any) -> str:
    if isinstance(raw, datetime):
        return _ensure_aware(raw).isoformat()
    return "" if raw is None else str(raw)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(getattr(value, "value", value))
    return text or None


def _attr(event: Any, *names: str) -> Any:
    for name in names:
        if isinstance(event, dict) and name in event:
            return event[name]
        value = getattr(event, name, None)
        if value is not None:
            return value
    return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return _ensure_aware(value).isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(getattr(value, "value", value))
