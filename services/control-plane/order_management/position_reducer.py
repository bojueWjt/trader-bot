from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg2.extras import Json

from db.connection import transaction

from .db_helpers import decimal_or_none, ensure_aware, ensure_execution_event
from .identifiers import PositionKey, canonical_account_id, canonical_instrument_key
from .state_descriptor import event_target


@dataclass(frozen=True)
class PositionApplyResult:
    event_id: str
    position_key: str
    status: str


class PositionProjectionReducer:
    def apply_event(self, conn, event: dict[str, Any]) -> PositionApplyResult:
        normalized = _normalize_event(event)
        with transaction(conn):
            ensure_execution_event(conn, normalized)
            current_quantity = _current_quantity(
                conn, normalized["account_id"], normalized["position_key"]
            )
            target = event_target(
                "position",
                normalized["event_type"],
                normalized["payload"],
                current_quantity=current_quantity,
            )
            if _is_external(normalized["payload"]):
                target = "external"
            if target is None or target == "self":
                target = "open"
            quantity = _event_quantity(normalized["payload"])
            if target == "closed":
                quantity = Decimal("0")
            _upsert_position(conn, normalized, status=target, quantity=quantity)
            if target == "closed":
                _clear_protective_orders(conn, normalized["account_id"], normalized["position_key"])
            return PositionApplyResult(
                event_id=normalized["event_id"],
                position_key=normalized["position_key"],
                status=target,
            )


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("payload") or {})
    account_id = canonical_account_id(event["account_id"])
    instrument_id = str(payload.get("instrument_id") or event.get("instrument_id") or "UNKNOWN")
    venue_symbol = canonical_instrument_key(instrument_id)
    key = PositionKey(account_id, venue_symbol, _key_side(payload)).key()
    return {
        **event,
        "account_id": account_id,
        "payload": {**payload, "instrument_id": instrument_id},
        "position_key": key,
        "venue_symbol": venue_symbol,
        "ts_event": ensure_aware(event.get("ts_event")),
    }


def _key_side(payload: dict[str, Any]) -> str | None:
    mode = str(payload.get("position_mode") or "").upper()
    raw_side = str(payload.get("position_side") or payload.get("side") or "").lower()
    if mode == "HEDGE" and raw_side in {"long", "short"}:
        return raw_side
    return None


def _is_external(payload: dict[str, Any]) -> bool:
    return str(payload.get("ownership") or payload.get("source") or "").upper() == "EXTERNAL"


def _event_quantity(payload: dict[str, Any]) -> Decimal:
    return abs(decimal_or_none(payload.get("quantity") or payload.get("qty")) or Decimal("0"))


def _current_quantity(conn, account_id: str, position_key: str) -> Decimal | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT quantity
            FROM positions_projection
            WHERE account_id=%s AND position_id=%s
            FOR UPDATE
            """,
            (account_id, position_key),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _upsert_position(
    conn,
    event: dict[str, Any],
    *,
    status: str,
    quantity: Decimal,
) -> None:
    payload = event["payload"]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity,
                avg_entry_price, mark_price, unrealized_pnl, status,
                updated_from_event_id, ts_event, updated_at, payload
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
            ON CONFLICT (account_id, position_id) DO UPDATE SET
                instrument_id=EXCLUDED.instrument_id,
                side=EXCLUDED.side,
                quantity=EXCLUDED.quantity,
                avg_entry_price=COALESCE(EXCLUDED.avg_entry_price, positions_projection.avg_entry_price),
                mark_price=COALESCE(EXCLUDED.mark_price, positions_projection.mark_price),
                unrealized_pnl=COALESCE(EXCLUDED.unrealized_pnl, positions_projection.unrealized_pnl),
                status=EXCLUDED.status,
                updated_from_event_id=EXCLUDED.updated_from_event_id,
                ts_event=EXCLUDED.ts_event,
                updated_at=now(),
                payload=positions_projection.payload || EXCLUDED.payload
            """,
            (
                event["account_id"],
                event["position_key"],
                payload["instrument_id"],
                _position_side(payload),
                quantity,
                decimal_or_none(payload.get("avg_entry_price")),
                decimal_or_none(payload.get("mark_price")),
                decimal_or_none(payload.get("unrealized_pnl")),
                status,
                event["event_id"],
                event["ts_event"],
                Json(payload),
            ),
        )


def _position_side(payload: dict[str, Any]) -> str:
    raw = str(payload.get("side") or payload.get("position_side") or "").lower()
    if raw in {"short", "sell"}:
        return "short"
    quantity = decimal_or_none(payload.get("quantity") or payload.get("qty"))
    if quantity is not None and quantity < 0:
        return "short"
    return "long"


def _clear_protective_orders(conn, account_id: str, position_key: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE protective_orders_projection
            SET active=false, status='cleared', updated_at=now()
            WHERE account_id=%s AND position_key=%s AND active
            """,
            (account_id, position_key),
        )
