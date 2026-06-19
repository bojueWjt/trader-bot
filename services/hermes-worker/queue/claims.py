from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from psycopg2.extras import Json
from psycopg2.extensions import connection as PsycopgConnection


ACTIVE_STATUSES = ("started", "processing")
REQUIRED_STATUSES = (
    "started",
    "processing",
    "succeeded",
    "hermes_timeout",
    "hermes_failed",
    "outbox_failed",
)
REQUIRED_COLUMNS = {
    "processing_run_id",
    "raw_message_id",
    "worker_id",
    "status",
    "started_at",
    "finished_at",
    "error",
    "created_at",
    "lease_expires_at",
}


@dataclass(frozen=True)
class ProcessingRun:
    processing_run_id: str
    raw_message_id: str
    worker_id: str | None
    status: str
    lease_expires_at: Any
    started_at: Any
    finished_at: Any
    error: str | None


class QueueSchemaError(RuntimeError):
    pass


class QueueStateError(RuntimeError):
    pass


def claim(
    conn: PsycopgConnection, worker_id: str, *, lease_seconds: int = 30
) -> ProcessingRun | None:
    """Claim one pending raw-message outbox item with a visibility lease."""

    if not worker_id:
        raise ValueError("worker_id is required")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    with conn:
        with conn.cursor() as cur:
            _assert_schema_compatible(cur)
            _expire_timed_out_runs(cur)

            candidate = _select_claim_candidate(cur)
            if candidate is None:
                return None

            outbox_event_id, aggregate_id, raw_message_id = candidate
            if raw_message_id is None:
                raise QueueStateError(
                    f"pending outbox event {outbox_event_id} references missing raw message {aggregate_id}"
                )

            _assert_no_active_run(cur, raw_message_id)

            processing_run_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO message_processing_runs (
                    processing_run_id,
                    raw_message_id,
                    worker_id,
                    status,
                    lease_expires_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    'started',
                    now() + (%s * interval '1 second')
                )
                RETURNING
                    processing_run_id::text,
                    raw_message_id::text,
                    worker_id,
                    status,
                    lease_expires_at,
                    started_at,
                    finished_at,
                    error
                """,
                (processing_run_id, raw_message_id, worker_id, lease_seconds),
            )
            run = _run_from_row(cur.fetchone())
            _insert_run_audit(
                cur,
                run,
                "message_processing.started",
                {
                    "outbox_event_id": outbox_event_id,
                    "lease_seconds": lease_seconds,
                },
            )
            return run


def mark_processing(
    conn: PsycopgConnection,
    processing_run_id: UUID | str,
    *,
    lease_seconds: int | None = None,
) -> ProcessingRun:
    if lease_seconds is not None and lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    with conn:
        with conn.cursor() as cur:
            _assert_schema_compatible(cur)
            if lease_seconds is None:
                cur.execute(
                    """
                    UPDATE message_processing_runs
                    SET status = 'processing'
                    WHERE processing_run_id = %s
                      AND status = 'started'
                      AND lease_expires_at > now()
                    RETURNING
                        processing_run_id::text,
                        raw_message_id::text,
                        worker_id,
                        status,
                        lease_expires_at,
                        started_at,
                        finished_at,
                        error
                    """,
                    (str(processing_run_id),),
                )
            else:
                cur.execute(
                    """
                    UPDATE message_processing_runs
                    SET status = 'processing',
                        lease_expires_at = now() + (%s * interval '1 second')
                    WHERE processing_run_id = %s
                      AND status = 'started'
                      AND lease_expires_at > now()
                    RETURNING
                        processing_run_id::text,
                        raw_message_id::text,
                        worker_id,
                        status,
                        lease_expires_at,
                        started_at,
                        finished_at,
                        error
                    """,
                    (lease_seconds, str(processing_run_id)),
                )
            row = cur.fetchone()
            if row is None:
                raise QueueStateError(
                    f"processing run {processing_run_id} is not an active started run"
                )
            run = _run_from_row(row)
            _insert_run_audit(cur, run, "message_processing.processing", {})
            return run


def complete_run(conn: PsycopgConnection, processing_run_id: UUID | str) -> ProcessingRun:
    return _finish_run(conn, processing_run_id, "succeeded", None)


def fail_run(
    conn: PsycopgConnection, processing_run_id: UUID | str, error: str
) -> ProcessingRun:
    if not error:
        raise ValueError("error is required")
    return _finish_run(conn, processing_run_id, "hermes_failed", error)


def mark_outbox_failed(
    conn: PsycopgConnection, raw_message_id: UUID | str, error: str
) -> ProcessingRun:
    if not error:
        raise ValueError("error is required")
    with conn:
        with conn.cursor() as cur:
            _assert_schema_compatible(cur)
            cur.execute(
                """
                UPDATE message_processing_runs
                SET status = 'outbox_failed',
                    finished_at = now(),
                    error = %s
                WHERE raw_message_id = %s
                  AND status IN ('started', 'processing')
                RETURNING
                    processing_run_id::text,
                    raw_message_id::text,
                    worker_id,
                    status,
                    lease_expires_at,
                    started_at,
                    finished_at,
                    error
                """,
                (_format_error(error), str(raw_message_id)),
            )
            row = cur.fetchone()
            if row is None:
                processing_run_id = str(uuid4())
                cur.execute(
                    """
                    INSERT INTO message_processing_runs (
                        processing_run_id,
                        raw_message_id,
                        worker_id,
                        status,
                        lease_expires_at,
                        finished_at,
                        error
                    )
                    VALUES (%s, %s, %s, 'outbox_failed', now(), now(), %s)
                    RETURNING
                        processing_run_id::text,
                        raw_message_id::text,
                        worker_id,
                        status,
                        lease_expires_at,
                        started_at,
                        finished_at,
                        error
                    """,
                    (
                        processing_run_id,
                        str(raw_message_id),
                        "outbox_publisher",
                        _format_error(error),
                    ),
                )
                row = cur.fetchone()
            run = _run_from_row(row)
            _insert_run_audit(cur, run, "message_processing.outbox_failed", {})
            return run


def _finish_run(
    conn: PsycopgConnection,
    processing_run_id: UUID | str,
    status: str,
    error: str | None,
) -> ProcessingRun:
    with conn:
        with conn.cursor() as cur:
            _assert_schema_compatible(cur)
            cur.execute(
                """
                UPDATE message_processing_runs
                SET status = %s,
                    finished_at = now(),
                    error = %s
                WHERE processing_run_id = %s
                  AND status IN ('started', 'processing')
                  AND lease_expires_at > now()
                RETURNING
                    processing_run_id::text,
                    raw_message_id::text,
                    worker_id,
                    status,
                    lease_expires_at,
                    started_at,
                    finished_at,
                    error
                """,
                (status, _format_error(error) if error is not None else None, str(processing_run_id)),
            )
            row = cur.fetchone()
            if row is None:
                raise QueueStateError(
                    f"processing run {processing_run_id} is not active or its lease expired"
                )
            run = _run_from_row(row)
            _insert_run_audit(cur, run, f"message_processing.{status}", {})
            return run


def _assert_schema_compatible(cur) -> None:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'message_processing_runs'
        """
    )
    columns = {row[0] for row in cur.fetchall()}
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise QueueSchemaError(
            "message_processing_runs is missing required queue columns: "
            + ", ".join(missing)
        )

    cur.execute(
        """
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'message_processing_runs'::regclass
          AND conname = 'ck_message_processing_runs_status'
        """
    )
    row = cur.fetchone()
    if row is None:
        raise QueueSchemaError(
            "message_processing_runs is missing ck_message_processing_runs_status"
        )
    constraint = row[0]
    missing_statuses = [status for status in REQUIRED_STATUSES if status not in constraint]
    if missing_statuses:
        raise QueueSchemaError(
            "message_processing_runs status constraint is missing: "
            + ", ".join(missing_statuses)
        )


