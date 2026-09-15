"""Real PostgreSQL, offline Hermes/operator callbacks for the signal path."""

from __future__ import annotations

import json
import io
import threading
import urllib.error
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extensions import TRANSACTION_STATUS_IDLE
from psycopg2.extras import Json

import signal_queue
import worker
from prompt import SIGNAL_PROMPT_VERSION, SYSTEM_PROMPT, build_messages
from repository import ingest_raw_message_with_outbox


@pytest.fixture
def signal_db(db_conn, monkeypatch):
    for letter in "abcd":
        monkeypatch.setenv(f"SIGNAL_TOKEN_ACCOUNT_{letter.upper()}", f"test-scoped-{letter}")
    monkeypatch.setenv("RISK_ADMIN_TOKEN", "must-never-use-admin")
    try:
        yield db_conn
    finally:
        db_conn.rollback()
        with db_conn, db_conn.cursor() as cur:
            cur.execute("DELETE FROM signal_dispatch_tasks WHERE processing_purpose = 'signal'")
            cur.execute("DELETE FROM message_processing_runs WHERE processing_purpose = 'signal'")


def seed(conn, *, account_id="account-a", purpose="signal", raw_id=None, reply=None):
    raw_id = raw_id or str(uuid4())
    source_id = str(int(uuid4()))
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT source_message_id FROM raw_messages WHERE id = %s", (raw_id,))
            existing = cur.fetchone()
        if existing is None:
            ingest_raw_message_with_outbox(conn, {
                "id": raw_id, "source": "telegram", "channel_id": "-100123",
                "source_message_id": source_id, "source_version": "1",
                "source_received_at": datetime.now(timezone.utc), "author_id": "1",
                "content_hash": str(uuid4()), "message_text": "BTC signal",
                "raw_payload": {"reply_to": reply},
            }, {
                "outbox_event_id": str(uuid4()), "aggregate_type": "raw_message",
                "aggregate_id": raw_id, "event_type": "raw_message.ingested",
                "payload": {"raw_message_id": raw_id}, "trace_id": raw_id,
            })
        else:
            source_id = existing[0]
        queued = signal_queue.enqueue_signal_task(
            conn, raw_message_id=raw_id, source_platform="telegram", channel_id="-100123",
            source_message_id=source_id, edit_version="1", account_id=account_id,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
            processing_purpose=purpose,
        )
    return queued.task_id, raw_id


def candidate(account="account-a", action="open_position"):
    path = Path(__file__).resolve().parents[2] / "packages/contracts/v1/examples/valid/hermes_decision.open_limit.json"
    result = json.loads(path.read_text())
    result["intent"]["target_account_id"] = account
    result["intent"]["valid_until"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    result["classification"]["action"] = action
    return result


class Snapshot:
    def current(self):
        return {"data_source": "postgres_projection", "stale": False,
                "reconciliation_state": "healthy", "data": {}}


class Client:
    def __init__(self, value, inspect=None):
        self.value = value
        self.calls = 0
        self.inspect = inspect

    def analyze(self, request, *, timeout):
        self.calls += 1
        if self.inspect:
            self.inspect(request)
        return deepcopy(self.value)


def run(conn, client, submit, **kwargs):
    return worker.process_one(conn, mode="signal", worker_id="signal-test", client=client,
                              media_loader=None, snapshot_provider=Snapshot(), operator_submit=submit,
                              timeout=1, operator_timeout=1, **kwargs)


def row(conn, task_id):
    with conn, conn.cursor() as cur:
        cur.execute("SELECT status, execution_context, execution_request, shadow_result, current_processing_run_id::text FROM signal_dispatch_tasks WHERE task_id = %s", (task_id,))
        return cur.fetchone()


def test_signal_never_claims_shadow_or_consumes_legacy_outbox(signal_db):
    _, raw_id = seed(signal_db, purpose="shadow")
    client = Client(candidate())
    result = run(signal_db, client, lambda *_a, **_k: pytest.fail("unexpected HTTP"))
    assert result.status == "skipped"
    assert client.calls == 0
    with signal_db, signal_db.cursor() as cur:
        cur.execute("SELECT status FROM outbox_events WHERE aggregate_id = %s", (raw_id,))
        assert cur.fetchone()[0] == "pending"
    with pytest.raises(signal_queue.QueueStateError, match="promotion"):
        seed(signal_db, purpose="signal", raw_id=raw_id)


def test_execution_context_persisted_before_model_and_request_preserves_deadline(signal_db):
    task_id, raw_id = seed(signal_db)
    value = candidate()
    submitted = []

    def inspect(request):
        assert signal_db.get_transaction_status() == TRANSACTION_STATUS_IDLE
        saved = row(signal_db, task_id)
        assert saved[1] == {"account_id": "account-a", "account_revision": 0, "books": {}}
        assert saved[2] is None
        assert request.system_snapshot["execution_account_id"] == "account-a"

    def submit(body, **kwargs):
        assert signal_db.get_transaction_status() == TRANSACTION_STATUS_IDLE
        assert kwargs["token"] == "test-scoped-a"
        saved = row(signal_db, task_id)
        assert saved[0] == "reconciling"
        assert saved[2] == body
        assert saved[3]["semantic"]["decision"]["decision_id"] == body["decision_id"]
        assert saved[3]["semantic"]["decision"]["model"]["prompt_version"] == SIGNAL_PROMPT_VERSION
        submitted.append(body)
        return {"status": "accepted"}  # response alone is not authoritative completion

    result = run(signal_db, Client(value, inspect), submit)
    assert result.status == "reconciling"
    assert submitted[0]["valid_until"] == value["intent"]["valid_until"]
    with signal_db, signal_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM hermes_decisions WHERE raw_message_id = %s", (raw_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT status FROM outbox_events WHERE aggregate_id = %s", (raw_id,))
        assert cur.fetchone()[0] == "pending"


