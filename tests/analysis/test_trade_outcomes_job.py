from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.analysis import trade_outcomes


ROOT = Path(__file__).resolve().parents[2]
JOB_RUNS_UP = ROOT / "db" / "migrations" / "0009_trade_outcome_job_runs.up.sql"
JOB_RUNS_DOWN = ROOT / "db" / "migrations" / "0009_trade_outcome_job_runs.down.sql"


def test_migration_0009_matches_job_run_contract():
    up_sql = JOB_RUNS_UP.read_text(encoding="utf-8")
    down_sql = JOB_RUNS_DOWN.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS trade_outcome_job_runs" in up_sql
    assert "job_name text PRIMARY KEY" in up_sql
    assert "status text NOT NULL" in up_sql
    assert "started_at timestamptz NOT NULL" in up_sql
    assert "completed_at timestamptz" in up_sql
    assert "outcome_count bigint NOT NULL DEFAULT 0" in up_sql
    assert "error text" in up_sql
    assert "CHECK (status IN ('running', 'succeeded', 'failed'))" in up_sql
    assert "DROP TABLE IF EXISTS trade_outcome_job_runs" in down_sql


def test_cli_uses_environment_database_url_and_commits_success(monkeypatch, tmp_path):
    conn = _FakeJobRunConnection()
    connected_urls = []

    def fake_connect(db_url):
        connected_urls.append(db_url)
        return conn

    monkeypatch.setattr(trade_outcomes.psycopg2, "connect", fake_connect)
    monkeypatch.setenv("DATABASE_URL", "postgres://from-environment")
    monkeypatch.setattr(
        trade_outcomes,
        "load_closed_intents",
        lambda _conn, intent_id=None: [{"intent_id": intent_id or "intent-1"}],
    )
    monkeypatch.setattr(
        trade_outcomes,
        "compute_outcomes",
        lambda _intents, **_kwargs: [{"outcome_id": "outcome-1"}],
    )
    monkeypatch.setattr(
        trade_outcomes,
        "upsert_trade_outcomes",
        lambda _conn, outcomes: len(outcomes),
    )
    output = tmp_path / "result.json"

    result = trade_outcomes.main(
        [
            "--cache-dir",
            str(tmp_path),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert connected_urls == ["postgres://from-environment"]
    assert conn.committed_statuses == ["running", "succeeded"]
    assert conn.rollback_count == 0
    assert conn.state == {
        "job_name": "trade_outcomes",
        "status": "succeeded",
        "outcome_count": 1,
        "error": None,
    }
    assert conn.closed is True
    assert json.loads(output.read_text(encoding="utf-8"))["upserted_count"] == 1


def test_cli_commits_failed_watermark_and_reraises(monkeypatch, tmp_path):
    conn = _FakeJobRunConnection()
    monkeypatch.setattr(
        trade_outcomes.psycopg2,
        "connect",
        lambda _db_url: conn,
    )

    def fail_load(_conn, intent_id=None):
        raise RuntimeError(f"outcome load failed: {intent_id}")

    monkeypatch.setattr(trade_outcomes, "load_closed_intents", fail_load)

    with pytest.raises(RuntimeError, match="outcome load failed"):
        trade_outcomes.main(
            [
                "--db-url",
                "postgres://example",
                "--cache-dir",
                str(tmp_path),
                "--intent-id",
                "intent-2",
            ]
        )

    assert conn.committed_statuses == ["running", "failed"]
    assert conn.rollback_count == 1
    assert conn.state == {
        "job_name": "trade_outcomes",
        "status": "failed",
        "outcome_count": 0,
        "error": "RuntimeError: outcome load failed: intent-2",
    }
    assert conn.closed is True


def test_cli_requires_database_url_from_flag_or_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as raised:
        trade_outcomes.main(
            [
                "--cache-dir",
                str(tmp_path),
            ]
        )

    assert raised.value.code == 2


class _FakeJobRunCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if "INSERT INTO trade_outcome_job_runs" in normalized:
            self.connection.state = {
                "job_name": params[0],
                "status": "running",
                "outcome_count": 0,
                "error": None,
            }
            return
        if "SET status = 'succeeded'" in normalized:
            self.connection.state.update(
                {
                    "job_name": params[1],
                    "status": "succeeded",
                    "outcome_count": params[0],
                    "error": None,
                }
            )
            return
        if "SET status = 'failed'" in normalized:
            self.connection.state.update(
                {
                    "job_name": params[1],
                    "status": "failed",
                    "outcome_count": 0,
                    "error": params[0],
                }
            )
            return
        raise AssertionError(f"unexpected SQL: {normalized}")


class _FakeJobRunConnection:
    def __init__(self):
        self.state = {}
        self.committed_statuses = []
        self.rollback_count = 0
        self.closed = False

    def cursor(self):
        return _FakeJobRunCursor(self)

    def commit(self):
        self.committed_statuses.append(self.state.get("status"))

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True
