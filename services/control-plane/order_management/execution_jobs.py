from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg2.extras import Json, RealDictCursor

from db.connection import transaction

from .metrics import inject_trace_context
from .outbox import enqueue_order_management_event

_REPO = Path(__file__).resolve().parents[3]
_EXECUTION_DOMAIN = _REPO / "packages" / "execution-domain"
if str(_EXECUTION_DOMAIN) not in sys.path:
    sys.path.insert(0, str(_EXECUTION_DOMAIN))

from execution_domain.idempotency import RequestId, execution_job_key  # noqa: E402


PENDING_STATUSES = ("pending", "retry_scheduled")
TERMINAL_STATUSES = frozenset(
    {"completed", "denied", "rejected", "failed", "cancelled", "expired"}
)


@dataclass(frozen=True)
class ExecutionJobRequest:
    execution_job_id: str
    intent_id: str
    account_id: str
    instrument_id: str
    venue_symbol: str
    action: str
    status: str
    request_id: str
    idempotency_key: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ClaimedExecutionJob:
    execution_job_id: str
    intent_id: str | None
    account_id: str
    instrument_id: str
    venue_symbol: str
    action: str
    status: str
    request_id: str | None
    idempotency_key: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RetryPayloadResult:
    status: str
    payload: dict[str, Any]


def deterministic_execution_job_id(idempotency_key: str) -> str:
    return str(UUID(hex=hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:32]))


def build_execution_job_request(
    intent_row: dict[str, Any],
    *,
    now: datetime | None = None,
    request_id: str | None = None,
) -> ExecutionJobRequest:
    now = _aware(now or datetime.now(timezone.utc))
    intent_id = str(intent_row["intent_id"])
    account_id = str(intent_row["account_id"])
    instrument_id = str(intent_row["instrument_id"])
    action = str(intent_row["action"])
    idempotency_key = execution_job_key(intent_id, action, instrument_id, account_id)
    job_id = deterministic_execution_job_id(idempotency_key)
    request_id = request_id or str(RequestId.for_material("execution_job", intent_id, idempotency_key))
    risk_budget = dict(intent_row.get("risk_budget") or {})
    order_plan = dict(intent_row.get("order_plan") or {})
    reservation = _reservation_payload(risk_budget, order_plan)
    payload = {
        "schema_version": "1.0",
        "created_by": "order_management.execution_jobs",
        "created_at": now.isoformat(),
        "attempts": 0,
        "intent": {
            "intent_id": intent_id,
            "account_id": account_id,
            "instrument_id": instrument_id,
            "action": action,
            "idempotency_key": str(intent_row["idempotency_key"]),
            "valid_until": _iso_or_none(intent_row.get("valid_until")),
            "order_plan": order_plan,
            "risk_budget": risk_budget,
        },
        "reservation": reservation,
    }
    payload = inject_trace_context(
        payload,
        request_id=request_id,
        idempotency_key=idempotency_key,
        intent_id=intent_id,
    )
    payload["intent"] = inject_trace_context(
        payload["intent"],
        trace_id=payload["trace_id"],
        request_id=request_id,
        idempotency_key=idempotency_key,
        intent_id=intent_id,
    )
    return ExecutionJobRequest(
        execution_job_id=job_id,
        intent_id=intent_id,
        account_id=account_id,
        instrument_id=instrument_id,
        venue_symbol=_venue_symbol(instrument_id),
        action=action,
        status="pending",
        request_id=request_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )


def create_execution_job_for_approved_intent(
    conn,
    intent_id: str,
    *,
    now: datetime | None = None,
    request_id: str | None = None,
    reserve: bool = True,
    manage_transaction: bool = True,
) -> ExecutionJobRequest:
    def _work() -> ExecutionJobRequest:
        intent_row = _load_intent_for_update(conn, intent_id)
        if intent_row is None:
            raise ValueError(f"approved intent not found: {intent_id}")
        if str(intent_row["status"]) != "approved":
            raise ValueError(f"intent is not approved: {intent_id}")
        request = build_execution_job_request(intent_row, now=now, request_id=request_id)
        _insert_execution_job(conn, request)
        if reserve:
            _ensure_risk_reservation(conn, request)
        enqueue_order_management_event(
            conn,
            aggregate_type="execution_job",
            aggregate_id=request.execution_job_id,
            event_type="order.execution_job.created",
            idempotency_key=request.idempotency_key,
            request_id=request.request_id,
            payload={
                "execution_job_id": request.execution_job_id,
                "intent_id": request.intent_id,
                "account_id": request.account_id,
                "venue_symbol": request.venue_symbol,
            },
        )
        return request

    if manage_transaction:
        with transaction(conn):
            return _work()
    return _work()


