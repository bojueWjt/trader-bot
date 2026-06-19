from __future__ import annotations

from app.contracts.message_processing import MessageProcessingRecord, MessageProcessingStatus
from app.db.models_message_processing import (
    MessageProcessingEvent,
    MessageProcessingTransitionResult,
)
from app.db.repositories_message_processing import MessageProcessingRepository, repository_from_database_url


ALLOWED_TRANSITIONS: dict[MessageProcessingStatus, set[MessageProcessingStatus]] = {
    MessageProcessingStatus.RECEIVED: {
        MessageProcessingStatus.DB_SAVED,
        MessageProcessingStatus.FAILED,
    },
    MessageProcessingStatus.DB_SAVED: {
        MessageProcessingStatus.CRON_CREATED,
        MessageProcessingStatus.FAILED,
    },
    MessageProcessingStatus.CRON_CREATED: {
        MessageProcessingStatus.CRON_STARTED,
        MessageProcessingStatus.FAILED,
    },
    MessageProcessingStatus.CRON_STARTED: {
        MessageProcessingStatus.CRON_OUTPUT,
        MessageProcessingStatus.FAILED,
    },
    MessageProcessingStatus.CRON_OUTPUT: {
        MessageProcessingStatus.DELIVERED,
        MessageProcessingStatus.FAILED,
    },
    MessageProcessingStatus.DELIVERED: set(),
    MessageProcessingStatus.FAILED: set(),
}


class MessageProcessingStore:
    def __init__(self, repository: MessageProcessingRepository | None = None) -> None:
        if repository:
            self.repository = repository
        else:
            self.repository = MessageProcessingRepository()

    def upsert_message(
        self,
        record: MessageProcessingRecord,
    ) -> MessageProcessingRecord:
        return self.repository.upsert(record)

    def get_message(self, message_id: str) -> MessageProcessingRecord:
        record = self.repository.get(message_id)
        if not record:
            raise KeyError(message_id)
        return record

    def transition_message(
        self,
        message_id: str,
        target_status: MessageProcessingStatus,
        actor: str,
        reason: str = "",
    ) -> MessageProcessingTransitionResult:
        record = self.repository.get(message_id)
        if not record:
            return MessageProcessingTransitionResult(
                ok=False,
                message_id=message_id,
                status=MessageProcessingStatus.FAILED,
                reason="message_missing",
            )

        updated = self.repository.transition_status(
            message_id,
            target_status,
            actor,
            ALLOWED_TRANSITIONS,
            reason=reason,
        )
        if not updated:
            current = self.repository.get(message_id)
            status = record.status
            if current:
                status = current.status
            return MessageProcessingTransitionResult(
                ok=False,
                message_id=message_id,
                status=status,
                reason="invalid_transition",
            )

        return MessageProcessingTransitionResult(
            ok=True,
            message_id=message_id,
            status=target_status,
        )

    def fail_message(
        self,
        message_id: str,
        actor: str,
        reason: str,
    ) -> MessageProcessingTransitionResult:
        record = self.repository.get(message_id)
        if not record:
            return MessageProcessingTransitionResult(
                ok=False,
                message_id=message_id,
                status=MessageProcessingStatus.FAILED,
                reason="message_missing",
            )
        return self.transition_message(
            message_id,
            MessageProcessingStatus.FAILED,
            actor=actor,
            reason=reason,
        )

    def list_events(self, message_id: str) -> list[MessageProcessingEvent]:
        return self.repository.list_events(message_id)

    def update_lifecycle_fields(
        self,
        message_id: str,
        updates: dict[str, str],
    ) -> MessageProcessingRecord | bool:
        return self.repository.update_lifecycle_fields(message_id, updates)

    def list_events_by_signal_id(self, signal_id: str) -> list[MessageProcessingEvent]:
        return self.repository.list_events_by_signal_id(signal_id)

    def count(self) -> int:
        return self.repository.count()


def build_message_processing_store(database_url: str) -> MessageProcessingStore:
    repository = repository_from_database_url(database_url)
    return MessageProcessingStore(repository=repository)
