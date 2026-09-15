"""Hermes worker: raw message -> validated HermesDecisionV1 -> hermes_decisions.

The worker consumes a claimed processing run (A-04 queue), gathers the raw text, all
images, referenced messages, recent context and a real system snapshot, asks Hermes
(the only semantic processor) for a decision, validates it against contracts-v1, and
persists it atomically. Every failure path is fail-closed: no decision is written and
the run terminates in an explicit failure state. No regex/OCR is used for semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from psycopg2.extras import Json
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, connection as PsycopgConnection

# A-02 connection helper + A-04 queue claim live next to / under services/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_REPO_ROOT / "services" / "control-plane" / "db"),
    str(Path(__file__).resolve().parent / "queue"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import claims  # noqa: E402  (services/hermes-worker/queue/claims.py)
import signal_queue  # noqa: E402
from connection import transaction  # noqa: E402  (services/control-plane/db/connection.py)

from hermes_client import (  # noqa: E402
    HermesImage,
    HermesRequest,
    HermesResponseError,
    HermesTimeoutError,
    HermesUnavailableError,
)
from prompt import MODEL_TEMPERATURE, PROMPT_VERSION  # noqa: E402

CONTEXT_VERSION = "ctx-v1"
SCHEMA_PATH = _REPO_ROOT / "packages" / "contracts" / "v1" / "hermes_decision.v1.json"
DEFAULT_TIMEOUT = 30.0
RECENT_CONTEXT_LIMIT = 5
WORKER_MODE_LEGACY = "legacy"
WORKER_MODE_SHADOW = "shadow"
DEFAULT_WORKER_MODE = WORKER_MODE_LEGACY


class MediaLoader(Protocol):
    def load(self, object_key: str) -> tuple[str, str]:
        """Return (mime, base64_bytes) for a stored media object, or raise."""
        ...


class SnapshotProvider(Protocol):
    def current(self) -> dict[str, Any]:
        """Return a SystemSnapshotV1-shaped dict (must include data_source/stale/...)."""
        ...


@dataclass(frozen=True)
class WorkerResult:
    status: str  # "succeeded" | "skipped" | "shadow_dispatched" | failure code
    processing_run_id: str | None = None
    raw_message_id: str | None = None
    decision_id: str | None = None
    detail: str | None = None
    task_id: str | None = None
    account_id: str | None = None
    operator_submitted: bool = False


def _load_schema_validator():
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def resolve_worker_mode(mode: str | None = None) -> str:
    raw = (mode or os.environ.get("HERMES_WORKER_MODE") or DEFAULT_WORKER_MODE)
    resolved = str(raw).strip().lower()
    if resolved not in {WORKER_MODE_LEGACY, WORKER_MODE_SHADOW}:
        raise ValueError(
            f"HERMES_WORKER_MODE must be {WORKER_MODE_LEGACY!r} or "
            f"{WORKER_MODE_SHADOW!r}, got {raw!r}"
        )
    return resolved


def process_one(
    conn: PsycopgConnection,
    *,
    worker_id: str,
    client: Any,
    media_loader: MediaLoader,
    snapshot_provider: SnapshotProvider,
    model_version: str = "pinned",
    timeout: float = DEFAULT_TIMEOUT,
    lease_seconds: int = 30,
    now_iso: str | None = None,
    mode: str | None = None,
) -> WorkerResult:
    """Default mode is legacy outbox claim (sole live writer until G3 cutover).

    Shadow mode claims signal_dispatch_tasks only, never publishes outbox, and
    never submits operator/exchange orders.
    """
    resolved_mode = resolve_worker_mode(mode)
    if resolved_mode == WORKER_MODE_SHADOW:
        return process_shadow_one(
            conn,
            worker_id=worker_id,
            client=client,
            media_loader=media_loader,
            snapshot_provider=snapshot_provider,
            model_version=model_version,
            timeout=timeout,
            lease_seconds=lease_seconds,
            now_iso=now_iso,
        )

    run = claims.claim(conn, worker_id, lease_seconds=lease_seconds)
    if run is None:
        return WorkerResult(status="skipped")

    run_id = run.processing_run_id
    raw_message_id = run.raw_message_id

    try:
        message = _load_raw_message(conn, raw_message_id)

        images, media_problem = _load_images(conn, raw_message_id, media_loader)
        if media_problem is not None:
            return _fail(conn, run_id, raw_message_id, "media_failed", media_problem)

        snapshot = snapshot_provider.current()
        snapshot_problem = _snapshot_problem(snapshot)
        # A stale-but-present projection must NEVER drop the user's signal: Hermes still
        # classifies the message (it is recorded + visible, never blocked). Per contract
        # §2.2 the staleness gates only AUTO-APPROVAL of new risk downstream (enforced in
        # the Decision Gateway), not classification. Only a structurally unusable snapshot
        # (no PostgreSQL projection at all) is a hard fail.
        if snapshot_problem == "context_unavailable":
            return _fail(conn, run_id, raw_message_id, snapshot_problem, "system snapshot unusable")

        request = HermesRequest(
            raw_message_id=raw_message_id,
            text=message["message_text"] or "",
            images=images,
            referenced_messages=_referenced_messages(message),
            recent_context=_recent_context(conn, message, raw_message_id),
            system_snapshot=snapshot,
        )
        _close_read_transaction(conn)

        try:
            candidate = client.analyze(request, timeout=timeout)
        except HermesTimeoutError as exc:
            return _fail(conn, run_id, raw_message_id, "hermes_timeout", str(exc))
        except HermesUnavailableError as exc:
            return _fail(conn, run_id, raw_message_id, "hermes_unavailable", str(exc))
        except HermesResponseError as exc:
            return _fail(conn, run_id, raw_message_id, "hermes_failed", str(exc))

        if not isinstance(candidate, dict):
            return _fail(conn, run_id, raw_message_id, "hermes_failed", "non-object model response")

        context_snapshot_id = str(uuid4())
        decision_id = str(uuid4())
        created_at = now_iso or _utc_now_iso()
        decision = _assemble_decision(
            candidate,
            decision_id=decision_id,
            raw_message_id=raw_message_id,
            processing_run_id=run_id,
            context_snapshot_id=context_snapshot_id,
            model_version=model_version,
            created_at=created_at,
        )

        validator = _load_schema_validator()
        errors = sorted(validator.iter_errors(decision), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            location = "$" + "".join(f".{p}" for p in first.path)
            return _fail(
                conn,
                run_id,
                raw_message_id,
                "invalid_decision_schema",
                f"{location}: {first.message}",
            )

        _enforce_action_safety(decision)

        response_sha256 = hashlib.sha256(
            json.dumps(candidate, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        _persist_success(
            conn,
            run_id=run_id,
            raw_message_id=raw_message_id,
            context_snapshot_id=context_snapshot_id,
            snapshot=snapshot,
            decision=decision,
            response_sha256=response_sha256,
            claim_token=run.claim_token,
        )
        return WorkerResult(
            status="succeeded",
            processing_run_id=run_id,
            raw_message_id=raw_message_id,
            decision_id=decision_id,
        )
    except Exception as exc:  # fail closed on anything unexpected
        conn.rollback()
        return _fail(conn, run_id, raw_message_id, "hermes_failed", f"unexpected: {exc}")


# --- data loading -----------------------------------------------------------------


def _load_raw_message(conn: PsycopgConnection, raw_message_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, channel_id, source_received_at, message_text, raw_payload
            FROM raw_messages WHERE id = %s
            """,
            (raw_message_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"raw_message {raw_message_id} not found")
    return {
        "id": row[0],
        "channel_id": row[1],
        "source_received_at": row[2],
        "message_text": row[3],
        "raw_payload": row[4] or {},
    }


