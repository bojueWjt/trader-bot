import io
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from app.db.repositories_signal import SignalRepository
from app.db.schema import migrate_signal_store_schema
from app.services.signal_parser import SignalStatus, parse_signal
from app.services.signal_store import SignalStore
from app.services.idempotency import AtomicReservationStore
from freqtrade.signal_strategy.importer import main as importer_main
from freqtrade.signal_strategy.domain import (
    EntryPlan,
    MessageType,
    SignalStatus as CoreSignalStatus,
    TradingSignal,
)
from freqtrade.signal_strategy.store import InMemorySignalStore, SQLiteSignalStore


def make_signal(received_at="2026-02-08T16:32:18.000Z"):
    return parse_signal(
        {
            "signal_id": "telegram:1",
            "source": "telegram",
            "source_channel_id": "-1001",
            "source_channel_name": "coinAlert",
            "source_message_id": "1",
            "received_at": received_at,
            "raw_text": "BTCUSDT LONG Entry: 现价 SL: 70400 TP: 72000",
            "media": [],
        },
        pair_whitelist={"BTC/USDT:USDT"},
    )


def make_core_signal(
    signal_id="core:1",
    status=CoreSignalStatus.APPROVED,
    received_at=None,
    approved_at=None,
    message_type=MessageType.NEW_SIGNAL,
):
    if received_at is None:
        received_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    return TradingSignal(
        signal_id=signal_id,
        source="telegram",
        source_channel_id="-1001",
        source_message_id=signal_id,
        raw_text="BTCUSDT LONG Entry: 100 SL: 90 TP: 110",
        pair_raw="BTCUSDT",
        pair_freqtrade="BTC/USDT:USDT",
        side="long",
        message_type=message_type,
        entry=EntryPlan(mode="limit", primary_price=100.0),
        status=status,
        received_at=received_at,
        approved_at=approved_at,
    )


def test_signal_store_upsert_keeps_signal_id_unique():
    store = SignalStore()
    signal = make_signal()

    first = store.upsert_signal(signal)
    second = store.upsert_signal(signal)

    assert first.signal_id == "telegram:1"
    assert second.signal_id == "telegram:1"
    assert store.count() == 1
    assert store.get_signal("telegram:1").pair_freqtrade == "BTC/USDT:USDT"


def test_signal_store_validates_status_transitions():
    store = SignalStore()
    signal = make_signal()
    store.upsert_signal(signal)

    approved = store.transition_signal("telegram:1", SignalStatus.APPROVED, actor="reviewer")
    duplicate_approve = store.transition_signal("telegram:1", SignalStatus.APPROVED, actor="reviewer")

    assert approved.ok is True
    assert approved.status == SignalStatus.APPROVED
    assert duplicate_approve.ok is False
    assert duplicate_approve.reason == "invalid_transition"
    assert store.get_signal("telegram:1").status == SignalStatus.APPROVED


def test_signal_store_persists_lifecycle_events():
    store = SignalStore()
    signal = make_signal()
    store.upsert_signal(signal)
    store.transition_signal("telegram:1", SignalStatus.APPROVED, actor="reviewer")

    events = store.list_events("telegram:1")

    assert [event.to_status for event in events] == [
        SignalStatus.PARSED,
        SignalStatus.APPROVED,
    ]
    assert events[0].from_status is False
    assert events[1].from_status == SignalStatus.PARSED


def test_duplicate_upsert_preserves_existing_lifecycle_state():
    store = SignalStore()
    signal = make_signal()
    store.upsert_signal(signal)
    store.transition_signal("telegram:1", SignalStatus.APPROVED, actor="reviewer")
    duplicate = make_signal()

    store.upsert_signal(duplicate)

    assert store.get_signal("telegram:1").status == SignalStatus.APPROVED
    assert [event.to_status for event in store.list_events("telegram:1")] == [
        SignalStatus.PARSED,
        SignalStatus.APPROVED,
    ]


