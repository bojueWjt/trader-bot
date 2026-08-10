from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any


SCHEMA_VERSION = "1.1"
LEGACY_SCHEMA_VERSION = "1.0"
AMBIGUOUS_APPLY_ERROR = "ambiguous_apply_after_restart"
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
MAX_ERROR_JSON_BYTES = 96
COMMAND_ACK_STATUSES = frozenset(
    {
        "accepted",
        "completed",
        "failed",
    }
)
MAX_COMMAND_ACK_STATUS = "completed"


class CommandJournalPhase(str, Enum):
    APPLYING = "APPLYING"
    ACK_QUEUED = "ACK_QUEUED"
    ACKED = "ACKED"


class CommandJournalDurabilityUncertainError(OSError):
    """The replacement is visible, but its directory entry may not be durable."""

    committed = True
    durability_uncertain = True

    def __init__(self, path: Path, cause: OSError) -> None:
        super().__init__(
            "command journal replacement committed with durability uncertain "
            f"for {path}: {cause}"
        )
        self.path = path


@dataclass(frozen=True)
class CommandJournalRecord:
    command_id: str
    command_type: str
    phase: CommandJournalPhase
    status: str | bool = False
    error: str | bool = False
    acked_sequence: int | bool = False


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
        normalized_status = self._normalize_status(status)
        normalized_error = self._normalize_error(error)
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
            if existing.phase is CommandJournalPhase.ACKED:
                return existing
            record = CommandJournalRecord(
                command_id=existing.command_id,
                command_type=existing.command_type,
                phase=CommandJournalPhase.ACKED,
                status=existing.status,
                error=existing.error,
                acked_sequence=self._next_acked_sequence(),
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
            previous_records = dict(self._records)
            self._records.pop(normalized_id)
            try:
                self._save()
            except Exception as exc:
                if not _exception_committed(exc):
                    self._records = previous_records
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
            except Exception as exc:
                if not _exception_committed(exc):
                    self._records = previous_records
                raise
        return changed

    def _store_record(
        self,
        record: CommandJournalRecord,
    ) -> None:
        previous_records = dict(self._records)
        self._records[record.command_id] = record
        try:
            self._save()
        except Exception as exc:
            if not _exception_committed(exc):
                self._records = previous_records
            raise

    def _snapshot(self) -> tuple[CommandJournalRecord, ...]:
        return tuple(
            self._records[command_id]
            for command_id in sorted(self._records)
        )

    def _save(self) -> None:
        return

    def _normalize_status(self, status: Any) -> str:
        return _command_ack_status(status)

    def _normalize_error(self, error: Any) -> str | bool:
        return _optional_text(error)

    def _next_acked_sequence(self) -> int | bool:
        return False


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

    def _normalize_error(self, error: Any) -> str | bool:
        return _bounded_optional_text(
            error,
            max_json_bytes=MAX_ERROR_JSON_BYTES,
            digest_characters=64,
        )

    def _next_acked_sequence(self) -> int:
        sequences = [
            record.acked_sequence
            for record in self._records.values()
            if (
                record.phase is CommandJournalPhase.ACKED
                and isinstance(record.acked_sequence, int)
                and not isinstance(record.acked_sequence, bool)
            )
        ]
        if not sequences:
            return 1
        return max(sequences) + 1

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("command journal must be an object")
        schema_version = raw.get("schema_version")
        if schema_version not in {
            LEGACY_SCHEMA_VERSION,
            SCHEMA_VERSION,
        }:
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
        legacy_acked: list[CommandJournalRecord] = []
        for command_id, payload in commands.items():
            is_legacy = schema_version == LEGACY_SCHEMA_VERSION
            if (
                is_legacy
                and isinstance(payload, dict)
                and payload.get("phase") == CommandJournalPhase.ACKED.value
            ):
                legacy_acked.append(
                    _record_from_payload(
                        command_id,
                        payload,
                        require_acked_sequence=False,
                        bound_error=True,
                    )
                )
                continue
            record = _record_from_payload(
                command_id,
                payload,
                bound_error=is_legacy,
            )
            loaded[record.command_id] = record
        for sequence, record in enumerate(
            _legacy_acked_migration_order(legacy_acked),
            start=1,
        ):
            loaded[record.command_id] = replace(
                record,
                acked_sequence=sequence,
            )
        self._records = loaded
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        records, encoded = self._prepared_payload()
        try:
            self._write_payload(encoded)
        except CommandJournalDurabilityUncertainError:
            self._records = records
            raise
        self._records = records

    def _prepared_payload(
        self,
    ) -> tuple[dict[str, CommandJournalRecord], bytes]:
        records = _with_rebased_acked_sequences(self._records)
        while True:
            encoded = self._serialize_records(records)
            queued_reserved_records = (
                _with_terminal_reservations(records)
            )
            queued_reserved = self._serialize_records(
                queued_reserved_records
            )
            acked_reserved = self._serialize_records(
                _with_acked_reservations(
                    queued_reserved_records
                )
            )
            if (
                len(encoded) <= self._max_bytes
                and len(queued_reserved) <= self._max_bytes
                and len(acked_reserved) <= self._max_bytes
            ):
                return records, encoded
            acked_id = _first_acked_command_id(records)
            if acked_id is False:
                raise ValueError(
                    "command journal exceeds max_bytes before "
                    "terminal state can be reserved"
                )
            records.pop(acked_id)
            records = _with_rebased_acked_sequences(records)

    def _serialize_records(
        self,
        records: dict[str, CommandJournalRecord],
    ) -> bytes:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "account_id": self._account_id,
            "node_id": self._node_id,
            "commands": {
                command_id: _record_to_payload(record)
                for command_id, record in sorted(records.items())
            },
        }
        serialized = json.dumps(
            payload,
            separators=(",", ":"),
        )
        return (serialized + "\n").encode("utf-8")

    def _write_payload(self, encoded: bytes) -> None:
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
            try:
                _fsync_directory(self._path.parent)
            except OSError as exc:
                raise CommandJournalDurabilityUncertainError(
                    self._path,
                    exc,
                ) from exc
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def _record_from_payload(
    command_id: Any,
    payload: Any,
    *,
    require_acked_sequence: bool = True,
    bound_error: bool = False,
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
    status: str | bool = False
    raw_error = payload.get("error", False)
    error = _optional_text(raw_error)
    if bound_error:
        error = _bounded_optional_text(
            raw_error,
            max_json_bytes=MAX_ERROR_JSON_BYTES,
            digest_characters=64,
        )
    acked_sequence: int | bool = False
    if phase is not CommandJournalPhase.APPLYING:
        status = _command_ack_status(payload.get("status", False))
    if phase is CommandJournalPhase.ACKED:
        raw_sequence = payload.get("acked_sequence")
        if raw_sequence is None and not require_acked_sequence:
            raw_sequence = False
        if (
            require_acked_sequence
            and (
                isinstance(raw_sequence, bool)
                or not isinstance(raw_sequence, int)
                or raw_sequence <= 0
            )
        ):
            raise ValueError(
                f"command journal record {normalized_id!r} "
                "missing acked_sequence"
            )
        if require_acked_sequence:
            acked_sequence = raw_sequence
    return CommandJournalRecord(
        command_id=normalized_id,
        command_type=_required_text(
            "record.command_type",
            payload.get("command_type"),
        ),
        phase=phase,
        status=status,
        error=error,
        acked_sequence=acked_sequence,
    )


def _record_to_payload(
    record: CommandJournalRecord,
) -> dict[str, Any]:
    if record.phase is CommandJournalPhase.ACKED:
        return {
            "command_id": record.command_id,
            "command_type": record.command_type,
            "phase": record.phase.value,
            "status": record.status,
            "error": record.error,
            "acked_sequence": record.acked_sequence,
        }
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


def _exception_committed(exc: BaseException) -> bool:
    return isinstance(exc, CommandJournalDurabilityUncertainError)


def _bounded_optional_text(
    value: Any,
    *,
    max_json_bytes: int,
    digest_characters: int,
) -> str | bool:
    normalized = _optional_text(value)
    if normalized is False:
        return False
    return _bounded_text(
        normalized,
        max_json_bytes=max_json_bytes,
        digest_characters=digest_characters,
    )


def _bounded_text(
    value: str,
    *,
    max_json_bytes: int,
    digest_characters: int,
) -> str:
    if _json_text_size(value) <= max_json_bytes:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    suffix = (
        "...[truncated:sha256="
        f"{digest[:digest_characters]}]"
    )
    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = value[:middle] + suffix
        if _json_text_size(candidate) <= max_json_bytes:
            low = middle
        else:
            high = middle - 1
    bounded = value[:low] + suffix
    if _json_text_size(bounded) > max_json_bytes:
        raise ValueError("bounded journal text budget is too small")
    return bounded


def _json_text_size(value: str) -> int:
    return len(json.dumps(value).encode("utf-8"))


def _command_ack_status(value: Any) -> str:
    normalized = _required_text("status", value)
    if normalized not in COMMAND_ACK_STATUSES:
        raise ValueError(
            "status must be accepted, completed, or failed"
        )
    return normalized


def _with_terminal_reservations(
    records: dict[str, CommandJournalRecord],
) -> dict[str, CommandJournalRecord]:
    reserved = {}
    error = "e" * (MAX_ERROR_JSON_BYTES - 2)
    for command_id, record in records.items():
        terminal = record
        if record.phase is CommandJournalPhase.APPLYING:
            terminal = CommandJournalRecord(
                command_id=record.command_id,
                command_type=record.command_type,
                phase=CommandJournalPhase.ACK_QUEUED,
                status=MAX_COMMAND_ACK_STATUS,
                error=error,
            )
        reserved[command_id] = terminal
    return reserved


def _with_acked_reservations(
    records: dict[str, CommandJournalRecord],
) -> dict[str, CommandJournalRecord]:
    reserved = {}
    next_sequence = 1
    for record in records.values():
        if record.phase is not CommandJournalPhase.ACKED:
            continue
        next_sequence = max(
            next_sequence,
            int(record.acked_sequence) + 1,
        )
    for command_id, record in records.items():
        terminal = record
        if record.phase is not CommandJournalPhase.ACKED:
            terminal = CommandJournalRecord(
                command_id=record.command_id,
                command_type=record.command_type,
                phase=CommandJournalPhase.ACKED,
                status=record.status,
                error=record.error,
                acked_sequence=next_sequence,
            )
            next_sequence += 1
        reserved[command_id] = terminal
    return reserved


def _with_rebased_acked_sequences(
    records: dict[str, CommandJournalRecord],
) -> dict[str, CommandJournalRecord]:
    ordered = sorted(
        (
            record
            for record in records.values()
            if record.phase is CommandJournalPhase.ACKED
        ),
        key=lambda record: int(record.acked_sequence),
    )
    sequences = {
        record.command_id: sequence
        for sequence, record in enumerate(ordered, start=1)
    }
    rebased = {}
    for command_id, record in records.items():
        sequence = sequences.get(command_id)
        if sequence is None or record.acked_sequence == sequence:
            rebased[command_id] = record
            continue
        rebased[command_id] = replace(
            record,
            acked_sequence=sequence,
        )
    return rebased


def _legacy_acked_migration_order(
    records: list[CommandJournalRecord],
) -> tuple[CommandJournalRecord, ...]:
    # v1.0 has no ACK timestamp; content order avoids treating JSON keys as time.
    return tuple(
        sorted(
            records,
            key=lambda record: (
                hashlib.sha256(
                    record.command_id.encode("utf-8")
                ).hexdigest(),
                record.command_id,
            ),
        )
    )


def _first_acked_command_id(
    records: dict[str, CommandJournalRecord],
) -> str | bool:
    acked = [
        record
        for record in records.values()
        if record.phase is CommandJournalPhase.ACKED
    ]
    if not acked:
        return False
    oldest = min(
        acked,
        key=lambda record: int(record.acked_sequence),
    )
    return oldest.command_id


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
