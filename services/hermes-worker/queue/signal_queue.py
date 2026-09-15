"""Per-account durable signal dispatch queue (G2).

Hermes claim layer (this module + message_processing_runs):
  processing_run_id, attempt, claim_token, lease_expires_at, account_id

Stable source identity (G0):
  source_platform, channel_id, source_message_id, edit_version, account_id

This claim_token is NOT the node writer fence. Node single-writer remains
read_api._require_node_writer headers:
  x_redis_fencing_epoch / x_runtime_generation / x_lease_fencing_token
Do not reuse attempt as runtime_generation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json
from psycopg2.extensions import connection as PsycopgConnection

from claims import (
    PROCESSING_PURPOSE_SHADOW,
    QueueSchemaError,
    QueueStateError,
)


OPEN_SIGNAL_TTL_SECONDS = 30 * 60
DEFAULT_ACTION = "evaluate"
DEFAULT_ACCOUNT_ID = "unassigned"
SOURCE_PLATFORM = "telegram"

TERMINAL_STATUSES = ("shadow_dispatched", "expired", "failed", "skipped")

# Exact persistent claim fields for G3 operator-ingress validation.
# G3 must match the server's current claim + authenticated account; G2 does
# not submit operator/exchange orders.
CLAIM_CONTRACT = {
    "source_identity": (
        "source_platform",
        "channel_id",
        "source_message_id",
        "edit_version",
        "account_id",
    ),
    "claim_fields": (
        "processing_run_id",
        "attempt",
        "claim_token",
        "lease_expires_at",
        "account_id",
        "raw_message_id",
        "task_id",
    ),
    "node_writer_fence_headers": (
        "x_redis_fencing_epoch",
        "x_runtime_generation",
        "x_lease_fencing_token",
    ),
    "notes": (
        "claim_token is single-use per Hermes attempt",
        "attempt is not runtime_generation",
        "node writer fence stays on read_api._require_node_writer",
        "shadow dispatch does not submit live orders",
    ),
}

REQUIRED_COLUMNS = {
    "task_id",
    "identity_key",
    "source_platform",
    "channel_id",
    "source_message_id",
    "edit_version",
    "account_id",
    "action",
    "raw_message_id",
    "related_task_id",
    "current_processing_run_id",
    "status",
    "attempt",
    "claim_token",
    "worker_id",
    "lease_expires_at",
    "disposition",
    "disposition_reason",
    "shadow_result",
    "expires_at",
    "created_at",
    "updated_at",
}

_TASK_RETURNING = """
    task_id::text,
    raw_message_id::text,
    account_id,
    source_platform,
    channel_id,
    source_message_id,
    edit_version,
    action,
    status,
    attempt,
    claim_token::text,
    related_task_id::text,
    current_processing_run_id::text,
    expires_at,
    lease_expires_at,
    worker_id,
    disposition,
    disposition_reason