def test_store_returns_copies_to_protect_lifecycle_validation():
    store = SignalStore()
    signal = make_signal()
    store.upsert_signal(signal)

    signal.status = SignalStatus.EXITED
    loaded = store.get_signal("telegram:1")
    loaded.status = SignalStatus.EXITED

    assert store.get_signal("telegram:1").status == SignalStatus.PARSED


def test_missing_signal_transition_returns_failed_result():
    store = SignalStore()

    result = store.transition_signal("missing", SignalStatus.APPROVED, actor="reviewer")

    assert result.ok is False
    assert result.status == SignalStatus.FAILED
    assert result.reason == "signal_missing"


def test_signal_store_lists_recent_approved_signals():
    store = SignalStore()
    signal = make_signal(received_at="2026-05-31T12:00:00+00:00")
    store.upsert_signal(signal)
    store.transition_signal("telegram:1", SignalStatus.APPROVED, actor="reviewer")

    approved = store.list_approved(
        since_minutes=60,
        current_time=signal.received_at,
    )

    assert len(approved) == 1
    assert approved[0].signal_id == "telegram:1"
    approved[0].status = SignalStatus.REJECTED
    assert store.get_signal("telegram:1").status == SignalStatus.APPROVED


def test_signal_store_excludes_stale_approved_signals():
    store = SignalStore()
    signal = make_signal(received_at="2026-05-31T10:00:00+00:00")
    store.upsert_signal(signal)
    store.transition_signal("telegram:1", SignalStatus.APPROVED, actor="reviewer")

    approved = store.list_approved(
        since_minutes=60,
        current_time=parse_signal(
            {
                "signal_id": "clock",
                "source": "manual",
                "source_channel_id": "manual",
                "source_channel_name": "",
                "source_message_id": "clock",
                "received_at": "2026-05-31T12:00:00+00:00",
                "raw_text": "BTCUSDT LONG Entry: 现价 SL: 70400 TP: 72000",
                "media": [],
            }
        ).received_at,
    )

    assert approved == []


def test_competing_transitions_from_same_state_allow_one_success():
    store = SignalStore()
    signal = make_signal()
    store.upsert_signal(signal)

    def transition(target_status):
        return store.transition_signal("telegram:1", target_status, actor="reviewer")

    from concurrent.futures import ThreadPoolExecutor

    targets = [SignalStatus.APPROVED, SignalStatus.REJECTED]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(transition, targets))

    successes = [result for result in results if result.ok]

    assert len(successes) == 1
    assert store.get_signal("telegram:1").status in {SignalStatus.APPROVED, SignalStatus.REJECTED}
    assert len(store.list_events("telegram:1")) == 2


def test_atomic_reservation_allows_exactly_one_concurrent_success():
    store = AtomicReservationStore()

    def reserve_entry():
        return store.reserve("telegram:1", "entry")

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _: reserve_entry(), range(20)))

    successes = [result for result in results if result.reserved]
    duplicates = [result for result in results if not result.reserved]

    assert len(successes) == 1
    assert len(duplicates) == 19
    assert {result.reason for result in duplicates} == {"duplicate_operation"}


def test_duplicate_reservation_returns_duplicate_operation():
    store = AtomicReservationStore()

    first = store.reserve("telegram:1", "entry")
    second = store.reserve("telegram:1", "entry")

    assert first.reserved is True
    assert second.reserved is False
    assert second.reason == "duplicate_operation"


def test_signal_store_reserve_signal_is_idempotent_and_marks_reserved():
    store = SignalStore()
    signal = make_signal()
    store.upsert_signal(signal)
    store.transition_signal("telegram:1", SignalStatus.APPROVED, actor="reviewer")

    first = store.reserve_signal("telegram:1", operation_type="entry")
    second = store.reserve_signal("telegram:1", operation_type="entry")

    assert first.reserved is True
    assert second.reserved is False
    assert second.reason == "duplicate_operation"
    assert store.get_signal("telegram:1").status == SignalStatus.RESERVED