def claim_next_execution_job(
    conn,
    *,
    worker_id: str,
    now: datetime | None = None,
    manage_transaction: bool = True,
) -> ClaimedExecutionJob | None:
    now = _aware(now or datetime.now(timezone.utc))

    def _work() -> ClaimedExecutionJob | None:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM execution_jobs
                WHERE status = ANY(%s)
                  AND (
                    status <> 'retry_scheduled'
                    OR payload#>>'{retry,next_attempt_at}' IS NULL
                    OR (payload#>>'{retry,next_attempt_at}')::timestamptz <= %s
                  )
                ORDER BY created_at ASC, execution_job_id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (list(PENDING_STATUSES), now),
            )
            row = cur.fetchone()
            if row is None:
                return None
            job_id = str(row["execution_job_id"])
            payload = dict(row.get("payload") or {})
            attempts = int(payload.get("attempts") or 0)
            payload["claimed_by"] = worker_id
            payload["claimed_at"] = now.isoformat()
            payload["attempts"] = attempts
            cur.execute(
                """
                UPDATE execution_jobs
                SET status='claimed',
                    started_at=COALESCE(started_at, %s),
                    updated_at=now(),
                    payload=%s
                WHERE execution_job_id=%s
                RETURNING *
                """,
                (now, Json(payload), job_id),
            )
            updated = cur.fetchone()
        return _claimed_from_row(updated)

    if manage_transaction:
        with transaction(conn):
            return _work()
    return _work()


def retry_payload(
    payload: dict[str, Any],
    *,
    reason: str,
    delay_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
) -> RetryPayloadResult:
    now = _aware(now or datetime.now(timezone.utc))
    next_payload = dict(payload)
    attempts = int(next_payload.get("attempts") or 0) + 1
    next_payload["attempts"] = attempts
    if attempts > max_attempts:
        next_payload["terminal"] = {
            "reason": "max_attempts_exceeded",
            "last_error": reason,
            "at": now.isoformat(),
        }
        return RetryPayloadResult(status="failed", payload=next_payload)
    next_payload["retry"] = {
        "reason": reason,
        "next_attempt_at": (now + timedelta(seconds=int(delay_seconds))).isoformat(),
    }
    return RetryPayloadResult(status="retry_scheduled", payload=next_payload)


def schedule_job_retry(
    conn,
    execution_job_id: str,
    *,
    reason: str,
    delay_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
) -> RetryPayloadResult:
    now = _aware(now or datetime.now(timezone.utc))
    with transaction(conn), conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT payload FROM execution_jobs WHERE execution_job_id=%s FOR UPDATE",
            (execution_job_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"execution job not found: {execution_job_id}")
        result = retry_payload(
            dict(row["payload"] or {}),
            reason=reason,
            delay_seconds=delay_seconds,
            max_attempts=max_attempts,
            now=now,
        )
        completed = now if result.status in TERMINAL_STATUSES else None
        cur.execute(
            """
            UPDATE execution_jobs
            SET status=%s, payload=%s, completed_at=COALESCE(%s, completed_at), updated_at=now()
            WHERE execution_job_id=%s
            """,
            (result.status, Json(result.payload), completed, execution_job_id),
        )
        return result


