import json
import sqlite3
from pathlib import Path

import pytest

from freqtrade.signal_strategy.domain import MessageType, SignalStatus
from freqtrade.signal_strategy.importer import import_signal_items, normalize_watcher_signal
from freqtrade.signal_strategy.store import SQLiteSignalStore


FIXTURES = Path(__file__).parents[2] / "fixtures"
CORPUS_PATH = FIXTURES / "signals" / "telegram_latest20" / "messages.latest20.json"

NEW_SIGNAL_IDS = {4327, 4934, 4938, 4942}
UPDATE_CLOSE_IDS = {4328, 4935, 4940, 4943, 4945}
NOISE_ANALYSIS_IDS = {4329, 4937, 4939, 4941, 4946}
PURE_MEDIA_IDS = {4326, 4936, 4944, 4947}


@pytest.fixture
def corpus_items():
    with CORPUS_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def imported_corpus(tmp_path, corpus_items):
    db_path = tmp_path / "real-corpus.sqlite"
    result = import_signal_items(
        corpus_items,
        f"sqlite:///{db_path}",
        approve_parsed=True,
    )
    return db_path, result


def expected_signal_id(item):
    return f"sig-c{abs(int(item['chatId']))}-m{item['id']}"


def load_payloads(db_path):
    store = SQLiteSignalStore(db_path)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT signal_id FROM signals ORDER BY signal_id").fetchall()
    return {signal_id: store.get_signal(signal_id) for (signal_id,) in rows}


def signal_ids_for(items, message_ids):
    return {expected_signal_id(item) for item in items if item["id"] in message_ids}


def assert_rejected_non_signal(payload):
    assert payload["message_type"] != MessageType.NEW_SIGNAL.value
    assert payload["status"] == SignalStatus.REJECTED.value
    assert "message_type_not_new_signal" in payload["review_reason_codes"]


def test_real_telegram_corpus_generates_cli_safe_signal_ids(corpus_items):
    for item in corpus_items:
        normalized = normalize_watcher_signal(item)

        assert normalized["signal_id"] == expected_signal_id(item)


def test_real_telegram_corpus_classifies_semantic_groups(corpus_items, imported_corpus):
    db_path, result = imported_corpus
    payloads = load_payloads(db_path)

    assert result.total == len(corpus_items)

    for signal_id in signal_ids_for(corpus_items, NEW_SIGNAL_IDS):
        payload = payloads[signal_id]
        assert payload["message_type"] == MessageType.NEW_SIGNAL.value
        assert payload["status"] in {
            SignalStatus.APPROVED.value,
            SignalStatus.EXPIRED.value,
            SignalStatus.NEEDS_REVIEW.value,
            SignalStatus.PARSED.value,
        }
        assert "message_type_not_new_signal" not in payload["review_reason_codes"]

    for signal_id in signal_ids_for(corpus_items, UPDATE_CLOSE_IDS):
        payload = payloads[signal_id]
        assert payload["message_type"] == MessageType.UPDATE.value
        assert_rejected_non_signal(payload)

    for signal_id in signal_ids_for(corpus_items, NOISE_ANALYSIS_IDS):
        payload = payloads[signal_id]
        assert payload["message_type"] == MessageType.NOISE.value
        assert_rejected_non_signal(payload)

    for signal_id in signal_ids_for(corpus_items, PURE_MEDIA_IDS):
        payload = payloads[signal_id]
        assert payload["message_type"] == MessageType.POSITION_SCREENSHOT.value
        assert_rejected_non_signal(payload)


def test_real_telegram_corpus_duplicate_messages_are_idempotent(corpus_items, imported_corpus):
    db_path, _ = imported_corpus

    with sqlite3.connect(db_path) as connection:
        total_rows = connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        duplicate_4327_rows = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE signal_id = ?",
            ("sig-c1002198013097-m4327",),
        ).fetchone()[0]
        duplicate_4937_rows = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE signal_id = ?",
            ("sig-c1002228497993-m4937",),
        ).fetchone()[0]

    assert total_rows == len({expected_signal_id(item) for item in corpus_items})
    assert duplicate_4327_rows == 1
    assert duplicate_4937_rows == 1
