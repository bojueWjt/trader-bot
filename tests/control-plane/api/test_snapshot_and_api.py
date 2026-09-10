from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import read_api
from connection import transaction
from snapshot import build_system_snapshot


REPO_ROOT = Path(__file__).resolve().parents[3]


def _seed_account(conn, *, account_id="acct-1", reconciliation_state="healthy", last_event_minutes_ago=None):
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts_projection (account_id, currency, equity, margin, reconciliation_state)"
            " VALUES (%s,'USDT',1000,100,%s)",
            (account_id, reconciliation_state),
        )
        if last_event_minutes_ago is not None:
            ts = datetime.now(timezone.utc) - timedelta(minutes=last_event_minutes_ago)
            cur.execute(
                "INSERT INTO execution_events (execution_event_row_id, event_id, node_id, account_id,"
                " event_type, ts_event) VALUES (gen_random_uuid(), %s, 'node-1', %s, 'order.filled', %s)",
                (f"evt-{account_id}", account_id, ts),
            )


def test_empty_system_returns_real_empty_state(db_conn):
    snap = build_system_snapshot(db_conn)
    read_api.validate_snapshot(snap)
    assert snap["data_source"] == "postgres_projection"
    assert snap["stale"] is False
    assert snap["last_execution_event_at"] is None
    assert snap["projection_lag_ms"] == 0
    assert snap["reconciliation_state"] == "healthy"
    assert snap["data"]["balances"] == {"equity": 0.0, "margin": 0.0}
    assert snap["data"]["orders"] == []
    assert snap["missing_nodes"] == []


def test_populated_snapshot_is_schema_valid(db_conn):
    _seed_account(db_conn, last_event_minutes_ago=0)
    _seed_mirror(db_conn)
    snap = build_system_snapshot(db_conn)
    read_api.validate_snapshot(snap)
    assert snap["data"]["balances"]["equity"] == 1000.0
    assert snap["last_execution_event_at"] is not None
    assert snap["stale"] is False


def _seed_mirror(conn, *, account_id="acct-1", age_seconds=0):
    with transaction(conn), conn.cursor() as cur:
        ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        cur.execute(
            "INSERT INTO exchange_state_mirror (account_id, payload, updated_at) VALUES (%s, '{}'::jsonb, %s)",
            (account_id, ts),
        )


def test_quiet_execution_stream_is_not_stale_when_mirror_fresh(db_conn):
    # Execution events only arrive on fills/placements; an hour of silence is normal and
    # must not turn the whole panel red while the venue mirror is fresh.
    _seed_account(db_conn, last_event_minutes_ago=120)
    _seed_mirror(db_conn, age_seconds=30)
    snap = build_system_snapshot(db_conn, staleness_threshold_ms=60_000)
    read_api.validate_snapshot(snap)
    assert snap["projection_lag_ms"] > 3_600_000
    assert snap["stale"] is False


def test_stale_when_mirror_old(db_conn):
    _seed_account(db_conn, last_event_minutes_ago=0)
    _seed_mirror(db_conn, age_seconds=400)
    snap = build_system_snapshot(db_conn, staleness_threshold_ms=300_000)
    read_api.validate_snapshot(snap)
    assert snap["stale"] is True


def test_expected_account_without_mirror_row_is_stale(db_conn):
    _seed_account(db_conn, last_event_minutes_ago=120)
    snap = build_system_snapshot(db_conn, staleness_threshold_ms=60_000)
    assert snap["stale"] is True


def test_v1_envelope_matches_snapshot_verdict(db_conn):
    from psycopg2.extras import RealDictCursor

    _seed_account(db_conn, last_event_minutes_ago=120)
    _seed_mirror(db_conn, age_seconds=400)
    snap = build_system_snapshot(db_conn, staleness_threshold_ms=300_000)
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        env = read_api._envelope(cur, threshold_ms=300_000)
    assert env["stale"] is snap["stale"] is True
    assert env["projection_lag_ms"] > 3_600_000
    assert env["reconciliation_state"] == snap["reconciliation_state"]
    assert env["missing_nodes"] == snap["missing_nodes"]


def _snapshot_and_envelope(db_conn, **thresholds):
    from psycopg2.extras import RealDictCursor

    snap = build_system_snapshot(db_conn, **thresholds)
    envelope_thresholds = {}
    staleness_threshold_ms = thresholds.get("staleness_threshold_ms")
    if staleness_threshold_ms is not None:
        envelope_thresholds["threshold_ms"] = staleness_threshold_ms
    heartbeat_threshold_ms = thresholds.get("heartbeat_threshold_ms")
    if heartbeat_threshold_ms is not None:
        envelope_thresholds["heartbeat_threshold_ms"] = heartbeat_threshold_ms
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        env = read_api._envelope(cur, **envelope_thresholds)
    return snap, env


def test_oldest_expected_account_mirror_controls_staleness(db_conn):
    _seed_account(db_conn, account_id="acct-new")
    _seed_account(db_conn, account_id="acct-old")
    _seed_mirror(db_conn, account_id="acct-new", age_seconds=10)
    _seed_mirror(db_conn, account_id="acct-old", age_seconds=400)

    snap, env = _snapshot_and_envelope(db_conn)

    assert snap["stale"] is True
    assert env["stale"] is snap["stale"]


def test_all_expected_account_mirrors_fresh_are_not_stale(db_conn):
    _seed_account(db_conn, account_id="acct-a")
    _seed_account(db_conn, account_id="acct-b")
    _seed_mirror(db_conn, account_id="acct-a", age_seconds=10)
    _seed_mirror(db_conn, account_id="acct-b", age_seconds=20)

    snap, env = _snapshot_and_envelope(db_conn)

    assert snap["stale"] is False
    assert env["stale"] is snap["stale"]


