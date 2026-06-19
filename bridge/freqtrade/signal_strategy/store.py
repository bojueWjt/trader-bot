from __future__ import annotations

from datetime import UTC, timedelta
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any
from urllib.parse import urlparse, unquote

try:
    from app.db.schema import connect_signal_store, migrate_signal_store_schema
except ModuleNotFoundError:
    connect_signal_store = None
    migrate_signal_store_schema = None

from freqtrade.signal_strategy.domain import (
    ApprovalResult,
    DirectiveKind,
    MessageType,
    PositionDirective,
    ReservationResult,
    SignalStatus,
    TradingSignal,
    parse_utc_datetime,
    utc_now,
)
from freqtrade.signal_strategy.risk import RiskPolicy


ALLOWED_TRANSITIONS: dict[SignalStatus, set[SignalStatus]] = {
    SignalStatus.RAW: {SignalStatus.PARSED, SignalStatus.IGNORED},
    SignalStatus.PARSED: {
        SignalStatus.NEEDS_REVIEW,
        SignalStatus.APPROVED,
        SignalStatus.REJECTED,
        SignalStatus.EXPIRED,
        SignalStatus.BLOCKED_BY_RISK,
    },
    SignalStatus.NEEDS_REVIEW: {
        SignalStatus.APPROVED,
        SignalStatus.REJECTED,
        SignalStatus.EXPIRED,
        SignalStatus.BLOCKED_BY_RISK,
    },
    SignalStatus.APPROVED: {
        SignalStatus.RESERVED,
        SignalStatus.EXPIRED,
        SignalStatus.REJECTED,
        SignalStatus.BLOCKED_BY_RISK,
    },
    SignalStatus.RESERVED: {
        SignalStatus.APPROVED,
        SignalStatus.EXPIRED,
        SignalStatus.SENT_TO_FREQTRADE,
        SignalStatus.FAILED,
    },
    SignalStatus.SENT_TO_FREQTRADE: {
        SignalStatus.APPROVED,
        SignalStatus.ENTERED,
        SignalStatus.FAILED,
        SignalStatus.EXPIRED,
    },
    SignalStatus.ENTERED: {
        SignalStatus.PARTIALLY_EXITED,
        SignalStatus.EXITED,
        SignalStatus.FAILED,
    },
    SignalStatus.PARTIALLY_EXITED: {
        SignalStatus.PARTIALLY_EXITED,
        SignalStatus.EXITED,
    },
    SignalStatus.EXITED: set(),
    SignalStatus.REJECTED: set(),
    SignalStatus.EXPIRED: set(),
    SignalStatus.FAILED: set(),
    SignalStatus.IGNORED: set(),
    SignalStatus.BLOCKED_BY_RISK: set(),
}

STATUS_RANK: dict[str, int] = {
    SignalStatus.RAW.value: 0,
    SignalStatus.PARSED.value: 1,
    SignalStatus.NEEDS_REVIEW.value: 2,
    SignalStatus.APPROVED.value: 3,
    SignalStatus.RESERVED.value: 4,
    SignalStatus.SENT_TO_FREQTRADE.value: 5,
    SignalStatus.ENTERED.value: 6,
    SignalStatus.PARTIALLY_EXITED.value: 7,
    SignalStatus.EXITED.value: 8,
    SignalStatus.REJECTED.value: 8,
    SignalStatus.EXPIRED.value: 8,
    SignalStatus.FAILED.value: 8,
    SignalStatus.IGNORED.value: 8,
    SignalStatus.BLOCKED_BY_RISK.value: 8,
}

MESSAGE_TYPE_NOT_NEW_REASON = "message_type_not_new_signal"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _message_type_text(value: Any) -> str:
    if value is None or value == "":
        return MessageType.NEW_SIGNAL.value
    enum_value = getattr(value, "value", False)
    if enum_value is not False:
        value = enum_value
    return MessageType(str(value)).value


def _signal_message_type(signal: TradingSignal) -> str:
    return _message_type_text(getattr(signal, "message_type", None))


def _payload_message_type(payload: dict[str, Any]) -> str:
    message_type = _message_type_text(payload.get("message_type"))
    payload["message_type"] = message_type
    return message_type


