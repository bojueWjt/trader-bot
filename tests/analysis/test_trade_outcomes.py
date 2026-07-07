from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
import pytest
from psycopg2.extras import Json

from scripts.analysis.trade_outcomes import build_trade_outcome


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "analysis" / "trade_outcomes.py"
UP = ROOT / "db" / "migrations" / "0006_trade_outcomes.up.sql"
DOWN = ROOT / "db" / "migrations" / "0006_trade_outcomes.down.sql"


def event(event_type: str, ts: datetime, payload: dict, *, account_id="acct-1"):
    return {
        "event_type": event_type,
        "ts_event": ts,
        "account_id": account_id,
        "payload": payload,
    }


def test_build_trade_outcome_calculates_long_metrics_from_events_and_klines():
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    intent = {
        "intent_id": "11111111-1111-1111-1111-111111111111",
        "account_id": "acct-1",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "order_plan": {"side": "long", "stop_loss": "95"},
    }
    events = [
        event("OrderFilled", start, {"order_side": "BUY", "last_qty": "1", "avg_px": "100", "commission": "0.10"}),
        event("OrderFilled", start + timedelta(minutes=1), {"order_side": "BUY", "last_qty": "1", "avg_px": "102", "fee": "0.20"}),
        event("OrderFilled", start + timedelta(minutes=10), {"order_side": "SELL", "last_qty": "2", "avg_px": "110", "commission": "0.40"}),
        event("PositionClosed", start + timedelta(minutes=10), {"side": "LONG", "realized_pnl": "18"}),
    ]
    klines = [
        (start, 100.0, 103.0, 99.0, 102.0),
        (start + timedelta(minutes=1), 102.0, 109.0, 96.0, 108.0),
        (start + timedelta(minutes=2), 108.0, 112.0, 98.0, 110.0),
    ]

    result = build_trade_outcome(intent, events, klines=klines, kline_source="fixture")

    assert result["entry_avg_price"] == Decimal("101")
    assert result["exit_avg_price"] == Decimal("110")
    assert result["filled_quantity"] == Decimal("2")
    assert result["realized_pnl"] == Decimal("18")
    assert result["fees"] == Decimal("0.70")
    assert result["initial_risk"] == Decimal("12")
    assert result["r_multiple"] == Decimal("1.5")
    assert result["mae"] == pytest.approx(5 / 101)
    assert result["mfe"] == pytest.approx(11 / 101)
    assert result["mae_price"] == Decimal("96")
    assert result["mfe_price"] == Decimal("112")
    assert result["holding_seconds"] == 600


def test_build_trade_outcome_calculates_short_metrics_from_events_and_klines():
    start = datetime(2026, 7, 2, 13, 0, tzinfo=timezone.utc)
    intent = {
        "intent_id": "22222222-2222-2222-2222-222222222222",
        "account_id": "acct-2",
        "instrument_id": "ETHUSDT-PERP.BINANCE",
        "order_plan": {"side": "short", "stop_loss": "210"},
    }
    events = [
        event("OrderFilled", start, {"order_side": "SELL", "last_qty": "3", "avg_px": "200"}),
        event("OrderFilled", start + timedelta(minutes=5), {"order_side": "BUY", "last_qty": "3", "avg_px": "180"}),
        event("PositionClosed", start + timedelta(minutes=5), {"side": "SHORT", "realized_pnl": "60"}),
    ]
    klines = [
        (start, 200.0, 205.0, 195.0, 198.0),
        (start + timedelta(minutes=1), 198.0, 208.0, 170.0, 180.0),
    ]

    result = build_trade_outcome(intent, events, klines=klines, kline_source="fixture")

    assert result["entry_avg_price"] == Decimal("200")
    assert result["exit_avg_price"] == Decimal("180")
    assert result["filled_quantity"] == Decimal("3")
    assert result["realized_pnl"] == Decimal("60")
    assert result["initial_risk"] == Decimal("30")
    assert result["r_multiple"] == Decimal("2")
    assert result["mae"] == pytest.approx(8 / 200)
    assert result["mfe"] == pytest.approx(30 / 200)
    assert result["mae_price"] == Decimal("208")
    assert result["mfe_price"] == Decimal("170")


def test_build_trade_outcome_without_stop_loss_leaves_r_metrics_null():
    start = datetime(2026, 7, 2, 14, 0, tzinfo=timezone.utc)
    intent = {
        "intent_id": "33333333-3333-3333-3333-333333333333",
        "account_id": "acct-3",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "order_plan": {"side": "long"},
    }
    events = [
        event("OrderFilled", start, {"order_side": "BUY", "last_qty": "1", "avg_px": "100"}),
        event("OrderFilled", start + timedelta(minutes=1), {"order_side": "SELL", "last_qty": "1", "avg_px": "102"}),
        event("PositionClosed", start + timedelta(minutes=1), {"side": "LONG", "realized_pnl": "2"}),
    ]

    result = build_trade_outcome(intent, events, klines=[], kline_source="fixture")

    assert result["initial_risk"] is None
    assert result["r_multiple"] is None
    assert result["details"]["r_multiple"]["reason"] == "missing stop_loss"