def test_missing_expected_account_mirror_fails_closed_consistently(db_conn):
    _seed_account(db_conn, account_id="acct-a")
    _seed_account(db_conn, account_id="acct-b")
    _seed_mirror(db_conn, account_id="acct-a", age_seconds=10)

    snap, env = _snapshot_and_envelope(db_conn)

    assert snap["stale"] is True
    assert env["stale"] is snap["stale"]


def test_empty_system_mirror_state_is_not_stale_consistently(db_conn):
    snap, env = _snapshot_and_envelope(db_conn)

    assert snap["stale"] is False
    assert env["stale"] is snap["stale"]


def test_default_thresholds_separate_heartbeat_and_mirror_age(db_conn):
    _seed_account(db_conn)
    _seed_mirror(db_conn, age_seconds=200)
    with transaction(db_conn), db_conn.cursor() as cur:
        old = datetime.now(timezone.utc) - timedelta(seconds=61)
        cur.execute(
            "INSERT INTO node_heartbeats (node_id, status, last_seen_at) "
            "VALUES ('node-61s','up',%s)",
            (old,),
        )

    snap, env = _snapshot_and_envelope(db_conn)

    assert "node-61s" in snap["missing_nodes"]
    assert snap["stale"] is True
    assert env["missing_nodes"] == snap["missing_nodes"]
    assert env["stale"] is snap["stale"]


def test_failed_reconciliation_marks_stale(db_conn):
    _seed_account(db_conn, reconciliation_state="failed")
    snap = build_system_snapshot(db_conn)
    read_api.validate_snapshot(snap)
    assert snap["reconciliation_state"] == "failed"
    assert snap["stale"] is True


def test_missing_node_detected_and_blocks(db_conn):
    with transaction(db_conn), db_conn.cursor() as cur:
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        cur.execute(
            "INSERT INTO node_heartbeats (node_id, status, last_seen_at) VALUES ('node-x','up',%s)",
            (old,),
        )
    snap = build_system_snapshot(db_conn, staleness_threshold_ms=60_000)
    read_api.validate_snapshot(snap)
    assert "node-x" in snap["missing_nodes"]
    assert snap["stale"] is True


# --- API endpoint -----------------------------------------------------------------


def test_api_requires_auth_and_returns_snapshot(db_conn, migrated_db, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("SYSTEM_OBSERVER_TOKEN", "obs-token")

    client = TestClient(read_api.app)

    assert client.get("/api/system/snapshot").status_code == 401
    assert client.get("/api/system/snapshot", headers={"Authorization": "Bearer wrong"}).status_code == 403

    ok = client.get("/api/system/snapshot", headers={"Authorization": "Bearer obs-token"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["data_source"] == "postgres_projection"
    read_api.validate_snapshot(body)


def test_api_fails_closed_without_reader_tokens(monkeypatch):
    from fastapi.testclient import TestClient

    for env_name in ("SYSTEM_OBSERVER_TOKEN", "VIEWER_TOKEN", "RISK_ADMIN_TOKEN", "REVIEWER_TOKEN"):
        monkeypatch.delenv(env_name, raising=False)

    client = TestClient(read_api.app)
    resp = client.get("/api/system/snapshot", headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 503  # no anonymous reads, fail closed


def test_system_snapshot_schema_accepts_live_market_and_exchange_rows(db_conn):
    event_at = datetime(2026, 8, 8, 12, 5, 1, tzinfo=timezone.utc)
    with transaction(db_conn), db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO price_feed_status (
                price_feed_status_id,
                account_id,
                venue_symbol,
                source,
                mark_price,
                last_price,
                bid_price,
                ask_price,
                last_event_at,
                stale
            )
            VALUES (
                gen_random_uuid(),
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                "account-a",
                "SOLUSDT",
                "binance_ws",
                181.25,
                181.20,
                181.15,
                181.30,
                event_at,
                False,
            ),
        )
        cur.execute(
            """
            INSERT INTO exchange_state_mirror (
                account_id,
                payload,
                updated_at
            )
            VALUES (%s, %s::jsonb, %s)
            """,
            (
                "account-a",
                '{"positions": [], "orders": [], "algo_orders": []}',
                event_at,
            ),
        )

    snap = build_system_snapshot(
        db_conn,
        now=event_at + timedelta(seconds=30),
    )
    read_api.validate_snapshot(snap)

    assert snap["data"]["market_prices"][0]["venue_symbol"] == "SOLUSDT"
    assert snap["data"]["exchange_state"][0]["account_id"] == "account-a"
    assert snap["data"]["exchange_state"][0]["stale"] is True


def test_live_mirror_validates_the_canonical_system_snapshot():
    mirror_path = REPO_ROOT / ".live-mirror" / "api" / "read_api.py"
    spec = importlib.util.spec_from_file_location(
        "_snapshot_schema_live_mirror",
        mirror_path,
    )
    assert spec is not None
    assert spec.loader is not None
    mirror = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mirror)

    snapshot = {
        "schema_version": "1.0",
        "data_source": "postgres_projection",
        "snapshot_id": "f329d937-e65b-4500-bc83-2cf6374ac0fc",
        "generated_at": "2026-08-08T12:00:00+00:00",
        "last_execution_event_at": None,
        "projection_lag_ms": 0,
        "stale": False,
        "missing_nodes": [],
        "reconciliation_state": "healthy",
        "data": {
            "account": {},
            "balances": {"equity": 0, "margin": 0},
            "orders": [],
            "positions": [],
            "market_prices": [],
            "exchange_state": [],
            "recent_messages": [],
            "hermes_decisions": [],
            "risk_decisions": [],
            "node_health": [],
            "audit_trail": [],
        },
    }
    read_api.validate_snapshot(snapshot)
    mirror.validate_snapshot(snapshot)
