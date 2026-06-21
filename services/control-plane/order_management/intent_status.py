from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json

from db.connection import transaction


@dataclass(frozen=True)
class IntentStatusUpdate:
    intent_status: str
    job_status: str
    reason: str


def classify_intent_outcome(event: dict[str, Any]) -> IntentStatusUpdate:
    event_type = str(event.get("event_type") or "")
    payload = dict(event.get("payload") or {})
    reason = str(payload.get("reason") or payload.get("error") or "")
    if event_type in {"OrderDenied", "IntentDenied"}:
        return IntentStatusUpdate("denied", "denied", reason or "denied")
    if event_type in {"OrderRejected", "IntentRejected"}:
        return IntentStatusUpdate("rejected", "rejected", reason or "rejected")
    if event_type in {"OrderSubmitFailed", "ExecutionFailed"}:
        return IntentStatusUpdate("failed", "failed", reason or "failed")
    if event_type in {"OrderFilled", "IntentCompleted"}:
        return IntentStatusUpdate("completed", "completed", "filled")
    if event_type == "NodeAcceptedIntent":
        return IntentStatusUpdate("node_accepted", "claimed", "node_accepted")
    if event_type in {"OrderWorking", "OrderAccepted"}:
        return IntentStatusUpdate("executing", "executing", "executing")
    return IntentStatusUpdate("needs_review", "needs_review", reason or "unmapped_event")


def build_timeline_item(
    *,
    intent_id: str,
    execution_job_id: str | None,
    status_update: IntentStatusUpdate,
    event_id: str | None,
    event_type: str,
    observed_at: datetime,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observed_at = _aware(observed_at)
    return {
        "intent_id": intent_id,
        "execution_job_id": execution_job_id,
        "intent_status": status_update.intent_status,
        "job_status": status_update.job_status,
        "reason": status_update.reason,
        "event_id": event_id,
        "event_type": event_type,
        "observed_at": observed_at.isoformat(),
        "payload": payload or {},
    }


def persist_intent_status(
    conn,
    *,
    intent_id: str,
    execution_job_id: str | None,
    event: dict[str, Any],
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _aware(observed_at or datetime.now(timezone.utc))
    update = classify_intent_outcome(event)
    timeline_item = build_timeline_item(
        intent_id=intent_id,
        execution_job_id=execution_job_id,
        status_update=update,
        event_id=event.get("event_id"),
        event_type=str(event.get("event_type")),
        observed_at=observed_at,
        payload=dict(event.get("payload") or {}),
    )
    with transaction(conn):
        intent_status = _db_intent_status(conn, update.intent_status)
        if intent_status is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE trade_intents
                    SET status=%s, updated_at=now()
                    WHERE intent_id=%s
                    """,
                    (intent_status, intent_id),
                )
        if execution_job_id is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE execution_jobs
                    SET status=%s,
                        completed_at=CASE
                            WHEN %s IN ('completed','denied','rejected','failed','cancelled','expired')
                            THEN COALESCE(completed_at, %s)
                            ELSE completed_at
                        END,
                        payload=payload || jsonb_build_object('intent_timeline_last', %s::jsonb),
                        updated_at=now()
                    WHERE execution_job_id=%s
                    """,
                    (
                        update.job_status,
                        update.job_status,
                        observed_at,
                        Json(timeline_item),
                        execution_job_id,
                    ),
                )
        _insert_audit_timeline(conn, timeline_item)
    return timeline_item


def _db_intent_status(conn, desired: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT enumlabel
            FROM pg_enum
            JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
            WHERE pg_type.typname='trade_intent_status'
            """
        )
        values = {row[0] for row in cur.fetchall()}
    if desired in values:
        return desired
    fallback = {
        "denied": "rejected",
        "failed": "rejected",
        "needs_review": "approved",
        "node_accepted": "approved",
        "executing": "approved",
        "completed": None,
    }.get(desired, desired)
    return fallback if fallback in values else None


def _insert_audit_timeline(conn, item: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_events (
                audit_event_id, event_type, aggregate_type, aggregate_id,
                intent_id, payload, created_at
            )
            VALUES (%s, 'intent.timeline', 'trade_intent', %s, %s, %s, now())
            """,
            (str(uuid4()), item["intent_id"], item["intent_id"], Json(item)),
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