def _load_images(
    conn: PsycopgConnection, raw_message_id: str, media_loader: MediaLoader
) -> tuple[list[HermesImage], str | None]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sha256, mime, object_key, download_status
            FROM media_assets WHERE raw_message_id = %s ORDER BY created_at
            """,
            (raw_message_id,),
        )
        rows = cur.fetchall()

    images: list[HermesImage] = []
    for sha256, mime, object_key, download_status in rows:
        if download_status != "downloaded":
            return [], f"media {sha256} status={download_status}"
        if not object_key:
            return [], f"media {sha256} has no object_key"
        try:
            loaded_mime, data_base64 = media_loader.load(object_key)
        except Exception as exc:  # loader failure is a media failure
            return [], f"media {sha256} load failed: {exc}"
        images.append(HermesImage(sha256=sha256, mime=mime or loaded_mime, data_base64=data_base64))
    return images, None


def _snapshot_problem(snapshot: dict[str, Any]) -> str | None:
    if not isinstance(snapshot, dict):
        return "context_unavailable"
    if snapshot.get("data_source") != "postgres_projection":
        return "context_unavailable"
    if snapshot.get("stale") is True:
        return "context_stale"
    if snapshot.get("reconciliation_state") == "failed":
        return "context_stale"
    return None


def _shadow_snapshot_problem(snapshot: dict[str, Any]) -> str | None:
    """Shadow may use a labeled local fixture or a real postgres_projection.

    A fixture must keep fixture_provenance and must not impersonate
    data_source=postgres_projection.
    """
    if not isinstance(snapshot, dict):
        return "context_unavailable"
    provenance = str(snapshot.get("fixture_provenance") or "").strip()
    if provenance:
        if snapshot.get("data_source") == "postgres_projection":
            return "context_unavailable"
        return None
    return _snapshot_problem(snapshot)


def _referenced_messages(message: dict[str, Any]) -> list[dict[str, Any]]:
    payload = message.get("raw_payload") or {}
    reply = payload.get("reply_to") or payload.get("reply")
    return [reply] if isinstance(reply, dict) else []


def _recent_context(
    conn: PsycopgConnection, message: dict[str, Any], raw_message_id: str
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, message_text, source_received_at
            FROM raw_messages
            WHERE channel_id = %s AND id <> %s
            ORDER BY source_received_at DESC
            LIMIT %s
            """,
            (message["channel_id"], raw_message_id, RECENT_CONTEXT_LIMIT),
        )
        return [
            {"raw_message_id": rid, "text": text, "source_received_at": str(ts)}
            for rid, text, ts in cur.fetchall()
        ]


