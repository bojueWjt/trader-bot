"""B-03 / M1b: GET /v1/mirror/positions and GET /v1/reconcile (T1-1, T1-2, T1-4, T1-5, T1-6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

import read_api
from connection import transaction

VIEWER = "viewer-token"
AUTH = {"Authorization": f"Bearer {VIEWER}"}
BOT_ID = "B" + ("c" * 32) + "07"
MANUAL_ID = "aos_manual_eth_sl"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("VIEWER_TOKEN", VIEWER)
    return TestClient(read_api.app)


def _seed_account(conn, account_id: str = "account-a") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts_projection (account_id, currency, equity, margin, reconciliation_state)"
            " VALUES (%s,'USDT',1000,100,'healthy')",
            (account_id,),
        )


def _insert_mirror(conn, *, account_id: str, payload: dict, age_seconds: float = 5.0) -> None:
    updated = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO exchange_state_mirror (account_id, payload, updated_at)
            VALUES (%s, %s, %s)
            """,
            (account_id, Json(payload), updated),
        )


def _sl(*, side: str, qty: str, cid: str, oid: str, symbol: str = "ETHUSDT") -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "trigger_price": "2400",
        "quantity": qty,
        "client_order_id": cid,
        "order_id": oid,
    }


def _tp(*, side: str, qty: str, cid: str, oid: str, symbol: str = "ETHUSDT") -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "type": "TAKE_PROFIT_MARKET",
        "trigger_price": "2800",
        "quantity": qty,
        "client_order_id": cid,
        "order_id": oid,
    }


def _pos(*, side: str, qty: str = "1.25", symbol: str = "ETHUSDT") -> dict:
    return {
        "symbol": symbol,
        "position_side": side,
        "position_amt": qty,
        "entry_price": "2500",
        "mark_price": "2510",
        "unrealized_pnl": "12.5",
        "leverage": "10",
    }


def _accounts(body: dict) -> list[dict]:
    return body["data"]["accounts"]


def test_t1_1_full_sl_tp_is_protected(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn)
        _insert_mirror(
            db_conn,
            account_id="account-a",
            payload={
                "positions": [_pos(side="LONG")],
                "open_orders": [_sl(side="SELL", qty="1.25", cid=BOT_ID, oid="sl-1")],
                "algo_orders": [_tp(side="SELL", qty="1.25", cid=BOT_ID[:-2] + "08", oid="tp-1")],
                "filters": {"ETHUSDT": {"quantity_step": "0.001", "min_quantity": "0.001"}},
            },
        )
    body = client.get("/v1/mirror/positions", headers=AUTH).json()
    assert body["data_source"] == "exchange_state_mirror"
    pos = _accounts(body)[0]["positions"][0]
    assert pos["protection"]["status"] == "protected"
    assert "protection_policy" not in pos["protection"]
    assert pos["quantity"] == "1.25"
    assert pos["quantity_step"] == "0.001"
    assert pos["position_side"] == "LONG"


def test_t1_2_hedge_long_short_no_cross_attach(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn)
        _insert_mirror(
            db_conn,
            account_id="account-a",
            payload={
                "positions": [_pos(side="LONG", qty="1.0"), _pos(side="SHORT", qty="2.0")],
                "open_orders": [],
                "algo_orders": [
                    _sl(side="SELL", qty="1.0", cid=BOT_ID, oid="sl-long"),
                    _sl(side="BUY", qty="2.0", cid=BOT_ID[:-2] + "09", oid="sl-short"),
                    _tp(side="SELL", qty="1.0", cid=BOT_ID[:-2] + "10", oid="tp-long"),
                    _tp(side="BUY", qty="2.0", cid=BOT_ID[:-2] + "11", oid="tp-short"),
                ],
            },
        )
    positions = {p["position_side"]: p for p in _accounts(client.get("/v1/mirror/positions", headers=AUTH).json())[0]["positions"]}
    long_sl = {row["order_id"] for row in positions["LONG"]["protection"]["stop_loss"]}
    short_sl = {row["order_id"] for row in positions["SHORT"]["protection"]["stop_loss"]}
    assert long_sl == {"sl-long"}
    assert short_sl == {"sl-short"}
    assert positions["LONG"]["protection"]["status"] == "protected"
    assert positions["SHORT"]["protection"]["status"] == "protected"
    assert "sl-short" not in {row["order_id"] for row in positions["LONG"]["protection"]["take_profits"]}


def test_t1_4_mirror_age_over_300s_marks_account_and_envelope_stale(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn)
        _insert_mirror(
            db_conn,
            account_id="account-a",
            payload={"positions": [_pos(side="LONG")], "open_orders": [], "algo_orders": []},
            age_seconds=301,
        )
    body = client.get("/v1/mirror/positions", headers=AUTH).json()
    acct = _accounts(body)[0]
    assert acct["stale"] is True
    assert acct["mirror_age_seconds"] >= 300
    assert body["stale"] is True
    assert body["data_source"] == "exchange_state_mirror"


