from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable
from uuid import UUID

from .contracts import ExecutionEventEnvelopeV1


DEFAULT_SPOOL_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_SPOOL_DEGRADED_RATIO = 0.8
DEFAULT_SPOOL_SEEN_EVENT_LIMIT = 4096
EVENT_TYPE_ORDER = {
    "OrderSubmitted": 10,
    "OrderAccepted": 20,
    "OrderPendingUpdate": 30,
    "OrderUpdated": 40,
    "OrderFilled": 50,
    "OrderPendingCancel": 60,
    "OrderCanceled": 70,
    "OrderRejected": 80,
    "OrderExpired": 90,
}


class SpoolCapacityError(RuntimeError):
    """Raised before a spool mutation would exceed its durable byte budget."""


class SpoolCorruptionError(RuntimeError):
    """Raised when a complete WAL record is corrupt."""


class JsonExecutionSpool:
    """Durable ordered spool for execution events.

    The file keeps pending envelopes plus a durable ``seen_event_ids`` set. Acked
    events leave ``pending`` only after the sink returns their id.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_SPOOL_MAX_BYTES,
        degraded_ratio: float = DEFAULT_SPOOL_DEGRADED_RATIO,
        seen_event_limit: int = DEFAULT_SPOOL_SEEN_EVENT_LIMIT,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if degraded_ratio <= 0 or degraded_ratio >= 1:
            raise ValueError("degraded_ratio must be between zero and one")
        if seen_event_limit < 1:
            raise ValueError("seen_event_limit must be positive")
        self._path = Path(path)
        self._max_bytes = int(max_bytes)
        self._degraded_ratio = float(degraded_ratio)
        self._seen_event_limit = int(seen_event_limit)
        self._size_bytes = 0
        self._last_append_size = 0
        self._pending: list[ExecutionEventEnvelopeV1] = []
        self._seen_event_ids: dict[str, None] = {}
        self._lock = RLock()
        self._load()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._size_bytes

    @property
    def usage_ratio(self) -> float:
        return self._size_bytes / self._max_bytes

    @property
    def is_degraded(self) -> bool:
        with self._lock:
            threshold_reached = (
                self._size_bytes / self._max_bytes >= self._degraded_ratio
            )
            next_append_would_overflow = (
                self._last_append_size > 0
                and self._size_bytes + self._last_append_size > self._max_bytes
            )
            return threshold_reached or next_append_would_overflow

    @property
    def seen_event_count(self) -> int:
        with self._lock:
            return len(self._seen_event_ids)

    def append_once(self, envelope: ExecutionEventEnvelopeV1) -> bool:
        with self._lock:
            if envelope.event_id in self._seen_event_ids:
                return False
            encoded = _encode_record(
                {
                    "op": "append",
                    "event": _envelope_to_payload(envelope),
                }
            )
            self._assert_capacity_bytes(self._size_bytes + len(encoded))
            self._append_encoded(encoded)
            self._pending.append(envelope)
            self._pending.sort(key=_event_order_key)
            self._seen_event_ids[envelope.event_id] = None
            self._size_bytes += len(encoded)
            self._last_append_size = max(self._last_append_size, len(encoded))
            return True

    def pending_events(
        self, limit: int | None = None
    ) -> list[ExecutionEventEnvelopeV1]:
        with self._lock:
            if limit is None:
                return list(self._pending)
            return list(self._pending[:limit])

    def mark_acked(self, event_ids: Iterable[str]) -> None:
        acked = set(event_ids)
        if not acked:
            return
        with self._lock:
            pending = [
                event for event in self._pending if event.event_id not in acked
            ]
            self._commit_state(
                pending=pending,
                seen_event_ids=dict(self._seen_event_ids),
            )

    def _load(self) -> None:
        if not self._path.exists():
            return
        encoded = self._path.read_bytes()
        if not encoded:
            return
        records, legacy_snapshot, durable_size = _decode_records(encoded)
        if durable_size < len(encoded):
            self._truncate_torn_tail(durable_size)
            encoded = encoded[:durable_size]
        for record in records:
            if record.get("op") == "append":
                self._last_append_size = max(
                    self._last_append_size,
                    len(_encode_record(record)),
                )
            self._apply_record(record)
        compacted_seen_event_ids = self._compact_seen_event_ids(
            pending=self._pending,
            seen_event_ids=self._seen_event_ids,
        )
        needs_compaction = (
            list(compacted_seen_event_ids) != list(self._seen_event_ids)
        )
        self._seen_event_ids = compacted_seen_event_ids
        self._pending.sort(key=_event_order_key)
        self._assert_capacity_bytes(len(encoded))
        self._size_bytes = len(encoded)
        if legacy_snapshot or needs_compaction:
            self._commit_state(
                pending=self._pending,
                seen_event_ids=self._seen_event_ids,
            )

    def _apply_record(self, record: dict[str, Any]) -> None:
        operation = str(record.get("op") or "snapshot")
        if operation == "snapshot":
            self._seen_event_ids = {
                str(event_id): None
                for event_id in record.get("seen_event_ids", [])
            }
            self._pending = [
                _envelope_from_payload(item)
                for item in record.get("pending", [])
            ]
            for event in self._pending:
                self._seen_event_ids[event.event_id] = None
            return
        if operation == "append":
            envelope = _envelope_from_payload(record["event"])
            if envelope.event_id in self._seen_event_ids:
                return
            self._pending.append(envelope)
            self._seen_event_ids[envelope.event_id] = None
            return
        raise ValueError(f"unknown execution spool operation: {operation}")

    def _commit_state(
        self,
        *,
        pending: list[ExecutionEventEnvelopeV1],
        seen_event_ids: dict[str, None],
    ) -> None:
        compacted_seen_event_ids = self._compact_seen_event_ids(
            pending=pending,
            seen_event_ids=seen_event_ids,
        )
        payload = {
            "op": "snapshot",
            "schema_version": "2.0",
            "seen_event_ids": list(compacted_seen_event_ids),
            "pending": [_envelope_to_payload(event) for event in pending],
        }
        encoded = _encode_record(payload)
        self._assert_capacity(encoded)
        self._save_encoded(encoded)
        self._pending = pending
        self._seen_event_ids = compacted_seen_event_ids
        self._size_bytes = len(encoded)

    def _compact_seen_event_ids(
        self,
        *,
        pending: list[ExecutionEventEnvelopeV1],
        seen_event_ids: dict[str, None],
    ) -> dict[str, None]:
        pending_ids = {event.event_id for event in pending}
        acked_ids = [
            event_id
            for event_id in seen_event_ids
            if event_id not in pending_ids
        ]
        retained_acked_ids = acked_ids[-self._seen_event_limit :]
        compacted = {event_id: None for event_id in retained_acked_ids}
        for event in pending:
            compacted[event.event_id] = None
        return compacted

    def _assert_capacity(self, encoded: bytes) -> None:
        self._assert_capacity_bytes(len(encoded))

    def _assert_capacity_bytes(self, size_bytes: int) -> None:
        if size_bytes <= self._max_bytes:
            return
        raise SpoolCapacityError(
            "execution event spool capacity exceeded: "
            f"{size_bytes} > {self._max_bytes} bytes"
        )

    def _append_encoded(self, encoded: bytes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        created = not self._path.exists()
        with self._path.open("ab") as wal:
            wal.write(encoded)
            wal.flush()
            os.fsync(wal.fileno())
        if created:
            _fsync_directory(self._path.parent)

    def _save_encoded(self, encoded: bytes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(encoded)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
            _fsync_directory(self._path.parent)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _truncate_torn_tail(self, durable_size: int) -> None:
        with self._path.open("r+b") as wal:
            wal.truncate(durable_size)
            wal.flush()
            os.fsync(wal.fileno())
        _fsync_directory(self._path.parent)


def _encode_record(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _decode_records(
    encoded: bytes,
) -> tuple[list[dict[str, Any]], bool, int]:
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _decode_wal_records(encoded)
    if not isinstance(payload, dict):
        raise TypeError("execution spool root must be a JSON object")
    legacy_snapshot = "op" not in payload
    if legacy_snapshot:
        return [payload], True, len(encoded)
    if encoded.endswith(b"\n"):
        return [payload], False, len(encoded)
    return [], False, 0


def _decode_wal_records(
    encoded: bytes,
) -> tuple[list[dict[str, Any]], bool, int]:
    records: list[dict[str, Any]] = []
    durable_size = 0
    for record_index, line in enumerate(
        encoded.splitlines(keepends=True),
        start=1,
    ):
        if not line.endswith(b"\n"):
            return records, False, durable_size
        raw_record = line[:-1]
        if not raw_record:
            raise SpoolCorruptionError(
                "execution spool WAL record "
                f"{record_index} is empty"
            )
        try:
            record = json.loads(raw_record)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpoolCorruptionError(
                "execution spool WAL record "
                f"{record_index} is corrupt"
            ) from exc
        if not isinstance(record, dict):
            raise SpoolCorruptionError(
                "execution spool WAL record "
                f"{record_index} must be an object"
            )
        records.append(record)
        durable_size += len(line)
    return records, False, durable_size


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


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


def _event_order_key(
    envelope: ExecutionEventEnvelopeV1,
) -> tuple[datetime, int, str]:
    event_order = EVENT_TYPE_ORDER.get(envelope.event_type, 100)
    return envelope.ts_event, event_order, envelope.event_id


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
