from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4


SECRET_KEY_PATTERN = r"(authorization|cookie|password|passwd|secret|token|jwt|api[_-]?key|exchange[_-]?key)"
SECRET_VALUE_PATTERNS = [
    re.compile(r"(?i)(password|passwd|secret|token|jwt|api[_-]?key|exchange[_-]?key)\s*[:=]\s*([^,\s]+)"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
]


def _is_secret_key(key: str) -> bool:
    return bool(re.search(SECRET_KEY_PATTERN, key, re.IGNORECASE))


def _redact_secret_values(value: str) -> str:
    redacted = value
    for pattern in SECRET_VALUE_PATTERNS:
        if pattern.pattern.lower().startswith("(?i)bearer"):
            redacted = pattern.sub("Bearer [REDACTED]", redacted)
            continue
        redacted = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return redacted


def redact_payload(payload):
    if isinstance(payload, dict):
        redacted = {}
        for key, value in payload.items():
            if _is_secret_key(str(key)):
                redacted[key] = "[REDACTED]"
                continue
            redacted[key] = redact_payload(value)
        return redacted
    if isinstance(payload, list):
        items = []
        for item in payload:
            items.append(redact_payload(item))
        return items
    if isinstance(payload, str):
        return _redact_secret_values(payload)
    return payload


def create_audit_event(
    event_type: str,
    actor_id: str,
    actor_role: str,
    request_id: str,
    correlation_id: str,
    reason: str,
    payload: dict | None = None,
    result: str = "success",
) -> dict:
    if not request_id:
        raise ValueError("request_id is required")
    if not reason or not reason.strip():
        raise ValueError("reason is required")
    if payload is None:
        payload = {}
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "correlation_id": correlation_id,
        "reason": reason,
        "payload_redacted": redact_payload(payload),
        "result": result,
    }


class AuditLog:
    def __init__(self):
        self._events: list[dict] = []

    def record(
        self,
        event_type: str,
        actor_id: str,
        actor_role: str,
        request_id: str,
        correlation_id: str,
        reason: str,
        payload: dict | None = None,
        result: str = "success",
    ) -> dict:
        event = create_audit_event(
            event_type=event_type,
            actor_id=actor_id,
            actor_role=actor_role,
            request_id=request_id,
            correlation_id=correlation_id,
            reason=reason,
            payload=payload,
            result=result,
        )
        self._events.append(event)
        return event

    def list_events(self) -> list[dict]:
        return list(self._events)
