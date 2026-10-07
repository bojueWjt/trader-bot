"""Actual signal operator ingress using PostgreSQL's operator-query login."""

from uuid import uuid4

import psycopg2
from psycopg2.extensions import make_dsn
import pytest
from psycopg2.extras import Json
from datetime import datetime, timedelta, timezone

import read_api
from position_revision import invalidate_account, invalidate_book
from signal_handoff import load_request
from test_operator_add_position import (
    ACCOUNT_B, _entry_body, _same_side_books, client as base_client,
)
from test_signal_handoff import _seed, signal_db


SIGNAL_TOKEN = "signal-account-b-token"


@pytest.fixture()
def signal_client(base_client, signal_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_TOKEN_ACCOUNT_B", SIGNAL_TOKEN)
    monkeypatch.setenv("DATABASE_URL", make_dsn(signal_db, user="trader_v3_operator_query"))
    monkeypatch.setattr(read_api, "_channel_risk_capital_addon", lambda _channel, _account: 0.0)
    return base_client


def _request(url, *, action="open_position", related=None, purpose="signal"):
    entry = _entry_body(action, f"signal-ingress-{uuid4()}")
    entry.update({
        "source": "hermes-signal:telegram", "created_by_service": "hermes-signal:telegram",
        "authorized_by_type": "channel", "authorized_by_id": "-100123",
        "source_channel": "-100123", "channel": "-100123",
    })
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        source = None
        if related is not None:
            cur.execute("SELECT source_message_id,edit_version FROM signal_dispatch_tasks WHERE task_id=%s", (related,))
            message_id, version = cur.fetchone()
            source = {"source_message_id": message_id, "edit_version": str(int(version) + 1)}
        return _seed(cur, account=ACCOUNT_B, related=related, purpose=purpose,
                     request_overrides=entry, source_overrides=source)


def _post(client, body):
    return client.post("/v1/operator/orders", json=body, headers={
        "Authorization": f"Bearer {SIGNAL_TOKEN}", "X-Request-Id": str(uuid4()),
    })


def _counts(url):
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute("""SELECT
            (SELECT count(*) FROM trade_intents),
            (SELECT count(*) FROM outbox_events),
            (SELECT count(*) FROM risk_decisions),
            (SELECT count(*) FROM raw_messages WHERE source='operator')""")
        return cur.fetchone()


@pytest.mark.parametrize("source_ts,code", [
    (None, "signal_source_time_unknown"),
    ("not-a-date", "signal_source_time_unknown"),
    ("2020-01-01T00:00:00+00:00", "signal_source_expired"),
])
def test_entry_requires_real_fresh_source_time(signal_client, signal_db, source_ts, code):
    body = _request(signal_db)
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE raw_messages SET raw_payload=%s WHERE id=%s", (Json({"source_ts": source_ts}), body["raw_message_id"]))
    counts = _counts(signal_db)
    response = _post(signal_client, body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == code
    assert _counts(signal_db) == counts


def test_entry_deadline_cannot_extend_source_expiry(signal_client, signal_db):
    body = _request(signal_db)
    source_ts = datetime.now(timezone.utc) - timedelta(minutes=29)
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE raw_messages SET raw_payload=%s WHERE id=%s", (Json({"source_ts": source_ts.isoformat()}), body["raw_message_id"]))
    response = _post(signal_client, body)
    assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()["valid_until"]) == source_ts + timedelta(minutes=30)


def test_signal_accepts_actual_intent_and_replays_after_claim_expiry(signal_client, signal_db):
    body = _request(signal_db)
    response = _post(signal_client, body)
    assert response.status_code == 200, response.text
    intent_id = response.json()["intent_id"]
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        cur.execute("""SELECT t.operator_intent_id::text,t.status,r.status,i.account_id,i.order_plan
            FROM signal_dispatch_tasks t
            JOIN message_processing_runs r ON r.processing_run_id=t.current_processing_run_id
            JOIN trade_intents i ON i.intent_id=t.operator_intent_id WHERE t.task_id=%s""",
            (body["signal_claim"]["task_id"],))
        row = cur.fetchone()
        assert row[:4] == (intent_id, "dispatched", "succeeded", ACCOUNT_B)
        assert row[4]["signal_execution"]["task_id"] == body["signal_claim"]["task_id"]
        assert row[4]["principal"] == {
            "kind": "signal", "actor_id": "signal:account-b", "scope": "account", "account_id": ACCOUNT_B,
        }
        cur.execute("SELECT actor,payload FROM audit_events WHERE intent_id=%s AND event_type='operator_order'", (intent_id,))
        audit_actor, audit_payload = cur.fetchone()
        assert audit_actor == "signal:account-b"
        assert audit_payload["principal"]["kind"] == "signal"
        cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at=now()-interval '1 hour'")
        cur.execute("UPDATE message_processing_runs SET lease_expires_at=now()-interval '1 hour' WHERE processing_purpose='signal'")
    counts = _counts(signal_db)
    replay = _post(signal_client, body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["intent_id"] == intent_id
    assert replay.json()["replay"] is True
    assert _counts(signal_db) == counts


def test_source_edit_has_distinct_key_and_cannot_repeat_accepted_risk_increase(signal_client, signal_db):
    original = _request(signal_db)
    first = _post(signal_client, original)
    assert first.status_code == 200, first.text
    edit = _request(signal_db, related=original["signal_claim"]["task_id"])
    assert edit["source_identity"]["source_message_id"] == original["source_identity"]["source_message_id"]
    assert edit["source_identity"]["edit_version"] == "2"
    counts = _counts(signal_db)
    rejected = _post(signal_client, edit)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"] == "signal_edit_requires_reconciliation"
    assert _counts(signal_db) == counts


def test_unaccepted_source_edit_has_its_own_stable_business_key(signal_client, signal_db):
    original = _request(signal_db)
    edit = _request(signal_db, related=original["signal_claim"]["task_id"])
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        original_context = load_request(cur, ACCOUNT_B, original)
        edit_context = load_request(cur, ACCOUNT_B, edit)
        assert original_context["idempotency_key"] != edit_context["idempotency_key"]
    response = _post(signal_client, edit)
    assert response.status_code == 200, response.text


def test_stale_add_snapshot_rolls_back_all_operation_records(signal_client, signal_db):
    _same_side_books(signal_db)
    body = _request(signal_db, action="add_position")
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        read_api._account_risk_increase_lock(cur, ACCOUNT_B)
        invalidate_book(cur, ACCOUNT_B, body["symbol"], "LONG", "close-before-model-result")
    counts = _counts(signal_db)
    response = _post(signal_client, body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "stale_position_revision"
    assert _counts(signal_db) == counts
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT operator_intent_id,status FROM signal_dispatch_tasks WHERE task_id=%s", (body["signal_claim"]["task_id"],))
        assert cur.fetchone() == (None, "leased")


def test_delayed_open_cannot_cross_close_all_even_when_account_is_flat(signal_client, signal_db):
    body = _request(signal_db)
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        read_api._account_risk_increase_lock(cur, ACCOUNT_B)
        invalidate_account(cur, ACCOUNT_B, "close-all-before-model-result")
    counts = _counts(signal_db)
    response = _post(signal_client, body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "stale_position_revision"
    assert _counts(signal_db) == counts


@pytest.mark.parametrize("forgery", ["human_authority", "cross_account", "shadow", "payload", "old_attempt"])
def test_signal_authority_and_payload_cannot_be_forged(signal_client, signal_db, forgery):
    body = _request(signal_db, purpose="shadow" if forgery == "shadow" else "signal")
    if forgery == "human_authority":
        body["authorized_by_type"] = "user"
        body["authorized_by_id"] = "owner"
    elif forgery == "cross_account":
        body["account_id"] = "account-a"
    elif forgery == "payload":
        body["stop_loss"] += 0.01
    elif forgery == "old_attempt":
        body["signal_claim"]["attempt"] += 1
    counts = _counts(signal_db)
    response = _post(signal_client, body)
    assert response.status_code in (403, 409), response.text
    assert _counts(signal_db) == counts
