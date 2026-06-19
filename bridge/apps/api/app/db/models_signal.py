from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from app.services.signal_parser import ParsedSignal, SignalStatus


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SignalRecord:
    signal: ParsedSignal
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class SignalLifecycleEvent:
    signal_id: str
    from_status: SignalStatus | bool
    to_status: SignalStatus
    actor: str
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class SignalTransitionResult:
    ok: bool
    signal_id: str
    status: SignalStatus
    reason: str = ""