# --- decision assembly + persistence ----------------------------------------------


def _enforce_action_safety(decision: dict) -> None:
    """Deterministic backstop (not prompt-only): illegal action combinations are
    coerced to needs_review before persistence (PLAN: update messages never open a
    position; close/partial/move actions require a target_position_id)."""
    classification = decision["classification"]
    intent = decision.get("intent") or {}
    action = classification.get("action")
    message_type = classification.get("message_type")
    reasons = classification.setdefault("ambiguity_reasons", [])

    def to_review(reason: str) -> None:
        classification["action"] = "needs_review"
        if reason not in reasons:
            reasons.append(reason)

    update_types = {"position_update", "close_update"}
    update_actions = {
        "partial_close", "close_position", "move_stop_loss",
        "move_stop_to_entry", "replace_take_profits",
    }
    if message_type in update_types and action == "open_position":
        to_review(f"update message_type {message_type} cannot open a position")
    if action in update_actions and not intent.get("target_position_id"):
        to_review(f"{action} requires a target_position_id")
    # Protection completeness note (2026-08-28 WP-E, advisory per operator
    # directive — owner-operated account, never block): an open decision with
    # neither take profits nor a stop loss proceeds, but the gap is recorded
    # on the decision so reports and operators can see it.
    if action in {"open_position", "add_position"}:
        take_profits = intent.get("take_profits") or []
        if not take_profits and intent.get("stop_loss") is None:
            reason = f"{action} has neither take_profits nor stop_loss (protection incomplete; advisory)"
            if reason not in reasons:
                reasons.append(reason)


