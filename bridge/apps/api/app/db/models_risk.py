from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


@dataclass
class PairLockRecord:
    pair: str
    reason: str
    actor_id: str
    expires_at: str
    lock_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    released_at: str | bool = False
