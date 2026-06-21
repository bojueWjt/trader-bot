from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg2

from order_management.execution_jobs import (
    ExecutionJobRequest,
    build_execution_job_request,
    claim_next_execution_job,
    deterministic_execution_job_id,
    retry_payload,
)


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_execution_job_request_is_deterministic_and_preserves_intent_payload() -> None:
    intent_row = {
        "intent_id": "11111111-1111-4111-8111-111111111111",
        "account_id": "acct-om4",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "action": "open_position",
        "idempotency_key": "a" * 64,
        "order_plan": {"side": "buy", "type": "market"},
        "risk_budget": {"max_notional": "1000", "risk_amount": "25", "max_leverage": "5"},
        "valid_until": NOW,
    }

    first = build_execution_job_request(intent_row, now=NOW)
    second = build_execution_job_request(intent_row, now=NOW)

    assert first == second
    assert isinstance(first, ExecutionJobRequest)
    assert first.execution_job_id == deterministic_execution_job_id(first.idempotency_key)
    assert first.status == "pending"
    assert first.request_id.startswith("req_")
    assert first.payload["intent"]["intent_id"] == intent_row["intent_id"]
    assert first.payload["reservation"]["notional"] == "1000"
    assert first.payload["reservation"]["risk_amount"] == "25"


def test_retry_payload_increments_attempts_and_stops_at_terminal() -> None:
    payload = {"attempts": 1}

    retry = retry_payload(payload, reason="rate_limit", delay_seconds=4, max_attempts=3, now=NOW)
    terminal = retry_payload(retry.payload, reason="rate_limit", delay_seconds=8, max_attempts=2, now=NOW)

    assert retry.status == "retry_scheduled"
    assert retry.payload["attempts"] == 2
    assert retry.payload["retry"]["reason"] == "rate_limit"
    assert retry.payload["retry"]["next_attempt_at"] == "2026-06-21T12:00:04+00:00"
    assert terminal.status == "failed"
    assert terminal.payload["terminal"]["reason"] == "max_attempts_exceeded"


def test_two_workers_claim_distinct_jobs_live_db(execution_db_url: str) -> None:
    setup = psycopg2.connect(execution_db_url)
    account_id = "acct-om4-claim"
    job_ids = [str(uuid4()), str(uuid4())]
    try:
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute("DELETE FROM execution_jobs WHERE account_id=%s", (account_id,))
            for index, job_id in enumerate(job_ids):
                cur.execute(
                    """
                    INSERT INTO execution_jobs (
                        execution_job_id, account_id, instrument_id, venue_symbol,
                        action, status, request_id, idempotency_key, payload
                    )
                    VALUES (%s,%s,'BTCUSDT-PERP.BINANCE','BTCUSDT-PERP.BINANCE',
                            'open_position','pending',%s,%s,'{}'::jsonb)
                    """,
                    (job_id, account_id, f"req-{index}", f"claim-key-{index}"),
                )
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    claimed: list[str] = []

    def worker(name: str) -> None:
        conn = psycopg2.connect(execution_db_url)
        try:
            barrier.wait(timeout=10)
            result = claim_next_execution_job(conn, worker_id=name, now=NOW)
            assert result is not None
            claimed.append(result.execution_job_id)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    verify = psycopg2.connect(execution_db_url)
    try:
        assert sorted(claimed) == sorted(job_ids)
        with verify.cursor() as cur:
            cur.execute(
                """
                SELECT count(DISTINCT payload->>'claimed_by'), count(*)
                FROM execution_jobs
                WHERE account_id=%s AND status='claimed'
                """,
                (account_id,),
            )
            distinct_workers, claimed_count = cur.fetchone()
            assert claimed_count == 2
            assert distinct_workers == 2
    finally:
        verify.rollback()  # close the read transaction before toggling autocommit
        verify.autocommit = True
        with verify.cursor() as cur:
            cur.execute("DELETE FROM execution_jobs WHERE account_id=%s", (account_id,))
        verify.close()

