from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4


SECRET_KEY_RE = re.compile(
    r"(authorization|cookie|password|passwd|secret|token|jwt|api[_-]?key|exchange[_-]?key)",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(password|passwd|secret|token|jwt|api[_-]?key|exchange[_-]?key)\s*[:=]\s*([^,\s]+)"
)


@dataclass(frozen=True)
class AuditEvent:
    event_type: str
    aggregate_type: str
    aggregate_id: str
    actor: str
    payload: dict[str, Any]
    audit_event_id: UUID = field(default_factory=uuid4)
    trace_id: UUID | None = None


class AuditEventWriter:
    def __init__(self, conn: Any):
        self.conn = conn

    def write(self, event: AuditEvent) -> AuditEvent:
        _load_repository_module()
        redacted_payload = redact_payload(event.payload)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_events (
                    audit_event_id, event_type, aggregate_type, aggregate_id,
                    actor, trace_id, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(event.audit_event_id),
                    event.event_type,
                    event.aggregate_type,
                    event.aggregate_id,
                    event.actor,
                    str(event.trace_id) if event.trace_id else None,
                    _json(redacted_payload),
                ),
            )
        return event


def record_audit_event(
    conn: Any,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    actor: str,
    payload: Mapping[str, Any],
    trace_id: UUID | str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        actor=actor,
        trace_id=_trace_uuid(trace_id),
        payload=dict(payload),
    )
    return AuditEventWriter(conn).write(event)


def dangerous_operation_payload(
    *,
    request_id: str,
    reason: str,
    actor: Mapping[str, str],
    operation: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return redact_payload(
        {
            "request_id": request_id,
            "reason": reason,
            "actor": {"actor_id": actor["actor_id"], "role": actor["role"]},
            "operation": operation,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dangerous_operation": True,
            "payload": dict(payload or {}),
        }
    )


def redact_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    if isinstance(value, str):
        return SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return value


def _trace_uuid(value: UUID | str | None) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _json(value: Any) -> Any:
    from psycopg2.extras import Json

    return Json(value)


def _load_repository_module() -> Any:
    try:
        from db import repository
    except ImportError as exc:
        raise RuntimeError("control-plane db.repository is required for audit writes") from exc
    return repository
