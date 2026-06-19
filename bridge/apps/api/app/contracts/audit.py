from __future__ import annotations

from typing import TypedDict


class AuditEvent(TypedDict):
    event_id: str
    event_type: str
    actor_id: str
    actor_role: str
    occurred_at: str
    request_id: str
    correlation_id: str
    reason: str
    payload_redacted: dict
    result: str