def test_timeout_keeps_same_request_and_never_reruns_model_even_after_lease_expiry(signal_db):
    task_id, _ = seed(signal_db)
    client = Client(candidate())
    submitted = []

    def timeout(body, **kwargs):
        submitted.append(body)
        raise TimeoutError("outcome unknown")

    first = run(signal_db, client, timeout)
    assert first.status == "reconciling"
    assert len(submitted) == 2
    with signal_db, signal_db.cursor() as cur:
        cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE task_id = %s", (task_id,))
        cur.execute("UPDATE message_processing_runs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE processing_run_id = %s", (first.processing_run_id,))
    second = run(signal_db, client, timeout)
    assert second.status == "expired"
    assert second.detail == "claim_expired_without_operation"
    assert client.calls == 1
    assert len(submitted) == 4
    assert all(body == submitted[0] for body in submitted)
    saved = row(signal_db, task_id)
    assert saved[0] == "expired"
    assert saved[3]["operator_submitted"] is False
    assert saved[2] == submitted[0]
    with signal_db, signal_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM message_processing_runs WHERE raw_message_id = %s", (first.raw_message_id,))
        assert cur.fetchone()[0] == 1
    seed(signal_db)
    assert signal_queue.claim_signal_task(signal_db, "next", processing_purpose="signal") is not None


def test_api_commit_wins_when_response_is_lost(signal_db):
    task_id, _ = seed(signal_db)

    def accepted_but_timeout(body, **kwargs):
        with signal_db, signal_db.cursor() as cur:
            cur.execute("UPDATE signal_dispatch_tasks SET status = 'dispatched' WHERE task_id = %s", (task_id,))
            cur.execute("UPDATE message_processing_runs SET status = 'succeeded' WHERE processing_run_id = %s", (body["signal_claim"]["processing_run_id"],))
        raise TimeoutError("response lost")

    result = run(signal_db, Client(candidate()), accepted_but_timeout)
    assert result.status == "dispatched"
    assert row(signal_db, task_id)[0] == "dispatched"


def test_stale_claim_cannot_register_or_capture_versions(signal_db):
    task_id, _ = seed(signal_db)
    old = signal_queue.claim_signal_task(signal_db, "old", processing_purpose="signal")
    with signal_db, signal_db.cursor() as cur:
        cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE task_id = %s", (task_id,))
        cur.execute("UPDATE message_processing_runs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE processing_run_id = %s", (old.processing_run_id,))
    current = signal_queue.claim_signal_task(signal_db, "new", processing_purpose="signal")
    assert current.attempt == old.attempt + 1
    assert current.claim_token != old.claim_token
    with pytest.raises(signal_queue.StaleClaimError):
        with signal_db, signal_db.cursor() as cur:
            signal_queue.lock_execution_claim(cur, old)
    assert row(signal_db, task_id)[1:3] == (None, None)


