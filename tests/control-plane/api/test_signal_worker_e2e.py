"""Offline worker -> real operator ingress -> PostgreSQL acceptance and replay."""

from __future__ import annotations

import importlib.util
import sys
import threading
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE

from test_operator_add_position import ACCOUNT_B, ENTRY_PRICE, STOP_LOSS, SYMBOL, _same_side_books
from test_signal_operator_ingress import SIGNAL_TOKEN, _request, base_client, signal_client, signal_db
from test_signal_handoff import _seed, _intent


ROOT = Path(__file__).resolve().parents[3]
HERMES = ROOT / "services" / "hermes-worker"
for path in (HERMES, HERMES / "queue"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Other suites also have modules named worker. Load this real implementation
# under a test-specific name while retaining its ordinary dependency imports.
spec = importlib.util.spec_from_file_location("signal_e2e_hermes_worker", HERMES / "worker.py")
assert spec is not None and spec.loader is not None
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)

import signal_queue  # noqa: E402
from repository import ingest_raw_message_with_outbox  # noqa: E402
from snapshot import build_system_snapshot, validate_snapshot  # noqa: E402
from signal_handoff import accept_operation, load_request  # noqa: E402


class PostgresSnapshot:
    def __init__(self, conn):
        self.conn = conn

    def current(self):
        with self.conn:
            snapshot = build_system_snapshot(self.conn)
        validate_snapshot(snapshot)
        assert snapshot["stale"] is False
        return snapshot


class Model:
    def __init__(self, conn, deadline):
        self.conn = conn
        self.calls = 0
        self.deadline = deadline

    def analyze(self, request, *, timeout):
        self.calls += 1
        assert self.conn.get_transaction_status() == TRANSACTION_STATUS_IDLE
        assert request.system_snapshot["execution_account_id"] == ACCOUNT_B
        assert request.system_snapshot["data_source"] == "postgres_projection"
        assert request.system_snapshot["generated_at"]
        return {
            "classification": {"message_type": "new_signal", "action": "add_position",
                               "ambiguous": False, "ambiguity_reasons": []},
            "intent": {
                "account_scope": "single", "target_account_id": ACCOUNT_B,
                "target_position_id": None, "instrument_symbol": SYMBOL, "side": "long",
                "entry": {"type": "limit", "price": ENTRY_PRICE, "price_min": None, "price_max": None},
                "stop_loss": STOP_LOSS, "take_profits": [1.65], "leverage": 3,
                "valid_until": self.deadline.isoformat(),
            },
            "evidence": ["original Telegram message requests adding to the long"],
            "confidence": 0.9,
        }


def test_worker_actual_operator_accepts_once_after_lost_response(signal_client, signal_db, monkeypatch):
    _same_side_books(signal_db)
    monkeypatch.setenv("SIGNAL_TOKEN_ACCOUNT_A", "signal-account-a-token")
    monkeypatch.setenv("VIEWER_TOKEN", "signal-e2e-reader")
    raw_id = str(uuid4())
    source_message_id = str(int(uuid4()))
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=5)
    requests = []
    replies = []
    with closing(psycopg2.connect(signal_db)) as conn:
        with conn:
            ingest_raw_message_with_outbox(conn, {
                "id": raw_id, "source": "telegram", "channel_id": "-100123",
                "source_message_id": source_message_id, "source_version": "1",
                "source_received_at": now, "author_id": "e2e-source",
                "content_hash": uuid4().hex * 2, "message_text": "Add ATOM long at 1.55, stop 1.50",
                "raw_payload": {"source_ts": now.isoformat(), "receive_ts": now.isoformat()},
            }, {
                "outbox_event_id": str(uuid4()), "aggregate_type": "raw_message",
                "aggregate_id": raw_id, "event_type": "raw_message.ingested",
                "payload": {"raw_message_id": raw_id}, "trace_id": raw_id,
            })
            queued = signal_queue.enqueue_signal_task(
                conn, raw_message_id=raw_id, source_platform="telegram", channel_id="-100123",
                source_message_id=source_message_id, edit_version="1", account_id=ACCOUNT_B,
                expires_at=now + timedelta(minutes=20), processing_purpose="signal",
            )
        model = Model(conn, deadline)

        def submit(body, *, account_id, token, timeout):
            assert conn.get_transaction_status() == TRANSACTION_STATUS_IDLE
            assert account_id == ACCOUNT_B
            assert token == SIGNAL_TOKEN
            requests.append(deepcopy(body))
            response = signal_client.post("/v1/operator/orders", json=body,
                                          headers={"Authorization": f"Bearer {token}"})
            # Retain real endpoint errors; the worker intentionally treats
            # unknown callback failures as reconciling instead of fabricating a
            # negative order outcome.
            replies.append((response.status_code, response.json()))
            if len(requests) == 1:
                raise TimeoutError("accepted response was lost in transit")
            return response.json()

        result = worker.process_signal_one(
            conn, worker_id="signal-worker-e2e", client=model, media_loader=None,
            snapshot_provider=PostgresSnapshot(conn), operator_submit=submit,
            account_id=ACCOUNT_B, timeout=1, operator_timeout=1,
        )
        assert [status for status, _ in replies] == [200, 200], replies
        assert result.status == "dispatched", result
        assert model.calls == 1
        assert len(requests) == 2
        assert requests[0] == requests[1]
        assert requests[0]["valid_until"] == deadline.isoformat()
        assert replies[1][1]["replay"] is True
        intent_id = replies[0][1]["intent_id"]
        assert replies[1][1]["intent_id"] == intent_id
        with conn, conn.cursor() as cur:
            cur.execute("""SELECT t.status,r.status,t.operator_intent_id::text,i.valid_until,
                                i.order_plan,t.shadow_result,t.execution_request
                FROM signal_dispatch_tasks t
                JOIN message_processing_runs r ON r.processing_run_id=t.current_processing_run_id
                JOIN trade_intents i ON i.intent_id=t.operator_intent_id WHERE t.task_id=%s""",
                (queued.task_id,))
            task_status, run_status, bound_intent, actual_deadline, plan, saved, registered = cur.fetchone()
            assert (task_status, run_status, bound_intent) == ("dispatched", "succeeded", intent_id)
            assert actual_deadline == deadline
            assert registered == requests[0]
            original = saved["semantic"]["decision"]
            assert original["raw_message_id"] == raw_id
            assert original["decision_id"] == requests[0]["decision_id"]
            assert plan["signal_execution"]["raw_message_id"] == raw_id
            assert plan["signal_execution"]["processing_run_id"] == result.processing_run_id
            assert plan["signal_execution"]["source_identity"]["source_message_id"] == source_message_id
            cur.execute("SELECT count(*) FROM trade_intents")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT status FROM outbox_events WHERE aggregate_type='raw_message' AND aggregate_id=%s", (raw_id,))
            assert cur.fetchone() == ("pending",)
            cur.execute("SELECT count(*) FROM outbox_events WHERE aggregate_type='trade_intent' AND aggregate_id=%s", (intent_id,))
            assert cur.fetchone()[0] == 1

    own = signal_client.get(f"/v1/operator/orders/{intent_id}",
                            headers={"Authorization": f"Bearer {SIGNAL_TOKEN}"})
    assert own.status_code == 200, own.text
    assert own.json()["intent"]["account_id"] == ACCOUNT_B
    other = signal_client.get(f"/v1/operator/orders/{intent_id}",
                              headers={"Authorization": "Bearer signal-account-a-token"})
    assert other.status_code == 403, other.text
    trace = signal_client.get(f"/v1/intents/{intent_id}/trace",
                              headers={"Authorization": "Bearer signal-e2e-reader"})
    assert trace.status_code == 200, trace.text
    data = trace.json()["data"]
    assert data["raw_message"]["id"] == raw_id
    assert data["raw_message"]["channel_id"] == "-100123"
    assert data["hermes_decision"]["decision_id"] == original["decision_id"]


