from __future__ import annotations

import base64
from uuid import uuid4

import pytest

import worker
from connection import transaction
from hermes_client import HermesTimeoutError, HermesUnavailableError
from repository import ingest_raw_message_with_outbox


# --- test doubles -----------------------------------------------------------------


class MockHermesClient:
    def __init__(self, *, candidate=None, raises=None):
        self._candidate = candidate
        self._raises = raises
        self.calls = 0

    def analyze(self, request, *, timeout):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._candidate


class MockMediaLoader:
    def __init__(self):
        self.loaded = []

    def load(self, object_key):
        self.loaded.append(object_key)
        return "image/png", base64.b64encode(b"fake-image-bytes").decode("ascii")


class StaticSnapshot:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def current(self):
        return self._snapshot


def fresh_snapshot():
    return {
        "data_source": "postgres_projection",
        "snapshot_id": str(uuid4()),
        "generated_at": "2026-06-19T12:00:00Z",
        "last_execution_event_at": None,
        "projection_lag_ms": 0,
        "stale": False,
        "missing_nodes": [],
        "reconciliation_state": "healthy",
        "data": {"accounts": [], "orders": [], "positions": []},
    }


def valid_candidate(action="open_position", message_type="new_signal", side="long"):
    return {
        "classification": {
            "message_type": message_type,
            "action": action,
            "ambiguous": False,
            "ambiguity_reasons": [],
        },
        "intent": {
            "account_scope": "unassigned",
            "target_account_id": None,
            "target_position_id": None,
            "instrument_symbol": "BTCUSDT",
            "side": side,
            "entry": {"type": "market", "price": None, "price_min": None, "price_max": None},
            "stop_loss": None,
            "take_profits": [],
            "leverage": None,
            "valid_until": None,
        },
        "evidence": [],
        "confidence": 0.8,
    }


# --- helpers ----------------------------------------------------------------------


def seed_message(conn, *, text="BTC long entry now", source_message_id=None):
    raw_id = uuid4()
    smid = source_message_id or f"m-{raw_id}"
    with transaction(conn):
        ingest_raw_message_with_outbox(
            conn,
            {
                "id": raw_id,
                "source": "telegram",
                "channel_id": "signals",
                "source_message_id": smid,
                "source_version": "v1",
                "source_received_at": "2026-06-19T12:00:00Z",
                "author_id": "9001",
                "content_hash": f"sha-{smid}",
                "message_text": text,
                "raw_payload": {"text": text},
            },
            {
                "outbox_event_id": uuid4(),
                "aggregate_type": "raw_message",
                "aggregate_id": raw_id,
                "event_type": "raw_message.ingested",
                "payload": {"raw_message_id": str(raw_id)},
                "trace_id": raw_id,
            },
        )
    return str(raw_id)


