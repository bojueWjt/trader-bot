"""Tests for the standalone market snapshot recorder (record-only path)."""

from __future__ import annotations

from uuid import uuid4

from psycopg2.extras import Json

import market_snapshot_recorder as recorder
from test_hermes_worker import seed_message


class FakeFetcher:
    def __init__(self, *, raises: Exception | None = None):
        self._raises = raises
        self.symbols: list[str] = []

    def fetch(self, symbol: str) -> dict:
        self.symbols.append(symbol)
        if self._raises is not None:
            raise self._raises
        return {
            "context_version": "market-v1",
            "symbol": symbol,
            "status": "ok",
            "partial_failures": [],
        }


def seed_decision(conn, *, action="open_position", symbol="BTCUSDT", age_seconds=10):
    raw_id = seed_message(conn, source_message_id=f"m-{uuid4()}")
    run_id, ctx_id, decision_id = uuid4(), uuid4(), uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message_processing_runs (processing_run_id, raw_message_id, status) "
            "VALUES (%s, %s, 'succeeded')",
            (str(run_id), raw_id),
        )
        cur.execute(
            "INSERT INTO context_snapshots (context_snapshot_id, raw_message_id, snapshot_type, "
            "context_version, snapshot) VALUES (%s, %s, 'system', 'ctx-v1', %s)",
            (str(ctx_id), raw_id, Json({})),
        )
        cur.execute(
            "INSERT INTO hermes_decisions (decision_id, raw_message_id, processing_run_id, "
            "context_snapshot_id, message_type, action, ambiguous, account_scope, "
            "instrument_symbol, entry_type, model_version, prompt_version, context_version, "
            "temperature, created_at) "
            "VALUES (%s, %s, %s, %s, 'new_signal', %s, false, 'unassigned', %s, 'market', "
            "'m1', 'p1', 'ctx-v1', 0, now() - make_interval(secs => %s))",
            (str(decision_id), raw_id, str(run_id), str(ctx_id), action, symbol, age_seconds),
        )
    conn.commit()
    return raw_id, str(decision_id)


def market_rows(conn, raw_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot FROM context_snapshots "
            "WHERE raw_message_id = %s AND snapshot_type = 'market'",
            (raw_id,),
        )
        return [row[0] for row in cur.fetchall()]


def test_scan_writes_market_snapshot_for_fresh_actionable_decision(db_conn):
    raw_id, decision_id = seed_decision(db_conn)
    fetcher = FakeFetcher()

    written = recorder.scan_once(db_conn, fetcher, max_age_seconds=900)

    assert written == 1
    assert fetcher.symbols == ["BTCUSDT"]
    rows = market_rows(db_conn, raw_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["recorder"] == "market-snapshot-recorder-v1"
    assert rows[0]["decision_id"] == decision_id


def test_scan_is_idempotent(db_conn):
    raw_id, _ = seed_decision(db_conn)
    recorder.scan_once(db_conn, FakeFetcher(), max_age_seconds=900)

    written = recorder.scan_once(db_conn, FakeFetcher(), max_age_seconds=900)

    assert written == 0
    assert len(market_rows(db_conn, raw_id)) == 1


def test_scan_skips_decisions_older_than_max_age(db_conn):
    raw_id, _ = seed_decision(db_conn, age_seconds=3600)

    written = recorder.scan_once(db_conn, FakeFetcher(), max_age_seconds=900)

    assert written == 0
    assert market_rows(db_conn, raw_id) == []


def test_scan_ignores_non_actionable_actions(db_conn):
    raw_id, _ = seed_decision(db_conn, action="hold")

    written = recorder.scan_once(db_conn, FakeFetcher(), max_age_seconds=900)

    assert written == 0
    assert market_rows(db_conn, raw_id) == []


def test_fetch_failure_writes_skipped_record_and_continues(db_conn):
    raw_id, _ = seed_decision(db_conn)

    written = recorder.scan_once(db_conn, FakeFetcher(raises=TimeoutError("deadline")), max_age_seconds=900)

    assert written == 1
    rows = market_rows(db_conn, raw_id)
    assert rows[0]["status"] == "skipped"
    assert rows[0]["reason"] == "market_context_fetch_failed"
