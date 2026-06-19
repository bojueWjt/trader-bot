from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import psycopg2


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "ingress"))
sys.path.insert(0, str(REPO_ROOT / "services" / "telegram-watcher"))


class DirectIngressClient:
    def __init__(self, db_url: str):
        self.db_url = db_url

    def submit(self, payload: dict) -> dict:
        from ingress.service import ingest_raw_telegram_update

        return ingest_raw_telegram_update(payload, self.db_url)


def _counts(db_url: str):
    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT count(*) FROM raw_messages),
                (SELECT count(*) FROM media_assets),
                (SELECT count(*) FROM outbox_events)
            """
        )
        return cur.fetchone()


def test_watcher_restart_redelivery_is_idempotent_and_persists_media(tmp_path, migrated_db):
    from telegram_watcher.collector import TelegramWatcherAdapter
    from telegram_watcher.media_store import LocalObjectStore

    media_bytes = b"raw-photo-bytes"
    update = {
        "update_id": "tg-upd-7000",
        "message": {
            "id": 7000,
            "chat_id": -100123456,
            "date": "2026-06-19T12:10:00Z",
            "sender_id": 9001,
            "text": "collector sends raw text",
            "media": [
                {
                    "bytes": media_bytes,
                    "mime": "image/jpeg",
                    "width": 640,
                    "height": 480,
                    "filename": "photo.jpg",
                }
            ],
        },
    }

    first_watcher = TelegramWatcherAdapter(
        ingress_client=DirectIngressClient(migrated_db),
        object_store=LocalObjectStore(tmp_path / "objects"),
        delivery_log_path=tmp_path / "deliveries.jsonl",
    )
    second_watcher = TelegramWatcherAdapter(
        ingress_client=DirectIngressClient(migrated_db),
        object_store=LocalObjectStore(tmp_path / "objects"),
        delivery_log_path=tmp_path / "deliveries-after-restart.jsonl",
    )

    first = first_watcher.handle_update(update)
    second = second_watcher.handle_update(update)

    expected_sha = hashlib.sha256(media_bytes).hexdigest()
    assert first["inserted"] is True
    assert second["inserted"] is False
    assert _counts(migrated_db) == (1, 1, 1)

    object_path = tmp_path / "objects" / "telegram" / "-100123456" / "7000" / f"{expected_sha}.jpg"
    assert object_path.read_bytes() == media_bytes

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT sha256, object_key, mime, width, height FROM media_assets")
        assert cur.fetchone() == (
            expected_sha,
            f"telegram/-100123456/7000/{expected_sha}.jpg",
            "image/jpeg",
            640,
            480,
        )


def test_watcher_edit_uses_new_source_version(migrated_db, tmp_path):
    from telegram_watcher.collector import TelegramWatcherAdapter
    from telegram_watcher.media_store import LocalObjectStore

    watcher = TelegramWatcherAdapter(
        ingress_client=DirectIngressClient(migrated_db),
        object_store=LocalObjectStore(tmp_path / "objects"),
        delivery_log_path=tmp_path / "deliveries.jsonl",
    )

    watcher.handle_update(
        {
            "update_id": "tg-upd-8000",
            "message": {
                "id": 8000,
                "chat_id": -100123456,
                "date": "2026-06-19T12:10:00Z",
                "text": "original",
            },
        }
    )
    edit = watcher.handle_update(
        {
            "update_id": "tg-upd-8001",
            "message": {
                "id": 8000,
                "chat_id": -100123456,
                "date": "2026-06-19T12:10:00Z",
                "edit_date": "2026-06-19T12:11:00Z",
                "text": "edited",
            },
        }
    )

    assert edit["inserted"] is True
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT source_version, message_text FROM raw_messages ORDER BY message_text")
        assert cur.fetchall() == [("edit:2026-06-19T12:11:00Z", "edited"), ("v1", "original")]


def test_watcher_retries_and_records_delivery_results(tmp_path):
    from telegram_watcher.collector import TelegramWatcherAdapter
    from telegram_watcher.media_store import LocalObjectStore

    class FlakyIngressClient:
        def __init__(self):
            self.calls = 0

        def submit(self, payload: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary ingress outage")
            return {"inserted": True, "raw_message_id": "00000000-0000-0000-0000-000000000001"}

    delivery_log = tmp_path / "deliveries.jsonl"
    client = FlakyIngressClient()
    watcher = TelegramWatcherAdapter(
        ingress_client=client,
        object_store=LocalObjectStore(tmp_path / "objects"),
        delivery_log_path=delivery_log,
        max_attempts=2,
        retry_sleep_seconds=0,
    )

    result = watcher.handle_update(
        {
            "update_id": "tg-upd-retry",
            "message": {
                "id": 9000,
                "chat_id": -100123456,
                "date": "2026-06-19T12:20:00Z",
                "text": "retry raw message",
            },
        }
    )

    records = [json.loads(line) for line in delivery_log.read_text().splitlines()]
    assert result["inserted"] is True
    assert client.calls == 2
    assert records[0]["attempt"] == 1
    assert records[0]["error"] == "temporary ingress outage"
    assert records[1]["attempt"] == 2
    assert records[1]["result"]["raw_message_id"] == "00000000-0000-0000-0000-000000000001"


def test_new_services_do_not_depend_on_semantic_or_execution_paths():
    service_roots = [
        REPO_ROOT / "services" / "ingress",
        REPO_ROOT / "services" / "telegram-watcher",
    ]
    forbidden = [
        "signal-importer",
        "freqtrade.signal_strategy.importer",
        "importSignalToFreqtrade",
        "forwardToTrader",
        "--approve-parsed",
        "--refresh-window",
        "trade_intents",
        "ccxt",
        "binance.client",
        "exchange_secret",
        "exchange_key",
    ]

    scanned = []
    for root in service_roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".js", ".ts", ".json"}:
                scanned.append(path)
                text = path.read_text(encoding="utf-8")
                for token in forbidden:
                    assert token not in text, f"{path} contains forbidden token {token}"

    assert scanned
