from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json

from db.connection import transaction

from .db_helpers import decimal_or_none, ensure_aware, ensure_execution_event, record_reconciliation_finding
from .identifiers import canonical_account_id, canonical_instrument_key
from .state_descriptor import (
    domain_descriptor,
    event_target,
    legal_transition,
    state_rank,
    transition_target,
)


@dataclass(frozen=True)
class OrderApplyResult:
    event_id: str
    status: str | None
    applied: bool
    duplicate: bool = False
    anomaly: dict[str, Any] | None = None


class OrderProjectionReducer:
    def apply_event(
        self,
        conn,
        event: dict[str, Any],
        *,
        manage_transaction: bool = True,
    ) -> OrderApplyResult:
        """Apply one order event to the projection.

        manage_transaction=True (default) keeps the historical behavior of
        committing/rolling back the connection itself. manage_transaction=False
        runs entirely inside the caller's transaction (e.g. the node event
        ingest savepoint) without committing or rolling back.
        """
        normalized = _normalize_event(event)
        tx_scope = transaction(conn) if manage_transaction else nullcontext(conn)
        with tx_scope:
            if _order_event_seen(conn, normalized["event_id"]):
                status = _current_status(
                    conn, normalized["account_id"], normalized["client_order_id"]
                )
                return OrderApplyResult(
                    event_id=normalized["event_id"],
                    status=status,
                    applied=False,
                    duplicate=True,
                )

            ensure_execution_event(conn, normalized)
            current = _fetch_order(
                conn,
                normalized["account_id"],
                normalized["client_order_id"],
            )
            current_status = current["status"] if current else None
            current_ts = current["ts_event"] if current else None
            target = event_target(
                "order", normalized["event_type"], normalized["payload"]
            )
            if target is None:
                target = transition_target("order", current_status, normalized["event_type"])
            if target is None:
                target = _explicit_target(normalized["payload"]) or "lost"
            if target == "self":
                target = current_status if current else "submitted"

            if current_status is not None and target != current_status:
                stale_backfill = (
                    current_ts is not None
                    and normalized["ts_event"] < ensure_aware(current_ts)
                    and state_rank("order", target) <= state_rank("order", current_status)
                )
                if stale_backfill:
                    _insert_order_event(conn, normalized, current.get("order_projection_id"))
                    return OrderApplyResult(
                        event_id=normalized["event_id"],
                        status=current_status,
                        applied=False,
                    )
                if not legal_transition("order", current_status, target):
                    finding = record_reconciliation_finding(
                        conn,
                        account_id=normalized["account_id"],
                        finding_type="illegal_order_transition",
                        severity="error",
                        order_projection_id=current.get("order_projection_id"),
                        payload={
                            "event_id": normalized["event_id"],
                            "event_type": normalized["event_type"],
                            "from_status": current_status,
                            "to_status": target,
                            "client_order_id": normalized["client_order_id"],
                        },
                    )
                    _insert_order_event(conn, normalized, current.get("order_projection_id"))
                    return OrderApplyResult(
                        event_id=normalized["event_id"],
                        status=current_status,
                        applied=False,
                        anomaly=finding,
                    )

            order_projection_id = (
                current["order_projection_id"] if current else str(uuid4())
            )
            _upsert_order_projection(
                conn,
                normalized,
                order_projection_id=order_projection_id,
                status=target,
                current=current,
            )
            _insert_order_event(conn, normalized, order_projection_id)
            return OrderApplyResult(
                event_id=normalized["event_id"],
                status=target,
                applied=True,
            )


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("payload") or {})
    account_id = canonical_account_id(event["account_id"])
    client_order_id = (
        event.get("client_order_id")
        or payload.get("client_order_id")
        or (f"venue:{event.get('venue_order_id')}" if event.get("venue_order_id") else None)
        or f"event:{event['event_id']}"
    )
    if "instrument_id" not in payload and event.get("instrument_id"):
        payload["instrument_id"] = event["instrument_id"]
    return {
        **event,
        "account_id": account_id,
        "client_order_id": str(client_order_id),
        "payload": payload,
        "ts_event": ensure_aware(event.get("ts_event")),
    }