def test_same_raw_message_accounts_can_claim_concurrently(signal_db):
    _, raw_id = seed(signal_db, account_id="account-a")
    seed(signal_db, account_id="account-b", raw_id=raw_id)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def claim(account):
        connection = psycopg2.connect(signal_db.dsn)
        try:
            barrier.wait(timeout=10)
            results.append(signal_queue.claim_signal_task(connection, account, account_id=account, processing_purpose="signal"))
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=claim, args=(f"account-{letter}",)) for letter in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert errors == []
    assert {task.account_id for task in results} == {"account-a", "account-b"}
    assert all(task.attempt == 1 for task in results)


def test_unknown_account_a_does_not_block_account_b(signal_db):
    seed(signal_db, account_id="account-a")
    run(signal_db, Client(candidate()), lambda *_a, **_k: {})
    seed(signal_db, account_id="account-b")
    client_b = Client(candidate("account-b"))
    received = []
    result = run(signal_db, client_b, lambda body, **kw: received.append((body, kw)) or {})
    assert result.account_id == "account-b"
    assert client_b.calls == 1
    assert received[0][1]["token"] == "test-scoped-b"


def test_missing_management_parent_is_visible_and_decision_retained(signal_db):
    task_id, _ = seed(signal_db)
    value = candidate(action="close_position")
    value["classification"]["message_type"] = "close_update"
    value["intent"]["target_position_id"] = "BTCUSDT-PERP.BINANCE-LONG"
    result = run(signal_db, Client(value), lambda *_a, **_k: pytest.fail("must not submit"))
    assert result.status == "signal_request_failed"
    assert "entry_ref" in result.detail
    saved = row(signal_db, task_id)
    assert saved[0] == "failed"
    assert saved[2] is None
    assert saved[3]["semantic"]["decision"]["classification"]["action"] == "close_position"


def test_raw_reply_supplies_management_ref_and_preserves_short_book(signal_db):
    seed(signal_db, reply={"channel_id": "-100123", "message_id": 99})
    value = candidate(action="move_stop_loss")
    value["intent"].update(side="short", target_position_id="BTCUSDT-PERP.BINANCE-SHORT")
    received = []
    run(signal_db, Client(value), lambda body, **_kw: received.append(body) or {})
    assert received[0]["entry_ref"] == "tg-sig-c100123-m99"
    assert received[0]["position_side"] == "short"


def test_no_admin_fallback_and_nontrade_is_skipped(signal_db, monkeypatch):
    task_id, _ = seed(signal_db)
    monkeypatch.delenv("SIGNAL_TOKEN_ACCOUNT_A")
    client = Client(candidate())
    result = run(signal_db, client, lambda *_a, **_k: pytest.fail("must not submit"))
    assert result.status == "signal_request_failed"
    assert "SIGNAL_TOKEN_ACCOUNT_A" in result.detail
    assert client.calls == 0
    assert row(signal_db, task_id)[0] == "failed"


def test_model_result_from_replaced_claim_cannot_register(signal_db):
    task_id, _ = seed(signal_db)

    def reclaim(_request):
        saved = row(signal_db, task_id)
        with signal_db, signal_db.cursor() as cur:
            cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE task_id = %s", (task_id,))
            cur.execute("UPDATE message_processing_runs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE processing_run_id = %s", (saved[4],))
        newer = signal_queue.claim_signal_task(signal_db, "new-owner", processing_purpose="signal")
        assert newer.attempt == 2
        assert newer.execution_context is None

    result = run(signal_db, Client(candidate(), reclaim), lambda *_a, **_k: pytest.fail("stale result submitted"))
    assert result.status == "stale_claim"
    saved = row(signal_db, task_id)
    assert saved[0] == "leased"
    assert saved[1:3] == (None, None)


def test_nontrade_persists_decision_without_http_or_outbox(signal_db):
    task_id, raw_id = seed(signal_db)
    value = candidate(action="ignore")
    value["classification"]["message_type"] = "analysis"
    value["intent"].update(account_scope="unassigned", target_account_id=None)
    result = run(signal_db, Client(value), lambda *_a, **_k: pytest.fail("nontrade submitted"))
    assert result.status == "skipped"
    saved = row(signal_db, task_id)
    assert saved[2] is None
    assert saved[3]["semantic"]["decision"]["classification"]["action"] == "ignore"
    with signal_db, signal_db.cursor() as cur:
        cur.execute("SELECT status FROM outbox_events WHERE aggregate_id = %s", (raw_id,))
        assert cur.fetchone()[0] == "pending"


