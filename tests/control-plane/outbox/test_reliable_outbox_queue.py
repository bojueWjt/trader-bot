from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2


REPO_ROOT = Path(__file__).resolve().parents[3]
HERMES_QUEUE_CLAIMS = REPO_ROOT / "services" / "hermes-worker" / "queue" / "claims.py"


class SimulatedCrash(BaseException):
    pass


class IdempotentRecordingSink:
    def __init__(self):
        self.delivered_keys: set[str] = set()
        self.calls: list[str] = []
        self.crash_after_first_unique_delivery = False

    def deliver(self, event):
        key = event.idempotency_key
        self.calls.append(key)
        first_delivery = key not in self.delivered_keys
        if first_delivery:
            self.delivered_keys.add(key)
            if self.crash_after_first_unique_delivery:
                self.crash_after_first_unique_delivery = False
                raise SimulatedCrash("crash after external delivery")


def _load_queue_claims():
    spec = importlib.util.spec_from_file_location("hermes_queue_claims", HERMES_QUEUE_CLAIMS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _insert_raw_message_with_outbox(conn, *, source_message_id: str):
    from db.connection import transaction
    from db.repository import ingest_raw_message_with_outbox

    raw_id = uuid4()
    with transaction(conn):
        inserted_raw_id, outbox_event_id = ingest_raw_message_with_outbox(
            conn,
            {
                "id": raw_id,
                "source": "telegram",
                "channel_id": "signals",
                "source_message_id": source_message_id,
                "source_version": "v1",
                "source_received_at": "2026-06-19T12:00:00Z",
                "author_id": "author-a",
                "content_hash": f"sha-{source_message_id}",
                "message_text": "BTC long",
                "raw_payload": {"text": "BTC long"},
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
    return inserted_raw_id, outbox_event_id


def test_crash_between_commit_and_publish(db_conn, migrated_db):
    from outbox.publisher import OutboxPublisher

    _, outbox_event_id = _insert_raw_message_with_outbox(
        db_conn, source_message_id="crash-between-commit-and-publish"
    )
    sink = IdempotentRecordingSink()
    sink.crash_after_first_unique_delivery = True
    publisher = OutboxPublisher(sink)

    try:
        publisher.publish_pending_once(db_conn)
    except SimulatedCrash:
        db_conn.rollback()
    else:
        raise AssertionError("publisher did not simulate a crash")

    with psycopg2.connect(migrated_db) as observer, observer.cursor() as cur:
        cur.execute(
            "SELECT status, published_at FROM outbox_events WHERE outbox_event_id = %s",
            (str(outbox_event_id),),
        )
        assert cur.fetchone() == ("pending", None)

    assert publisher.publish_pending_once(db_conn) == 1

    with psycopg2.connect(migrated_db) as observer, observer.cursor() as cur:
        cur.execute(
            "SELECT status, published_at, error FROM outbox_events WHERE outbox_event_id = %s",
            (str(outbox_event_id),),
        )
        status, published_at, error = cur.fetchone()
        assert status == "published"
        assert published_at is not None
        assert error is None

    assert sink.calls == [str(outbox_event_id), str(outbox_event_id)]
    assert sink.delivered_keys == {str(outbox_event_id)}


def test_duplicate_delivery_idempotent(db_conn):
    from outbox.publisher import OutboxPublisher

    _, outbox_event_id = _insert_raw_message_with_outbox(
        db_conn, source_message_id="duplicate-delivery-idempotent"
    )
    sink = IdempotentRecordingSink()
    publisher = OutboxPublisher(sink)

    assert publisher.publish_event(db_conn, outbox_event_id) == "published"
    assert publisher.publish_event(db_conn, outbox_event_id) == "already_published"

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, count(*), count(published_at), count(error)
            FROM outbox_events
            WHERE outbox_event_id = %s
            GROUP BY status
            """,
            (str(outbox_event_id),),
        )
        assert cur.fetchone() == ("published", 1, 1, 0)

    assert sink.calls == [str(outbox_event_id)]
    assert sink.delivered_keys == {str(outbox_event_id)}


def test_worker_lease_expiry_and_takeover(db_conn):
    claims = _load_queue_claims()
    raw_message_id, _ = _insert_raw_message_with_outbox(
        db_conn, source_message_id="worker-lease-expiry-and-takeover"
    )

    worker_a_run = claims.claim(db_conn, "worker-a", lease_seconds=1)
    assert UUID(worker_a_run.raw_message_id) == raw_message_id
    assert worker_a_run.worker_id == "worker-a"
    assert worker_a_run.status == "started"

    time.sleep(1.2)

    worker_b_run = claims.claim(db_conn, "worker-b", lease_seconds=30)
    assert UUID(worker_b_run.raw_message_id) == raw_message_id
    assert worker_b_run.worker_id == "worker-b"
    assert worker_b_run.status == "started"

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, finished_at IS NOT NULL
            FROM message_processing_runs
            WHERE processing_run_id = %s
            """,
            (worker_a_run.processing_run_id,),
        )
        assert cur.fetchone() == ("hermes_timeout", True)

        cur.execute(
            """
            SELECT count(*)
            FROM message_processing_runs
            WHERE raw_message_id = %s
              AND status IN ('started', 'processing')
            """,
            (str(raw_message_id),),
        )
        assert cur.fetchone() == (1,)