def _order_event_seen(conn, event_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM order_events WHERE event_id=%s", (event_id,))
        return cur.fetchone() is not None


def _current_status(conn, account_id: str, client_order_id: str) -> str | None:
    row = _fetch_order(conn, account_id, client_order_id)
    return row["status"] if row else None


def _fetch_order(conn, account_id: str, client_order_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_projection_id::text, status, ts_event, filled_quantity
            FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            FOR UPDATE
            """,
            (account_id, client_order_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "order_projection_id": row[0],
        "status": row[1],
        "ts_event": row[2],
        "filled_quantity": row[3],
    }


def _upsert_order_projection(
    conn,
    event: dict[str, Any],
    *,
    order_projection_id: str,
    status: str,
    current: dict[str, Any] | None,
) -> None:
    payload = event["payload"]
    instrument_id = str(payload.get("instrument_id") or "UNKNOWN")
    venue_symbol = canonical_instrument_key(instrument_id)
    filled_quantity = (
        decimal_or_none(payload.get("filled_qty"))
        or (current or {}).get("filled_quantity")
        or 0
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_projection (
                order_projection_id, account_id, instrument_id, venue_symbol, intent_id,
                client_order_id, venue_order_id, status, side, order_type, quantity,
                filled_quantity, price, average_fill_price, lifecycle_role,
                updated_from_event_id, ts_event, updated_at, payload
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
            ON CONFLICT (account_id, client_order_id) DO UPDATE SET
                instrument_id=EXCLUDED.instrument_id,
                venue_symbol=EXCLUDED.venue_symbol,
                intent_id=COALESCE(EXCLUDED.intent_id, orders_projection.intent_id),
                venue_order_id=COALESCE(EXCLUDED.venue_order_id, orders_projection.venue_order_id),
                status=EXCLUDED.status,
                side=COALESCE(EXCLUDED.side, orders_projection.side),
                order_type=COALESCE(EXCLUDED.order_type, orders_projection.order_type),
                quantity=COALESCE(EXCLUDED.quantity, orders_projection.quantity),
                filled_quantity=GREATEST(EXCLUDED.filled_quantity, orders_projection.filled_quantity),
                price=COALESCE(EXCLUDED.price, orders_projection.price),
                average_fill_price=COALESCE(EXCLUDED.average_fill_price, orders_projection.average_fill_price),
                lifecycle_role=COALESCE(EXCLUDED.lifecycle_role, orders_projection.lifecycle_role),
                updated_from_event_id=EXCLUDED.updated_from_event_id,
                ts_event=EXCLUDED.ts_event,
                updated_at=now(),
                payload=orders_projection.payload || EXCLUDED.payload
            """,
            (
                order_projection_id,
                event["account_id"],
                instrument_id,
                venue_symbol,
                event.get("intent_id"),
                event["client_order_id"],
                event.get("venue_order_id"),
                status,
                _side(payload),
                payload.get("order_type"),
                decimal_or_none(payload.get("quantity") or payload.get("qty")),
                filled_quantity,
                decimal_or_none(payload.get("price")),
                decimal_or_none(payload.get("avg_px") or payload.get("average_fill_price")),
                payload.get("lifecycle_role"),
                event["event_id"],
                event["ts_event"],
                Json(payload),
            ),
        )


def _insert_order_event(
    conn,
    event: dict[str, Any],
    order_projection_id: str | None,
) -> None:
    payload = event["payload"]
    position_key = payload.get("position_key")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO order_events (
                order_event_row_id, event_id, execution_job_id, account_id,
                order_projection_id, client_order_id, venue_order_id, event_type,
                lifecycle_role, position_key, ts_event, payload
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                str(uuid4()),
                event["event_id"],
                event.get("execution_job_id"),
                event["account_id"],
                order_projection_id,
                event.get("client_order_id"),
                event.get("venue_order_id"),
                event["event_type"],
                payload.get("lifecycle_role"),
                position_key,
                event["ts_event"],
                Json(payload),
            ),
        )


def _side(payload: dict[str, Any]) -> str | None:
    raw = str(payload.get("side") or payload.get("order_side") or "").lower()
    if raw in {"buy", "long"}:
        return "long"
    if raw in {"sell", "short"}:
        return "short"
    return None


def _explicit_target(payload: dict[str, Any]) -> str | None:
    target = payload.get("target_status")
    if target is None:
        return None
    target = str(target)
    if target in domain_descriptor("order")["states"]:
        return target
    return None
