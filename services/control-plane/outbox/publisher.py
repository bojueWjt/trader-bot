from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg2.extras import Json
from psycopg2.extensions import connection as PsycopgConnection


class OutboxSink(Protocol):
    """Idempotent sink contract for transactional outbox delivery.

    The publisher may call `deliver` more than once for the same event when a
    process crashes after the external delivery but before the database update
    commits. Implementations must deduplicate by `event.idempotency_key`, which
    is always the outbox event id.
    """

    def deliver(self, event: "OutboxEvent") -> None:
        ...


@dataclass(frozen=True)
class OutboxEvent:
    outbox_event_id: str
    status: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    trace_id: str | None
    attempts: int
    created_at: Any

    @property
    def idempotency_key(self) -> str:
        return self.outbox_event_id


class OutboxStateError(RuntimeError):
    pass


class OutboxPublisher:
    """Publishes pending outbox rows with at-least-once delivery semantics."""

    def __init__(self, sink: OutboxSink, *, batch_size: int = 100):
        if not callable(getattr(sink, "deliver", None)):
            raise TypeError("sink must expose a callable deliver(event) method")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._sink = sink
        self._batch_size = batch_size

    def publish_pending_once(
        self, conn: PsycopgConnection, *, batch_size: int | None = None
    ) -> int:
        """Attempt pending rows ordered by creation time and return publish count.

        This is deliberately a single polling pass, not a daemon loop. The
        caller owns scheduling and backoff.
        """

        limit = batch_size or self._batch_size
        if limit <= 0:
            raise ValueError("batch_size must be positive")

        published = 0
        for _ in range(limit):
            result = self._publish_next_pending(conn)
            if result == "empty":
                break
            if result == "published":
                published += 1
        return published

    def publish_event(self, conn: PsycopgConnection, event_id: UUID | str) -> str:
        """Publish one event by id.

        Returns `published`, `failed`, or `already_published`. A published row
        is skipped without calling the sink so duplicate invocations are safe.
        """

        with conn:
            with conn.cursor() as cur:
                event = self._select_event_for_update(cur, event_id)
                if event.status == "published":
                    return "already_published"
                if event.status != "pending":
                    raise OutboxStateError(
                        f"outbox event {event.outbox_event_id} is {event.status}, not pending"
                    )
                return self._deliver_locked_event(cur, event)

    def _publish_next_pending(self, conn: PsycopgConnection) -> str:
        with conn:
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
                        trace_id::text,
                        attempts,
                        created_at
                    FROM outbox_events
                    WHERE status = 'pending'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                    ORDER BY created_at ASC, outbox_event_id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cur.fetchone()
                if row is None:
                    return "empty"
                return self._deliver_locked_event(cur, _event_from_row(row))

    def _select_event_for_update(self, cur, event_id: UUID | str) -> OutboxEvent:
        cur.execute(
            """
            SELECT
                outbox_event_id::text,
                status::text,
                aggregate_type,
                aggregate_id,
                event_type,
                payload,
                trace_id::text,
                attempts,
                created_at
            FROM outbox_events
            WHERE outbox_event_id = %s
            FOR UPDATE
            """,
            (str(event_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise OutboxStateError(f"outbox event {event_id} does not exist")
        return _event_from_row(row)

    def _deliver_locked_event(self, cur, event: OutboxEvent) -> str:
        try:
            self._sink.deliver(event)
        except Exception as exc:
            error = _format_error(exc)
            cur.execute(
                """
                UPDATE outbox_events
                SET status = 'failed',
                    failed_at = now(),
                    attempts = attempts + 1,
                    error = %s
                WHERE outbox_event_id = %s
                  AND status = 'pending'
                """,
                (error, event.outbox_event_id),
            )
            if cur.rowcount != 1:
                raise OutboxStateError(
                    f"failed to mark outbox event {event.outbox_event_id} as failed"
                ) from exc
            _insert_outbox_audit(
                cur,
                event,
                "outbox.failed",
                {"error": error, "attempt": event.attempts + 1},
            )
            return "failed"

        cur.execute(
            """
            UPDATE outbox_events
            SET status = 'published',
                published_at = now(),
                failed_at = NULL,
                attempts = attempts + 1,
                error = NULL
            WHERE outbox_event_id = %s
              AND status = 'pending'
            """,
            (event.outbox_event_id,),
        )
        if cur.rowcount != 1:
            raise OutboxStateError(
                f"failed to mark outbox event {event.outbox_event_id} as published"
            )
        _insert_outbox_audit(
            cur,
            event,
            "outbox.published",
            {"attempt": event.attempts + 1},
        )
        return "published"


def _event_from_row(row) -> OutboxEvent:
    return OutboxEvent(
        outbox_event_id=row[0],
        status=row[1],
        aggregate_type=row[2],
        aggregate_id=row[3],
        event_type=row[4],
        payload=row[5],
        trace_id=row[6],
        attempts=row[7],
        created_at=row[8],
    )


def _insert_outbox_audit(
    cur, event: OutboxEvent, event_type: str, payload: dict[str, Any]
) -> None:
    raw_message_id = None
    if event.aggregate_type == "raw_message":
        try:
            raw_message_id = str(UUID(event.aggregate_id))
        except ValueError:
            raw_message_id = None

    cur.execute(
        """
        INSERT INTO audit_events (
            audit_event_id,
            event_type,
            aggregate_type,
            aggregate_id,
            actor,
            trace_id,
            raw_message_id,
            payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            event_type,
            event.aggregate_type,
            event.aggregate_id,
            "outbox_publisher",
            event.trace_id,
            raw_message_id,
            Json(
                {
                    "outbox_event_id": event.outbox_event_id,
                    "outbox_event_type": event.event_type,
                    **payload,
                }
            ),
        ),
    )


def _format_error(exc: Exception) -> str:
    text = f"{exc.__class__.__name__}: {exc}"
    return text[:2000]
