from __future__ import annotations

import sys
from pathlib import Path

import psycopg2
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "ingress"))


def _base_payload(**overrides):
    payload = {
        "source": "telegram",
        "channel_id": "-100123456",
        "source_message_id": "4242",
        "source_version": "v1",
        "source_received_at": "2026-06-19T12:00:00Z",
        "author_id": "9001",
        "message_text": "raw telegram text only",
        "message_kind": "text",
        "update_id": "upd-4242",
        "raw_payload": {"telegram": {"id": 4242, "message": "raw telegram text only"}},
        "reply_to": None,
        "media_assets": [],
    }
    payload.update(overrides)
    return payload


def _fetch_one(db_url: str, sql: str, params=()):
    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def test_new_message_writes_raw_message_and_queued_outbox(migrated_db):
    from ingress.service import ingest_raw_telegram_update

    result = ingest_raw_telegram_update(_base_payload(), migrated_db)

    assert result["inserted"] is True
    row = _fetch_one(
        migrated_db,
        """
        SELECT source, channel_id, source_message_id, source_version,
               source_received_at::text, author_id, message_text, raw_payload
        FROM raw_messages
        WHERE id = %s
        """,
        (result["raw_message_id"],),
    )
    assert row[0:7] == (
        "telegram",
        "-100123456",
        "4242",
        "v1",
        "2026-06-19 12:00:00+00",
        "9001",
        "raw telegram text only",
    )
    assert row[7]["message_kind"] == "text"
    assert row[7]["update_id"] == "upd-4242"

    outbox = _fetch_one(
        migrated_db,
        """
        SELECT event_type, status, aggregate_type, aggregate_id, payload
        FROM outbox_events
        WHERE aggregate_id = %s
        """,
        (result["raw_message_id"],),
    )
    assert outbox[0:4] == (
        "queued_for_hermes",
        "pending",
        "raw_message",
        result["raw_message_id"],
    )
    assert outbox[4]["raw_message_id"] == result["raw_message_id"]


def test_edit_is_stored_as_a_new_source_version(migrated_db):
    from ingress.service import ingest_raw_telegram_update

    first = ingest_raw_telegram_update(_base_payload(), migrated_db)
    edited = ingest_raw_telegram_update(
        _base_payload(
            source_version="edit:2026-06-19T12:05:00Z",
            message_text="edited raw telegram text",
            message_kind="edit",
            raw_payload={"telegram": {"id": 4242, "edit_date": "2026-06-19T12:05:00Z"}},
        ),
        migrated_db,
    )

    assert first["raw_message_id"] != edited["raw_message_id"]
    assert edited["inserted"] is True
    counts = _fetch_one(
        migrated_db,
        """
        SELECT count(*), count(DISTINCT source_version)
        FROM raw_messages
        WHERE source = 'telegram' AND channel_id = '-100123456' AND source_message_id = '4242'
        """,
    )
    assert counts == (2, 2)


def test_reply_relationship_is_preserved_in_raw_payload(migrated_db):
    from ingress.service import ingest_raw_telegram_update

    result = ingest_raw_telegram_update(
        _base_payload(
            source_message_id="4243",
            update_id="upd-4243",
            reply_to={"source_message_id": "4242", "source_version": "v1"},
            raw_payload={"telegram": {"id": 4243, "reply_to_msg_id": 4242}},
        ),
        migrated_db,
    )

    row = _fetch_one(
        migrated_db,
        "SELECT raw_payload FROM raw_messages WHERE id = %s",
        (result["raw_message_id"],),
    )
    assert row[0]["reply_to"] == {"source_message_id": "4242", "source_version": "v1"}
    assert row[0]["telegram"]["reply_to_msg_id"] == 4242


def test_duplicate_source_identity_is_idempotent(migrated_db):
    from ingress.service import ingest_raw_telegram_update

    first = ingest_raw_telegram_update(_base_payload(), migrated_db)
    duplicate = ingest_raw_telegram_update(_base_payload(), migrated_db)

    assert duplicate["inserted"] is False
    assert duplicate["raw_message_id"] == first["raw_message_id"]
    counts = _fetch_one(
        migrated_db,
        """
        SELECT
            (SELECT count(*) FROM raw_messages),
            (SELECT count(*) FROM outbox_events)
        """,
    )
    assert counts == (1, 1)


def test_media_metadata_writes_sha256_object_key_and_dimensions(migrated_db):
    from ingress.service import ingest_raw_telegram_update

    result = ingest_raw_telegram_update(
        _base_payload(
            source_message_id="5000",
            update_id="upd-5000",
            message_kind="photo",
            media_assets=[
                {
                    "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
                    "object_key": "telegram/-100123456/5000/2cf24dba.bin",
                    "mime": "image/jpeg",
                    "width": 1280,
                    "height": 720,
                    "download_status": "downloaded",
                }
            ],
        ),
        migrated_db,
    )

    row = _fetch_one(
        migrated_db,
        """
        SELECT sha256, object_key, mime, width, height, download_status, raw_message_id::text
        FROM media_assets
        WHERE raw_message_id = %s
        """,
        (result["raw_message_id"],),
    )
    assert row == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        "telegram/-100123456/5000/2cf24dba.bin",
        "image/jpeg",
        1280,
        720,
        "downloaded",
        result["raw_message_id"],
    )


def test_media_failure_rolls_back_raw_message_and_outbox(migrated_db):
    from ingress.service import IngressValidationError, ingest_raw_telegram_update

    with pytest.raises(IngressValidationError):
        ingest_raw_telegram_update(
            _base_payload(
                media_assets=[
                    {
                        "sha256": "abc",
                        "object_key": "telegram/bad.bin",
                        "mime": "image/jpeg",
                        "width": 0,
                        "height": 720,
                        "download_status": "downloaded",
                    }
                ],
            ),
            migrated_db,
        )

    counts = _fetch_one(
        migrated_db,
        """
        SELECT
            (SELECT count(*) FROM raw_messages),
            (SELECT count(*) FROM media_assets),
            (SELECT count(*) FROM outbox_events)
        """,
    )
    assert counts == (0, 0, 0)
