from __future__ import annotations

from copy import deepcopy
import json
import os
import sqlite3
from threading import Lock

from app.contracts.message_processing import MessageProcessingRecord, MessageProcessingStatus
from app.db.models_message_processing import (
    MessageProcessingEvent,
)


class MessageProcessingRepository:
    def __init__(self) -> None:
        self._records: dict[str, MessageProcessingRecord] = {}
        self._events: list[MessageProcessingEvent] = []
        self._lock = Lock()

    def upsert(self, record: MessageProcessingRecord) -> MessageProcessingRecord:
        with self._lock:
            existing = self._records.get(record.message_id)
            if existing:
                incoming = deepcopy(record)
                existing.source = incoming.source
                existing.channel_id = incoming.channel_id
                existing.signal_id = incoming.signal_id
                existing.updated_at = _event_timestamp()
                return deepcopy(existing)

            stored_record = deepcopy(record)
            self._records[record.message_id] = stored_record
            self._events.append(
                MessageProcessingEvent(
                    message_id=record.message_id,
                    from_status=False,
                    to_status=stored_record.status,
                    actor="store",
                )
            )
            return deepcopy(stored_record)

    def get(self, message_id: str) -> MessageProcessingRecord | bool:
        with self._lock:
            record = self._records.get(message_id)
            if not record:
                return False
            return deepcopy(record)

    def list_events(self, message_id: str) -> list[MessageProcessingEvent]:
        with self._lock:
            events = []
            for event in self._events:
                if event.message_id != message_id:
                    continue
                events.append(deepcopy(event))
            return events

    def update_lifecycle_fields(
        self,
        message_id: str,
        updates: dict[str, str],
    ) -> MessageProcessingRecord | bool:
        allowed_fields = {"cron_job_id", "output_path", "error"}
        with self._lock:
            record = self._records.get(message_id)
            if not record:
                return False

            accepted_updates = {}
            for field, value in updates.items():
                if field not in allowed_fields:
                    continue
                if value == "":
                    continue
                accepted_updates[field] = value

            if not accepted_updates:
                return deepcopy(record)

            for field, value in accepted_updates.items():
                setattr(record, field, value)
            record.updated_at = _event_timestamp()
            return deepcopy(record)

    def list_events_by_signal_id(self, signal_id: str) -> list[MessageProcessingEvent]:
        with self._lock:
            message_ids = set()
            for message_id, record in self._records.items():
                if record.signal_id != signal_id:
                    continue
                message_ids.add(message_id)

            events = []
            for event in self._events:
                if event.message_id not in message_ids:
                    continue
                events.append(deepcopy(event))
            return events

    def transition_status(
        self,
        message_id: str,
        target_status: MessageProcessingStatus,
        actor: str,
        allowed_transitions: dict[MessageProcessingStatus, set[MessageProcessingStatus]],
        reason: str = "",
    ) -> MessageProcessingRecord | bool:
        with self._lock:
            record = self._records.get(message_id)
            if not record:
                return False

            previous_status = record.status
            allowed_targets = allowed_transitions.get(previous_status, set())
            if target_status not in allowed_targets:
                return False

            record.status = target_status
            if target_status == MessageProcessingStatus.FAILED:
                record.error = reason
            record.updated_at = _event_timestamp()
            self._events.append(
                MessageProcessingEvent(
                    message_id=message_id,
                    from_status=previous_status,
                    to_status=target_status,
                    actor=actor,
                    reason=reason,
                )
            )
            return deepcopy(record)

    def count(self) -> int:
        with self._lock:
            return len(self._records)