def _append_review_reason(payload: dict[str, Any], reason_code: str) -> None:
    existing = payload.get("review_reason_codes")
    if isinstance(existing, list):
        reason_codes = [str(item) for item in existing]
    else:
        reason_codes = []
    reason_codes.append(reason_code)
    payload["review_reason_codes"] = sorted(set(reason_codes))


def _reject_non_new_signal_object(signal: TradingSignal) -> None:
    signal.status = SignalStatus.REJECTED
    signal.approved_at = None
    signal.review_reason_codes.append(MESSAGE_TYPE_NOT_NEW_REASON)
    signal.review_reason_codes = sorted(set(signal.review_reason_codes))


def _reject_non_new_payload(payload: dict[str, Any]) -> None:
    payload["status"] = SignalStatus.REJECTED.value
    payload["approved_at"] = ""
    _append_review_reason(payload, MESSAGE_TYPE_NOT_NEW_REASON)


class InMemorySignalStore:
    def __init__(self, risk_policy: RiskPolicy | None = None) -> None:
        if risk_policy:
            self.risk_policy = risk_policy
        else:
            self.risk_policy = RiskPolicy()
        self._signals: dict[str, TradingSignal] = {}
        self._operations: set[tuple[str, str]] = set()
        self._directives: dict[int, PositionDirective] = {}
        self._next_directive_id = 1
        self._lock = Lock()
        self.risk_events: list[dict[str, Any]] = []
        self.audit_events: list[dict[str, Any]] = []

    def upsert_signal(self, signal: TradingSignal) -> None:
        if signal.status == SignalStatus.APPROVED:
            if _signal_message_type(signal) != MessageType.NEW_SIGNAL.value:
                _reject_non_new_signal_object(signal)
        with self._lock:
            self._signals[signal.signal_id] = signal

    def get_signal(self, signal_id: str) -> TradingSignal:
        return self._signals[signal_id]

    def reserve_signal(self, signal_id: str, operation_type: str) -> ReservationResult:
        operation_key = (signal_id, operation_type)
        with self._lock:
            if operation_key in self._operations:
                return ReservationResult(
                    reserved=False,
                    signal_id=signal_id,
                    operation_type=operation_type,
                    reason="duplicate_operation",
                )

            signal = self._signals.get(signal_id)
            if not signal:
                return ReservationResult(
                    reserved=False,
                    signal_id=signal_id,
                    operation_type=operation_type,
                    reason="signal_missing",
                )

            if signal.status != SignalStatus.APPROVED:
                return ReservationResult(
                    reserved=False,
                    signal_id=signal_id,
                    operation_type=operation_type,
                    reason="signal_not_approved",
                )

            if _signal_message_type(signal) != MessageType.NEW_SIGNAL.value:
                return ReservationResult(
                    reserved=False,
                    signal_id=signal_id,
                    operation_type=operation_type,
                    reason=MESSAGE_TYPE_NOT_NEW_REASON,
                )

            self._operations.add(operation_key)
            self._transition_locked(signal, SignalStatus.RESERVED, "store", {})
            return ReservationResult(
                reserved=True,
                signal_id=signal_id,
                operation_type=operation_type,
            )

    def operation_count(self, signal_id: str, operation_type: str) -> int:
        operation_key = (signal_id, operation_type)
        with self._lock:
            if operation_key in self._operations:
                return 1
            return 0

    def release_signal_reservation(self, signal_id: str, operation_type: str = "entry") -> None:
        operation_key = (signal_id, operation_type)
        with self._lock:
            self._operations.discard(operation_key)

    def save_directive(self, directive: PositionDirective) -> None:
        with self._lock:
            if directive.directive_id is None:
                directive.directive_id = self._next_directive_id
                self._next_directive_id += 1
            self._directives[directive.directive_id] = directive
            self._record_directive_event_locked("directive_saved", directive)

    def get_pending_directives(self, pair: str) -> list[PositionDirective]:
        normalized_pair = str(pair)
        with self._lock:
            self._expire_old_directives_locked()
            return [
                directive
                for directive in self._directives.values()
                if directive.pair == normalized_pair and directive.status == "pending"
            ]

    def consume_directive(self, directive_id: int | PositionDirective | None) -> None:
        active_id = self._directive_id(directive_id)
        if active_id is None:
            return
        with self._lock:
            directive = self._directives.get(active_id)
            if not directive or directive.status != "pending":
                return
            directive.status = "consumed"
            self._record_directive_event_locked("directive_consumed", directive)

    def expire_old_directives(self) -> list[int]:
        with self._lock:
            return self._expire_old_directives_locked()

    def _expire_old_directives_locked(self) -> list[int]:
        expired_ids = []
        for directive_id, directive in self._directives.items():
            if directive.status != "pending":
                continue
            if not directive.is_expired():
                continue
            directive.status = "expired"
            expired_ids.append(directive_id)
            self._record_directive_event_locked("directive_expired", directive)
        return expired_ids

    def _record_directive_event_locked(
        self,
        event_type: str,
        directive: PositionDirective,
    ) -> None:
        self.audit_events.append(
            {
                "type": event_type,
                "directive_id": directive.directive_id,
                "kind": directive.kind.value,
                "pair": directive.pair,
                "source_message_id": directive.source_message_id,
                "status": directive.status,
                "created_at": utc_now().isoformat(),
            }
        )

    def _directive_id(self, directive_id: int | PositionDirective | None) -> int | None:
        if isinstance(directive_id, PositionDirective):
            return directive_id.directive_id
        if directive_id is None:
            return None
        return int(directive_id)

    def expire_stale_signals(
        self,
        current_time: datetime | None = None,
        max_age_minutes: int = 24 * 60,
    ) -> list[str]:
        active_time = _ensure_utc(current_time or utc_now())
        cutoff = active_time - timedelta(minutes=max_age_minutes)
        expired_signal_ids = []
        expirable_statuses = {SignalStatus.APPROVED, SignalStatus.RESERVED}
        with self._lock:
            for signal in self._signals.values():
                if signal.status not in expirable_statuses:
                    continue
                effective_time = signal.approved_at or signal.received_at
                if _ensure_utc(effective_time) > cutoff:
                    continue
                result = self._transition_locked(
                    signal,
                    SignalStatus.EXPIRED,
                    "store",
                    {
                        "reason": "signal_ttl_expired",
                        "max_age_minutes": max_age_minutes,
                    },
                )
                if result.approved:
                    expired_signal_ids.append(signal.signal_id)
        return expired_signal_ids

    def approve_signal(self, signal_id: str, now: datetime | None = None) -> ApprovalResult:
        with self._lock:
            signal = self._signals.get(signal_id)
            if not signal:
                return ApprovalResult(
                    approved=False,
                    signal_id=signal_id,
                    status=SignalStatus.FAILED,
                    reason="signal_missing",
                )

            active_now = now
            if active_now is None:
                active_now = utc_now()

            approvable_statuses = {SignalStatus.PARSED, SignalStatus.NEEDS_REVIEW}
            if signal.status not in approvable_statuses:
                return ApprovalResult(
                    approved=False,
                    signal_id=signal_id,
                    status=signal.status,
                    reason="invalid_transition",
                )

            if signal.entry.expires_at and active_now > signal.entry.expires_at:
                self._transition_locked(signal, SignalStatus.EXPIRED, "store", {})
                self.risk_events.append(
                    {
                        "type": "signal_expired",
                        "signal_id": signal.signal_id,
                        "created_at": active_now.isoformat(),
                    }
                )
                return ApprovalResult(
                    approved=False,
                    signal_id=signal_id,
                    status=SignalStatus.EXPIRED,
                    reason="signal_expired",
                )

            return self._transition_locked(signal, SignalStatus.APPROVED, "reviewer", {})

    def transition_signal(
        self,
        signal_id: str,
        target_status: SignalStatus,
        actor: str,
        context: dict[str, Any] | None = None,
    ) -> ApprovalResult:
        with self._lock:
            signal = self._signals.get(signal_id)
            if not signal:
                return ApprovalResult(
                    approved=False,
                    signal_id=signal_id,
                    status=SignalStatus.FAILED,
                    reason="signal_missing",
                )

            active_context = context
            if active_context is None:
                active_context = {}

            return self._transition_locked(signal, target_status, actor, active_context)

    def _transition_locked(
        self,
        signal: TradingSignal,
        target_status: SignalStatus,
        actor: str,
        context: dict[str, Any],
    ) -> ApprovalResult:
        allowed_targets = ALLOWED_TRANSITIONS.get(signal.status, set())
        if target_status not in allowed_targets:
            return ApprovalResult(
                approved=False,
                signal_id=signal.signal_id,
                status=signal.status,
                reason="invalid_transition",
            )

        if target_status == SignalStatus.APPROVED:
            if _signal_message_type(signal) != MessageType.NEW_SIGNAL.value:
                return ApprovalResult(
                    approved=False,
                    signal_id=signal.signal_id,
                    status=signal.status,
                    reason=MESSAGE_TYPE_NOT_NEW_REASON,
                )

        previous_status = signal.status
        signal.status = target_status
        if target_status == SignalStatus.APPROVED and not signal.approved_at:
            signal.approved_at = utc_now()
        self.audit_events.append(
            {
                "signal_id": signal.signal_id,
                "from_status": previous_status.value,
                "to_status": target_status.value,
                "actor": actor,
                "context": context,
                "created_at": utc_now().isoformat(),
            }
        )
        return ApprovalResult(
            approved=True,
            signal_id=signal.signal_id,
            status=target_status,
        )


class SQLiteSignalOperationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._initialize()

    def reserve_operation(self, signal_id: str, operation_type: str) -> ReservationResult:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO signal_operations(signal_id, operation_type, status)
                    VALUES (?, ?, ?)
                    """,
                    (signal_id, operation_type, "reserved"),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            return ReservationResult(
                reserved=False,
                signal_id=signal_id,
                operation_type=operation_type,
                reason="duplicate_operation",
            )

        return ReservationResult(
            reserved=True,
            signal_id=signal_id,
            operation_type=operation_type,
        )

    def operation_count(self, signal_id: str, operation_type: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT COUNT(*)
                FROM signal_operations
                WHERE signal_id = ? AND operation_type = ?
                """,
                (signal_id, operation_type),
            )
            row = cursor.fetchone()
        if not row:
            return 0
        return int(row[0])

    def release_signal_reservation(self, signal_id: str, operation_type: str = "entry") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM signal_operations
                WHERE signal_id = ? AND operation_type = ?
                """,
                (signal_id, operation_type),
            )
            connection.commit()

    def expire_stale_signals(
        self,
        current_time: datetime | None = None,
        max_age_minutes: int = 24 * 60,
    ) -> list[str]:
        active_time = self._ensure_utc(current_time or utc_now())
        cutoff = active_time - timedelta(minutes=max_age_minutes)
        expired_signal_ids = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                SELECT signal_id, received_at, approved_at
                FROM signals
                WHERE status IN (?, ?)
                """,
                (SignalStatus.APPROVED.value, SignalStatus.RESERVED.value),
            )
            rows = cursor.fetchall()
            for row in rows:
                effective_time = self._parse_datetime(row["approved_at"])
                if not effective_time:
                    effective_time = self._parse_datetime(row["received_at"])
                if not effective_time or effective_time > cutoff:
                    continue
                self._update_signal_status(
                    connection,
                    row["signal_id"],
                    SignalStatus.EXPIRED.value,
                    actor="store",
                    context={
                        "reason": "signal_ttl_expired",
                        "max_age_minutes": max_age_minutes,
                    },
                )
                expired_signal_ids.append(row["signal_id"])
            connection.commit()
        return expired_signal_ids

    def _initialize(self) -> None:
        if migrate_signal_store_schema is not None:
            migrate_signal_store_schema(self.db_path)
            return

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(signal_id, operation_type)
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        if connect_signal_store is not None:
            return connect_signal_store(self.db_path)
        return sqlite3.connect(self.db_path, timeout=30, isolation_level="IMMEDIATE")


class SQLiteSignalStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._initialize()

    def upsert_signal(self, signal: TradingSignal | dict[str, Any]) -> None:
        payload = self._signal_payload(signal)
        signal_id = str(payload.get("signal_id", ""))
        if not signal_id:
            signal_id = str(payload.get("id", ""))
        if not signal_id:
            raise ValueError("signal_id is required")

        payload["signal_id"] = signal_id
        payload["message_type"] = _payload_message_type(payload)
        status = self._status_text(payload.get("status", SignalStatus.RAW))
        payload["status"] = status
        received_at = self._datetime_text(payload.get("received_at"))
        approved_at = self._datetime_text(payload.get("approved_at"))
        if status == SignalStatus.APPROVED.value and not approved_at:
            approved_at = received_at
            payload["approved_at"] = approved_at
        expires_at = self._entry_datetime_text(payload, "expires_at")
        now = utc_now().isoformat()

        with self._connect() as connection:
            existing = self._signal_row(connection, signal_id)
            is_new_signal = existing is None
            if existing:
                existing_status = str(existing["status"])
                if self._status_rank(existing_status) > self._status_rank(status):
                    status = existing_status
                    payload["status"] = existing_status

            if status == SignalStatus.APPROVED.value:
                if _payload_message_type(payload) != MessageType.NEW_SIGNAL.value:
                    _reject_non_new_payload(payload)
                    status = SignalStatus.REJECTED.value
                    approved_at = ""

            connection.execute(
                """
                INSERT INTO signals(
                    signal_id,
                    status,
                    message_type,
                    pair,
                    payload,
                    received_at,
                    approved_at,
                    expires_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    status = excluded.status,
                    message_type = excluded.message_type,
                    pair = excluded.pair,
                    payload = excluded.payload,
                    received_at = excluded.received_at,
                    approved_at = excluded.approved_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    signal_id,
                    status,
                    str(payload.get("message_type") or MessageType.NEW_SIGNAL.value),
                    str(payload.get("pair_freqtrade") or payload.get("pair") or ""),
                    json.dumps(payload, sort_keys=True, default=str),
                    received_at,
                    approved_at,
                    expires_at,
                    now,
                ),
            )
            if is_new_signal:
                self._record_initial_signal_events(connection, signal_id, status, payload)
            connection.commit()

    def get_signal(self, signal_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT payload
                FROM signals
                WHERE signal_id = ?
                """,
                (signal_id,),
            )
            row = cursor.fetchone()

        if not row:
            raise KeyError(signal_id)
        return self._decode_payload(row[0])

    def get_approved_signals(self, since_minutes: int, current_time: datetime) -> list[dict[str, Any]]:
        current_time = self._ensure_utc(current_time)
        since_time = current_time - timedelta(minutes=since_minutes)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT payload, received_at, approved_at, expires_at
                FROM signals
                WHERE status = ?
                """,
                (SignalStatus.APPROVED.value,),
            )
            rows = cursor.fetchall()

        signals = []
        for payload_text, received_at, approved_at, expires_at in rows:
            effective_time = self._parse_datetime(approved_at)
            if not effective_time:
                effective_time = self._parse_datetime(received_at)
            if effective_time and effective_time < since_time:
                continue
            expires = self._parse_datetime(expires_at)
            if expires and expires <= current_time:
                continue
            signals.append(self._decode_payload(payload_text))
        return signals

    def reserve(self, signal_id: str, operation_type: str = "entry", **payload) -> ReservationResult:
        return self.reserve_signal(signal_id, operation_type)

    def reserve_signal(self, signal_id: str, operation_type: str) -> ReservationResult:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._operation_row(connection, signal_id, operation_type)
                if row:
                    return ReservationResult(
                        reserved=False,
                        signal_id=signal_id,
                        operation_type=operation_type,
                        reason="duplicate_operation",
                    )

                signal_row = self._signal_row(connection, signal_id)
                if not signal_row:
                    return ReservationResult(
                        reserved=False,
                        signal_id=signal_id,
                        operation_type=operation_type,
                        reason="signal_missing",
                    )

                status = signal_row["status"]
                if status != SignalStatus.APPROVED.value:
                    return ReservationResult(
                        reserved=False,
                        signal_id=signal_id,
                        operation_type=operation_type,
                        reason="signal_not_approved",
                    )

                payload = self._decode_payload(signal_row["payload"])
                if _payload_message_type(payload) != MessageType.NEW_SIGNAL.value:
                    return ReservationResult(
                        reserved=False,
                        signal_id=signal_id,
                        operation_type=operation_type,
                        reason=MESSAGE_TYPE_NOT_NEW_REASON,
                    )

                connection.execute(
                    """
                    INSERT INTO signal_operations(signal_id, operation_type, status)
                    VALUES (?, ?, ?)
                    """,
                    (signal_id, operation_type, "reserved"),
                )
                self._update_signal_status(
                    connection,
                    signal_id,
                    SignalStatus.RESERVED.value,
                    actor="store",
                    context={},
                )
                connection.commit()
            except sqlite3.IntegrityError:
                return ReservationResult(
                    reserved=False,
                    signal_id=signal_id,
                    operation_type=operation_type,
                    reason="duplicate_operation",
                )

        return ReservationResult(
            reserved=True,
            signal_id=signal_id,
            operation_type=operation_type,
        )

    def operation_count(self, signal_id: str, operation_type: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT COUNT(*)
                FROM signal_operations
                WHERE signal_id = ? AND operation_type = ?
                """,
                (signal_id, operation_type),
            )
            row = cursor.fetchone()
        if not row:
            return 0
        return int(row[0])

    def transition_signal(
        self,
        signal_id: str,
        target_status: SignalStatus,
        actor: str,
        context: dict[str, Any] | None = None,
    ) -> ApprovalResult:
        active_context = context
        if active_context is None:
            active_context = {}

        target = self._status_text(target_status)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            signal_row = self._signal_row(connection, signal_id)
            if not signal_row:
                return ApprovalResult(
                    approved=False,
                    signal_id=signal_id,
                    status=SignalStatus.FAILED,
                    reason="signal_missing",
                )

            current_status = SignalStatus(signal_row["status"])
            next_status = SignalStatus(target)
            allowed_targets = ALLOWED_TRANSITIONS.get(current_status, set())
            if next_status not in allowed_targets:
                return ApprovalResult(
                    approved=False,
                    signal_id=signal_id,
                    status=current_status,
                    reason="invalid_transition",
                )

            if next_status == SignalStatus.APPROVED:
                payload = self._decode_payload(signal_row["payload"])
                if _payload_message_type(payload) != MessageType.NEW_SIGNAL.value:
                    return ApprovalResult(
                        approved=False,
                        signal_id=signal_id,
                        status=current_status,
                        reason=MESSAGE_TYPE_NOT_NEW_REASON,
                    )

            self._update_signal_status(
                connection,
                signal_id,
                target,
                actor=actor,
                context=active_context,
            )
            connection.commit()

        return ApprovalResult(
            approved=True,
            signal_id=signal_id,
            status=next_status,
        )

    def record_risk_event(self, event_type: str, **payload) -> None:
        self._insert_event("risk", event_type, "", payload)

    def record_audit_event(self, event_type: str, signal_id: str, **payload) -> None:
        self._insert_event("audit", event_type, signal_id, payload)

    def save_directive(self, directive: PositionDirective) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO position_directives(
                    kind,
                    pair,
                    fraction,
                    price,
                    source_message_id,
                    status,
                    created_at,
                    ttl_hours
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    directive.kind.value,
                    directive.pair,
                    directive.fraction,
                    directive.price,
                    directive.source_message_id,
                    directive.status,
                    self._datetime_text(directive.created_at),
                    directive.ttl_hours,
                ),
            )
            directive.directive_id = int(cursor.lastrowid)
            self._insert_event_with_connection(
                connection,
                "audit",
                "directive_saved",
                "",
                self._directive_event_payload(directive),
            )
            connection.commit()

    def get_pending_directives(self, pair: str) -> list[PositionDirective]:
        self.expire_old_directives()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT *
                FROM position_directives
                WHERE pair = ? AND status = ?
                ORDER BY id ASC
                """,
                (str(pair), "pending"),
            )
            rows = cursor.fetchall()
        return [self._directive_from_row(row) for row in rows]

    def consume_directive(self, directive_id: int | PositionDirective | None) -> None:
        active_id = self._directive_id(directive_id)
        if active_id is None:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._directive_row(connection, active_id)
            if not row or row["status"] != "pending":
                connection.commit()
                return
            connection.execute(
                """
                UPDATE position_directives
                SET status = ?
                WHERE id = ?
                """,
                ("consumed", active_id),
            )
            directive = self._directive_from_row(row)
            directive.status = "consumed"
            self._insert_event_with_connection(
                connection,
                "audit",
                "directive_consumed",
                "",
                self._directive_event_payload(directive),
            )
            connection.commit()

    def expire_old_directives(self) -> list[int]:
        expired_ids = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                SELECT *
                FROM position_directives
                WHERE status = ?
                ORDER BY id ASC
                """,
                ("pending",),
            )
            rows = cursor.fetchall()
            for row in rows:
                directive = self._directive_from_row(row)
                if not directive.is_expired():
                    continue
                connection.execute(
                    """
                    UPDATE position_directives
                    SET status = ?
                    WHERE id = ?
                    """,
                    ("expired", directive.directive_id),
                )
                directive.status = "expired"
                expired_ids.append(int(directive.directive_id))
                self._insert_event_with_connection(
                    connection,
                    "audit",
                    "directive_expired",
                    "",
                    self._directive_event_payload(directive),
                )
            connection.commit()
        return expired_ids

    def _initialize(self) -> None:
        if migrate_signal_store_schema is not None:
            migrate_signal_store_schema(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            if migrate_signal_store_schema is None:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signals (
                        signal_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        message_type TEXT NOT NULL DEFAULT 'new_signal',
                        pair TEXT NOT NULL DEFAULT '',
                        payload TEXT NOT NULL,
                        received_at TEXT NOT NULL DEFAULT '',
                        approved_at TEXT NOT NULL DEFAULT '',
                        expires_at TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signal_operations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        signal_id TEXT NOT NULL,
                        operation_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(signal_id, operation_type)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signal_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_kind TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        signal_id TEXT NOT NULL DEFAULT '',
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS position_directives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    fraction REAL,
                    price REAL,
                    source_message_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ttl_hours INTEGER NOT NULL DEFAULT 24
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_position_directives_pair_status
                ON position_directives(pair, status)
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        if connect_signal_store is not None:
            return connect_signal_store(self.db_path)
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level="IMMEDIATE")
        connection.row_factory = sqlite3.Row
        return connection

    def _signal_payload(self, signal: TradingSignal | dict[str, Any]) -> dict[str, Any]:
        if isinstance(signal, dict):
            return dict(signal)
        if hasattr(signal, "model_dump"):
            return signal.model_dump(mode="json")
        raise TypeError("signal must be a TradingSignal or dict")

    def _decode_payload(self, payload_text: str) -> dict[str, Any]:
        payload = json.loads(payload_text)
        if isinstance(payload, dict):
            return payload
        return {}

    def _status_text(self, status: Any) -> str:
        value = getattr(status, "value", False)
        if value is not False:
            return str(value)
        return str(status)

    def _status_rank(self, status: str) -> int:
        return STATUS_RANK.get(status, -1)

    def _datetime_text(self, value: Any) -> str:
        parsed = self._parse_datetime(value)
        if not parsed:
            return ""
        return parsed.isoformat()

    def _entry_datetime_text(self, payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if value:
            return self._datetime_text(value)
        entry = payload.get("entry")
        if not isinstance(entry, dict):
            return ""
        return self._datetime_text(entry.get(key))

    def _parse_datetime(self, value: Any) -> datetime | bool:
        if not value:
            return False
        if isinstance(value, datetime):
            return self._ensure_utc(value)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return False
            return self._ensure_utc(parsed)
        return False

    def _ensure_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _operation_row(
        self,
        connection: sqlite3.Connection,
        signal_id: str,
        operation_type: str,
    ) -> sqlite3.Row | None:
        cursor = connection.execute(
            """
            SELECT id
            FROM signal_operations
            WHERE signal_id = ? AND operation_type = ?
            """,
            (signal_id, operation_type),
        )
        return cursor.fetchone()

    def _signal_row(self, connection: sqlite3.Connection, signal_id: str) -> sqlite3.Row | None:
        cursor = connection.execute(
            """
            SELECT signal_id, status, payload, approved_at
            FROM signals
            WHERE signal_id = ?
            """,
            (signal_id,),
        )
        return cursor.fetchone()

    def _directive_row(
        self,
        connection: sqlite3.Connection,
        directive_id: int,
    ) -> sqlite3.Row | None:
        cursor = connection.execute(
            """
            SELECT *
            FROM position_directives
            WHERE id = ?
            """,
            (directive_id,),
        )
        return cursor.fetchone()

    def _directive_from_row(self, row: sqlite3.Row) -> PositionDirective:
        return PositionDirective(
            directive_id=int(row["id"]),
            kind=DirectiveKind(str(row["kind"])),
            pair=str(row["pair"]),
            fraction=row["fraction"],
            price=row["price"],
            source_message_id=row["source_message_id"],
            status=str(row["status"]),
            created_at=parse_utc_datetime(row["created_at"]),
            ttl_hours=int(row["ttl_hours"]),
        )

    def _directive_event_payload(self, directive: PositionDirective) -> dict[str, Any]:
        return {
            "directive_id": directive.directive_id,
            "kind": directive.kind.value,
            "pair": directive.pair,
            "fraction": directive.fraction,
            "price": directive.price,
            "source_message_id": directive.source_message_id,
            "status": directive.status,
        }

    def _directive_id(self, directive_id: int | PositionDirective | None) -> int | None:
        if isinstance(directive_id, PositionDirective):
            return directive_id.directive_id
        if directive_id is None:
            return None
        return int(directive_id)

    def _update_signal_status(
        self,
        connection: sqlite3.Connection,
        signal_id: str,
        target_status: str,
        actor: str,
        context: dict[str, Any],
    ) -> None:
        signal_row = self._signal_row(connection, signal_id)
        if not signal_row:
            return

        previous_status = signal_row["status"]
        payload = self._decode_payload(signal_row["payload"])
        if target_status == SignalStatus.APPROVED.value:
            if _payload_message_type(payload) != MessageType.NEW_SIGNAL.value:
                raise ValueError(MESSAGE_TYPE_NOT_NEW_REASON)
        payload["status"] = target_status
        approved_at = self._datetime_text(payload.get("approved_at"))
        if not approved_at:
            approved_at = self._datetime_text(signal_row["approved_at"])
        updated_at = utc_now().isoformat()
        if target_status == SignalStatus.APPROVED.value and not approved_at:
            approved_at = updated_at
            payload["approved_at"] = approved_at
        elif approved_at:
            payload["approved_at"] = approved_at

        connection.execute(
            """
            UPDATE signals
            SET status = ?, payload = ?, approved_at = ?, updated_at = ?
            WHERE signal_id = ?
            """,
            (
                target_status,
                json.dumps(payload, sort_keys=True, default=str),
                approved_at,
                updated_at,
                signal_id,
            ),
        )
        self._insert_event_with_connection(
            connection,
            "audit",
            "status_transition",
            signal_id,
            {
                "actor": actor,
                "context": context,
                "from_status": previous_status,
                "result": {
                    "status": target_status,
                },
                "to_status": target_status,
            },
        )

    def _record_initial_signal_events(
        self,
        connection: sqlite3.Connection,
        signal_id: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        result = {"status": status}
        self._insert_event_with_connection(
            connection,
            "audit",
            "raw_received",
            signal_id,
            {
                "actor": "importer",
                "source": payload.get("source", ""),
                "source_channel_id": payload.get("source_channel_id", ""),
                "source_message_id": payload.get("source_message_id", ""),
                "result": result,
            },
        )
        self._insert_event_with_connection(
            connection,
            "audit",
            "status_transition",
            signal_id,
            {
                "actor": "parser",
                "context": {
                    "parser_version": payload.get("parser_version", ""),
                },
                "from_status": SignalStatus.RAW.value,
                "to_status": status,
                "result": result,
            },
        )

    def _insert_event(
        self,
        event_kind: str,
        event_type: str,
        signal_id: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            self._insert_event_with_connection(
                connection,
                event_kind,
                event_type,
                signal_id,
                payload,
            )
            connection.commit()

    def _insert_event_with_connection(
        self,
        connection: sqlite3.Connection,
        event_kind: str,
        event_type: str,
        signal_id: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO signal_events(event_kind, event_type, signal_id, payload)
            VALUES (?, ?, ?, ?)
            """,
            (event_kind, event_type, signal_id, json.dumps(payload, sort_keys=True, default=str)),
        )


def make_signal_store_from_url(
    store_url: str,
    risk_policy: RiskPolicy | None = None,
) -> InMemorySignalStore | SQLiteSignalStore:
    if store_url.startswith("sqlite:///"):
        parsed = urlparse(store_url)
        db_path = _sqlite_path_from_url(parsed.path, absolute_hint=store_url.startswith("sqlite:////"))
        return SQLiteSignalStore(db_path)

    if store_url.startswith("sqlite://"):
        parsed = urlparse(store_url)
        db_path = _sqlite_path_from_url(parsed.netloc + parsed.path, absolute_hint=False)
        return SQLiteSignalStore(db_path)

    if store_url:
        raise ValueError("unsupported signal store url")

    return InMemorySignalStore(risk_policy=risk_policy)


def _sqlite_path_from_url(raw_path: str, absolute_hint: bool) -> str:
    db_path = unquote(raw_path)
    if absolute_hint:
        while db_path.startswith("//"):
            db_path = db_path[1:]
        return db_path

    if db_path.startswith("/"):
        return db_path[1:]

    while db_path.startswith("//"):
        db_path = db_path[1:]
    return db_path