def _assemble_decision(
    candidate: dict[str, Any],
    *,
    decision_id: str,
    raw_message_id: str,
    processing_run_id: str,
    context_snapshot_id: str,
    model_version: str,
    created_at: str,
) -> dict[str, Any]:
    """Keep the model's semantics; force authoritative identity + pinned model block.

    Whitelist only the contract's semantic fields so a model that adds extra keys
    (metadata, extracted_data, ...) does not trip additionalProperties validation."""
    decision = {
        key: candidate[key]
        for key in ("classification", "intent", "evidence", "confidence")
        if key in candidate
    }
    decision["schema_version"] = "1.0"
    decision["decision_id"] = decision_id
    decision["raw_message_id"] = raw_message_id
    decision["processing_run_id"] = processing_run_id
    decision["context_snapshot_id"] = context_snapshot_id
    decision["model"] = {
        "provider": "hermes",
        "model_version": model_version,
        "prompt_version": PROMPT_VERSION,
        "temperature": MODEL_TEMPERATURE,
    }
    decision["created_at"] = created_at
    decision.setdefault("evidence", [])
    return decision


def process_shadow_one(
    conn: PsycopgConnection,
    *,
    worker_id: str,
    client: Any,
    media_loader: MediaLoader,
    snapshot_provider: SnapshotProvider,
    model_version: str = "pinned",
    timeout: float = DEFAULT_TIMEOUT,
    lease_seconds: int = 30,
    now_iso: str | None = None,
) -> WorkerResult:
    """Shadow consumer: per-account claim, semantic shadow, no outbox consume."""

    if client is None:
        raise ValueError("shadow mode requires a Hermes client for semantic shadow")
    bounded_lease = max(int(lease_seconds), int(timeout) + 15)
    task = signal_queue.claim_signal_task(
        conn, worker_id, lease_seconds=bounded_lease
    )
    if task is None:
        return WorkerResult(status="skipped")
    return _process_claimed_signal(
        conn,
        task,
        client=client,
        media_loader=media_loader,
        snapshot_provider=snapshot_provider,
        model_version=model_version,
        timeout=timeout,
        now_iso=now_iso,
    )


