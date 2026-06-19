from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from app.contracts.message_processing import MessageProcessingStatus


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MessageProcessingEvent:
    message_id: str
    from_status: MessageProcessingStatus | bool
    to_status: MessageProcessingStatus
    actor: str
    reason: str = ""
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class MessageProcessingTransitionResult:
    ok: bool
    message_id: str
    status: MessageProcessingStatus
    reason: str = ""