def mark_job_terminal(
    conn,
    execution_job_id: str,
    *,
    status: str,
    reason: str,
    now: datetime | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"not a terminal execution job status: {status}")
    now = _aware(now or datetime.now(timezone.utc))
    with transaction(conn), conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT payload FROM execution_jobs WHERE execution_job_id=%s FOR UPDATE",
            (execution_job_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"execution job not found: {execution_job_id}")
        payload = dict(row["payload"] or {})
        payload["terminal"] = {"status": status, "reason": reason, "at": now.isoformat(), **(detail or {})}
        cur.execute(
            """
            UPDATE execution_jobs
            SET status=%s, payload=%s, completed_at=%s, updated_at=now()
            WHERE execution_job_id=%s
            """,
            (status, Json(payload), now, execution_job_id),
        )


def _load_intent_for_update(conn, intent_id: str) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT intent_id::text, account_id, instrument_id, action::text, status::text,
                   order_plan, risk_budget, valid_until, idempotency_key
            FROM trade_intents
            WHERE intent_id=%s
            FOR UPDATE
            """,
            (intent_id,),
        )
        return cur.fetchone()


def _insert_execution_job(conn, request: ExecutionJobRequest) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO execution_jobs (
                execution_job_id, intent_id, account_id, instrument_id, venue_symbol,
                action, status, request_id, idempotency_key, payload
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (account_id, idempotency_key) DO UPDATE SET
                request_id=COALESCE(execution_jobs.request_id, EXCLUDED.request_id),
                payload=execution_jobs.payload || EXCLUDED.payload,
                updated_at=now()
            """,
            (
                request.execution_job_id,
                request.intent_id,
                request.account_id,
                request.instrument_id,
                request.venue_symbol,
                request.action,
                request.status,
                request.request_id,
                request.idempotency_key,
                Json(request.payload),
            ),
        )


def _ensure_risk_reservation(conn, request: ExecutionJobRequest) -> None:
    reservation = dict(request.payload.get("reservation") or {})
    idempotency_key = hashlib.sha256(f"{request.idempotency_key}|risk".encode("utf-8")).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk_reservations (
                risk_reservation_id, execution_job_id, account_id, venue_symbol,
                idempotency_key, status, notional, risk_amount, margin_amount,
                expires_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, 'held', %s, %s, %s, %s, %s)
            ON CONFLICT (account_id, idempotency_key) DO UPDATE SET
                execution_job_id=COALESCE(risk_reservations.execution_job_id, EXCLUDED.execution_job_id),
                payload=risk_reservations.payload || EXCLUDED.payload,
                updated_at=now()
            """,
            (
                str(uuid4()),
                request.execution_job_id,
                request.account_id,
                request.venue_symbol,
                idempotency_key,
                reservation.get("notional", "0"),
                reservation.get("risk_amount", "0"),
                reservation.get("margin_amount", "0"),
                reservation.get("expires_at"),
                Json({"intent_id": request.intent_id, "execution_job_id": request.execution_job_id}),
            ),
        )


def _claimed_from_row(row: dict[str, Any]) -> ClaimedExecutionJob:
    return ClaimedExecutionJob(
        execution_job_id=str(row["execution_job_id"]),
        intent_id=str(row["intent_id"]) if row.get("intent_id") is not None else None,
        account_id=row["account_id"],
        instrument_id=row["instrument_id"],
        venue_symbol=row["venue_symbol"],
        action=row["action"],
        status=row["status"],
        request_id=row.get("request_id"),
        idempotency_key=row["idempotency_key"],
        payload=dict(row.get("payload") or {}),
    )


def _reservation_payload(risk_budget: dict[str, Any], order_plan: dict[str, Any]) -> dict[str, str | None]:
    max_notional = _decimal(
        risk_budget.get("notional")
        or risk_budget.get("max_notional")
        or order_plan.get("notional")
        or "0"
    )
    risk_amount = _decimal(
        risk_budget.get("risk_amount")
        or risk_budget.get("max_risk")
        or (max_notional * _decimal(risk_budget.get("risk_fraction") or "0"))
    )
    leverage = _decimal(risk_budget.get("max_leverage") or "0")
    margin = Decimal("0") if leverage <= 0 else max_notional / leverage
    return {
        "notional": _fmt(max_notional),
        "risk_amount": _fmt(risk_amount),
        "margin_amount": _fmt(margin),
        "expires_at": None,
    }


def _venue_symbol(instrument_id: str) -> str:
    return str(instrument_id)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    return str(value)
