#!/usr/bin/env python3
"""Replay a real Telegram signal corpus through the Window A pipeline.

Drives the real 80-message / 50-image corpus through ingress -> outbox queue ->
Hermes worker (loading the REAL images) -> decision gateway, on a local PostgreSQL.
The Hermes model call itself is mocked (there is no real multimodal endpoint in a
local run) and deterministically returns needs_review, so no risk is ever fabricated.
This exercises real-data ingestion, real media handling (sha256 + load), idempotency,
and fail-closed processing end to end.

Usage:
  DATABASE_URL=postgresql://... \
    python scripts/replay_real_corpus.py --corpus <dir-with-messages.json+media/>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2

REPO = Path(__file__).resolve().parents[1]
for _rel in (
    "services/ingress",
    "services/hermes-worker",
    "services/hermes-worker/queue",
    "services/control-plane",
    "services/control-plane/db",
    "services/control-plane/decision_gateway",
    "services/control-plane/risk",
):
    sys.path.insert(0, str(REPO / _rel))

import gateway  # noqa: E402
import worker  # noqa: E402
from ingress.service import ingest_raw_telegram_update  # noqa: E402
from policy import RiskPolicy  # noqa: E402


class MockHermes:
    """No real multimodal endpoint locally -> fail-safe needs_review (no fabricated risk)."""

    def analyze(self, request, *, timeout):
        return {
            "classification": {
                "message_type": "ambiguous",
                "action": "needs_review",
                "ambiguous": True,
                "ambiguity_reasons": ["local replay: no real Hermes endpoint configured"],
            },
            "intent": {
                "account_scope": "unassigned",
                "target_account_id": None,
                "target_position_id": None,
                "instrument_symbol": None,
                "side": None,
                "entry": {"type": "none", "price": None, "price_min": None, "price_max": None},
                "stop_loss": None,
                "take_profits": [],
                "leverage": None,
                "valid_until": None,
            },
            "evidence": [{"images_seen": len(request.images), "text_chars": len(request.text or "")}],
        }


class FileMediaLoader:
    def load(self, object_key):
        data = Path(object_key).read_bytes()
        return "image/jpeg", base64.b64encode(data).decode("ascii")


class FreshSnapshot:
    def current(self):
        return {
            "data_source": "postgres_projection",
            "snapshot_id": str(uuid4()),
            "generated_at": "2026-06-19T12:00:00Z",
            "last_execution_event_at": None,
            "projection_lag_ms": 0,
            "stale": False,
            "missing_nodes": [],
            "reconciliation_state": "healthy",
            "data": {},
        }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count(db_url: str, table: str) -> int:
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            return cur.fetchone()[0]
    finally:
        conn.close()


def ingest_corpus(messages, corpus_dir: Path, db_url: str) -> dict:
    inserted = duplicates = media = missing_media = 0
    for message in messages:
        media_assets = []
        media_meta = message.get("media")
        if media_meta:
            image = corpus_dir / media_meta["path"]
            if image.exists():
                media_assets = [{
                    "sha256": _sha256_file(image),
                    "object_key": str(image),
                    "mime": media_meta.get("mimeType", "image/jpeg"),
                    "download_status": "downloaded",
                }]
                media += 1
            else:
                missing_media += 1
        payload = {
            "source": "telegram",
            "channel_id": message["chatId"],
            "source_message_id": str(message["id"]),
            "source_version": "v1",
            "source_received_at": message["date"],
            "author_id": message.get("sender") or None,
            "message_text": message.get("text"),
            "message_kind": "photo" if media_assets else "text",
            "raw_payload": {"chat_title": message.get("chatTitle")},
            "media_assets": media_assets,
        }
        result = ingest_raw_telegram_update(payload, db_url)
        if result["inserted"]:
            inserted += 1
        else:
            duplicates += 1
    return {"inserted": inserted, "duplicates": duplicates, "media": media, "missing_media": missing_media}


def drain_worker(db_url: str) -> dict:
    conn = psycopg2.connect(db_url)
    outcomes: dict[str, int] = {}
    try:
        while True:
            result = worker.process_one(
                conn,
                worker_id="real-corpus-replay",
                client=MockHermes(),
                media_loader=FileMediaLoader(),
                snapshot_provider=FreshSnapshot(),
                model_version="local-replay-mock",
                timeout=5,
                lease_seconds=120,
            )
            if result.status == "skipped":
                break
            outcomes[result.status] = outcomes.get(result.status, 0) + 1
    finally:
        conn.close()
    return outcomes


def drain_gateway(db_url: str) -> dict:
    conn = psycopg2.connect(db_url)
    outcomes: dict[str, int] = {}
    try:
        while True:
            result = gateway.process_one_decision(conn, policy=RiskPolicy(default_account_id="acct-replay"))
            if result is None:
                break
            outcomes[result["status"]] = outcomes.get(result["status"], 0) + 1
    finally:
        conn.close()
    return outcomes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Replay a real signal corpus through Window A")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args(argv)
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")

    corpus_dir = Path(args.corpus)
    messages = json.loads((corpus_dir / "messages.json").read_text(encoding="utf-8"))

    pass1 = ingest_corpus(messages, corpus_dir, args.database_url)
    raw_after_1 = _count(args.database_url, "raw_messages")
    media_after_1 = _count(args.database_url, "media_assets")

    pass2 = ingest_corpus(messages, corpus_dir, args.database_url)  # idempotency
    raw_after_2 = _count(args.database_url, "raw_messages")

    worker_outcomes = drain_worker(args.database_url)
    gateway_outcomes = drain_gateway(args.database_url)

    report = {
        "corpus": str(corpus_dir),
        "messages": len(messages),
        "ingest_pass1": pass1,
        "raw_messages_after_pass1": raw_after_1,
        "media_assets_after_pass1": media_after_1,
        "ingest_pass2": pass2,
        "raw_messages_after_pass2": raw_after_2,
        "idempotent": raw_after_1 == raw_after_2 and pass2["inserted"] == 0,
        "worker_outcomes": worker_outcomes,
        "hermes_decisions": _count(args.database_url, "hermes_decisions"),
        "gateway_outcomes": gateway_outcomes,
        "risk_decisions": _count(args.database_url, "risk_decisions"),
        "trade_intents": _count(args.database_url, "trade_intents"),
        "note": (
            "Hermes model call is MOCKED (no real multimodal endpoint in a local run) and returns "
            "needs_review, so zero risk is fabricated. Real ingestion, real media (sha256 + load), "
            "idempotency, worker, and gateway are exercised on the REAL corpus. The true gate #8 "
            "(real Hermes classifications + approvals) needs HERMES_API_URL/KEY/MODEL."
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["idempotent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