def test_migration_0006_files_match_required_contract():
    up_sql = UP.read_text(encoding="utf-8")
    down_sql = DOWN.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS trade_outcomes" in up_sql
    assert "outcome_id uuid PRIMARY KEY" in up_sql
    assert "intent_id uuid NOT NULL REFERENCES trade_intents(intent_id)" in up_sql
    assert "CONSTRAINT uq_trade_outcomes_intent_account UNIQUE (intent_id, account_id)" in up_sql
    assert "DROP TABLE IF EXISTS trade_outcomes" in down_sql


def test_migration_0006_is_reversible_on_throwaway_postgres(run_migration, db_url):
    run_migration("down")
    run_migration("up")
    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.trade_outcomes')")
        assert cur.fetchone() == ("trade_outcomes",)
    second = run_migration("up")
    assert "No pending migrations." in second.stdout
    run_migration("down")
    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.trade_outcomes')")
        assert cur.fetchone() == (None,)
    run_migration("up")


def test_cli_upserts_trade_outcomes_idempotently_with_cached_klines(run_migration, db_url, tmp_path):
    run_migration("down")
    run_migration("up")
    intent_id = uuid4()
    account_id = "acct-cli"
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    _seed_closed_intent(
        db_url,
        intent_id=intent_id,
        account_id=account_id,
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="long",
        stop_loss="95",
        events=[
            event("OrderFilled", start, {"instrument_id": "BTCUSDT-PERP.BINANCE", "order_side": "BUY", "last_qty": "2", "avg_px": "100"}),
            event("OrderFilled", start + timedelta(minutes=5), {"instrument_id": "BTCUSDT-PERP.BINANCE", "order_side": "SELL", "last_qty": "2", "avg_px": "108"}),
            event("PositionClosed", start + timedelta(minutes=5), {"instrument_id": "BTCUSDT-PERP.BINANCE", "side": "LONG", "realized_pnl": "16"}),
        ],
    )
    _write_kline_zip(
        tmp_path,
        "BTCUSDT",
        start.date().isoformat(),
        [
            [int(start.timestamp() * 1000), "100", "101", "99", "100"],
            [int((start + timedelta(minutes=1)).timestamp() * 1000), "100", "110", "94", "108"],
        ],
    )

    output = tmp_path / "outcomes.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "--db-url",
        db_url,
        "--cache-dir",
        str(tmp_path),
        "--base-url",
        "https://example.invalid",
        "--output",
        str(output),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)

    first = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
    assert first.returncode == 0, first.stdout + first.stderr
    before = _fetch_outcome(db_url, intent_id, account_id)
    second = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
    assert second.returncode == 0, second.stdout + second.stderr
    after = _fetch_outcome(db_url, intent_id, account_id)

    assert before == after
    assert before["entry_avg_price"] == Decimal("100")
    assert before["exit_avg_price"] == Decimal("108")
    assert before["realized_pnl"] == Decimal("16")
    assert before["initial_risk"] == Decimal("10")
    assert before["r_multiple"] == Decimal("1.6")
    assert before["mae_price"] == Decimal("94")
    assert before["mfe_price"] == Decimal("110")
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["upserted_count"] == 1