def add_media(conn, raw_message_id, *, download_status="downloaded"):
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO media_assets
                (asset_id, raw_message_id, sha256, object_key, mime, download_status)
            VALUES (%s, %s, %s, %s, 'image/png', %s)
            """,
            (str(uuid4()), raw_message_id, f"sha-{uuid4()}", "obj/key.png", download_status),
        )


def run_worker(conn, client, *, media_loader=None, snapshot=None):
    return worker.process_one(
        conn,
        worker_id="w1",
        client=client,
        media_loader=media_loader or MockMediaLoader(),
        snapshot_provider=StaticSnapshot(snapshot or fresh_snapshot()),
        model_version="mock-hermes",
        timeout=5,
        lease_seconds=30,
    )


def _count(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _run_status(conn, run_id):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM message_processing_runs WHERE processing_run_id = %s", (run_id,))
        return cur.fetchone()[0]


# --- happy paths ------------------------------------------------------------------


@pytest.mark.parametrize("with_media", [False, True])
def test_text_and_image_messages_persist_valid_decision(db_conn, with_media):
    raw_id = seed_message(db_conn)
    if with_media:
        add_media(db_conn, raw_id)
    client = MockHermesClient(candidate=valid_candidate())

    result = run_worker(db_conn, client)

    assert result.status == "succeeded"
    assert client.calls == 1
    assert _count(db_conn, "SELECT count(*) FROM hermes_decisions WHERE raw_message_id = %s", (raw_id,)) == 1
    assert _run_status(db_conn, result.processing_run_id) == "succeeded"
    assert _count(
        db_conn,
        "SELECT count(*) FROM audit_events WHERE raw_message_id = %s AND event_type = 'hermes.decided'",
        (raw_id,),
    ) == 1


def test_decision_persists_classification_and_intent(db_conn):
    raw_id = seed_message(db_conn)
    client = MockHermesClient(candidate=valid_candidate(action="open_position", side="long"))

    result = run_worker(db_conn, client)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT message_type::text, action::text, side::text, instrument_symbol, model_provider, prompt_version "
            "FROM hermes_decisions WHERE decision_id = %s",
            (result.decision_id,),
        )
        row = cur.fetchone()
    assert row == ("new_signal", "open_position", "long", "BTCUSDT", "hermes", "hermes-trader-v1")


# --- fail-closed paths ------------------------------------------------------------


def test_invalid_model_output_fails_closed(db_conn):
    raw_id = seed_message(db_conn)
    bad = valid_candidate()
    bad["classification"]["message_type"] = "not_a_real_enum"
    client = MockHermesClient(candidate=bad)

    result = run_worker(db_conn, client)

    assert result.status == "invalid_decision_schema"
    assert _count(db_conn, "SELECT count(*) FROM hermes_decisions WHERE raw_message_id = %s", (raw_id,)) == 0
    assert _run_status(db_conn, result.processing_run_id) == "hermes_failed"


def test_timeout_sets_hermes_timeout_and_no_decision(db_conn):
    raw_id = seed_message(db_conn)
    client = MockHermesClient(raises=HermesTimeoutError("deadline"))

    result = run_worker(db_conn, client)

    assert result.status == "hermes_timeout"
    assert _run_status(db_conn, result.processing_run_id) == "hermes_timeout"
    assert _count(db_conn, "SELECT count(*) FROM hermes_decisions WHERE raw_message_id = %s", (raw_id,)) == 0


def test_unavailable_hermes_fails_closed(db_conn):
    raw_id = seed_message(db_conn)
    client = MockHermesClient(raises=HermesUnavailableError("not configured"))

    result = run_worker(db_conn, client)

    assert result.status == "hermes_unavailable"
    assert _run_status(db_conn, result.processing_run_id) == "hermes_failed"
    assert _count(db_conn, "SELECT count(*) FROM hermes_decisions WHERE raw_message_id = %s", (raw_id,)) == 0


def test_missing_media_fails_closed(db_conn):
    raw_id = seed_message(db_conn)
    add_media(db_conn, raw_id, download_status="failed")
    client = MockHermesClient(candidate=valid_candidate())

    result = run_worker(db_conn, client)

    assert result.status == "media_failed"
    assert client.calls == 0  # never reached the model
    assert _count(db_conn, "SELECT count(*) FROM hermes_decisions WHERE raw_message_id = %s", (raw_id,)) == 0


def test_stale_snapshot_fails_closed(db_conn):
    raw_id = seed_message(db_conn)
    stale = fresh_snapshot()
    stale["stale"] = True
    client = MockHermesClient(candidate=valid_candidate())

    result = run_worker(db_conn, client, snapshot=stale)

    assert result.status == "context_stale"
    assert client.calls == 0
    assert _count(db_conn, "SELECT count(*) FROM hermes_decisions WHERE raw_message_id = %s", (raw_id,)) == 0


# --- queue semantics --------------------------------------------------------------


def test_no_pending_returns_skipped(db_conn):
    result = run_worker(db_conn, MockHermesClient(candidate=valid_candidate()))
    assert result.status == "skipped"


def test_success_is_not_reprocessed_into_duplicate_decision(db_conn):
    raw_id = seed_message(db_conn)
    client = MockHermesClient(candidate=valid_candidate())

    first = run_worker(db_conn, client)
    second = run_worker(db_conn, client)

    assert first.status == "succeeded"
    assert second.status == "skipped"  # outbox consumed -> nothing left to claim
    assert _count(db_conn, "SELECT count(*) FROM hermes_decisions WHERE raw_message_id = %s", (raw_id,)) == 1


def test_worker_source_has_no_semantic_regex(db_conn):
    # semantics must come from Hermes, never regex over the message text.
    src = (worker.__file__, __import__("hermes_client").__file__)
    for path in src:
        text = open(path, encoding="utf-8").read()
        assert "import re" not in text, f"{path} must not use regex for semantics"
