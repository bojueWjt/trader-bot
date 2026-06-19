from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.contracts.message_processing import (
    MessageProcessingRecord,
    MessageProcessingStatus,
)
from app.services.message_processing import MessageProcessingStore, build_message_processing_store


def make_record(message_id="telegram:1"):
    return MessageProcessingRecord(
        message_id=message_id,
        source="telegram",
        channel_id="-1001",
        signal_id="signal:1",
        metadata={"source_message_id": "1"},
    )


def test_upsert_creates_received_record_and_initial_event():
    store = MessageProcessingStore()

    record = store.upsert_message(make_record())

    events = store.list_events("telegram:1")
    assert record.message_id == "telegram:1"
    assert record.status == MessageProcessingStatus.RECEIVED
    assert store.count() == 1
    assert [event.to_status for event in events] == [MessageProcessingStatus.RECEIVED]
    assert events[0].from_status is False
    assert events[0].actor == "store"


def test_valid_full_lifecycle_records_ordered_events():
    store = MessageProcessingStore()
    store.upsert_message(make_record())

    transitions = [
        MessageProcessingStatus.DB_SAVED,
        MessageProcessingStatus.CRON_CREATED,
        MessageProcessingStatus.CRON_STARTED,
        MessageProcessingStatus.CRON_OUTPUT,
        MessageProcessingStatus.DELIVERED,
    ]
    for status in transitions:
        result = store.transition_message("telegram:1", status, actor="worker")
        assert result.ok is True
        assert result.status == status

    events = store.list_events("telegram:1")
    assert [event.to_status for event in events] == [
        MessageProcessingStatus.RECEIVED,
        MessageProcessingStatus.DB_SAVED,
        MessageProcessingStatus.CRON_CREATED,
        MessageProcessingStatus.CRON_STARTED,
        MessageProcessingStatus.CRON_OUTPUT,
        MessageProcessingStatus.DELIVERED,
    ]
    assert events[-1].from_status == MessageProcessingStatus.CRON_OUTPUT
    assert store.get_message("telegram:1").status == MessageProcessingStatus.DELIVERED


def test_duplicate_upsert_preserves_existing_lifecycle_fields_and_status():
    store = MessageProcessingStore()
    record = MessageProcessingRecord(
        message_id="telegram:1",
        source="telegram",
        channel_id="-1001",
        signal_id="signal:1",
        status=MessageProcessingStatus.DB_SAVED,
        cron_job_id="cron-1",
        output_path="/tmp/out.txt",
        error="old-error",
        metadata={"preserved": True},
    )
    store.upsert_message(record)

    duplicate = MessageProcessingRecord(
        message_id="telegram:1",
        source="hermes",
        channel_id="updated-channel",
        signal_id="updated-signal",
        status=MessageProcessingStatus.RECEIVED,
        cron_job_id="new-cron",
        output_path="/tmp/new.txt",
        error="new-error",
        metadata={"preserved": False},
    )
    store.upsert_message(duplicate)

    loaded = store.get_message("telegram:1")
    assert loaded.source == "hermes"
    assert loaded.channel_id == "updated-channel"
    assert loaded.signal_id == "updated-signal"
    assert loaded.status == MessageProcessingStatus.DB_SAVED
    assert loaded.cron_job_id == "cron-1"
    assert loaded.output_path == "/tmp/out.txt"
    assert loaded.error == "old-error"
    assert loaded.metadata == {"preserved": True}


def test_invalid_transition_returns_invalid_transition_and_does_not_mutate():
    store = MessageProcessingStore()
    store.upsert_message(make_record())

    result = store.transition_message(
        "telegram:1",
        MessageProcessingStatus.CRON_STARTED,
        actor="worker",
    )

    assert result.ok is False
    assert result.status == MessageProcessingStatus.RECEIVED
    assert result.reason == "invalid_transition"
    assert store.get_message("telegram:1").status == MessageProcessingStatus.RECEIVED
    assert [event.to_status for event in store.list_events("telegram:1")] == [
        MessageProcessingStatus.RECEIVED,
    ]


def test_missing_transition_returns_message_missing():
    store = MessageProcessingStore()

    result = store.transition_message(
        "missing",
        MessageProcessingStatus.DB_SAVED,
        actor="worker",
    )

    assert result.ok is False
    assert result.status == MessageProcessingStatus.FAILED
    assert result.reason == "message_missing"


def test_repository_returns_copies_so_external_mutation_cannot_corrupt_store():
    store = MessageProcessingStore()
    record = make_record()
    store.upsert_message(record)

    record.status = MessageProcessingStatus.FAILED
    loaded = store.get_message("telegram:1")
    loaded.status = MessageProcessingStatus.FAILED
    loaded.metadata["source_message_id"] = "mutated"

    assert store.get_message("telegram:1").status == MessageProcessingStatus.RECEIVED
    assert store.get_message("telegram:1").metadata == {"source_message_id": "1"}