def test_cli_skips_mae_mfe_for_unsupported_symbol_but_writes_other_fields(run_migration, db_url, tmp_path):
    run_migration("down")
    run_migration("up")
    intent_id = uuid4()
    account_id = "acct-xau"
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    _seed_closed_intent(
        db_url,
        intent_id=intent_id,
        account_id=account_id,
        instrument_id="XAUUSDT-PERP.BINANCE",
        side="long",
        stop_loss=None,
        events=[
            event("OrderFilled", start, {"instrument_id": "XAUUSDT-PERP.BINANCE", "order_side": "BUY", "last_qty": "1", "avg_px": "2300"}),
            event("OrderFilled", start + timedelta(minutes=5), {"instrument_id": "XAUUSDT-PERP.BINANCE", "order_side": "SELL", "last_qty": "1", "avg_px": "2310"}),
            event("PositionClosed", start + timedelta(minutes=5), {"instrument_id": "XAUUSDT-PERP.BINANCE", "side": "LONG", "realized_pnl": "10"}),
        ],
    )

    command = [
        sys.executable,
        str(SCRIPT),
        "--db-url",
        db_url,
        "--cache-dir",
        str(tmp_path),
        "--base-url",
        "https://example.invalid",
        "--output",
        str(tmp_path / "outcomes.json"),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr

    row = _fetch_outcome(db_url, intent_id, account_id)
    assert row["realized_pnl"] == Decimal("10")
    assert row["mae"] is None
    assert row["mfe"] is None
    assert row["initial_risk"] is None
    assert row["r_multiple"] is None
    assert row["details"]["mae_mfe"]["status"] == "skipped"


def _seed_closed_intent(
    db_url: str,
    *,
    intent_id: UUID,
    account_id: str,
    instrument_id: str,
    side: str,
    stop_loss: str | None,
    events: list[dict],
) -> None:
    raw_id = uuid4()
    processing_run_id = uuid4()
    context_snapshot_id = uuid4()
    decision_id = uuid4()
    risk_id = uuid4()
    order_plan = {"side": side}
    if stop_loss is not None:
        order_plan["stop_loss"] = stop_loss
    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_messages (
                id, source, channel_id, source_message_id, source_version,
                source_received_at, content_hash, message_text, raw_payload
            ) VALUES (%s, 'telegram', 'chan', %s, 'v1', %s, %s, 'signal', '{}')
            """,
            (str(raw_id), str(raw_id), events[0]["ts_event"], f"sha-{raw_id}"),
        )
        cur.execute(
            """
            INSERT INTO message_processing_runs (
                processing_run_id, raw_message_id, status, model_version,
                prompt_version, context_version
            ) VALUES (%s, %s, 'succeeded', 'm', 'p', 'c')
            """,
            (str(processing_run_id), str(raw_id)),
        )
        cur.execute(
            """
            INSERT INTO context_snapshots (context_snapshot_id, raw_message_id, snapshot_type, context_version)
            VALUES (%s, %s, 'test', 'c')
            """,
            (str(context_snapshot_id), str(raw_id)),
        )
        cur.execute(
            """
            INSERT INTO hermes_decisions (
                decision_id, raw_message_id, processing_run_id, context_snapshot_id,
                message_type, action, ambiguous, account_scope, target_account_id,
                instrument_symbol, side, entry_type, model_version, prompt_version,
                context_version, temperature, created_at
            ) VALUES (
                %s, %s, %s, %s, 'new_signal', 'open_position', false, 'single', %s,
                %s, %s, 'market', 'm', 'p', 'c', 0, %s
            )
            """,
            (str(decision_id), str(raw_id), str(processing_run_id), str(context_snapshot_id), account_id, instrument_id, side, events[0]["ts_event"]),
        )
        cur.execute(
            """
            INSERT INTO risk_decisions (
                risk_decision_id, hermes_decision_id, status, account_id,
                instrument_id, decided_by
            ) VALUES (%s, %s, 'approved', %s, %s, 'test')
            """,
            (str(risk_id), str(decision_id), account_id, instrument_id),
        )
        cur.execute(
            """
            INSERT INTO trade_intents (
                intent_id, hermes_decision_id, risk_decision_id, account_id,
                instrument_id, action, status, order_plan, valid_until,
                idempotency_key, approved_at
            ) VALUES (%s, %s, %s, %s, %s, 'open_position', 'approved', %s, %s, %s, %s)
            """,
            (
                str(intent_id),
                str(decision_id),
                str(risk_id),
                account_id,
                instrument_id,
                Json(order_plan),
                events[-1]["ts_event"] + timedelta(hours=1),
                "a" * 64,
                events[0]["ts_event"],
            ),
        )
        for index, item in enumerate(events):
            cur.execute(
                """
                INSERT INTO execution_events (
                    execution_event_row_id, event_id, node_id, account_id, intent_id,
                    client_order_id, venue_order_id, trade_id, event_type, ts_event, payload
                ) VALUES (%s, %s, 'node-1', %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid4()),
                    f"{intent_id}-{index}",
                    account_id,
                    str(intent_id),
                    f"client-{index}",
                    f"venue-{index}",
                    f"trade-{index}",
                    item["event_type"],
                    item["ts_event"],
                    Json(item["payload"]),
                ),
            )
        conn.commit()


def _write_kline_zip(tmp_path: Path, symbol: str, day: str, rows: list[list[str]]) -> None:
    symbol_dir = tmp_path / "um" / symbol / "1m"
    symbol_dir.mkdir(parents=True)
    zip_path = symbol_dir / f"{symbol}-1m-{day}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        with archive.open(f"{symbol}-1m-{day}.csv", "w") as raw:
            writer = csv.writer(_TextWriter(raw))
            for row in rows:
                writer.writerow([*row, "1", str(int(row[0]) + 59_999), "1", "1", "1", "1", "0"])


def _fetch_outcome(db_url: str, intent_id: UUID, account_id: str) -> dict:
    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT entry_avg_price, exit_avg_price, filled_quantity, realized_pnl,
                   fees, initial_risk, r_multiple, mae, mfe, mae_price, mfe_price,
                   holding_seconds, details
            FROM trade_outcomes
            WHERE intent_id=%s AND account_id=%s
            """,
            (str(intent_id), account_id),
        )
        row = cur.fetchone()
    assert row is not None
    keys = [
        "entry_avg_price",
        "exit_avg_price",
        "filled_quantity",
        "realized_pnl",
        "fees",
        "initial_risk",
        "r_multiple",
        "mae",
        "mfe",
        "mae_price",
        "mfe_price",
        "holding_seconds",
        "details",
    ]
    return dict(zip(keys, row))


class _TextWriter:
    def __init__(self, raw):
        self.raw = raw

    def write(self, value: str) -> int:
        data = value.encode("utf-8")
        self.raw.write(data)
        return len(value)
