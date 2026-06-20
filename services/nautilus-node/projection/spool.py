from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from .contracts import ExecutionEventEnvelopeV1


class JsonExecutionSpool:
    """Durable ordered spool for execution events.

    The file keeps pending envelopes plus a durable ``seen_event_ids`` set. Acked
    events leave ``pending`` only after the sink returns their id.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._pending: list[ExecutionEventEnvelopeV1] = []
        self._seen_event_ids: set[str] = set()
        self._load()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def append_once(self, envelope: ExecutionEventEnvelopeV1) -> bool:
        if envelope.event_id in self._seen_event_ids:
            return False
        self._seen_event_ids.add(envelope.event_id)
        self._pending.append(envelope)
        self._pending.sort(key=lambda item: (item.ts_event, item.event_id))
        self._save()
        return True

    def pending_events(self, limit: int | None = None) -> list[ExecutionEventEnvelopeV1]:
        if limit is None:
            return list(self._pending)
        return list(self._pending[:limit])

    def mark_acked(self, event_ids: Iterable[str]) -> None:
        acked = set(event_ids)
        if not acked:
            return
        self._pending = [
            event for event in self._pending if event.event_id not in acked
        ]
        self._save()

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        self._seen_event_ids = set(raw.get("seen_event_ids", []))
        self._pending = [
            _envelope_from_payload(item) for item in raw.get("pending", [])
        ]
        for event in self._pending:
            self._seen_event_ids.add(event.event_id)
        self._pending.sort(key=lambda item: (item.ts_event, item.event_id))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "seen_event_ids": sorted(self._seen_event_ids),
            "pending": [_envelope_to_payload(event) for event in self._pending],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(payload, tmp, sort_keys=True)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def _envelope_to_payload(envelope: ExecutionEventEnvelopeV1) -> dict[str, Any]:
    if hasattr(envelope, "model_dump"):
        payload = envelope.model_dump(mode="python")
    else:
        payload = dict(envelope.__dict__)
    result: dict[str, Any] = dict(payload)
    for key in ("ts_event", "ts_ingest"):
        value = result[key]
        if isinstance(value, datetime):
            result[key] = _ensure_aware(value).isoformat()
    if result.get("intent_id") is not None:
        result["intent_id"] = str(result["intent_id"])
    return result


def _envelope_from_payload(payload: dict[str, Any]) -> ExecutionEventEnvelopeV1:
    values = dict(payload)
    values["ts_event"] = _datetime_from_iso(values["ts_event"])
    values["ts_ingest"] = _datetime_from_iso(values["ts_ingest"])
    if values.get("intent_id") is not None:
        values["intent_id"] = UUID(str(values["intent_id"]))
    return ExecutionEventEnvelopeV1(**values)


def _datetime_from_iso(value: str) -> datetime:
    return _ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