def test_fail_message_records_failed_state_and_reason():
    store = MessageProcessingStore()
    store.upsert_message(make_record())

    result = store.fail_message("telegram:1", actor="worker", reason="cron_error")

    events = store.list_events("telegram:1")
    assert result.ok is True
    assert result.status == MessageProcessingStatus.FAILED
    assert store.get_message("telegram:1").status == MessageProcessingStatus.FAILED
    assert events[-1].to_status == MessageProcessingStatus.FAILED
    assert events[-1].reason == "cron_error"


def test_update_lifecycle_fields_persists_allowed_fields_and_ignores_blank_values():
    store = MessageProcessingStore()
    store.upsert_message(make_record())
    original = store.update_lifecycle_fields(
        "telegram:1",
        {
            "cron_job_id": "cron-1",
            "output_path": "/tmp/out.txt",
            "error": "old-error",
        },
    )

    updated = store.update_lifecycle_fields(
        "telegram:1",
        {
            "cron_job_id": "",
            "output_path": "/tmp/final.txt",
            "error": "new-error",
            "ignored": "value",
        },
    )

    loaded = store.get_message("telegram:1")
    assert original is not False
    assert updated is not False
    assert loaded.cron_job_id == "cron-1"
    assert loaded.output_path == "/tmp/final.txt"
    assert loaded.error == "new-error"
    assert loaded.updated_at != original.updated_at


def test_update_lifecycle_fields_missing_returns_false():
    store = MessageProcessingStore()

    updated = store.update_lifecycle_fields(
        "missing",
        {
            "cron_job_id": "cron-1",
        },
    )

    assert updated is False


def test_list_events_by_signal_id_returns_only_matching_signal_events_and_copies():
    store = MessageProcessingStore()
    store.upsert_message(make_record("telegram:1"))
    store.upsert_message(
        MessageProcessingRecord(
            message_id="telegram:2",
            source="telegram",
            channel_id="-1002",
            signal_id="signal:2",
            metadata={"source_message_id": "2"},
        )
    )
    store.transition_message(
        "telegram:1",
        MessageProcessingStatus.DB_SAVED,
        actor="worker",
    )
    store.transition_message(
        "telegram:2",
        MessageProcessingStatus.DB_SAVED,
        actor="worker",
    )

    events = store.list_events_by_signal_id("signal:1")
    second_read = store.list_events_by_signal_id("signal:1")
    events.clear()

    assert [event.message_id for event in second_read] == ["telegram:1", "telegram:1"]
    assert [event.to_status for event in second_read] == [
        MessageProcessingStatus.RECEIVED,
        MessageProcessingStatus.DB_SAVED,
    ]
    assert second_read[0] is not store.list_events_by_signal_id("signal:1")[0]


def test_concurrent_competing_transitions_from_same_state_allow_exactly_one_success():
    store = MessageProcessingStore()
    store.upsert_message(make_record())
    ready = Barrier(2)

    def transition(target_status):
        ready.wait()
        return store.transition_message("telegram:1", target_status, actor="worker")

    targets = [
        MessageProcessingStatus.DB_SAVED,
        MessageProcessingStatus.FAILED,
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(transition, targets))

    successes = [result for result in results if result.ok]
    failures = [result for result in results if not result.ok]
    final_status = store.get_message("telegram:1").status

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].reason == "invalid_transition"
    assert final_status == successes[0].status
    assert len(store.list_events("telegram:1")) == 2


def test_build_message_processing_store_persists_records_and_events_to_sqlite(tmp_path):
    database_url = f"sqlite:///{tmp_path}/messages.db"
    store = build_message_processing_store(database_url)
    record = store.upsert_message(make_record("telegram:persisted"))
    record.metadata["source_message_id"] = "mutated"
    store.transition_message(
        "telegram:persisted",
        MessageProcessingStatus.DB_SAVED,
        actor="worker",
    )

    reloaded_store = build_message_processing_store(database_url)
    loaded = reloaded_store.get_message("telegram:persisted")
    events = reloaded_store.list_events("telegram:persisted")

    assert loaded.message_id == "telegram:persisted"
    assert loaded.status == MessageProcessingStatus.DB_SAVED
    assert loaded.metadata == {"source_message_id": "1"}
    loaded.metadata["source_message_id"] = "mutated-again"
    assert [event.to_status for event in events] == [
        MessageProcessingStatus.RECEIVED,
        MessageProcessingStatus.DB_SAVED,
    ]
    assert events[0].from_status is False
    assert reloaded_store.get_message("telegram:persisted").metadata == {
        "source_message_id": "1",
    }
