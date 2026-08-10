from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any


SCHEMA_VERSION = "1.0"
AMBIGUOUS_APPLY_ERROR = "ambiguous_apply_after_restart"
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


class CommandJournalPhase(str, Enum):
    APPLYING = "APPLYING"
    ACK_QUEUED = "ACK_QUEUED"
    ACKED = "ACKED"


@dataclass(frozen=True)
class CommandJournalRecord:
    command_id: str
    command_type: str
    phase: CommandJournalPhase
    status: str | bool = False
    error: str | bool = False


class InMemoryCommandJournal:
    """Process-local implementation used by isolated actor tests."""

    durable = False

    def __init__(self) -> None:
        self._records: dict[str, CommandJournalRecord] = {}
        self._lock = RLock()

    def recover(self) -> tuple[CommandJournalRecord, ...]:
        with self._lock:
            self._recover_ambiguous()
            return self._snapshot()

    def get(self, command_id: str) -> CommandJournalRecord | bool:
        with self._lock:
            return self._records.get(str(command_id), False)

    def begin(
        self,
        command_id: str,
        command_type: str,
    ) -> CommandJournalRecord:
        normalized_id = _required_text("command_id", command_id)
        normalized_type = _required_text(
            "command_type",
            command_type,
        )
        with self._lock:
            existing = self._records.get(normalized_id)
            if existing is not None:
                if existing.command_type != normalized_type:
                    raise ValueError(
                        "command_id reused with a different command_type"
                    )
                return existing
            record = CommandJournalRecord(
                command_id=normalized_id,
                command_type=normalized_type,
                phase=CommandJournalPhase.APPLYING,
            )
            self._store_record(record)
            return record

    def complete(
        self,
        command_id: str,
        *,
        status: str,
        error: str | bool,
    ) -> CommandJournalRecord:
        normalized_id = _required_text("command_id", command_id)
        normalized_status = _required_text("status", status)
        normalized_error = _optional_text(error)
        with self._lock:
            existing = self._records.get(normalized_id)
            if existing is None:
                raise ValueError(
                    f"unknown command_id {normalized_id!r}"
                )
            if existing.phase is CommandJournalPhase.ACKED:
                return existing
            if existing.phase is CommandJournalPhase.ACK_QUEUED:
                if (
                    existing.status != normalized_status
                    or existing.error != normalized_error
                ):
                    raise ValueError(
                        "command ACK result changed after persistence"
                    )
                return existing
            record = CommandJournalRecord(
                command_id=existing.command_id,
                command_type=existing.command_type,
                phase=CommandJournalPhase.ACK_QUEUED,
                status=normalized_status,
                error=normalized_error,
            )
            self._store_record(record)
            return record

    def mark_acked(
        self,
        command_id: str,
    ) -> CommandJournalRecord:
        normalized_id = _required_text("command_id", command_id)
        with self._lock:
            existing = self._records.get(normalized_id)
            if existing is None:
                raise ValueError(
                    f"unknown command_id {normalized_id!r}"
                )
            if existing.phase is CommandJournalPhase.APPLYING:
                raise ValueError(
                    "cannot mark APPLYING command as ACKED"
                )
            record = CommandJournalRecord(
                command_id=existing.command_id,
                command_type=existing.command_type,
                phase=CommandJournalPhase.ACKED,
                status=existing.status,
                error=existing.error,
            )
            self._store_record(record)
            return record

    def discard_unapplied(self, command_id: str) -> bool:
        normalized_id = _required_text("command_id", command_id)
        with self._lock:
            existing = self._records.get(normalized_id)
            if existing is None:
                return False
            if existing.phase is not CommandJournalPhase.APPLYING:
                return False
            self._records.pop(normalized_id)
            try:
                self._save()
            except Exception:
                self._records[normalized_id] = existing
                raise
            return True

    def _recover_ambiguous(self) -> bool:
        previous_records = dict(self._records)
        changed = False
        for command_id, record in tuple(self._records.items()):
            if record.phase is not CommandJournalPhase.APPLYING:
                continue
            self._records[command_id] = CommandJournalRecord(
                command_id=record.command_id,
                command_type=record.command_type,
                phase=CommandJournalPhase.ACK_QUEUED,
                status="failed",
                error=(
                    f"{AMBIGUOUS_APPLY_ERROR}:"
                    f"{record.command_type}"
                ),
            )
            changed = True
        if changed:
            try:
                self._save()
            except Exception:
                self._records = previous_records
                raise
        return changed

    def _store_record(
        self,
        record: CommandJournalRecord,
    ) -> None:
        previous = self._records.get(record.command_id)
        self._records[record.command_id] = record
        try:
            self._save()
        except Exception:
            if previous is None:
                self._records.pop(record.command_id, None)
            else:
                self._records[record.command_id] = previous
            raise

    def _snapshot(self) -> tuple[CommandJournalRecord, ...]:
        return tuple(
            self._records[command_id]
            for command_id in sorted(self._records)
        )

    def _save(self) -> None:
        return


