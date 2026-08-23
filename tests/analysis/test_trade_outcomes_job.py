from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

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
        lambda _conn, intent_id=None, stats=None: [
            {"intent_id": intent_id or "intent-1"}
        ],
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
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["upserted_count"] == 1
    assert payload["deleted_stale_count"] == 0
    assert payload["dropped_fill_count"] == 0
    assert payload["robot_fill_count"] == 0
    assert conn.deleted_stale == [(["outcome-1"],)]


def test_cli_commits_failed_watermark_and_reraises(monkeypatch, tmp_path):
    conn = _FakeJobRunConnection()
    monkeypatch.setattr(
        trade_outcomes.psycopg2,
        "connect",
        lambda _db_url: conn,
    )

    def fail_load(_conn, intent_id=None, stats=None):
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
        self.rowcount = 0

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
        if "DELETE FROM trade_outcomes" in normalized:
            self.connection.deleted_stale.append(params)
            self.rowcount = 0
            return
        raise AssertionError(f"unexpected SQL: {normalized}")


class _FakeJobRunConnection:
    def __init__(self):
        self.state = {}
        self.committed_statuses = []
        self.rollback_count = 0
        self.closed = False
        self.deleted_stale = []

    def cursor(self):
        return _FakeJobRunCursor(self)

    def commit(self):
        self.committed_statuses.append(self.state.get("status"))

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


def _robot_cid(suffix: str = "01") -> str:
    return "B" + ("a" * 32) + suffix


def _fill(
    *,
    account_id: str,
    instrument_id: str,
    side: int | str,
    qty: str,
    client_order_id: str,
    intent_id: str | None,
    ts: datetime,
) -> dict:
    return {
        "account_id": account_id,
        "intent_id": intent_id,
        "event_type": "OrderFilled",
        "ts_event": ts,
        "instrument_id": instrument_id,
        "order_plan": {"side": "long"},
        "client_order_id": client_order_id,
        "payload": {
            "order_side": side,
            "last_qty": qty,
            "last_px": "100",
            "client_order_id": client_order_id,
        },
    }


def test_signed_fill_qty_accepts_string_binance_side_keys():
    assert trade_outcomes._signed_fill_qty(
        {"order_side": "1", "last_qty": "2"}
    ) == Decimal("2")
    assert trade_outcomes._signed_fill_qty(
        {"order_side": "2", "last_qty": "2"}
    ) == Decimal("-2")
    assert trade_outcomes._signed_fill_qty(
        {"order_side": 1, "last_qty": "2"}
    ) == Decimal("2")


def test_signed_fill_qty_returns_none_for_unknown_side():
    assert (
        trade_outcomes._signed_fill_qty({"order_side": "BUY_SIDE", "last_qty": "1"})
        is None
    )


def test_robot_round_trip_on_manual_residual_stream_emits_episode(
    tmp_path,
    monkeypatch,
):
    intent_id = str(uuid4())
    t0 = datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)
    stats = trade_outcomes.EpisodeBuildStats()
    records = [
        _fill(
            account_id="account-a",
            instrument_id="BTCUSDT",
            side=2,
            qty="0.071",
            client_order_id="manual-btc-leftover",
            intent_id=None,
            ts=t0,
        ),
        _fill(
            account_id="account-a",
            instrument_id="BTCUSDT",
            side=1,
            qty="0.1",
            client_order_id=_robot_cid("01"),
            intent_id=intent_id,
            ts=t1,
        ),
        _fill(
            account_id="account-a",
            instrument_id="BTCUSDT",
            side=2,
            qty="0.1",
            client_order_id=_robot_cid("02"),
            intent_id=intent_id,
            ts=t2,
        ),
    ]
    monkeypatch.setattr(trade_outcomes, "load_klines", lambda *_args, **_kwargs: [])

    episodes = trade_outcomes.build_position_episodes(records, stats=stats)
    outcomes = trade_outcomes.compute_outcomes(episodes, cache_dir=tmp_path)

    assert stats.skipped_non_robot_fill_count == 1
    assert stats.robot_fill_count == 2
    assert stats.dropped_fill_count == 0
    assert len(episodes) == 1
    assert episodes[0]["intent_id"] == intent_id
    assert episodes[0]["account_id"] == "account-a"
    assert len(outcomes) == 1
    assert outcomes[0]["filled_quantity"] == Decimal("0.1")


def test_unparseable_robot_fill_side_is_counted_as_dropped():
    stats = trade_outcomes.EpisodeBuildStats()
    records = [
        _fill(
            account_id="account-a",
            instrument_id="ETHUSDT",
            side="BUY_SIDE",
            qty="1",
            client_order_id=_robot_cid("01"),
            intent_id=str(uuid4()),
            ts=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
    ]
    records[0]["payload"]["order_side"] = "BUY_SIDE"

    episodes = trade_outcomes.build_position_episodes(records, stats=stats)

    assert episodes == []
    assert stats.robot_fill_count == 1
    assert stats.dropped_fill_count == 1


def test_delete_stale_trade_outcomes_removes_ids_outside_keep_set():
    class _Cursor:
        def __init__(self):
            self.sql = ""
            self.params = None
            self.rowcount = 2

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            self.sql = " ".join(sql.split())
            self.params = params

    class _Conn:
        def __init__(self):
            self.cursor_obj = _Cursor()

        def cursor(self):
            return self.cursor_obj

    conn = _Conn()
    deleted = trade_outcomes.delete_stale_trade_outcomes(
        conn,
        ["keep-1"],
    )

    assert deleted == 2
    assert "DELETE FROM trade_outcomes" in conn.cursor_obj.sql
    assert conn.cursor_obj.params == (["keep-1"],)


def test_delete_stale_trade_outcomes_scoped_intent_without_keep_clears_that_intent():
    class _Cursor:
        def __init__(self):
            self.sql = ""
            self.params = None
            self.rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            self.sql = " ".join(sql.split())
            self.params = params

    class _Conn:
        def __init__(self):
            self.cursor_obj = _Cursor()

        def cursor(self):
            return self.cursor_obj

    conn = _Conn()
    deleted = trade_outcomes.delete_stale_trade_outcomes(
        conn,
        [],
        intent_id="intent-old",
    )

    assert deleted == 1
    assert conn.cursor_obj.params == ("intent-old",)
