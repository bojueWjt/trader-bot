from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable, Literal, Mapping
from uuid import UUID

_PROCESS_LOCKS_GUARD = Lock()
_PROCESS_LOCKS: dict[str, RLock] = {}
_DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_CLIENT_ORDER_ID_PATTERN = re.compile(
    r"B(?P<intent_id>[0-9a-f]{32})(?P<sequence>[0-9]{2})"
)


class IntentExecutionState(str, Enum):
    RECEIVED = "received"
    DISPATCHED = "dispatched"
    EXCHANGE_CONFIRMED = "exchange_confirmed"
    REJECTED = "rejected"


class IntentRegisterResult(str, Enum):
    REGISTERED = "registered"
    REPLAY = "replay"
    INTENT_CONFLICT = "intent_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class IntentDispatchResult(str, Enum):
    READY = "ready"
    RECOVERY_REQUIRED = "recovery_required"
    EXCHANGE_CONFIRMED = "exchange_confirmed"
    REJECTED = "rejected"


class IntentExecutionInboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class IntentExecutionIdentity:
    account_id: str
    intent_id: str
    idempotency_key: str
    instrument_id: str
    action: str

    def normalized(self) -> IntentExecutionIdentity:
        account_id = str(self.account_id).strip()
        intent_id = str(UUID(str(self.intent_id)))
        idempotency_key = str(self.idempotency_key).strip()
        instrument_id = str(self.instrument_id).strip()
        action = str(self.action).strip().lower()
        if not account_id:
            raise IntentExecutionInboxError("account_id is required")
        if not idempotency_key:
            raise IntentExecutionInboxError(
                "idempotency_key is required"
            )
        if not instrument_id:
            raise IntentExecutionInboxError(
                "instrument_id is required"
            )
        if not action:
            raise IntentExecutionInboxError("action is required")
        return IntentExecutionIdentity(
            account_id=account_id,
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            instrument_id=instrument_id,
            action=action,
        )


@dataclass(frozen=True)
class IntentExecutionRecord:
    account_id: str
    intent_id: str
    idempotency_key: str
    instrument_id: str
    action: str
    state: IntentExecutionState
    intent_payload: dict[str, Any]
    client_order_ids: tuple[str, ...]
    exchange_confirmed_client_order_ids: tuple[str, ...]
    updated_at: str
    rejection_reason: str = ""

    def identity(self) -> IntentExecutionIdentity:
        return IntentExecutionIdentity(
            account_id=self.account_id,
            intent_id=self.intent_id,
            idempotency_key=self.idempotency_key,
            instrument_id=self.instrument_id,
            action=self.action,
        )