class DurableCommandJournal(InMemoryCommandJournal):
    """Atomic account-scoped journal for command apply and final ACK state."""

    durable = True

    def __init__(
        self,
        path: str | Path,
        *,
        account_id: str,
        node_id: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._account_id = _required_text(
            "account_id",
            account_id,
        )
        self._node_id = _required_text("node_id", node_id)
        self._records = {}
        self._lock = RLock()
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def _load(self) -> None:
        if not self._path.exists():
            return
        if self._path.stat().st_size > self._max_bytes:
            raise ValueError("command journal exceeds max_bytes")
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("command journal must be an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                "unsupported command journal schema_version"
            )
        if raw.get("account_id") != self._account_id:
            raise ValueError("command journal account_id mismatch")
        if raw.get("node_id") != self._node_id:
            raise ValueError("command journal node_id mismatch")
        commands = raw.get("commands", {})
        if not isinstance(commands, dict):
            raise ValueError(
                "command journal commands must be an object"
            )
        loaded: dict[str, CommandJournalRecord] = {}
        for command_id, payload in commands.items():
            record = _record_from_payload(command_id, payload)
            loaded[record.command_id] = record
        self._records = loaded

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "account_id": self._account_id,
            "node_id": self._node_id,
            "commands": {
                command_id: _record_to_payload(record)
                for command_id, record in sorted(
                    self._records.items()
                )
            },
        }
        serialized = (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        encoded = serialized.encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise ValueError("command journal exceeds max_bytes")
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
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


def _record_from_payload(
    command_id: Any,
    payload: Any,
) -> CommandJournalRecord:
    normalized_id = _required_text("command_id", command_id)
    if not isinstance(payload, dict):
        raise ValueError(
            f"command journal record {normalized_id!r} must be an object"
        )
    payload_id = _required_text(
        "record.command_id",
        payload.get("command_id"),
    )
    if payload_id != normalized_id:
        raise ValueError(
            f"command journal key mismatch for {normalized_id!r}"
        )
    phase = CommandJournalPhase(
        _required_text("record.phase", payload.get("phase"))
    )
    status = _optional_text(payload.get("status", False))
    error = _optional_text(payload.get("error", False))
    if phase is not CommandJournalPhase.APPLYING and status is False:
        raise ValueError(
            f"command journal record {normalized_id!r} missing status"
        )
    return CommandJournalRecord(
        command_id=normalized_id,
        command_type=_required_text(
            "record.command_type",
            payload.get("command_type"),
        ),
        phase=phase,
        status=status,
        error=error,
    )


def _record_to_payload(
    record: CommandJournalRecord,
) -> dict[str, Any]:
    return {
        "command_id": record.command_id,
        "command_type": record.command_type,
        "phase": record.phase.value,
        "status": record.status,
        "error": record.error,
    }


def _required_text(name: str, value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _optional_text(value: Any) -> str | bool:
    if value is False or value is None:
        return False
    normalized = str(value).strip()
    if not normalized:
        return False
    return normalized


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
