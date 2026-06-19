from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


@dataclass
class AuditEventRecord:
    event_type: str
    actor_id: str
    actor_role: str
    request_id: str
    correlation_id: str
    reason: str
    payload_redacted: dict
    result: str = "success"
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