def test_sqlite_signal_repository_persists_across_instances(tmp_path):
    db_path = tmp_path / "signals.sqlite"
    first_store = SignalStore(repository=SignalRepository(db_path))
    signal = make_signal()
    first_store.upsert_signal(signal)
    first_store.transition_signal("telegram:1", SignalStatus.APPROVED, actor="reviewer")

    restarted_store = SignalStore(repository=SignalRepository(db_path))

    assert restarted_store.count() == 1
    assert restarted_store.get_signal("telegram:1").status == SignalStatus.APPROVED
    assert [event.to_status for event in restarted_store.list_events("telegram:1")] == [
        SignalStatus.PARSED,
        SignalStatus.APPROVED,
    ]


def test_api_repository_and_freqtrade_store_share_sqlite_file(tmp_path):
    db_path = tmp_path / "signals.sqlite"
    api_store = SignalStore(repository=SignalRepository(db_path))
    api_signal = make_signal()
    api_store.upsert_signal(api_signal)
    api_store.transition_signal("telegram:1", SignalStatus.APPROVED, actor="reviewer")

    freqtrade_store = SQLiteSignalStore(db_path)
    approved = freqtrade_store.get_approved_signals(
        since_minutes=60,
        current_time=api_signal.received_at,
    )

    assert [signal["signal_id"] for signal in approved] == ["telegram:1"]

    freqtrade_store.upsert_signal(make_core_signal("core:shared", status=CoreSignalStatus.APPROVED))
    api_repository = SignalRepository(db_path)

    assert api_repository.get("core:shared").status == SignalStatus.APPROVED


def test_signal_store_migration_is_idempotent_for_empty_database(tmp_path):
    db_path = tmp_path / "empty.sqlite"

    migrate_signal_store_schema(db_path)
    migrate_signal_store_schema(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(signals)").fetchall()
        }
        operations = {
            row[1]
            for row in connection.execute("PRAGMA table_info(signal_operations)").fetchall()
        }

    assert {"signal_id", "status", "message_type", "payload", "received_at"} <= columns
    assert {"signal_id", "operation_type", "status"} <= operations


def test_signal_store_migration_is_idempotent_for_existing_database(tmp_path):
    db_path = tmp_path / "existing.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
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
            INSERT INTO signals(signal_id, status, pair, payload, received_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "legacy:1",
                "approved",
                "BTC/USDT:USDT",
                json.dumps({"signal_id": "legacy:1", "status": "approved"}),
                "2026-06-01T12:00:00+00:00",
            ),
        )

    migrate_signal_store_schema(db_path)
    migrate_signal_store_schema(db_path)

    store = SQLiteSignalStore(db_path)

    assert store.get_signal("legacy:1")["status"] == CoreSignalStatus.APPROVED.value
    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(signals)").fetchall()
        }
    assert "message_type" in columns


def test_core_store_allows_approved_and_reserved_signals_to_expire_with_events():
    store = InMemorySignalStore()
    approved_signal = make_core_signal("core:approved", status=CoreSignalStatus.APPROVED)
    reserved_signal = make_core_signal("core:reserved", status=CoreSignalStatus.APPROVED)
    store.upsert_signal(approved_signal)
    store.upsert_signal(reserved_signal)
    store.transition_signal("core:reserved", CoreSignalStatus.RESERVED, actor="strategy")

    approved_result = store.transition_signal(
        "core:approved",
        CoreSignalStatus.EXPIRED,
        actor="store",
        context={"reason": "signal_ttl_expired"},
    )
    reserved_result = store.transition_signal(
        "core:reserved",
        CoreSignalStatus.EXPIRED,
        actor="store",
        context={"reason": "signal_ttl_expired"},
    )

    assert approved_result.approved is True
    assert reserved_result.approved is True
    assert store.get_signal("core:approved").status == CoreSignalStatus.EXPIRED
    assert store.get_signal("core:reserved").status == CoreSignalStatus.EXPIRED
    assert [
        (event["from_status"], event["to_status"])
        for event in store.audit_events
        if event["to_status"] == CoreSignalStatus.EXPIRED.value
    ] == [
        (CoreSignalStatus.APPROVED.value, CoreSignalStatus.EXPIRED.value),
        (CoreSignalStatus.RESERVED.value, CoreSignalStatus.EXPIRED.value),
    ]