"""


class StaleClaimError(QueueStateError):
    """Result write did not match the current unexpired claim_token."""


@dataclass(frozen=True)
class SignalTask:
    task_id: str
    raw_message_id: str
    account_id: str
    source_platform: str
    channel_id: str
    source_message_id: str
    edit_version: str
    action: str
    status: str
    attempt: int
    claim_token: str | None
    related_task_id: str | None
    processing_run_id: str | None
    expires_at: Any
    lease_expires_at: Any
    worker_id: str | None
    disposition: str | None
    disposition_reason: str | None


@dataclass(frozen=True)
class EnqueueResult:
    task_id: str
    inserted: bool
    related_task_id: str | None
    identity_key: str
    status: str


@dataclass(frozen=True)
class ShadowDispatchResult:
    status: str
    task_id: str | None = None
    account_id: str | None = None
    raw_message_id: str | None = None
    related_task_id: str | None = None
    processing_run_id: str | None = None
    disposition_reason: str | None = None
    operator_submitted: bool = False
    exchange_submitted: bool = False
    claim_token: str | None = None


def identity_key(
    source_platform: str,
    channel_id: str,
    source_message_id: str,
    edit_version: str,
    account_id: str,
) -> str:
    return json.dumps(
        [
            source_platform,
            channel_id,
            source_message_id,
            edit_version,
            account_id,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def signal_expires_at(source_received_at: Any) -> datetime:
    dt = _as_utc(source_received_at)
    return dt + timedelta(seconds=OPEN_SIGNAL_TTL_SECONDS)


def enqueue_signal_task(
    conn: PsycopgConnection,
    *,
    raw_message_id: str,
    source_platform: str = SOURCE_PLATFORM,
    channel_id: str,
    source_message_id: str,
    edit_version: str,
    account_id: str | None = None,
    action: str | None = None,
    expires_at: Any = None,
    skipped_reason: str | None = None,
) -> EnqueueResult:
    """Insert-or-ignore one source identity. Caller owns the transaction."""

    resolved_account = (account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    resolved_action = (action or DEFAULT_ACTION).strip() or DEFAULT_ACTION
    key = identity_key(
        source_platform,
        channel_id,
        source_message_id,
        edit_version,
        resolved_account,
    )
    with conn.cursor() as cur:
        _assert_schema(cur)
        related_task_id = _lookup_related_task_id(
            cur,
            source_platform=source_platform,
            channel_id=channel_id,
            source_message_id=source_message_id,
            account_id=resolved_account,
            edit_version=edit_version,
        )
        status = "skipped" if skipped_reason else "pending"
        disposition = "skipped" if skipped_reason else "pending"
        disposition_reason = skipped_reason or "shadow_queued_legacy_still_live"
        task_id = str(uuid4())
        cur.execute(
            """
            INSERT INTO signal_dispatch_tasks (
                task_id,
                identity_key,
                source_platform,
                channel_id,
                source_message_id,
                edit_version,
                account_id,
                action,
                raw_message_id,
                related_task_id,
                status,
                attempt,
                disposition,
                disposition_reason,
                expires_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s
            )
            ON CONFLICT (source_platform, channel_id, source_message_id,
                         edit_version, account_id)
            DO NOTHING
            RETURNING task_id::text, related_task_id::text, status
            """,
            (
                task_id,
                key,
                source_platform,
                channel_id,
                source_message_id,
                edit_version,
                resolved_account,
                resolved_action,
                str(raw_message_id),
                related_task_id,
                status,
                disposition,
                disposition_reason,
                expires_at,
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            cur.execute(
                """
                SELECT task_id::text, related_task_id::text, status
                FROM signal_dispatch_tasks
                WHERE source_platform = %s
                  AND channel_id = %s
                  AND source_message_id = %s
                  AND edit_version = %s
                  AND account_id = %s
                """,
                (
                    source_platform,
                    channel_id,
                    source_message_id,
                    edit_version,
                    resolved_account,
                ),
            )
            existing = cur.fetchone()
            if existing is None:
                raise QueueStateError("signal identity conflict without existing task")
            return EnqueueResult(
                task_id=existing[0],
                inserted=False,
                related_task_id=existing[1],
                identity_key=key,
                status=existing[2],
            )
        _insert_audit(
            cur,
            event_type="signal.enqueued",
            raw_message_id=str(raw_message_id),
            actor="ingress",
            payload={
                "task_id": inserted[0],
                "identity_key": key,
                "account_id": resolved_account,
                "action": resolved_action,
                "edit_version": edit_version,
                "related_task_id": inserted[1],
                "status": inserted[2],
            },
        )
        return EnqueueResult(
            task_id=inserted[0],
            inserted=True,
            related_task_id=inserted[1],
            identity_key=key,
            status=inserted[2],
        )


def claim_signal_task(
    conn: PsycopgConnection,
    worker_id: str,
    *,
    lease_seconds: int = 30,
    account_id: str | None = None,
    open_ttl_seconds: int | None = None,
) -> SignalTask | None:
    """Claim one task from a free account.

    Per-account serialization uses pg_advisory_xact_lock + recheck in this
    short transaction. That is queue claim serialization, not the Nautilus
    node writer fence.
    """

    if not worker_id:
        raise ValueError("worker_id is required")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    with conn:
        with conn.cursor() as cur:
            _assert_schema(cur)
            skipped_accounts: set[str] = set()
            while True:
                candidate = _peek_claim_candidate(
                    cur,
                    account_id=account_id,
                    skip_accounts=skipped_accounts,
                )
                if candidate is None:
                    return None
                task = _task_from_row(candidate)
                _advisory_lock_account(cur, task.account_id)
                if _account_has_live_lease(cur, task.account_id):
                    skipped_accounts.add(task.account_id)
                    continue
                cur.execute(
                    f"""
                    SELECT {_TASK_RETURNING}
                    FROM signal_dispatch_tasks
                    WHERE task_id = %s
                    FOR UPDATE
                    """,
                    (task.task_id,),
                )
                locked = cur.fetchone()
                if locked is None:
                    skipped_accounts.add(task.account_id)
                    continue
                fresh = _task_from_row(locked)
                if not _row_is_claimable(fresh):
                    skipped_accounts.add(task.account_id)
                    continue
                if _task_ttl_expired(fresh, open_ttl_seconds=open_ttl_seconds):
                    _dispose_expired(cur, fresh, worker_id)
                    continue
                leased = _lease_task(cur, fresh, worker_id, lease_seconds)
                if leased is None:
                    skipped_accounts.add(task.account_id)
                    continue
                return leased


def complete_signal_task(
    conn: PsycopgConnection,
    task_id: str,
    claim_token: str,
    *,
    status: str,
    disposition: str | None = None,
    disposition_reason: str | None = None,
    shadow_result: dict[str, Any] | None = None,
) -> SignalTask:
    if not claim_token:
        raise ValueError("claim_token is required")
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"status {status!r} is not a terminal signal status")
    if status == "filled" or disposition == "filled":
        raise ValueError("G2 shadow must not mark filled; G3 owns venue fills")

    if status == "shadow_dispatched":
        run_status = "succeeded"
    elif status == "failed":
        run_status = "hermes_failed"
    else:
        run_status = "skipped"
    with conn:
        with conn.cursor() as cur:
            _assert_schema(cur)
            cur.execute(
                f"""
                UPDATE signal_dispatch_tasks
                SET status = %s,
                    disposition = %s,
                    disposition_reason = %s,
                    shadow_result = %s,
                    updated_at = now()
                WHERE task_id = %s
                  AND claim_token = %s
                  AND status = 'leased'
                  AND lease_expires_at > now()
                RETURNING {_TASK_RETURNING}
                """,
                (
                    status,
                    disposition,
                    disposition_reason,
                    Json(shadow_result or {}),
                    str(task_id),
                    str(claim_token),
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise StaleClaimError(
                    f"signal task {task_id} rejected stale or expired claim_token"
                )
            task = _task_from_row(row)
            if task.processing_run_id:
                cur.execute(
                    """
                    UPDATE message_processing_runs
                    SET status = %s,
                        finished_at = now()
                    WHERE processing_run_id = %s
                      AND claim_token = %s
                      AND status IN ('started', 'processing')
                      AND lease_expires_at > now()
                    """,
                    (run_status, task.processing_run_id, str(claim_token)),
                )
                if cur.rowcount != 1:
                    raise StaleClaimError(
                        f"processing run {task.processing_run_id} rejected stale claim"
                    )
            _insert_audit(
                cur,
                event_type=f"signal.{status}",
                raw_message_id=task.raw_message_id,
                actor=task.worker_id or "signal-worker",
                payload={
                    "task_id": task.task_id,
                    "claim_token": task.claim_token,
                    "processing_run_id": task.processing_run_id,
                    "attempt": task.attempt,
                    "account_id": task.account_id,
                    "disposition_reason": disposition_reason,
                    "shadow_result": shadow_result or {},
                    "outbox_written": False,
                    "operator_submitted": False,
                },
            )
            return task


def fail_signal_task(
    conn: PsycopgConnection,
    task_id: str,
    claim_token: str,
    *,
    disposition_reason: str,
    shadow_result: dict[str, Any] | None = None,
) -> SignalTask:
    """New-path failure: current claim_token and unexpired lease are required."""

    return complete_signal_task(
        conn,
        task_id,
        claim_token,
        status="failed",
        disposition="failed",
        disposition_reason=disposition_reason,
        shadow_result=shadow_result or {"operator_submitted": False, "outbox_published": False},
    )


def shadow_dispatch(
    conn: PsycopgConnection,
    task: SignalTask,
    *,
    semantic: dict[str, Any],
    stages: dict[str, Any] | None = None,
    open_ttl_seconds: int | None = None,
) -> ShadowDispatchResult:
    """Record a validated semantic shadow. Never submits or consumes outbox."""

    if not task.claim_token:
        raise StaleClaimError("shadow dispatch requires a claim_token")
    if not isinstance(semantic, dict) or not semantic.get("action"):
        raise ValueError("semantic shadow requires action")
    if _task_ttl_expired(task, open_ttl_seconds=open_ttl_seconds):
        complete_signal_task(
            conn,
            task.task_id,
            task.claim_token,
            status="expired",
            disposition="rejected",
            disposition_reason="signal_ttl_exceeded",
            shadow_result=_shadow_payload(expired=True, semantic=semantic, stages=stages),
        )
        return ShadowDispatchResult(
            status="expired",
            task_id=task.task_id,
            account_id=task.account_id,
            raw_message_id=task.raw_message_id,
            related_task_id=task.related_task_id,
            processing_run_id=task.processing_run_id,
            disposition_reason="signal_ttl_exceeded",
            operator_submitted=False,
            exchange_submitted=False,
            claim_token=task.claim_token,
        )
    complete_signal_task(
        conn,
        task.task_id,
        task.claim_token,
        status="shadow_dispatched",
        disposition="pending",
        disposition_reason="shadow_only_legacy_still_live",
        shadow_result=_shadow_payload(
            expired=False, semantic=semantic, stages=stages
        ),
    )
    return ShadowDispatchResult(
        status="shadow_dispatched",
        task_id=task.task_id,
        account_id=task.account_id,
        raw_message_id=task.raw_message_id,
        related_task_id=task.related_task_id,
        processing_run_id=task.processing_run_id,
        disposition_reason="shadow_only_legacy_still_live",
        operator_submitted=False,
        exchange_submitted=False,
        claim_token=task.claim_token,
    )


def _shadow_payload(
    *,
    expired: bool,
    semantic: dict[str, Any] | None = None,
    stages: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "mode": "shadow",
        "operator_submitted": False,
        "exchange_submitted": False,
        "live_order": False,
        "outbox_published": False,
        "legacy_outbox_consumed": False,
        "expired": expired,
    }
    if semantic is not None:
        payload["semantic"] = semantic
    if stages is not None:
        payload["stages"] = stages
    return payload


def _assert_schema(cur) -> None:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'signal_dispatch_tasks'
        """
    )
    columns = {row[0] for row in cur.fetchall()}
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise QueueSchemaError(
            "signal_dispatch_tasks is missing required columns: " + ", ".join(missing)
        )