def test_reconciliation_waits_for_acceptance_lock_and_keeps_committed_operation(signal_db):
    with closing(psycopg2.connect(signal_db)) as setup:
        with setup, setup.cursor() as cur:
            body = _seed(cur, status="reconciling")
            intent_id = _intent(cur)
            cur.execute(f"SELECT {signal_queue._TASK_RETURNING} FROM signal_dispatch_tasks WHERE task_id=%s", (body["signal_claim"]["task_id"],))
            task = signal_queue._task_from_row(cur.fetchone())
    started = threading.Event()
    finished = threading.Event()
    results = []
    errors = []

    def reconcile():
        with closing(psycopg2.connect(signal_db)) as conn:
            started.set()
            try:
                results.append(signal_queue.record_operator_uncertainty(conn, task, "http_timeout"))
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

    with closing(psycopg2.connect(signal_db)) as acceptance:
        with acceptance.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s), 0)", (task.account_id,))
            accepted = load_request(cur, task.account_id, body, lock=True)
            accept_operation(cur, accepted, intent_id)
            # Hold the successful acceptance uncommitted until reconciliation
            # arrives. Expiry must wait, then observe its operator_intent_id.
            cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task.task_id,))
            cur.execute("UPDATE message_processing_runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE processing_run_id=%s", (task.processing_run_id,))
        thread = threading.Thread(target=reconcile)
        thread.start()
        assert started.wait(timeout=5)
        assert not finished.wait(timeout=0.1)
        acceptance.commit()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert errors == []
    assert results[0].status == "dispatched"
    assert results[0].operator_intent_id == intent_id


def test_proven_expired_without_operation_releases_task_and_rejects_late_api(signal_client, signal_db):
    body = _request(signal_db)
    with closing(psycopg2.connect(signal_db)) as conn:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=%s", (body["signal_claim"]["task_id"],))
            cur.execute("UPDATE message_processing_runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE processing_run_id=%s", (body["signal_claim"]["processing_run_id"],))
            cur.execute(f"SELECT {signal_queue._TASK_RETURNING} FROM signal_dispatch_tasks WHERE task_id=%s", (body["signal_claim"]["task_id"],))
            task = signal_queue._task_from_row(cur.fetchone())
        resolved = signal_queue.record_operator_uncertainty(conn, task, "http_timeout")
        assert resolved.status == "expired"
        assert resolved.disposition_reason == "claim_expired_without_operation"
        assert resolved.operator_intent_id is None
        assert resolved.execution_request == body
    late = signal_client.post("/v1/operator/orders", json=body,
                              headers={"Authorization": f"Bearer {SIGNAL_TOKEN}"})
    assert late.status_code == 409, late.text
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trade_intents")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT status FROM message_processing_runs WHERE processing_run_id=%s", (task.processing_run_id,))
        assert cur.fetchone()[0] == "hermes_timeout"
