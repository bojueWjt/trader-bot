from __future__ import annotations

from datetime import datetime, timedelta, timezone

from connection import transaction
from snapshot import build_system_snapshot, validate_snapshot


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
    validate_snapshot(snap)
    assert snap["data_source"] == "postgres_projection"
    assert snap["stale"] is False
    assert snap["last_execution_event_at"] is None
    assert snap["projection_lag_ms"] == 0
    assert snap["reconciliation_state"] == "healthy"
    assert snap["data"]["balances"] == {"equity": 0.0, "margin": 0.0}
    assert snap["data"]["orders"] == []
    assert snap["data"]["market_prices"] == []
    assert snap["missing_nodes"] == []


def test_populated_snapshot_is_schema_valid(db_conn):
    _seed_account(db_conn, last_event_minutes_ago=0)
    snap = build_system_snapshot(db_conn)
    validate_snapshot(snap)
    assert snap["data"]["balances"]["equity"] == 1000.0
    assert snap["last_execution_event_at"] is not None
    assert snap["stale"] is False


def test_snapshot_includes_market_prices_from_price_feed_status(db_conn):
    event_at = datetime(2026, 6, 19, 12, 5, 1, tzinfo=timezone.utc)
    with transaction(db_conn), db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO price_feed_status ("
            "price_feed_status_id, account_id, venue_symbol, source, mark_price, last_price, "
            "bid_price, ask_price, last_event_at, stale"
            ") VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "acct-1",
                "BTCUSDT",
                "binance_ws",
                65123.45,
                65120.0,
                65119.5,
                65121.0,
                event_at,
                False,
            ),
        )

    snap = build_system_snapshot(db_conn)
    validate_snapshot(snap)

    assert snap["data"]["market_prices"] == [
        {
            "account_id": "acct-1",
            "venue_symbol": "BTCUSDT",
            "source": "binance_ws",
            "mark_price": 65123.45,
            "last_price": 65120.0,
            "bid_price": 65119.5,
            "ask_price": 65121.0,
            "last_event_at": event_at.isoformat(),
            "stale": False,
        }
    ]


def test_snapshot_survives_missing_price_feed_status_table(db_conn):
    # Live deployments still on migration 0004 have no price_feed_status table;
    # the snapshot must degrade to an empty list instead of erroring.
    with transaction(db_conn), db_conn.cursor() as cur:
        cur.execute("ALTER TABLE price_feed_status RENAME TO price_feed_status_hidden")
    try:
        snap = build_system_snapshot(db_conn)
        validate_snapshot(snap)
        assert snap["data"]["market_prices"] == []
    finally:
        with transaction(db_conn), db_conn.cursor() as cur:
            cur.execute("ALTER TABLE price_feed_status_hidden RENAME TO price_feed_status")


def test_stale_when_last_event_old(db_conn):
    _seed_account(db_conn, last_event_minutes_ago=120)
    snap = build_system_snapshot(db_conn, staleness_threshold_ms=60_000)
    validate_snapshot(snap)
    assert snap["projection_lag_ms"] > 60_000
    assert snap["stale"] is True


def test_failed_reconciliation_marks_stale(db_conn):
    _seed_account(db_conn, reconciliation_state="failed")
    snap = build_system_snapshot(db_conn)
    validate_snapshot(snap)
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
    validate_snapshot(snap)
    assert "node-x" in snap["missing_nodes"]
    assert snap["stale"] is True


# --- API endpoint -----------------------------------------------------------------


def test_api_requires_auth_and_returns_snapshot(db_conn, migrated_db, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("SYSTEM_OBSERVER_TOKEN", "obs-token")

    import read_api as api_app

    client = TestClient(api_app.app)

    assert client.get("/api/system/snapshot").status_code == 401
    assert client.get("/api/system/snapshot", headers={"Authorization": "Bearer wrong"}).status_code == 403

    ok = client.get("/api/system/snapshot", headers={"Authorization": "Bearer obs-token"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["data_source"] == "postgres_projection"
    validate_snapshot(body)


def test_api_fails_closed_without_reader_tokens(monkeypatch):
    from fastapi.testclient import TestClient

    for env_name in ("SYSTEM_OBSERVER_TOKEN", "VIEWER_TOKEN", "RISK_ADMIN_TOKEN", "REVIEWER_TOKEN"):
        monkeypatch.delenv(env_name, raising=False)

    import read_api as api_app

    client = TestClient(api_app.app)
    resp = client.get("/api/system/snapshot", headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 503  # no anonymous reads, fail closed
