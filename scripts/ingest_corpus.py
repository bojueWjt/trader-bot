"""C-04: ingest the real Telegram corpus into the v3 input tables.

Creates raw_messages + outbox(raw_message.ingested) + downloaded media_assets so the
hermes-worker claims and processes them through REAL Hermes. This seeds the worker's
*input* (what the watcher/ingress would produce) — it does NOT bypass Hermes.
Duplicate source messages (same channel+id) are skipped (the unique identity holds).
"""
import hashlib
import json
import os
import sys
from uuid import uuid4

from psycopg2 import errors as pg_errors

sys.path[:0] = ["services/control-plane", "services/control-plane/db"]
from db.connection import connect, transaction  # noqa: E402
from db.repository import ingest_raw_message_with_outbox  # noqa: E402

LIMIT = int(os.environ.get("LIMIT", "15"))
MEDIA_DIR = os.environ.get("MEDIA_DIR", "/srv/trader-v3/media")
CORPUS = os.environ.get("CORPUS", "/srv/trader-v3/corpus/messages.json")

msgs = json.load(open(CORPUS))[:LIMIT]
n_msg = n_media = n_dup = 0
with connect(os.environ["DATABASE_URL"]) as conn:
    for m in msgs:
        raw_id = uuid4()
        text = m.get("text") or ""
        try:
            with transaction(conn):
                ingest_raw_message_with_outbox(
                    conn,
                    {
                        "id": raw_id,
                        "source": "telegram",
                        "channel_id": str(m.get("chatId") or "unknown"),
                        "source_message_id": str(m["id"]),
                        "source_version": "v1",
                        "source_received_at": m.get("date") or "2026-02-19T00:00:00Z",
                        "content_hash": hashlib.sha256((str(m["id"]) + text).encode()).hexdigest(),
                        "message_text": text,
                        "raw_payload": m,
                    },
                    {
                        "event_type": "raw_message.ingested",
                        "payload": {"raw_message_id": str(raw_id)},
                        "trace_id": raw_id,
                    },
                )
                media = m.get("media")
                if isinstance(media, dict) and media.get("filename"):
                    path = f"{MEDIA_DIR}/{media['filename']}"
                    if os.path.exists(path):
                        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO media_assets (asset_id, raw_message_id, sha256, object_key, "
                                "mime, download_status, downloaded_at) VALUES (%s,%s,%s,%s,%s,'downloaded', now()) "
                                "ON CONFLICT (raw_message_id, sha256) DO NOTHING",
                                (str(uuid4()), str(raw_id), sha, path, media.get("mimeType") or "image/jpeg"),
                            )
                        n_media += 1
            n_msg += 1
        except pg_errors.UniqueViolation:
            conn.rollback()
            n_dup += 1
print(f"ingested {n_msg} messages, {n_media} media, skipped {n_dup} duplicates")