def _process_claimed_signal(
    conn: PsycopgConnection,
    task: signal_queue.SignalTask,
    *,
    client: Any,
    media_loader: MediaLoader,
    snapshot_provider: SnapshotProvider,
    model_version: str,
    timeout: float,
    now_iso: str | None,
) -> WorkerResult:
    """Persist a validated semantic shadow decision. Does not publish outbox."""

    if not task.claim_token:
        raise signal_queue.StaleClaimError("claimed signal task missing claim_token")
    if client is None:
        return _fail_claimed_signal(
            conn, task, "semantic_shadow_requires_model", "shadow client is required"
        )
    dispatch_at = datetime.now(timezone.utc).isoformat()
    model_started_at = None
    model_finished_at = None
    try:
        message = _load_raw_message(conn, task.raw_message_id)
        images, media_problem = _load_images(conn, task.raw_message_id, media_loader)
        if media_problem is not None:
            return _fail_claimed_signal(conn, task, "media_failed", media_problem)
        snapshot = snapshot_provider.current()
        if _shadow_snapshot_problem(snapshot) == "context_unavailable":
            return _fail_claimed_signal(
                conn, task, "context_unavailable", "system snapshot unusable"
            )
        request = HermesRequest(
            raw_message_id=task.raw_message_id,
            text=message["message_text"] or "",
            images=images,
            referenced_messages=_referenced_messages(message),
            recent_context=_recent_context(conn, message, task.raw_message_id),
            system_snapshot=snapshot,
        )
        _close_read_transaction(conn)
        model_started_at = datetime.now(timezone.utc).isoformat()
        try:
            candidate = client.analyze(request, timeout=timeout)
        except HermesTimeoutError as exc:
            return _fail_claimed_signal(conn, task, "hermes_timeout", str(exc))
        except HermesUnavailableError as exc:
            return _fail_claimed_signal(conn, task, "hermes_unavailable", str(exc))
        except HermesResponseError as exc:
            return _fail_claimed_signal(conn, task, "hermes_failed", str(exc))
        model_finished_at = datetime.now(timezone.utc).isoformat()
        if not isinstance(candidate, dict):
            return _fail_claimed_signal(
                conn, task, "hermes_failed", "non-object model response"
            )

        created_at = now_iso or _utc_now_iso()
        decision = _assemble_decision(
            candidate,
            decision_id=str(uuid4()),
            raw_message_id=task.raw_message_id,
            processing_run_id=task.processing_run_id or str(uuid4()),
            context_snapshot_id=str(uuid4()),
            model_version=model_version,
            created_at=created_at,
        )
        validator = _load_schema_validator()
        errors = sorted(validator.iter_errors(decision), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            location = "$" + "".join(f".{p}" for p in first.path)
            return _fail_claimed_signal(
                conn,
                task,
                "invalid_decision_schema",
                f"{location}: {first.message}",
            )
        _enforce_action_safety(decision)
        semantic = _semantic_shadow_fields(task, decision, message)
        stages = _shadow_stage_timestamps(
            message,
            dispatch_at=dispatch_at,
            model_started_at=model_started_at,
            model_finished_at=model_finished_at,
        )
        result = signal_queue.shadow_dispatch(
            conn,
            task,
            semantic=semantic,
            stages=stages,
        )
        return WorkerResult(
            status=result.status,
            processing_run_id=result.processing_run_id,
            raw_message_id=result.raw_message_id,
            decision_id=decision["decision_id"],
            detail=result.disposition_reason,
            task_id=result.task_id,
            account_id=result.account_id,
            operator_submitted=False,
        )
    except signal_queue.StaleClaimError as exc:
        return WorkerResult(
            status="stale_claim",
            processing_run_id=task.processing_run_id,
            raw_message_id=task.raw_message_id,
            detail=str(exc),
            task_id=task.task_id,
            account_id=task.account_id,
            operator_submitted=False,
        )
    except Exception as exc:  # fail closed
        conn.rollback()
        return _fail_claimed_signal(conn, task, "hermes_failed", f"unexpected: {exc}")


def _semantic_shadow_fields(
    task: signal_queue.SignalTask,
    decision: dict[str, Any],
    message: dict[str, Any],
) -> dict[str, Any]:
    classification = decision.get("classification") or {}
    intent = decision.get("intent") or {}
    action = str(classification.get("action") or task.action)
    identity = {
        "source_platform": task.source_platform,
        "channel_id": task.channel_id,
        "source_message_id": task.source_message_id,
        "edit_version": task.edit_version,
        "account_id": task.account_id,
    }
    client_ref = _shadow_client_ref(task)
    return {
        "action": action,
        "stable_action_or_leg_id": "|".join(
            [
                task.source_platform,
                task.channel_id,
                task.source_message_id,
                task.edit_version,
                task.account_id,
                action,
            ]
        ),
        "client_ref": client_ref,
        "source_identity": identity,
        "related_task_id": task.related_task_id,
        "related_edit": bool(task.related_task_id),
        "stable_operation_identity": {
            **identity,
            "action": action,
            "client_ref": client_ref,
            "related_task_id": task.related_task_id,
            "edit_version": task.edit_version,
        },
        "decision": decision,
    }


def _shadow_client_ref(task: signal_queue.SignalTask) -> str:
    channel = str(task.channel_id or "").lstrip("-")
    message_id = str(task.source_message_id or "")
    if channel.isdigit() and message_id.isdigit():
        return f"tg-sig-c{channel}-m{message_id}"
    return f"shadow:{task.task_id}"


def _shadow_stage_timestamps(
    message: dict[str, Any],
    *,
    dispatch_at: str,
    model_started_at: str | None,
    model_finished_at: str | None,
) -> dict[str, Any]:
    payload = message.get("raw_payload") or {}
    receive_ts = payload.get("receive_ts")
    source_ts = payload.get("source_ts")
    return {
        "source_ts": source_ts,
        "source_ts_unknown": source_ts is None,
        "receive_ts": receive_ts or str(message.get("source_received_at") or ""),
        "ingested_at_is_not_receive_ts": True,
        "persist_at": payload.get("persist_at"),
        "dispatch_at": dispatch_at,
        "model_started_at": model_started_at,
        "model_finished_at": model_finished_at,
        "intent": None,
        "submit": None,
        "ack": None,
        "fill": None,
    }


def _fail_claimed_signal(
    conn: PsycopgConnection,
    task: signal_queue.SignalTask,
    code: str,
    detail: str,
) -> WorkerResult:
    """New-path failure requires the current claim_token and unexpired lease."""

    if not task.claim_token:
        raise signal_queue.StaleClaimError("signal failure requires claim_token")
    try:
        signal_queue.fail_signal_task(
            conn,
            task.task_id,
            task.claim_token,
            disposition_reason=f"{code}: {detail}"[:500],
            shadow_result={
                "mode": "shadow",
                "operator_submitted": False,
                "outbox_published": False,
                "code": code,
            },
        )
    except signal_queue.StaleClaimError as exc:
        return WorkerResult(
            status="stale_claim",
            processing_run_id=task.processing_run_id,
            raw_message_id=task.raw_message_id,
            detail=str(exc),
            task_id=task.task_id,
            account_id=task.account_id,
            operator_submitted=False,
        )
    return WorkerResult(
        status=code,
        processing_run_id=task.processing_run_id,
        raw_message_id=task.raw_message_id,
        detail=detail,
        task_id=task.task_id,
        account_id=task.account_id,
        operator_submitted=False,
    )


def _persist_success(
    conn: PsycopgConnection,
    *,
    run_id: str,
    raw_message_id: str,
    context_snapshot_id: str,
    snapshot: dict[str, Any],
    decision: dict[str, Any],
    response_sha256: str,
    claim_token: str | None = None,
) -> None:
    classification = decision["classification"]
    intent = decision["intent"]
    entry = intent["entry"]
    model = decision["model"]

    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_snapshots
                    (context_snapshot_id, raw_message_id, snapshot_type, context_version, snapshot)
                VALUES (%s, %s, 'system_snapshot_v1', %s, %s)
                """,
                (context_snapshot_id, raw_message_id, CONTEXT_VERSION, Json(snapshot)),
            )
            cur.execute(
                """
                INSERT INTO hermes_decisions (
                    decision_id, raw_message_id, processing_run_id, context_snapshot_id,
                    schema_version, message_type, action, ambiguous, ambiguity_reasons,
                    account_scope, target_account_id, target_position_id, instrument_symbol,
                    side, entry_type, entry_price, entry_price_min, entry_price_max,
                    stop_loss, take_profits, leverage, valid_until, evidence,
                    model_provider, model_version, prompt_version, context_version,
                    temperature, confidence, created_at
                )
                VALUES (
                    %s, %s, %s, %s,
                    '1.0', %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    'hermes', %s, %s, %s,
                    %s, %s, %s
                )
                """,
                (
                    decision["decision_id"], raw_message_id, run_id, context_snapshot_id,
                    classification["message_type"], classification["action"],
                    classification["ambiguous"], Json(classification.get("ambiguity_reasons", [])),
                    intent["account_scope"], intent.get("target_account_id"),
                    intent.get("target_position_id"), intent.get("instrument_symbol"),
                    intent.get("side"), entry["type"], entry.get("price"),
                    entry.get("price_min"), entry.get("price_max"),
                    intent.get("stop_loss"), Json(intent.get("take_profits", [])),
                    intent.get("leverage"), intent.get("valid_until"),
                    Json(decision.get("evidence", [])),
                    model["model_version"], model["prompt_version"], CONTEXT_VERSION,
                    model["temperature"], decision.get("confidence"), decision["created_at"],
                ),
            )
            if claim_token:
                cur.execute(
                    """
                    UPDATE message_processing_runs
                       SET status = 'succeeded', finished_at = now(),
                           model_version = %s, prompt_version = %s, context_version = %s
                     WHERE processing_run_id = %s
                       AND claim_token = %s
                       AND status IN ('started', 'processing')
                       AND lease_expires_at > now()
                    """,
                    (
                        model["model_version"],
                        model["prompt_version"],
                        CONTEXT_VERSION,
                        run_id,
                        claim_token,
                    ),
                )
            else:
                # Legacy outbox completion until G3 cutover.
                cur.execute(
                    """
                    UPDATE message_processing_runs
                       SET status = 'succeeded', finished_at = now(),
                           model_version = %s, prompt_version = %s, context_version = %s
                     WHERE processing_run_id = %s AND status IN ('started', 'processing')
                    """,
                    (model["model_version"], model["prompt_version"], CONTEXT_VERSION, run_id),
                )
            if cur.rowcount != 1:
                raise RuntimeError("processing run not active at completion (lease lost)")
            # consume the queue item so the message is not re-processed into a duplicate
            # decision; failures intentionally leave it pending for retry.
            cur.execute(
                """
                UPDATE outbox_events
                   SET status = 'published', published_at = now()
                 WHERE aggregate_type = 'raw_message' AND aggregate_id = %s AND status = 'pending'
                """,
                (raw_message_id,),
            )
            _insert_audit(
                cur,
                event_type="hermes.decided",
                raw_message_id=raw_message_id,
                payload={
                    "decision_id": decision["decision_id"],
                    "response_sha256": response_sha256,
                    "summary": {
                        "message_type": classification["message_type"],
                        "action": classification["action"],
                        "instrument_symbol": intent.get("instrument_symbol"),
                    },
                },
            )


def _fail(
    conn: PsycopgConnection,
    run_id: str | None,
    raw_message_id: str | None,
    code: str,
    detail: str,
) -> WorkerResult:
    run_status = "hermes_timeout" if code == "hermes_timeout" else "hermes_failed"
    error_text = f"{code}: {detail}"[:2000]
    if run_id is not None:
        with transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE message_processing_runs
                       SET status = %s, finished_at = now(), error = %s
                     WHERE processing_run_id = %s AND status IN ('started', 'processing')
                    """,
                    (run_status, error_text, run_id),
                )
                _insert_audit(
                    cur,
                    event_type=f"hermes.{code}",
                    raw_message_id=raw_message_id,
                    payload={"detail": detail},
                )
    return WorkerResult(
        status=code,
        processing_run_id=run_id,
        raw_message_id=raw_message_id,
        detail=detail,
    )