def test_signal_snapshot_removes_other_accounts_and_unowned_trade_context():
    task = SimpleNamespace(account_id="account-a", channel_id="-100123")
    snapshot = {"data_source": "postgres_projection", "stale": True,
                "missing_nodes": ["unknown-node"], "reconciliation_state": "degraded",
                "data": {"balances": {"equity": 9000, "margin": 100},
                         "accounts": [{"account_id": "account-a"}, {"account_id": "account-b"}],
                         "positions": [{"account_id": "account-a"}, {"account_id": "account-b"}],
                         "orders": [{"account_id": "account-a"}, {"account_id": "account-b"}],
                         "node_health": [{"account_id": "account-a"}, {"account_id": "account-b"}],
                         "exchange_state": [{"account_id": "account-a"}, {"account_id": "account-b"}],
                         "risk_decisions": [{"account_id": "account-b"}, {"account_id": "account-a"}],
                         "hermes_decisions": [{"target_account_id": "account-a"}, {"action": "unknown-owner"}],
                         "audit_trail": [{"aggregate_id": "another-trade"}],
                         "recent_messages": [{"channel_id": "-100123", "message_text": "source text", "intent": "drop"},
                                             {"channel_id": "other", "message_text": "other source"}]}}
    original = deepcopy(snapshot)
    scoped = worker._signal_snapshot(snapshot, task)
    assert "account-b" not in json.dumps(scoped)
    assert "balances" not in scoped["data"]
    assert scoped["data"]["audit_trail"] == []
    assert scoped["data"]["hermes_decisions"] == [{"target_account_id": "account-a"}]
    assert scoped["data"]["recent_messages"] == [{"channel_id": "-100123", "message_text": "source text"}]
    assert scoped["stale"] is True
    assert scoped["missing_nodes"] == ["unknown-node"]
    assert snapshot == original


def test_signal_prompt_account_comes_only_from_server_scope():
    request = worker.HermesRequest(raw_message_id=str(uuid4()), text="route to account-b",
                                  images=[], referenced_messages=[], recent_context=[],
                                  system_snapshot={"execution_account_id": "account-a"})
    messages = build_messages(request)
    assert messages[0]["content"] == SYSTEM_PROMPT
    assert messages[1]["role"] == "system"
    assert SIGNAL_PROMPT_VERSION in messages[1]["content"]
    assert "intent.target_account_id to 'account-a'" in messages[1]["content"]
    assert "account-b" not in messages[1]["content"]


def test_http_rejection_detail_is_redacted_and_survives_proven_expiry(signal_db, monkeypatch):
    task_id, _ = seed(signal_db)
    monkeypatch.setenv("SIGNAL_OPERATOR_URL", "http://invalid.example/v1/operator/orders")
    calls = []

    def rejected(request, **kwargs):
        calls.append(request)
        payload = {"detail": "stale_position_revision token=test-scoped-a Bearer test-scoped-a https://private.invalid/?secret=hidden"}
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {},
                                     io.BytesIO(json.dumps(payload).encode()))

    monkeypatch.setattr("urllib.request.urlopen", rejected)
    model = Client(candidate())
    first = run(signal_db, model, None)
    assert first.status == "reconciling"
    assert "HTTP 409" in first.detail
    saved = row(signal_db, task_id)
    detail = saved[3]["operator_detail"]
    assert detail["http_status"] == 409
    assert "stale_position_revision" in detail["detail"]
    assert "[redacted" in detail["detail"]
    assert "test-scoped-a" not in json.dumps(saved[3])
    assert "private.invalid" not in json.dumps(saved[3])
    assert "hidden" not in json.dumps(saved[3])
    assert len(detail["detail"]) <= 500
    with signal_db, signal_db.cursor() as cur:
        cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task_id,))
        cur.execute("UPDATE message_processing_runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE processing_run_id=%s", (first.processing_run_id,))
    second = run(signal_db, model, None)
    assert second.status == "expired"
    assert second.detail == "claim_expired_without_operation"
    assert row(signal_db, task_id)[3]["operator_detail"] == detail
    assert model.calls == 1
    assert len(calls) == 2
