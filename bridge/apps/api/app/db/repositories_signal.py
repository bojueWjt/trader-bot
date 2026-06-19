from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any

from app.db.models_signal import SignalLifecycleEvent, SignalRecord
from app.db.schema import (
    configured_signal_store_path,
    connect_signal_store,
    migrate_signal_store_schema,
)
from app.services.signal_parser import (
    EntryPlan,
    LeveragePlan,
    MediaAsset,
    ParsedSignal,
    SignalStatus,
    TakeProfit,
)


class SignalRepository:
    def __init__(self, db_path: str | Path | None = None) -> None:
        configured_path = configured_signal_store_path(db_path)
        if configured_path:
            self.db_path = configured_path
        else:
            self.db_path = ":memory:"
        self._memory_connection: sqlite3.Connection | None = None
        self._memory_lock = Lock()
        self._initialize()

    def upsert(self, signal: ParsedSignal) -> ParsedSignal:
        payload = _signal_to_payload(signal)
        signal_id = str(payload.get("signal_id", ""))
        if not signal_id:
            raise ValueError("signal_id is required")

        status = _status_text(payload.get("status", signal.status))
        received_at = _datetime_text(payload.get("received_at"))
        approved_at = _datetime_text(payload.get("approved_at"))
        expires_at = _entry_datetime_text(payload, "expires_at")
        now = _now_iso()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._signal_row(connection, signal_id)
            is_new_signal = existing is None
            if existing:
                status = str(existing["status"])
                payload["status"] = status
                if existing["approved_at"]:
                    approved_at = str(existing["approved_at"])
                    payload["approved_at"] = approved_at

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
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(payload.get("message_type") or "new_signal"),
                    str(payload.get("pair_freqtrade") or payload.get("pair") or ""),
                    json.dumps(payload, sort_keys=True, default=str),
                    received_at,
                    approved_at,
                    expires_at,
                    now,
                    now,
                ),
            )
            if is_new_signal:
                self._insert_status_event(
                    connection,
                    signal_id=signal_id,
                    from_status=False,
                    to_status=status,
                    actor="store",
                )
            connection.commit()

        return self.get(signal_id) or deepcopy(signal)

    def get(self, signal_id: str) -> ParsedSignal | bool:
        with self._connect() as connection:
            row = self._signal_row(connection, signal_id)
        if not row:
            return False
        return _payload_to_signal(self._decode_payload(row["payload"]))

    def count(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute("SELECT COUNT(*) FROM signals")
            row = cursor.fetchone()
        if not row:
            return 0
        return int(row[0])

    def list_by_status(self, status: SignalStatus) -> list[ParsedSignal]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT payload
                FROM signals
                WHERE status = ?
                ORDER BY received_at ASC, signal_id ASC
                """,
                (_status_text(status),),
            )
            rows = cursor.fetchall()
        return [_payload_to_signal(self._decode_payload(row["payload"])) for row in rows]

    def transition_status(
        self,
        signal_id: str,
        target_status: SignalStatus,
        actor: str,
        allowed_transitions: dict[SignalStatus, set[SignalStatus]],
    ) -> ParsedSignal | bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._signal_row(connection, signal_id)
            if not row:
                return False

            previous_status = SignalStatus(str(row["status"]))
            allowed_targets = allowed_transitions.get(previous_status, set())
            if target_status not in allowed_targets:
                return False

            payload = self._decode_payload(row["payload"])
            target_status_text = _status_text(target_status)
            payload["status"] = target_status_text
            approved_at = _datetime_text(payload.get("approved_at"))
            if not approved_at and row["approved_at"]:
                approved_at = str(row["approved_at"])
            if target_status == SignalStatus.APPROVED and not approved_at:
                approved_at = _now_iso()
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
                    target_status_text,
                    json.dumps(payload, sort_keys=True, default=str),
                    approved_at,
                    _now_iso(),
                    signal_id,
                ),
            )
            self._insert_status_event(
                connection,
                signal_id=signal_id,
                from_status=previous_status.value,
                to_status=target_status_text,
                actor=actor,
            )
            connection.commit()

        return _payload_to_signal(payload)

    def list_events(self, signal_id: str) -> list[SignalLifecycleEvent]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT payload, created_at
                FROM signal_events
                WHERE signal_id = ? AND event_type = ?
                ORDER BY id ASC
                """,
                (signal_id, "status_transition"),
            )
            rows = cursor.fetchall()

        events = []
        for row in rows:
            payload = self._decode_payload(row["payload"])
            to_status = payload.get("to_status")
            if not to_status:
                result = payload.get("result", {})
                if isinstance(result, dict):
                    to_status = result.get("status")
            if not to_status:
                continue

            from_status = payload.get("from_status", False)
            if from_status is not False and from_status is not None and from_status != "":
                from_status = SignalStatus(str(from_status))
            else:
                from_status = False

            events.append(
                SignalLifecycleEvent(
                    signal_id=signal_id,
                    from_status=from_status,
                    to_status=SignalStatus(str(to_status)),
                    actor=str(payload.get("actor") or "store"),
                    occurred_at=str(row["created_at"] or _now_iso()),
                )
            )
        return events

    def _initialize(self) -> None:
        if self.db_path == ":memory:":
            self._memory_connection = sqlite3.connect(
                ":memory:",
                timeout=30,
                isolation_level="IMMEDIATE",
                check_same_thread=False,
            )
            self._memory_connection.row_factory = sqlite3.Row
            migrate_signal_store_schema(self._memory_connection)
            return
        migrate_signal_store_schema(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return _PersistentConnection(self._memory_connection, self._memory_lock)
        return connect_signal_store(self.db_path)

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

    def _insert_status_event(
        self,
        connection: sqlite3.Connection,
        signal_id: str,
        from_status: str | bool,
        to_status: str,
        actor: str,
    ) -> None:
        payload = {
            "actor": actor,
            "from_status": from_status,
            "result": {"status": to_status},
            "to_status": to_status,
        }
        connection.execute(
            """
            INSERT INTO signal_events(event_kind, event_type, signal_id, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "audit",
                "status_transition",
                signal_id,
                json.dumps(payload, sort_keys=True, default=str),
                _now_iso(),
            ),
        )

    def _decode_payload(self, payload_text: str) -> dict[str, Any]:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return {}
        if isinstance(payload, dict):
            return payload
        return {}


class _PersistentConnection:
    def __init__(self, connection: sqlite3.Connection, lock: Lock) -> None:
        self.connection = connection
        self.lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self.lock.acquire()
        return self.connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if exc_type:
            self.connection.rollback()
        else:
            self.connection.commit()
        self.lock.release()
        return False


def _signal_to_payload(signal: ParsedSignal) -> dict[str, Any]:
    return {
        field.name: _json_value(getattr(signal, field.name))
        for field in fields(signal)
    }


def _payload_to_signal(payload: dict[str, Any]) -> ParsedSignal:
    return ParsedSignal(
        signal_id=str(payload.get("signal_id") or payload.get("id") or ""),
        source=str(payload.get("source") or "telegram"),
        source_channel_id=str(payload.get("source_channel_id") or ""),
        source_channel_name=str(payload.get("source_channel_name") or ""),
        source_message_id=str(payload.get("source_message_id") or ""),
        received_at=_parse_datetime(payload.get("received_at")),
        raw_text=str(payload.get("raw_text") or ""),
        media=[_media_asset(item) for item in _list_value(payload.get("media"))],
        pair_raw=str(payload.get("pair_raw") or ""),
        pair_freqtrade=str(payload.get("pair_freqtrade") or payload.get("pair") or ""),
        side=str(payload.get("side") or "long"),
        entry=_entry_plan(payload.get("entry")),
        stop_loss=_optional_float(payload.get("stop_loss")),
        take_profits=[_take_profit(item) for item in _list_value(payload.get("take_profits"))],
        status=SignalStatus(str(payload.get("status") or SignalStatus.RAW.value)),
        parser_version=str(payload.get("parser_version") or "signal-parser-v1"),
        review_reason_codes=[str(item) for item in _list_value(payload.get("review_reason_codes"))],
        take_profit_parse_status=str(payload.get("take_profit_parse_status") or "parsed"),
        leverage=_leverage_plan(payload.get("leverage")),
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _media_asset(value: Any) -> MediaAsset:
    if not isinstance(value, dict):
        value = {}
    return MediaAsset(
        type=str(value.get("type") or ""),
        path=str(value.get("path") or ""),
        mime_type=str(value.get("mime_type") or value.get("mimeType") or ""),
    )


def _entry_plan(value: Any) -> EntryPlan:
    if not isinstance(value, dict):
        value = {}
    return EntryPlan(
        mode=str(value.get("mode") or value.get("type") or "cmp"),
        primary_price=_optional_float(value.get("primary_price", value.get("price"))),
        dca_prices=[
            float(item)
            for item in _list_value(value.get("dca_prices"))
            if _optional_float(item) is not None
        ],
        valid_from=_optional_datetime(value.get("valid_from")),
        expires_at=_optional_datetime(value.get("expires_at")),
    )


def _take_profit(value: Any) -> TakeProfit:
    if not isinstance(value, dict):
        value = {"price": value}
    price = _optional_float(value.get("price", value.get("target")))
    if price is None:
        price = 0.0
    close_pct = _optional_float(value.get("close_pct"))
    if close_pct is None:
        close_pct = 100.0
    return TakeProfit(price=price, close_pct=close_pct)


def _leverage_plan(value: Any) -> LeveragePlan:
    if not isinstance(value, dict):
        value = {}
    min_value = _optional_int(value.get("min"))
    max_value = _optional_int(value.get("max"))
    selected = _optional_int(value.get("selected"))
    return LeveragePlan(
        min=min_value if min_value is not None else 3,
        max=max_value if max_value is not None else 3,
        selected=selected if selected is not None else 3,
    )


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value:
        return [value]
    return []


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    return _parse_datetime(value)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo:
            return value.astimezone(timezone.utc)
        return value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _datetime_text(value: Any) -> str:
    if not value:
        return ""
    return _parse_datetime(value).isoformat()


def _entry_datetime_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value:
        return _datetime_text(value)
    entry = payload.get("entry")
    if not isinstance(entry, dict):
        return ""
    entry_value = entry.get(key)
    if not entry_value:
        return ""
    return _datetime_text(entry_value)


def _status_text(status: Any) -> str:
    value = getattr(status, "value", False)
    if value is not False:
        return str(value)
    return str(status)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
