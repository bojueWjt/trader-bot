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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from psycopg2.extras import Json
from psycopg2.extensions import connection as PsycopgConnection

# A-02 connection helper + A-04 queue claim live next to / under services/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_REPO_ROOT / "services" / "control-plane" / "db"),
    str(Path(__file__).resolve().parent / "queue"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import claims  # noqa: E402  (services/hermes-worker/queue/claims.py)
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
    status: str  # "succeeded" | "skipped" | failure code
    processing_run_id: str | None = None
    raw_message_id: str | None = None
    decision_id: str | None = None
    detail: str | None = None


def _load_schema_validator():
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


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
) -> WorkerResult:
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
        if snapshot_problem is not None:
            return _fail(conn, run_id, raw_message_id, snapshot_problem, "system snapshot unusable")

        request = HermesRequest(
            raw_message_id=raw_message_id,
            text=message["message_text"] or "",
            images=images,
            referenced_messages=_referenced_messages(message),
            recent_context=_recent_context(conn, message, raw_message_id),
            system_snapshot=snapshot,
        )

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
    """Keep the model's semantics; force authoritative identity + pinned model block."""
    decision = dict(candidate)
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


def _persist_success(
    conn: PsycopgConnection,
    *,
    run_id: str,
    raw_message_id: str,
    context_snapshot_id: str,
    snapshot: dict[str, Any],
    decision: dict[str, Any],
    response_sha256: str,
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


def _utc_now_iso() -> str:
    # imported lazily; Date.now-style call kept out of import time
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
