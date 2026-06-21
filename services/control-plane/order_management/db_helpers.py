from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def ensure_aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ensure_execution_event(conn, event: dict[str, Any]) -> None:
    payload = dict(event.get("payload") or {})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO execution_events (
                execution_event_row_id, event_id, schema_version, node_id, account_id,
                intent_id, client_order_id, venue_order_id, trade_id, event_type,
                ts_event, ts_ingest, payload
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                str(uuid4()),
                event["event_id"],
                event.get("schema_version", "1.0"),
                event.get("node_id", "unknown"),
                event["account_id"],
                event.get("intent_id"),
                event.get("client_order_id"),
                event.get("venue_order_id"),
                event.get("trade_id"),
                event["event_type"],
                ensure_aware(event.get("ts_event")),
                ensure_aware(event.get("ts_ingest")),
                Json(payload),
            ),
        )


def create_reconciliation_run(
    conn,
    *,
    account_id: str | None,
    status: str,
    reason: str,
    payload: dict[str, Any] | None = None,
    completed: bool = True,
) -> str:
    run_id = str(uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reconciliation_runs (
                reconciliation_run_id, account_id, status, reason,
                completed_at, payload
            )
            VALUES (%s,%s,%s,%s,CASE WHEN %s THEN now() ELSE NULL END,%s)
            """,
            (
                run_id,
                account_id,
                status,
                reason,
                completed,
                Json(payload or {}),
            ),
        )
    return run_id


def update_reconciliation_run(
    conn,
    run_id: str,
    *,
    status: str,
    payload: dict[str, Any] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reconciliation_runs
            SET status=%s, completed_at=now(), payload=payload || %s::jsonb
            WHERE reconciliation_run_id=%s
            """,
            (status, Json(payload or {}), run_id),
        )


def record_reconciliation_finding(
    conn,
    *,
    account_id: str,
    finding_type: str,
    severity: str,
    payload: dict[str, Any] | None = None,
    position_key: str | None = None,
    order_projection_id: str | None = None,
    reconciliation_run_id: str | None = None,
    run_reason: str = "projection_anomaly",
) -> dict[str, Any]:
    run_id = reconciliation_run_id or create_reconciliation_run(
        conn,
        account_id=account_id,
        status="failed",
        reason=run_reason,
        payload={"source": finding_type},
    )
    finding_id = str(uuid4())
    finding_payload = dict(payload or {})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reconciliation_findings (
                reconciliation_finding_id, reconciliation_run_id, account_id,
                finding_type, severity, status, position_key, order_projection_id, payload
            )
            VALUES (%s,%s,%s,%s,%s,'open',%s,%s,%s)
            """,
            (
                finding_id,
                run_id,
                account_id,
                finding_type,
                severity,
                position_key,
                order_projection_id,
                Json(finding_payload),
            ),
        )
    return {
        "reconciliation_finding_id": finding_id,
        "reconciliation_run_id": run_id,
        "account_id": account_id,
        "finding_type": finding_type,
        "severity": severity,
        "position_key": position_key,
        "order_projection_id": order_projection_id,
        "payload": finding_payload,
    }
