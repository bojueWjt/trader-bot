from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MessageProcessingStatus(str, Enum):
    RECEIVED = "received"
    DB_SAVED = "db_saved"
    CRON_CREATED = "cron_created"
    CRON_STARTED = "cron_started"
    CRON_OUTPUT = "cron_output"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass
class MessageProcessingRecord:
    message_id: str
    source: str = "telegram"
    channel_id: str = ""
    signal_id: str = ""
    status: MessageProcessingStatus = MessageProcessingStatus.RECEIVED
    cron_job_id: str = ""
    output_path: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
