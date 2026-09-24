from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "scripts" / "ops" / "converge_heartbeat_positions.py"


def _load():
    spec = importlib.util.spec_from_file_location("_converge_hb_positions", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _healthy(now, positions):
    return {
        "last_seen_at": now,
        "positions_snapshot_at": now,
        "payload": {"reconciliation_state": "healthy"},
        "positions": positions,
    }


def test_plan_converges_nine_venue_books_and_closes_ghosts():
    module = _load()
    now = datetime(2026, 9, 23, 18, 41, tzinfo=timezone.utc)
    heartbeats = [
        {
            "account_id": "account-a",
            **_healthy(
                now,
                [
                    {"symbol": "HYPEUSDT", "quantity": "27.00", "position_side": "LONG"},
                    {"symbol": "ANTHROPICUSDT", "quantity": "0.90", "position_side": "LONG"},
                ],
            ),
        },
        {
            "account_id": "account-b",
            **_healthy(
                now,
                [
                    {"symbol": "BTCUSDT", "quantity": "0.015", "position_side": "LONG"},
                    {"symbol": "AXSUSDT", "quantity": "275", "position_side": "LONG"},
                ],
            ),
        },
        {
            "account_id": "account-c",
            **_healthy(
                now,
                [
                    {"symbol": "SUIUSDT", "quantity": "1937.0", "position_side": "LONG"},
                    {"symbol": "XRPUSDT", "quantity": "461.5", "position_side": "LONG"},
                    {"symbol": "ASTERUSDT", "quantity": "-214", "position_side": "SHORT"},
                    {"symbol": "XMRUSDT", "quantity": "1.987", "position_side": "LONG"},
                ],
            ),
        },
        {
            "account_id": "account-d",
            **_healthy(
                now,
                [
                    {"symbol": "SOLUSDT", "quantity": "0.25", "position_side": "LONG"},
                ],
            ),
        },
    ]
    projections = [
        {
            "account_id": "account-d",
            "position_id": "BTCUSDT-PERP.BINANCE-LONG",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "side": "long",
            "quantity": Decimal("0.235"),
            "status": "open",
        }
    ]
    plan = module.plan_convergence(
        heartbeats,
        projections,
        now=now,
        max_age_seconds=10,
        backup_table="positions_projection_hb_backup_test",
        source_row_count=80,
    )
    assert plan.ok
    assert len(plan.upserts) == 9
    assert any(
        row["account_id"] == "account-d"
        and row["position_id"] == "BTCUSDT-PERP.BINANCE-LONG"
        for row in plan.closes
    )
    assert any(
        row["position_id"] == "HYPEUSDT-PERP.BINANCE-LONG"
        and row["quantity"] == Decimal("27")
        for row in plan.upserts
    )


def test_stale_heartbeat_fails_closed():
    module = _load()
    now = datetime(2026, 9, 23, 18, 41, tzinfo=timezone.utc)
    stale = now - timedelta(seconds=30)
    heartbeats = [
        {
            "account_id": account_id,
            "last_seen_at": stale,
            "positions_snapshot_at": stale,
            "payload": {"reconciliation_state": "healthy"},
            "positions": [],
        }
        for account_id in module.ACCOUNTS
    ]
    plan = module.plan_convergence(
        heartbeats,
        [],
        now=now,
        max_age_seconds=10,
        backup_table="positions_projection_hb_backup_test",
        source_row_count=0,
    )
    assert not plan.ok
    assert any("stale" in error for error in plan.errors)


def test_malformed_snapshot_is_rejected():
    module = _load()
    now = datetime(2026, 9, 23, 18, 41, tzinfo=timezone.utc)
    heartbeats = [
        {
            "account_id": account_id,
            "last_seen_at": now,
            "positions_snapshot_at": now,
            "payload": {"reconciliation_state": "healthy"},
            "positions": [{"symbol": "BTCUSDT", "quantity": "not-a-number", "position_side": "LONG"}]
            if account_id == "account-a"
            else [],
        }
        for account_id in module.ACCOUNTS
    ]
    plan = module.plan_convergence(
        heartbeats,
        [],
        now=now,
        backup_table="positions_projection_hb_backup_test",
        source_row_count=0,
    )
    assert not plan.ok
    assert any("not numeric" in error for error in plan.errors)


def test_alias_projection_is_closed_not_skipped():
    module = _load()
    now = datetime(2026, 9, 23, 18, 41, tzinfo=timezone.utc)
    heartbeats = [
        {
            "account_id": account_id,
            "last_seen_at": now,
            "positions_snapshot_at": now,
            "payload": {"reconciliation_state": "healthy"},
            "positions": (
                [{"symbol": "SOLUSDT", "quantity": "0.25", "position_side": "LONG"}]
                if account_id == "account-d"
                else []
            ),
        }
        for account_id in module.ACCOUNTS
    ]
    projections = [
        {
            "account_id": "account-d",
            "position_id": "SOLUSDT-PERP.BINANCE-EXTERNAL",
            "instrument_id": "SOLUSDT-PERP.BINANCE",
            "side": "long",
            "quantity": Decimal("0.25"),
            "status": "open",
        }
    ]
    plan = module.plan_convergence(
        heartbeats,
        projections,
        now=now,
        backup_table="t",
        source_row_count=1,
    )
    assert plan.ok
    assert any(row["position_id"].endswith("EXTERNAL") for row in plan.closes)
    assert any(row["position_id"].endswith("LONG") for row in plan.upserts)


def test_missing_reconciliation_state_is_rejected():
    module = _load()
    now = datetime(2026, 9, 23, 18, 41, tzinfo=timezone.utc)
    heartbeats = [
        {
            "account_id": account_id,
            "last_seen_at": now,
            "positions_snapshot_at": now,
            "payload": {},
            "positions": [],
        }
        for account_id in module.ACCOUNTS
    ]
    plan = module.plan_convergence(
        heartbeats,
        [],
        now=now,
        backup_table="t",
        source_row_count=0,
    )
    assert not plan.ok
    assert any("reconciliation_state=missing" in error for error in plan.errors)


def test_unrecognized_side_rejected_both_signed_qty_ok():
    module = _load()
    now = datetime(2026, 9, 23, 18, 41, tzinfo=timezone.utc)
    books = module.parse_heartbeat_positions(
        "account-d",
        [{"symbol": "BTCUSDT", "quantity": "-0.2", "position_side": "BOTH"}],
    )
    assert books[0]["side"] == "short"
    try:
        module.parse_heartbeat_positions(
            "account-d",
            [{"symbol": "BTCUSDT", "quantity": "1", "position_side": ""}],
        )
    except module.SnapshotRejected as exc:
        assert "unrecognized" in str(exc)
    else:
        raise AssertionError("missing side must reject")


def test_newer_projection_than_snapshot_is_rejected():
    module = _load()
    now = datetime(2026, 9, 23, 18, 41, tzinfo=timezone.utc)
    heartbeats = [
        {
            "account_id": account_id,
            **_healthy(
                now,
                [{"symbol": "SOLUSDT", "quantity": "0.25", "position_side": "LONG"}]
                if account_id == "account-d"
                else [],
            ),
        }
        for account_id in module.ACCOUNTS
    ]
    projections = [
        {
            "account_id": "account-d",
            "position_id": "SOLUSDT-PERP.BINANCE-LONG",
            "instrument_id": "SOLUSDT-PERP.BINANCE",
            "side": "long",
            "quantity": Decimal("9"),
            "status": "open",
            "ts_event": now + timedelta(seconds=30),
        }
    ]
    plan = module.plan_convergence(
        heartbeats,
        projections,
        now=now,
        backup_table="t",
        source_row_count=1,
    )
    assert not plan.ok
    assert any("newer than snapshot" in error for error in plan.errors)


def test_apply_requires_guards():
    module = _load()
    try:
        module.main(["--db-url", "postgresql:///unused", "--apply"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("apply without guards must fail")


def test_default_is_dry_run_in_cli_help():
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "Default is dry-run" in text or "default dry-run" in text
    assert "--apply" in text
    assert "CREATE TABLE" in text
    assert "clock_timestamp" in text
    assert "--apply requires --max-closes" in text