def _insert_audit(cur, *, event_type: str, raw_message_id: str | None, payload: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO audit_events
            (audit_event_id, event_type, aggregate_type, aggregate_id, actor, raw_message_id, payload)
        VALUES (%s, %s, 'raw_message', %s, 'hermes-worker', %s, %s)
        """,
        (str(uuid4()), event_type, str(raw_message_id), raw_message_id, Json(payload)),
    )


def _close_read_transaction(conn: PsycopgConnection) -> None:
    """Model I/O must not run inside an open SQL transaction or advisory lock."""
    if conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
        conn.commit()
    if conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
        raise RuntimeError("database transaction still open before model call")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FilesystemMediaLoader:
    """Same adapter as smoke_replay: load media bytes from object_key on disk."""

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root) if root else None

    def load(self, object_key: str) -> tuple[str, str]:
        import base64
        import mimetypes

        path = Path(object_key)
        if self._root is not None and not path.is_absolute():
            path = self._root / object_key
        data = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return mime, base64.b64encode(data).decode("ascii")


class PostgresSnapshotProvider:
    """Same adapter as smoke_replay: SystemSnapshotV1 from control-plane projection."""

    def __init__(self, conn: PsycopgConnection) -> None:
        self._conn = conn

    def current(self) -> dict[str, Any]:
        cp_api = _REPO_ROOT / "services" / "control-plane" / "api"
        if str(cp_api) not in sys.path:
            sys.path.insert(0, str(cp_api))
        from snapshot import build_system_snapshot

        return build_system_snapshot(self._conn)


class FixtureSnapshotProvider:
    """Local rehearsal only. Labeled fixtures cannot impersonate live projection."""

    def __init__(self, path: str | Path) -> None:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("fixture snapshot must be a JSON object")
        provenance = str(raw.get("fixture_provenance") or "").strip()
        if not provenance:
            raise ValueError(
                "fixture snapshot must include fixture_provenance; "
                "refusing unlabeled or manufactured production context"
            )
        if "snapshot" in raw:
            body = raw["snapshot"]
            if not isinstance(body, dict):
                raise ValueError("fixture snapshot body must be an object")
            snapshot = dict(body)
        else:
            snapshot = {key: value for key, value in raw.items() if key != "fixture_provenance"}
        snapshot["fixture_provenance"] = provenance
        if snapshot.get("data_source") == "postgres_projection":
            raise ValueError(
                "fixture snapshot must not claim data_source=postgres_projection; "
                "that source is reserved for verified projection adapters"
            )
        snapshot.setdefault("data_source", "local_rehearsal_fixture")
        self.provenance = provenance
        self._snapshot = snapshot

    def current(self) -> dict[str, Any]:
        return dict(self._snapshot)


def run_shadow_consumer(
    conn: PsycopgConnection,
    *,
    worker_id: str,
    client: Any,
    snapshot_provider: SnapshotProvider,
    media_loader: MediaLoader,
    once: bool = True,
    lease_seconds: int = 45,
    timeout: float = DEFAULT_TIMEOUT,
) -> WorkerResult:
    """Shadow consumer. Callers must supply real or explicitly labeled fixture adapters."""

    if snapshot_provider is None or media_loader is None:
        raise ValueError("snapshot_provider and media_loader are required")
    result = process_shadow_one(
        conn,
        worker_id=worker_id,
        client=client,
        media_loader=media_loader,
        snapshot_provider=snapshot_provider,
        timeout=timeout,
        lease_seconds=lease_seconds,
    )
    if once:
        return result
    while result.status != "skipped":
        result = process_shadow_one(
            conn,
            worker_id=worker_id,
            client=client,
            media_loader=media_loader,
            snapshot_provider=snapshot_provider,
            timeout=timeout,
            lease_seconds=lease_seconds,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "G2 shadow consumer. No live orders. Uses existing RealHermesClient. "
            "Does not cut over execution (G3)."
        )
    )
    run_mode = parser.add_mutually_exclusive_group(required=True)
    run_mode.add_argument(
        "--once",
        action="store_true",
        help="claim and process one task, then exit",
    )
    run_mode.add_argument(
        "--loop",
        action="store_true",
        help="keep claiming until the queue is idle, then exit",
    )
    adapters = parser.add_mutually_exclusive_group(required=True)
    adapters.add_argument(
        "--projection-adapters",
        action="store_true",
        help=(
            "use existing smoke_replay Postgres snapshot + filesystem media "
            "adapters; does not enable live orders or deployment"
        ),
    )
    adapters.add_argument(
        "--fixture-snapshot-json",
        help="local rehearsal snapshot JSON; must include fixture_provenance",
    )
    parser.add_argument(
        "--fixture-media-root",
        default=None,
        help="required with --fixture-snapshot-json; root for media object_key paths",
    )
    parser.add_argument("--worker-id", default="shadow-worker")
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    import psycopg2
    from hermes_client import RealHermesClient

    client = RealHermesClient()
    if not client.configured:
        raise SystemExit(
            "HERMES_API_URL / HERMES_API_KEY / HERMES_MODEL are required"
        )
    if args.fixture_snapshot_json:
        if not args.fixture_media_root:
            raise SystemExit(
                "--fixture-media-root is required with --fixture-snapshot-json"
            )
        snapshot_provider: SnapshotProvider = FixtureSnapshotProvider(
            args.fixture_snapshot_json
        )
        media_loader: MediaLoader = FilesystemMediaLoader(root=args.fixture_media_root)
        conn = psycopg2.connect(database_url)
    else:
        conn = psycopg2.connect(database_url)
        snapshot_provider = PostgresSnapshotProvider(conn)
        media_loader = FilesystemMediaLoader()
    try:
        result = run_shadow_consumer(
            conn,
            worker_id=args.worker_id,
            client=client,
            snapshot_provider=snapshot_provider,
            media_loader=media_loader,
            once=bool(args.once),
        )
    finally:
        conn.close()
    print(result.status)
    return 0 if result.status in {"shadow_dispatched", "skipped", "expired"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
