"""Order-management transactional outbox helpers."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg2.extras import Json

_REPO = Path(__file__).resolve().parents[3]
_EXECUTION_DOMAIN = _REPO / "packages" / "execution-domain"
if str(_EXECUTION_DOMAIN) not in sys.path:
    sys.path.insert(0, str(_EXECUTION_DOMAIN))

from execution_domain.idempotency import RequestId  # noqa: E402

__all__ = [
    "deterministic_outbox_event_id",
    "enqueue_order_management_event",
    "fetch_command_result_by_request_id",
]

TERMINAL_COMMAND_EVENTS = (
    "command.completed",
    "command.partial",
    "command.failed",
    "command.timed_out",
)


def deterministic_outbox_event_id(
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    idempotency_key: str,
) -> UUID:
    material = "|".join([aggregate_type, aggregate_id, event_type, idempotency_key])
    return UUID(hex=hashlib.sha256(material.encode("utf-8")).hexdigest()[:32])


def enqueue_order_management_event(
    conn,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> str:
    _validate_event_type(event_type)
    event_id = deterministic_outbox_event_id(
        aggregate_type,
        aggregate_id,
        event_type,
        idempotency_key,
    )
    event_payload = dict(payload or {})
    if request_id is not None:
        event_payload.setdefault("request_id", str(RequestId.from_value(request_id)))

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO outbox_events (
                outbox_event_id,
                status,
                aggregate_type,
                aggregate_id,
                event_type,
                payload
            )
            VALUES (%s, 'pending', %s, %s, %s, %s)
            ON CONFLICT (outbox_event_id) DO NOTHING
            RETURNING outbox_event_id::text
            """,
            (
                str(event_id),
                aggregate_type,
                aggregate_id,
                event_type,
                Json(event_payload),
            ),
        )
        row = cur.fetchone()
    return row[0] if row else str(event_id)


def fetch_command_result_by_request_id(conn, request_id: str) -> dict[str, Any] | None:
    request_id_value = str(RequestId.from_value(request_id))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                outbox_event_id::text,
                status::text,
                aggregate_type,
                aggregate_id,
                event_type,
                payload,
                error
            FROM outbox_events
            WHERE payload->>'request_id' = %s
              AND event_type IN (
                  'command.completed',
                  'command.partial',
                  'command.failed',
                  'command.timed_out'
              )
            ORDER BY created_at DESC, outbox_event_id DESC
            LIMIT 1
            """,
            (request_id_value,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "outbox_event_id": row[0],
        "status": row[1],
        "aggregate_type": row[2],
        "aggregate_id": row[3],
        "event_type": row[4],
        "payload": row[5],
        "error": row[6],
    }


def _validate_event_type(event_type: str) -> None:
    if event_type == "settings.changed":
        return
    if event_type.startswith("command.") or event_type.startswith("order."):
        return
    raise ValueError("order-management outbox event_type must be settings.changed, command.*, or order.*")