def _lookup_related_task_id(
    cur,
    *,
    source_platform: str,
    channel_id: str,
    source_message_id: str,
    account_id: str,
    edit_version: str,
) -> str | None:
    cur.execute(
        """
        SELECT task_id::text
        FROM signal_dispatch_tasks
        WHERE source_platform = %s
          AND channel_id = %s
          AND source_message_id = %s
          AND account_id = %s
          AND edit_version <> %s
        ORDER BY created_at DESC, task_id DESC
        LIMIT 1
        """,
        (
            source_platform,
            channel_id,
            source_message_id,
            account_id,
            edit_version,
        ),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _advisory_lock_account(cur, account_id: str) -> None:
    digest = hashlib.sha256(
        f"g2-signal-account-queue:{account_id}".encode("utf-8")
    ).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def _account_has_live_lease(cur, account_id: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM signal_dispatch_tasks
        WHERE account_id = %s
          AND status = 'leased'
          AND lease_expires_at > now()
        LIMIT 1
        """,
        (account_id,),
    )
    return cur.fetchone() is not None


def _row_is_claimable(task: SignalTask) -> bool:
    if task.status == "pending":
        return True
    if task.status != "leased":
        return False
    if task.lease_expires_at is None:
        return True
    return _as_utc(task.lease_expires_at) <= datetime.now(timezone.utc)


def _peek_claim_candidate(cur, *, account_id: str | None, skip_accounts: set[str]):
    clauses = [
        "t.status IN ('pending', 'leased')",
        """(
            t.status = 'pending'
            OR t.lease_expires_at IS NULL
            OR t.lease_expires_at <= now()
        )""",
        """NOT EXISTS (
            SELECT 1
            FROM signal_dispatch_tasks busy
            WHERE busy.account_id = t.account_id
              AND busy.status = 'leased'
              AND busy.lease_expires_at > now()
        )""",
    ]
    params: list[Any] = []
    if account_id:
        clauses.append("t.account_id = %s")
        params.append(account_id)
    if skip_accounts:
        clauses.append("NOT (t.account_id = ANY(%s))")
        params.append(list(skip_accounts))
    cur.execute(
        f"""
        SELECT {_TASK_RETURNING}
        FROM signal_dispatch_tasks t
        WHERE {' AND '.join(clauses)}
        ORDER BY t.created_at ASC, t.task_id ASC
        LIMIT 1
        """,
        params,
    )
    return cur.fetchone()


def _lease_task(
    cur, task: SignalTask, worker_id: str, lease_seconds: int
) -> SignalTask | None:
    if task.processing_run_id:
        cur.execute(
            """
            UPDATE message_processing_runs
            SET status = 'hermes_timeout',
                finished_at = now(),
                error = COALESCE(error, 'lease expired before completion')
            WHERE processing_run_id = %s
              AND status IN ('started', 'processing')
              AND lease_expires_at <= now()
            """,
            (task.processing_run_id,),
        )
    cur.execute(
        """
        SELECT COALESCE(MAX(attempt), 0)
        FROM message_processing_runs
        WHERE raw_message_id = %s
          AND COALESCE(processing_purpose, 'legacy') = %s
        """,
        (task.raw_message_id, PROCESSING_PURPOSE_SHADOW),
    )
    next_attempt = int(cur.fetchone()[0]) + 1
    claim_token = str(uuid4())
    processing_run_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO message_processing_runs (
            processing_run_id,
            raw_message_id,
            worker_id,
            status,
            lease_expires_at,
            attempt,
            claim_token,
            account_id,
            processing_purpose
        )
        VALUES (
            %s, %s, %s, 'started',
            now() + (%s * interval '1 second'),
            %s, %s, %s, %s
        )
        """,
        (
            processing_run_id,
            task.raw_message_id,
            worker_id,
            lease_seconds,
            next_attempt,
            claim_token,
            task.account_id,
            PROCESSING_PURPOSE_SHADOW,
        ),
    )
    cur.execute(
        f"""
        UPDATE signal_dispatch_tasks
        SET status = 'leased',
            worker_id = %s,
            claim_token = %s,
            attempt = %s,
            current_processing_run_id = %s,
            lease_expires_at = now() + (%s * interval '1 second'),
            updated_at = now()
        WHERE task_id = %s
          AND status IN ('pending', 'leased')
          AND (
                status = 'pending'
                OR lease_expires_at IS NULL
                OR lease_expires_at <= now()
              )
        RETURNING {_TASK_RETURNING}
        """,
        (
            worker_id,
            claim_token,
            next_attempt,
            processing_run_id,
            lease_seconds,
            task.task_id,
        ),
    )
    row = cur.fetchone()
    if row is None:
        raise QueueStateError(
            f"signal task {task.task_id} disappeared during claim"
        )
    leased = _task_from_row(row)
    _insert_audit(
        cur,
        event_type="signal.claimed",
        raw_message_id=leased.raw_message_id,
        actor=worker_id,
        payload={
            "task_id": leased.task_id,
            "processing_run_id": leased.processing_run_id,
            "attempt": leased.attempt,
            "claim_token": leased.claim_token,
            "account_id": leased.account_id,
            "lease_seconds": lease_seconds,
        },
    )
    return leased


def _dispose_expired(cur, task: SignalTask, worker_id: str) -> None:
    if task.processing_run_id:
        cur.execute(
            """
            UPDATE message_processing_runs
            SET status = 'skipped',
                finished_at = now(),
                error = COALESCE(error, 'signal_ttl_exceeded')
            WHERE processing_run_id = %s
              AND status IN ('started', 'processing')
            """,
            (task.processing_run_id,),
        )
    cur.execute(
        f"""
        UPDATE signal_dispatch_tasks
        SET status = 'expired',
            disposition = 'expired',
            disposition_reason = 'signal_ttl_exceeded',
            worker_id = %s,
            updated_at = now(),
            shadow_result = %s
        WHERE task_id = %s
          AND status IN ('pending', 'leased')
        RETURNING {_TASK_RETURNING}
        """,
        (worker_id, Json(_shadow_payload(expired=True)), task.task_id),
    )
    row = cur.fetchone()
    if row is None:
        return
    disposed = _task_from_row(row)
    _insert_audit(
        cur,
        event_type="signal.expired",
        raw_message_id=disposed.raw_message_id,
        actor=worker_id,
        payload={
            "task_id": disposed.task_id,
            "disposition_reason": "signal_ttl_exceeded",
            "account_id": disposed.account_id,
            "processing_run_id": disposed.processing_run_id,
        },
    )


def _task_ttl_expired(
    task: SignalTask,
    *,
    open_ttl_seconds: int | None = None,
    now: Any = None,
) -> bool:
    now_dt = datetime.now(timezone.utc) if now is None else _as_utc(now)
    if task.expires_at is None:
        return bool(open_ttl_seconds is not None and open_ttl_seconds <= 0)
    expires = _as_utc(task.expires_at)
    if open_ttl_seconds is None:
        return expires <= now_dt
    source_time = expires - timedelta(seconds=OPEN_SIGNAL_TTL_SECONDS)
    return now_dt >= source_time + timedelta(seconds=open_ttl_seconds)


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _task_from_row(row) -> SignalTask:
    return SignalTask(
        task_id=row[0],
        raw_message_id=row[1],
        account_id=row[2],
        source_platform=row[3],
        channel_id=row[4],
        source_message_id=row[5],
        edit_version=row[6],
        action=row[7],
        status=row[8],
        attempt=int(row[9] or 0),
        claim_token=row[10],
        related_task_id=row[11],
        processing_run_id=row[12],
        expires_at=row[13],
        lease_expires_at=row[14],
        worker_id=row[15],
        disposition=row[16],
        disposition_reason=row[17],
    )


def _insert_audit(
    cur,
    *,
    event_type: str,
    raw_message_id: str | None,
    actor: str,
    payload: dict[str, Any],
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
            "signal_dispatch_task",
            payload.get("task_id") or raw_message_id,
            actor,
            raw_message_id,
            Json(payload),
        ),
    )