class SqliteMessageProcessingRepository:
    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._lock = Lock()
        if database_path != ":memory:":
            parent_dir = os.path.dirname(database_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._install_schema()

    def upsert(self, record: MessageProcessingRecord) -> MessageProcessingRecord:
        with self._lock:
            existing = self._get_record_unlocked(record.message_id)
            if existing:
                updated_at = _event_timestamp()
                self._connection.execute(
                    """
                    UPDATE message_processing_records
                    SET source = ?, channel_id = ?, signal_id = ?, updated_at = ?
                    WHERE message_id = ?
                    """,
                    (
                        record.source,
                        record.channel_id,
                        record.signal_id,
                        updated_at,
                        record.message_id,
                    ),
                )
                self._connection.commit()
                updated = self._get_record_unlocked(record.message_id)
                return deepcopy(updated)

            stored_record = deepcopy(record)
            metadata_json = json.dumps(stored_record.metadata)
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO message_processing_records (
                        message_id,
                        source,
                        channel_id,
                        signal_id,
                        status,
                        cron_job_id,
                        output_path,
                        error,
                        metadata_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored_record.message_id,
                        stored_record.source,
                        stored_record.channel_id,
                        stored_record.signal_id,
                        stored_record.status.value,
                        stored_record.cron_job_id,
                        stored_record.output_path,
                        stored_record.error,
                        metadata_json,
                        stored_record.created_at,
                        stored_record.updated_at,
                    ),
                )
                self._insert_event_unlocked(
                    MessageProcessingEvent(
                        message_id=stored_record.message_id,
                        from_status=False,
                        to_status=stored_record.status,
                        actor="store",
                    )
                )
            return deepcopy(stored_record)

    def get(self, message_id: str) -> MessageProcessingRecord | bool:
        with self._lock:
            record = self._get_record_unlocked(message_id)
            if not record:
                return False
            return deepcopy(record)

    def list_events(self, message_id: str) -> list[MessageProcessingEvent]:
        with self._lock:
            cursor = self._connection.execute(
                """
                SELECT *
                FROM message_processing_events
                WHERE message_id = ?
                ORDER BY sequence ASC
                """,
                (message_id,),
            )
            events = []
            for row in cursor.fetchall():
                events.append(self._event_from_row(row))
            return events

    def update_lifecycle_fields(
        self,
        message_id: str,
        updates: dict[str, str],
    ) -> MessageProcessingRecord | bool:
        allowed_fields = {"cron_job_id", "output_path", "error"}
        accepted_updates = {}
        for field, value in updates.items():
            if field not in allowed_fields:
                continue
            if value == "":
                continue
            accepted_updates[field] = value

        with self._lock:
            record = self._get_record_unlocked(message_id)
            if not record:
                return False
            if not accepted_updates:
                return deepcopy(record)

            updated_at = _event_timestamp()
            assignments = []
            values = []
            for field, value in accepted_updates.items():
                assignments.append(f"{field} = ?")
                values.append(value)
            assignments.append("updated_at = ?")
            values.append(updated_at)
            values.append(message_id)

            sql = "UPDATE message_processing_records SET "
            sql += ", ".join(assignments)
            sql += " WHERE message_id = ?"
            self._connection.execute(sql, values)
            self._connection.commit()
            updated = self._get_record_unlocked(message_id)
            return deepcopy(updated)

    def list_events_by_signal_id(self, signal_id: str) -> list[MessageProcessingEvent]:
        with self._lock:
            cursor = self._connection.execute(
                """
                SELECT events.*
                FROM message_processing_events events
                INNER JOIN message_processing_records records
                    ON records.message_id = events.message_id
                WHERE records.signal_id = ?
                ORDER BY events.sequence ASC
                """,
                (signal_id,),
            )
            events = []
            for row in cursor.fetchall():
                events.append(self._event_from_row(row))
            return events

    def transition_status(
        self,
        message_id: str,
        target_status: MessageProcessingStatus,
        actor: str,
        allowed_transitions: dict[MessageProcessingStatus, set[MessageProcessingStatus]],
        reason: str = "",
    ) -> MessageProcessingRecord | bool:
        with self._lock:
            record = self._get_record_unlocked(message_id)
            if not record:
                return False

            previous_status = record.status
            allowed_targets = allowed_transitions.get(previous_status, set())
            if target_status not in allowed_targets:
                return False

            updated_at = _event_timestamp()
            error = record.error
            if target_status == MessageProcessingStatus.FAILED:
                error = reason

            with self._connection:
                self._connection.execute(
                    """
                    UPDATE message_processing_records
                    SET status = ?, error = ?, updated_at = ?
                    WHERE message_id = ?
                    """,
                    (
                        target_status.value,
                        error,
                        updated_at,
                        message_id,
                    ),
                )
                self._insert_event_unlocked(
                    MessageProcessingEvent(
                        message_id=message_id,
                        from_status=previous_status,
                        to_status=target_status,
                        actor=actor,
                        reason=reason,
                    )
                )

            updated = self._get_record_unlocked(message_id)
            return deepcopy(updated)

    def count(self) -> int:
        with self._lock:
            cursor = self._connection.execute("SELECT COUNT(*) AS count FROM message_processing_records")
            row = cursor.fetchone()
            return int(row["count"])

    def _install_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS message_processing_records (
                    message_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cron_job_id TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    error TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS message_processing_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )

    def _get_record_unlocked(self, message_id: str) -> MessageProcessingRecord | bool:
        cursor = self._connection.execute(
            """
            SELECT *
            FROM message_processing_records
            WHERE message_id = ?
            """,
            (message_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False
        return self._record_from_row(row)

    def _insert_event_unlocked(self, event: MessageProcessingEvent) -> None:
        from_status = ""
        if isinstance(event.from_status, MessageProcessingStatus):
            from_status = event.from_status.value
        self._connection.execute(
            """
            INSERT INTO message_processing_events (
                event_id,
                message_id,
                from_status,
                to_status,
                actor,
                reason,
                occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.message_id,
                from_status,
                event.to_status.value,
                event.actor,
                event.reason,
                event.occurred_at,
            ),
        )

    def _record_from_row(self, row: sqlite3.Row) -> MessageProcessingRecord:
        metadata = json.loads(row["metadata_json"])
        if not isinstance(metadata, dict):
            metadata = {}
        return MessageProcessingRecord(
            message_id=row["message_id"],
            source=row["source"],
            channel_id=row["channel_id"],
            signal_id=row["signal_id"],
            status=MessageProcessingStatus(row["status"]),
            cron_job_id=row["cron_job_id"],
            output_path=row["output_path"],
            error=row["error"],
            metadata=deepcopy(metadata),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _event_from_row(self, row: sqlite3.Row) -> MessageProcessingEvent:
        from_status: MessageProcessingStatus | bool = False
        if row["from_status"]:
            from_status = MessageProcessingStatus(row["from_status"])
        return MessageProcessingEvent(
            message_id=row["message_id"],
            from_status=from_status,
            to_status=MessageProcessingStatus(row["to_status"]),
            actor=row["actor"],
            reason=row["reason"],
            event_id=row["event_id"],
            occurred_at=row["occurred_at"],
        )


def repository_from_database_url(database_url: str) -> MessageProcessingRepository | SqliteMessageProcessingRepository:
    if database_url == "":
        return MessageProcessingRepository()

    sqlite_prefix = "sqlite:///"
    if not database_url.startswith(sqlite_prefix):
        return MessageProcessingRepository()

    database_path = database_url[len(sqlite_prefix) :]
    if database_path == ":memory:":
        return SqliteMessageProcessingRepository(":memory:")

    return SqliteMessageProcessingRepository(database_path)


def _event_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