def test_core_store_expires_stale_approved_and_reserved_signals_after_24h():
    store = InMemorySignalStore()
    stale_time = datetime(2026, 6, 1, 11, 59, tzinfo=timezone.utc)
    fresh_time = datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc)
    current_time = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    store.upsert_signal(
        make_core_signal(
            "core:stale-approved",
            status=CoreSignalStatus.APPROVED,
            received_at=stale_time,
            approved_at=stale_time,
        )
    )
    store.upsert_signal(
        make_core_signal(
            "core:fresh-approved",
            status=CoreSignalStatus.APPROVED,
            received_at=fresh_time,
            approved_at=fresh_time,
        )
    )
    store.upsert_signal(
        make_core_signal(
            "core:stale-reserved",
            status=CoreSignalStatus.APPROVED,
            received_at=stale_time,
            approved_at=stale_time,
        )
    )
    store.transition_signal("core:stale-reserved", CoreSignalStatus.RESERVED, actor="strategy")

    expired = store.expire_stale_signals(current_time=current_time)

    assert expired == ["core:stale-approved", "core:stale-reserved"]
    assert store.get_signal("core:stale-approved").status == CoreSignalStatus.EXPIRED
    assert store.get_signal("core:stale-reserved").status == CoreSignalStatus.EXPIRED
    assert store.get_signal("core:fresh-approved").status == CoreSignalStatus.APPROVED


def test_importer_stdin_rejects_inj_update_broadcast_with_approve_parsed(
    tmp_path,
    monkeypatch,
    capsys,
):
    fixture_path = Path("fixtures/signals/inj_update_broadcast.json")
    db_path = tmp_path / "signals.sqlite"
    monkeypatch.setattr("sys.stdin", io.StringIO(fixture_path.read_text(encoding="utf-8")))

    exit_code = importer_main(
        [
            "--store-url",
            f"sqlite:///{db_path}",
            "--pair-whitelist",
            "INJ/USDT:USDT",
            "--approve-parsed",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    store = SQLiteSignalStore(db_path)
    signal = store.get_signal("sig-c1001894206677-m8123")

    assert exit_code == 0
    assert output == {"approved": 0, "needs_review": 0, "rejected": 1, "total": 1}
    assert signal["message_type"] == MessageType.POSITION_SCREENSHOT.value
    assert signal["status"] == CoreSignalStatus.REJECTED.value
    assert signal.get("approved_at") in {"", None}


def test_importer_stdin_inj_update_broadcast_cannot_be_manually_approved(
    tmp_path,
    monkeypatch,
):
    fixture_path = Path("fixtures/signals/inj_update_broadcast.json")
    db_path = tmp_path / "signals.sqlite"
    monkeypatch.setattr("sys.stdin", io.StringIO(fixture_path.read_text(encoding="utf-8")))

    importer_main(
        [
            "--store-url",
            f"sqlite:///{db_path}",
            "--pair-whitelist",
            "INJ/USDT:USDT",
        ]
    )

    store = SQLiteSignalStore(db_path)
    signal_id = "sig-c1001894206677-m8123"
    result = store.transition_signal(signal_id, CoreSignalStatus.APPROVED, actor="reviewer")

    assert result.approved is False
    assert result.reason == "message_type_not_new_signal"
    assert store.get_signal(signal_id)["status"] != CoreSignalStatus.APPROVED.value


def test_core_store_approve_signal_blocks_non_new_message_type():
    store = InMemorySignalStore()
    signal = make_core_signal(
        "core:update",
        status=CoreSignalStatus.PARSED,
        message_type=MessageType.UPDATE,
    )
    store.upsert_signal(signal)

    result = store.approve_signal("core:update")

    assert result.approved is False
    assert result.reason == "message_type_not_new_signal"
    assert store.get_signal("core:update").status == CoreSignalStatus.PARSED