def test_t1_5_bot_regex_vs_manual(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn)
        _insert_mirror(
            db_conn,
            account_id="account-a",
            payload={
                "positions": [_pos(side="LONG")],
                "open_orders": [],
                "algo_orders": [
                    _sl(side="SELL", qty="0.5", cid=BOT_ID, oid="bot-sl"),
                    _sl(side="SELL", qty="0.5", cid=MANUAL_ID, oid="manual-sl"),
                ],
            },
        )
    sl = _accounts(client.get("/v1/mirror/positions", headers=AUTH).json())[0]["positions"][0]["protection"]["stop_loss"]
    by_id = {row["order_id"]: row["is_bot_order"] for row in sl}
    assert by_id["bot-sl"] is True
    assert by_id["manual-sl"] is False


def test_t1_6_ghost_missing_algo_not_ghost_stale_skip_and_last_run(client, db_conn):
    algo_cid = BOT_ID
    ghost_cid = "ghost-proj-1"
    missing_oid = "ex-missing-1"
    with transaction(db_conn):
        _seed_account(db_conn, "account-a")
        _seed_account(db_conn, "account-b")
        _insert_mirror(
            db_conn,
            account_id="account-a",
            payload={
                "positions": [],
                "open_orders": [],
                "algo_orders": [
                    _sl(side="SELL", qty="1", cid=algo_cid, oid="algo-live"),
                ],
            },
            age_seconds=10,
        )
        _insert_mirror(
            db_conn,
            account_id="account-b",
            payload={
                "open_orders": [{"symbol": "ETHUSDT", "client_order_id": "stale-live", "order_id": "x"}],
                "algo_orders": [],
            },
            age_seconds=400,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders_projection (
                    order_projection_id, account_id, instrument_id, client_order_id, status, side, quantity
                ) VALUES
                    (%s, 'account-a', 'ETHUSDT-PERP.BINANCE', %s, 'accepted', 'long', 1),
                    (%s, 'account-a', 'ETHUSDT-PERP.BINANCE', %s, 'accepted', 'long', 1),
                    (%s, 'account-b', 'ETHUSDT-PERP.BINANCE', 'stale-ghost', 'accepted', 'long', 1)
                """,
                (str(uuid4()), algo_cid, str(uuid4()), ghost_cid, str(uuid4())),
            )
            run_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO reconciliation_runs (
                    reconciliation_run_id, account_id, status, started_at, completed_at
                ) VALUES (%s, 'account-a', 'completed', now(), now())
                """,
                (run_id,),
            )
            cur.execute(
                """
                INSERT INTO reconciliation_findings (
                    reconciliation_finding_id, reconciliation_run_id, account_id,
                    finding_type, severity, status
                ) VALUES (%s, %s, 'account-a', 'ghost', 'P2', 'open')
                """,
                (str(uuid4()), run_id),
            )
        # missing exchange-only order
        with db_conn.cursor() as cur:
            cur.execute("SELECT payload FROM exchange_state_mirror WHERE account_id='account-a'")
            payload = cur.fetchone()[0]
        payload["open_orders"] = [
            {
                "symbol": "ETHUSDT",
                "client_order_id": "missing-ex",
                "order_id": missing_oid,
                "side": "BUY",
                "type": "LIMIT",
            }
        ]
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE exchange_state_mirror SET payload=%s WHERE account_id='account-a'",
                (Json(payload),),
            )

    body = client.get("/v1/reconcile", headers=AUTH).json()
    data = body["data"]
    ghost_cids = {g["client_order_id"] for g in data["ghost_orders"]}
    assert ghost_cid in ghost_cids
    assert algo_cid not in ghost_cids
    missing_ids = {m["exchange_order_id"] for m in data["missing_orders"]}
    assert missing_oid in missing_ids
    assert "account-b" in data["skipped_accounts"]
    assert all(g["account_id"] != "account-b" for g in data["ghost_orders"])
    assert data["last_run"] is not None
    assert data["last_run"]["findings_open"] >= 1
    assert data["last_run"]["run_id"]


def test_t1_6_empty_diff_has_last_run(client, db_conn):
    cid = BOT_ID
    with transaction(db_conn):
        _seed_account(db_conn)
        _insert_mirror(
            db_conn,
            account_id="account-a",
            payload={
                "open_orders": [{"symbol": "ETHUSDT", "client_order_id": cid, "order_id": "1"}],
                "algo_orders": [],
            },
        )
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders_projection (
                    order_projection_id, account_id, instrument_id, client_order_id, status
                ) VALUES (%s, 'account-a', 'ETHUSDT-PERP.BINANCE', %s, 'accepted')
                """,
                (str(uuid4()), cid),
            )
            cur.execute(
                """
                INSERT INTO reconciliation_runs (
                    reconciliation_run_id, status, started_at, completed_at
                ) VALUES (%s, 'completed', now(), now())
                """,
                (str(uuid4()),),
            )
    data = client.get("/v1/reconcile", headers=AUTH).json()["data"]
    assert data["ghost_orders"] == []
    assert data["missing_orders"] == []
    assert data["last_run"] is not None