class JsonIntentExecutionInbox:
    """Durable intent receipt and exchange-confirmation barrier."""

    _VERSION = 1

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise ValueError("max_bytes must be a positive integer")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._lock_path = self._path.with_suffix(
            self._path.suffix + ".lock"
        )
        self._process_lock = _process_lock_for(self._path)

    def register_received(
        self,
        identity: IntentExecutionIdentity,
        intent_payload: Mapping[str, Any],
    ) -> IntentRegisterResult:
        normalized = identity.normalized()
        payload_copy = _json_object(intent_payload)

        def mutate(
            payload: dict[str, Any],
        ) -> tuple[IntentRegisterResult, bool]:
            exact = self._record_for_identity(payload, normalized)
            if exact is not False:
                if exact.identity() != normalized:
                    return IntentRegisterResult.INTENT_CONFLICT, False
                if exact.intent_payload != payload_copy:
                    return IntentRegisterResult.INTENT_CONFLICT, False
                return IntentRegisterResult.REPLAY, False

            idempotency_match = self._record_for_idempotency_key(
                payload,
                normalized.account_id,
                normalized.idempotency_key,
            )
            if idempotency_match is not False:
                return IntentRegisterResult.IDEMPOTENCY_CONFLICT, False

            record = IntentExecutionRecord(
                account_id=normalized.account_id,
                intent_id=normalized.intent_id,
                idempotency_key=normalized.idempotency_key,
                instrument_id=normalized.instrument_id,
                action=normalized.action,
                state=IntentExecutionState.RECEIVED,
                intent_payload=payload_copy,
                client_order_ids=(),
                exchange_confirmed_client_order_ids=(),
                updated_at=_utc_now(),
            )
            payload["records"][_record_key(normalized)] = (
                _serialize_record(record)
            )
            return IntentRegisterResult.REGISTERED, True

        return self._mutate(mutate)

    def begin_dispatch(
        self,
        identity: IntentExecutionIdentity,
        client_order_ids: tuple[str, ...],
    ) -> IntentDispatchResult:
        normalized = identity.normalized()
        stable_ids = _stable_client_order_ids(
            normalized.intent_id,
            client_order_ids,
        )

        def mutate(
            payload: dict[str, Any],
        ) -> tuple[IntentDispatchResult, bool]:
            record = self._required_record(payload, normalized)
            if record.state is IntentExecutionState.EXCHANGE_CONFIRMED:
                return IntentDispatchResult.EXCHANGE_CONFIRMED, False
            if record.state is IntentExecutionState.REJECTED:
                return IntentDispatchResult.REJECTED, False
            if record.state is IntentExecutionState.DISPATCHED:
                if record.client_order_ids != stable_ids:
                    raise IntentExecutionInboxError(
                        "client_order_ids changed after dispatch"
                    )
                return IntentDispatchResult.RECOVERY_REQUIRED, False
            updated = replace(
                record,
                state=IntentExecutionState.DISPATCHED,
                client_order_ids=stable_ids,
                updated_at=_utc_now(),
            )
            payload["records"][_record_key(normalized)] = (
                _serialize_record(updated)
            )
            return IntentDispatchResult.READY, True

        return self._mutate(mutate)

    def mark_exchange_confirmed(
        self,
        identity: IntentExecutionIdentity,
    ) -> None:
        normalized = identity.normalized()

        def mutate(payload: dict[str, Any]) -> tuple[None, bool]:
            record = self._required_record(payload, normalized)
            if record.state is IntentExecutionState.EXCHANGE_CONFIRMED:
                return None, False
            if record.state is not IntentExecutionState.DISPATCHED:
                raise IntentExecutionInboxError(
                    "intent must be dispatched before exchange confirmation"
                )
            updated = replace(
                record,
                state=IntentExecutionState.EXCHANGE_CONFIRMED,
                exchange_confirmed_client_order_ids=(
                    record.client_order_ids
                ),
                updated_at=_utc_now(),
            )
            payload["records"][_record_key(normalized)] = (
                _serialize_record(updated)
            )
            return None, True

        self._mutate(mutate)

    def mark_exchange_confirmed_by_client_order_id(
        self,
        client_order_id: str,
    ) -> bool:
        target = str(client_order_id).strip()
        if not target:
            return False
        if not self._path.exists():
            return False

        def mutate(payload: dict[str, Any]) -> tuple[bool, bool]:
            match = self._record_entry_for_client_order_id(
                payload,
                target,
            )
            if match is False:
                return False, False
            key, record = match
            if record.state is IntentExecutionState.EXCHANGE_CONFIRMED:
                return True, False
            if record.state is not IntentExecutionState.DISPATCHED:
                raise IntentExecutionInboxError(
                    "intent must be dispatched before exchange confirmation"
                )
            confirmed_ids = set(
                record.exchange_confirmed_client_order_ids
            )
            confirmed_ids.add(target)
            state = IntentExecutionState.DISPATCHED
            if confirmed_ids.issuperset(record.client_order_ids):
                state = IntentExecutionState.EXCHANGE_CONFIRMED
            updated = replace(
                record,
                state=state,
                exchange_confirmed_client_order_ids=tuple(
                    sorted(confirmed_ids)
                ),
                updated_at=_utc_now(),
            )
            payload["records"][key] = _serialize_record(updated)
            return True, True

        return self._mutate(mutate)

    def mark_rejected(
        self,
        identity: IntentExecutionIdentity,
        reason: str,
    ) -> None:
        normalized = identity.normalized()
        rejection_reason = str(reason).strip()
        if not rejection_reason:
            raise IntentExecutionInboxError(
                "rejection reason is required"
            )

        def mutate(payload: dict[str, Any]) -> tuple[None, bool]:
            record = self._required_record(payload, normalized)
            if record.state is IntentExecutionState.EXCHANGE_CONFIRMED:
                raise IntentExecutionInboxError(
                    "exchange-confirmed intent cannot be rejected"
                )
            if (
                record.state is IntentExecutionState.REJECTED
                and record.rejection_reason == rejection_reason
            ):
                return None, False
            updated = replace(
                record,
                state=IntentExecutionState.REJECTED,
                rejection_reason=rejection_reason,
                updated_at=_utc_now(),
            )
            payload["records"][_record_key(normalized)] = (
                _serialize_record(updated)
            )
            return None, True

        self._mutate(mutate)

    def get(
        self,
        identity: IntentExecutionIdentity,
    ) -> IntentExecutionRecord | Literal[False]:
        normalized = identity.normalized()

        def read(
            payload: dict[str, Any],
        ) -> IntentExecutionRecord | Literal[False]:
            record = self._record_for_identity(payload, normalized)
            if record is False:
                return False
            if record.identity() != normalized:
                return False
            return record

        return self._read_locked(read)

    def pending(self) -> tuple[IntentExecutionRecord, ...]:
        def read(
            payload: dict[str, Any],
        ) -> tuple[IntentExecutionRecord, ...]:
            records = []
            for raw in payload["records"].values():
                record = self._record_from_raw(raw)
                if record.state in {
                    IntentExecutionState.RECEIVED,
                    IntentExecutionState.DISPATCHED,
                }:
                    records.append(record)
            records.sort(key=lambda item: item.updated_at)
            return tuple(records)

        return self._read_locked(read)

    def records(self) -> tuple[IntentExecutionRecord, ...]:
        def read(
            payload: dict[str, Any],
        ) -> tuple[IntentExecutionRecord, ...]:
            records = [
                self._record_from_raw(raw)
                for raw in payload["records"].values()
            ]
            records.sort(key=lambda item: item.updated_at)
            return tuple(records)

        return self._read_locked(read)

    def _required_record(
        self,
        payload: dict[str, Any],
        identity: IntentExecutionIdentity,
    ) -> IntentExecutionRecord:
        record = self._record_for_identity(payload, identity)
        if record is False:
            raise IntentExecutionInboxError(
                "intent receipt does not exist"
            )
        if record.identity() != identity:
            raise IntentExecutionInboxError(
                "intent identity conflicts with durable receipt"
            )
        return record

    def _record_for_identity(
        self,
        payload: dict[str, Any],
        identity: IntentExecutionIdentity,
    ) -> IntentExecutionRecord | Literal[False]:
        raw = payload["records"].get(_record_key(identity))
        if raw is None:
            return False
        return self._record_from_raw(raw)

    def _record_for_idempotency_key(
        self,
        payload: dict[str, Any],
        account_id: str,
        idempotency_key: str,
    ) -> IntentExecutionRecord | Literal[False]:
        for raw in payload["records"].values():
            record = self._record_from_raw(raw)
            if record.account_id != account_id:
                continue
            if record.idempotency_key == idempotency_key:
                return record
        return False

    def _record_entry_for_client_order_id(
        self,
        payload: dict[str, Any],
        client_order_id: str,
    ) -> tuple[str, IntentExecutionRecord] | Literal[False]:
        for key, raw in payload["records"].items():
            record = self._record_from_raw(raw)
            if client_order_id in record.client_order_ids:
                return key, record
        return False

    def _record_from_raw(self, raw: Any) -> IntentExecutionRecord:
        if not isinstance(raw, dict):
            raise IntentExecutionInboxError(
                "intent execution record must be an object"
            )
        try:
            intent_payload = _json_object(raw["intent_payload"])
            client_order_ids = tuple(
                str(value)
                for value in raw.get("client_order_ids", ())
            )
            confirmed_client_order_ids = tuple(
                str(value)
                for value in raw.get(
                    "exchange_confirmed_client_order_ids",
                    (),
                )
            )
            return IntentExecutionRecord(
                account_id=str(raw["account_id"]),
                intent_id=str(raw["intent_id"]),
                idempotency_key=str(raw["idempotency_key"]),
                instrument_id=str(raw["instrument_id"]),
                action=str(raw["action"]),
                state=IntentExecutionState(str(raw["state"])),
                intent_payload=intent_payload,
                client_order_ids=client_order_ids,
                exchange_confirmed_client_order_ids=(
                    confirmed_client_order_ids
                ),
                updated_at=str(raw["updated_at"]),
                rejection_reason=str(
                    raw.get("rejection_reason", "")
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntentExecutionInboxError(
                "intent execution record is invalid"
            ) from exc

    def _mutate(
        self,
        operation: Callable[[dict[str, Any]], tuple[Any, bool]],
    ) -> Any:
        with self._process_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    payload = self._load_payload()
                    result, changed = operation(payload)
                    if changed:
                        self._save_payload(payload)
                    return result
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_locked(
        self,
        operation: Callable[[dict[str, Any]], Any],
    ) -> Any:
        with self._process_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
                try:
                    payload = self._load_payload()
                    return operation(payload)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load_payload(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": self._VERSION, "records": {}}
        try:
            with self._path.open("rb") as inbox:
                payload_bytes = os.fstat(inbox.fileno()).st_size
                self._require_size_within_limit(payload_bytes)
                raw_payload = inbox.read(self._max_bytes + 1)
            self._require_size_within_limit(len(raw_payload))
            payload = json.loads(raw_payload.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise IntentExecutionInboxError(
                "intent execution inbox is unreadable"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise IntentExecutionInboxError(
                "intent execution inbox is unreadable"
            ) from exc
        if not isinstance(payload, dict):
            raise IntentExecutionInboxError(
                "intent execution inbox root must be an object"
            )
        if payload.get("version") != self._VERSION:
            raise IntentExecutionInboxError(
                "unsupported intent execution inbox version"
            )
        records = payload.get("records")
        if not isinstance(records, dict):
            raise IntentExecutionInboxError(
                "intent execution records must be an object"
            )
        return payload

    def _save_payload(self, payload: dict[str, Any]) -> None:
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw_payload = f"{serialized}\n".encode("utf-8")
        self._require_size_within_limit(len(raw_payload))
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(raw_payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
            _fsync_directory(self._path.parent)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _require_size_within_limit(self, payload_bytes: int) -> None:
        if payload_bytes <= self._max_bytes:
            return
        raise IntentExecutionInboxError(
            "intent execution inbox payload exceeds max_bytes: "
            f"{payload_bytes} bytes exceeds {self._max_bytes} bytes"
        )


def _stable_client_order_ids(
    intent_id: str,
    client_order_ids: tuple[str, ...],
) -> tuple[str, ...]:
    normalized_intent_id = UUID(str(intent_id))
    stable_ids = tuple(str(value).strip() for value in client_order_ids)
    if not stable_ids:
        raise IntentExecutionInboxError(
            "at least one client_order_id is required"
        )
    if len(set(stable_ids)) != len(stable_ids):
        raise IntentExecutionInboxError(
            "client_order_ids must be unique"
        )
    for client_order_id in stable_ids:
        match = _CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id)
        if match is None:
            raise IntentExecutionInboxError(
                "client_order_id must be derived from intent_id"
            )
        if match.group("intent_id") != normalized_intent_id.hex:
            raise IntentExecutionInboxError(
                "client_order_id must be derived from intent_id"
            )
    return stable_ids


def _json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        normalized = json.loads(
            json.dumps(
                dict(value),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise IntentExecutionInboxError(
            "intent payload must be JSON serializable"
        ) from exc
    if not isinstance(normalized, dict):
        raise IntentExecutionInboxError(
            "intent payload must be an object"
        )
    return normalized


def _record_key(identity: IntentExecutionIdentity) -> str:
    return f"{identity.account_id}:{identity.intent_id}"


def _serialize_record(
    record: IntentExecutionRecord,
) -> dict[str, Any]:
    serialized = asdict(record)
    serialized["state"] = record.state.value
    serialized["client_order_ids"] = list(record.client_order_ids)
    serialized["exchange_confirmed_client_order_ids"] = list(
        record.exchange_confirmed_client_order_ids
    )
    return serialized


def _process_lock_for(path: Path) -> RLock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