def _expire_timed_out_runs(cur) -> None:
    cur.execute(
        """
        UPDATE message_processing_runs
        SET status = 'hermes_timeout',
            finished_at = now(),
            error = COALESCE(error, 'lease expired before completion')
        WHERE status IN ('started', 'processing')
          AND lease_expires_at <= now()
        RETURNING
            processing_run_id::text,
            raw_message_id::text,
            worker_id,
            status,
            lease_expires_at,
            started_at,
            finished_at,
            error
        """
    )
    for row in cur.fetchall():
        run = _run_from_row(row)
        _insert_run_audit(cur, run, "message_processing.hermes_timeout", {})


def _select_claim_candidate(cur):
    cur.execute(
        """
        SELECT
            ob.outbox_event_id::text,
            ob.aggregate_id,
            rm.id::text
        FROM outbox_events ob
        LEFT JOIN raw_messages rm
          ON rm.id::text = ob.aggregate_id
        WHERE ob.status = 'pending'
          AND ob.aggregate_type = 'raw_message'
          AND NOT EXISTS (
              SELECT 1
              FROM message_processing_runs active
              WHERE active.raw_message_id::text = ob.aggregate_id
                AND active.status IN ('started', 'processing')
          )
        ORDER BY ob.created_at ASC, ob.outbox_event_id ASC
        LIMIT 1
        FOR UPDATE OF ob SKIP LOCKED
        """
    )
    return cur.fetchone()


def _assert_no_active_run(cur, raw_message_id: str) -> None:
    cur.execute(
        """
        SELECT processing_run_id::text
        FROM message_processing_runs
        WHERE raw_message_id = %s
          AND status IN ('started', 'processing')
        FOR UPDATE
        """,
        (raw_message_id,),
    )
    row = cur.fetchone()
    if row is not None:
        raise QueueStateError(
            f"raw message {raw_message_id} already has active processing run {row[0]}"
        )


def _insert_run_audit(
    cur, run: ProcessingRun, event_type: str, payload: dict[str, Any]
) -> None:
    cur.execute(
        """
        INSERT INTO audit_events (
            audit_event_id,
            event_type,
            aggregate_type,
            aggregate_id,
            actor,
            raw_message_id,
            payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            event_type,
            "raw_message",
            run.raw_message_id,
            run.worker_id,
            run.raw_message_id,
            Json(
                {
                    "processing_run_id": run.processing_run_id,
                    "status": run.status,
                    **payload,
                }
            ),
        ),
    )


def _run_from_row(row) -> ProcessingRun:
    return ProcessingRun(
        processing_run_id=row[0],
        raw_message_id=row[1],
        worker_id=row[2],
        status=row[3],
        lease_expires_at=row[4],
        started_at=row[5],
        finished_at=row[6],
        error=row[7],
    )


def _format_error(error: str) -> str:
    return str(error)[:2000]
