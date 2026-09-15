"""Atomic, account-scoped handoff from a registered signal claim to an intent.

No helper commits or owns a connection. The ingress holds its existing account
risk lock, calls load_request(lock=True), creates the operation, then accepts it
in the same transaction. Dispatch acknowledgement means accepted, never filled.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from uuid import UUID


SOURCE_FIELDS = ("source_platform", "channel_id", "source_message_id", "edit_version", "account_id")
_TRANSPORT = frozenset({"task_id", "processing_run_id", "attempt", "claim_token"})


class SignalHandoffError(ValueError):
    def __init__(self, code: str, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def _fail(code: str, detail: str, status_code: int = 409):
    raise SignalHandoffError(code, detail, status_code)


def _uuid(value, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _fail("invalid_signal_claim", f"{field} must be a UUID", 400)


def _row(cur):
    row = cur.fetchone()
    if row is None or isinstance(row, Mapping):
        return row
    return dict(zip((column[0] for column in cur.description), row))


def canonical_request_hash(body: dict) -> str:
    """Hash semantics while allowing transport claim renewal of the same request."""
    if not isinstance(body, Mapping):
        _fail("invalid_signal_request", "execution request must be an object", 400)
    semantic = {key: value for key, value in body.items() if key not in _TRANSPORT}
    claim = semantic.get("signal_claim")
    if isinstance(claim, Mapping):
        semantic["signal_claim"] = {key: value for key, value in claim.items() if key not in _TRANSPORT}
    try:
        encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        _fail("invalid_signal_request", "execution request must contain finite JSON values", 400)
    return hashlib.sha256(encoded.encode()).hexdigest()


def load_request(cur, account_id: str, body: dict, lock: bool = False) -> dict:
    claim = body.get("signal_claim")
    source = body.get("source_identity")
    if not isinstance(claim, Mapping) or not isinstance(source, Mapping):
        _fail("invalid_signal_claim", "signal_claim and source_identity are required", 400)
    if body.get("account_id") != account_id or source.get("account_id") != account_id:
        _fail("signal_account_mismatch", "authenticated account does not match signal request", 403)
    task_id = _uuid(claim.get("task_id"), "task_id")
    run_id = _uuid(claim.get("processing_run_id"), "processing_run_id")
    token = _uuid(claim.get("claim_token"), "claim_token")
    raw_id = _uuid(body.get("raw_message_id"), "raw_message_id")
    attempt = claim.get("attempt")
    stable = claim.get("stable_action_or_leg_id")
    if type(attempt) is not int or attempt < 1 or not isinstance(stable, str) or not stable.strip():
        _fail("invalid_signal_claim", "positive attempt and stable_action_or_leg_id are required", 400)
    suffix = " FOR UPDATE" if lock else ""
    cur.execute("SELECT * FROM signal_dispatch_tasks WHERE task_id = %s" + suffix, (task_id,))
    task = _row(cur)
    if task is None:
        _fail("signal_task_not_found", "signal task does not exist", 404)
    if task["account_id"] != account_id:
        _fail("signal_account_mismatch", "authenticated account does not own signal task", 403)
    if task["processing_purpose"] != "signal":
        _fail("invalid_signal_purpose", "shadow tasks cannot submit signal operations")
    if any(source.get(field) != task[field] for field in SOURCE_FIELDS) or str(task["raw_message_id"]) != raw_id:
        _fail("signal_source_mismatch", "source identity does not match persisted task")
    request = task["execution_request"]
    execution_context = task["execution_context"]
    if not isinstance(request, Mapping) or not isinstance(execution_context, Mapping):
        _fail("signal_request_unregistered", "signal execution request/context has not been registered")
    request_hash = canonical_request_hash(body)
    if request_hash != canonical_request_hash(request):
        _fail("signal_payload_mismatch", "signal payload differs from the registered execution request")
    cur.execute("SELECT * FROM message_processing_runs WHERE processing_run_id = %s" + suffix, (run_id,))
    run = _row(cur)
    if run is None or (
        run["processing_purpose"] != "signal" or run["account_id"] != account_id
        or str(run["raw_message_id"]) != raw_id or str(run["claim_token"]) != token
        or run["attempt"] != attempt
        or str(task["current_processing_run_id"]) != run_id
        or str(task["claim_token"]) != token or task["attempt"] != attempt
    ):
        _fail("stale_signal_claim", "claim does not match the current signal task and run")
    existing = task["operator_intent_id"]
    source_deadline = None
    if existing is None:
        # Read wall time after both row locks: now() would use transaction start
        # and could mistakenly accept a lease which expired waiting for a lock.
        cur.execute("SELECT clock_timestamp() AS checked_at")
        checked_at = _row(cur)["checked_at"]
        task_lease = task["lease_expires_at"]
        run_lease = run["lease_expires_at"]
        if (task["status"] not in {"leased", "reconciling"} or run["status"] not in {"started", "processing"}
                or task_lease is None or run_lease is None or task_lease <= checked_at or run_lease <= checked_at):
            _fail("stale_signal_claim", "signal execution requires a current unexpired claim")
        if task["expires_at"] is not None and task["expires_at"] <= checked_at:
            _fail("expired_signal", "signal task has expired")
        if body.get("action") in {"open_position", "add_position"}:
            cur.execute("SELECT raw_payload->>'source_ts' AS source_ts FROM raw_messages WHERE id=%s", (raw_id,))
            source_row = _row(cur)
            try:
                source_ts = datetime.fromisoformat(str(source_row["source_ts"]).replace("Z", "+00:00"))
                if source_ts.tzinfo is None:
                    raise ValueError("source timestamp has no timezone")
            except (ValueError, TypeError, KeyError):
                _fail("signal_source_time_unknown", "entry source timestamp is unknown; receive/ingest time cannot substitute")
            source_deadline = source_ts + timedelta(minutes=30)
            if source_deadline <= checked_at or source_ts > checked_at + timedelta(minutes=5):
                _fail("signal_source_expired", "entry source timestamp is expired or invalid")
        if body.get("action") in {"open_position", "add_position"}:
            # A rejected intermediate edit has no operation of its own. Check
            # the complete source identity, not only its immediate predecessor.
            cur.execute(
                """SELECT operator_intent_id FROM signal_dispatch_tasks
                WHERE source_platform=%s AND channel_id=%s AND source_message_id=%s
                  AND account_id=%s AND task_id<>%s AND operator_intent_id IS NOT NULL
                LIMIT 1""",
                (task["source_platform"], task["channel_id"], task["source_message_id"], account_id, task_id),
            )
            if _row(cur) is not None:
                _fail("signal_edit_requires_reconciliation", "edited source already has an operation; reconcile before increasing risk")
    identity = {field: task[field] for field in SOURCE_FIELDS}
    identity_bytes = json.dumps([*[identity[field] for field in SOURCE_FIELDS], stable], separators=(",", ":"), ensure_ascii=True).encode()
    return {
        "task_id": task_id, "account_id": account_id, "raw_message_id": raw_id,
        "processing_run_id": run_id, "attempt": attempt, "claim_token": token,
        "source_identity": identity, "stable_action_or_leg_id": stable,
        "idempotency_key": hashlib.sha256(identity_bytes).hexdigest(),
        "request_hash": request_hash, "execution_request": dict(request),
        "execution_context": dict(execution_context), "expected_versions": dict(execution_context),
        "operator_intent_id": str(existing) if existing is not None else None,
        "existing_operation": existing is not None, "locked": lock,
        "source_deadline": source_deadline,
    }


def accept_operation(cur, context: dict, intent_id: str) -> None:
    """Bind an intent and finish its current signal run in one SQL statement."""
    intent_id = _uuid(intent_id, "intent_id")
    if not context.get("locked"):
        _fail("signal_handoff_unlocked", "accept_operation requires load_request(lock=True)")
    if context["existing_operation"]:
        if context["operator_intent_id"] != intent_id:
            _fail("signal_operation_conflict", "signal task already accepted another intent")
        return
    cur.execute(
        """
        WITH eligible AS MATERIALIZED (
            SELECT t.task_id, r.processing_run_id
            FROM signal_dispatch_tasks t
            JOIN message_processing_runs r ON r.processing_run_id = t.current_processing_run_id
            JOIN trade_intents i ON i.intent_id = %(intent_id)s AND i.account_id = t.account_id
            WHERE t.task_id = %(task_id)s AND t.account_id = %(account_id)s
              AND t.processing_purpose = 'signal' AND r.processing_purpose = 'signal'
              AND r.account_id = t.account_id AND r.raw_message_id = t.raw_message_id
              AND t.operator_intent_id IS NULL AND t.status IN ('leased', 'reconciling')
              AND r.status IN ('started', 'processing')
              AND r.processing_run_id = %(processing_run_id)s
              AND t.claim_token = %(claim_token)s AND r.claim_token = %(claim_token)s
              AND t.attempt = %(attempt)s AND r.attempt = %(attempt)s
              AND t.lease_expires_at > clock_timestamp() AND r.lease_expires_at > clock_timestamp()
              AND (t.expires_at IS NULL OR t.expires_at > clock_timestamp())
              AND i.valid_until > clock_timestamp()
              AND (%(source_deadline)s::timestamptz IS NULL OR %(source_deadline)s > clock_timestamp())
        ), finished AS (
            UPDATE message_processing_runs r SET status = 'succeeded', finished_at = clock_timestamp()
            FROM eligible e WHERE r.processing_run_id = e.processing_run_id
            RETURNING r.processing_run_id
        )
        UPDATE signal_dispatch_tasks t
        SET operator_intent_id = %(intent_id)s, status = 'dispatched', updated_at = clock_timestamp()
        FROM eligible e JOIN finished f ON f.processing_run_id = e.processing_run_id
        WHERE t.task_id = e.task_id RETURNING t.task_id
        """,
        {**context, "intent_id": intent_id},
    )
    if cur.fetchone() is None:
        _fail("stale_signal_claim", "signal claim expired or changed before accepting operation")
