"""B-02 / M1a: additive fields on GET /v1/positions|orders|trades|nodes (T1-8, T1-3)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

import read_api
from connection import transaction

ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")
RELEASE_ID = "ab" * 32  # 64 hex chars
VIEWER = "viewer-token"
AUTH = {"Authorization": f"Bearer {VIEWER}"}
BOT_CLIENT_ID = "B" + ("a" * 32) + "01"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("VIEWER_TOKEN", VIEWER)
    return TestClient(read_api.app)


def _seed_account(conn, account_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts_projection (account_id, currency, equity, margin, reconciliation_state)"
            " VALUES (%s,'USDT',1000,100,'healthy')",
            (account_id,),
        )


def _insert_open_position(conn, *, account_id: str, symbol: str, side: str, quantity: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity,
                avg_entry_price, mark_price, unrealized_pnl, status
            ) VALUES (%s, %s, %s, %s, %s, 100, 101, 1, 'open')
            """,
            (
                account_id,
                f"pos-{account_id}-{symbol}-{side}",
                f"{symbol}-PERP.BINANCE",
                side,
                quantity,
            ),
        )


def _insert_closed_position(conn, *, account_id: str, symbol: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity,
                avg_entry_price, mark_price, unrealized_pnl, status
            ) VALUES (%s, %s, %s, 'long', 1, 100, 110, 10, 'closed')
            """,
            (account_id, f"trade-{account_id}-{symbol}", f"{symbol}-PERP.BINANCE"),
        )


def _insert_order(conn, *, account_id: str, symbol: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_projection (
                order_projection_id, account_id, instrument_id, client_order_id,
                status, side, order_type, quantity, filled_quantity
            ) VALUES (%s, %s, %s, %s, 'accepted', 'long', 'LIMIT', 1, 0)
            """,
            (str(uuid4()), account_id, f"{symbol}-PERP.BINANCE", f"ord-{account_id}-{symbol}"),
        )


def _insert_node(conn, *, account_id: str, halt_reason: str | None = "manual-halt") -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_heartbeats (
                node_id, account_id, status, version, last_seen_at, release_id, payload
            ) VALUES (%s, %s, 'HALTED', 'test-ver', %s, %s, %s)
            """,
            (
                f"node-{account_id}",
                account_id,
                datetime.now(timezone.utc),
                RELEASE_ID,
                Json({"halt_reason": halt_reason, "readiness": "halted"}),
            ),
        )


def _insert_mirror(conn, *, account_id: str, open_orders=None, algo_orders=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO exchange_state_mirror (account_id, payload, updated_at)
            VALUES (%s, %s, now())
            """,
            (
                account_id,
                Json(
                    {
                        "positions": [],
                        "open_orders": list(open_orders or []),
                        "algo_orders": list(algo_orders or []),
                    }
                ),
            ),
        )


def _seed_four_accounts(conn) -> None:
    with transaction(conn):
        for i, account_id in enumerate(ACCOUNTS):
            _seed_account(conn, account_id)
            _insert_open_position(
                conn, account_id=account_id, symbol="ETHUSDT", side="long", quantity="1.25"
            )
            _insert_closed_position(conn, account_id=account_id, symbol="BTCUSDT")
            _insert_order(conn, account_id=account_id, symbol="ETHUSDT")
            _insert_node(conn, account_id=account_id)
            _insert_mirror(conn, account_id=account_id)


def test_t1_8_positions_orders_trades_rows_have_account_id(client, db_conn):
    _seed_four_accounts(db_conn)
    positions = client.get("/v1/positions", headers=AUTH)
    orders = client.get("/v1/orders", headers=AUTH)
    trades = client.get("/v1/trades", headers=AUTH)
    assert positions.status_code == 200
    assert orders.status_code == 200
    assert trades.status_code == 200
    for name, rows in (
        ("positions", positions.json()["positions"]),
        ("orders", orders.json()["orders"]),
        ("trades", trades.json()["trades"]),
    ):
        assert rows, f"{name} empty"
        for row in rows:
            assert row.get("account_id") in ACCOUNTS, (name, row)


def test_t1_8_nodes_include_release_id_halt_reason_and_keep_status(client, db_conn):
    _seed_four_accounts(db_conn)
    resp = client.get("/v1/nodes", headers=AUTH)
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    assert nodes
    for node in nodes:
        assert node.get("release_id") == RELEASE_ID
        assert len(str(node["release_id"])) == 64
        assert node.get("halt_reason") == "manual-halt"
        assert node.get("trading_state") == "HALTED"
        assert "status" in node
        assert "version" in node


def test_t1_8_existing_position_keys_remain_and_protection_has_no_policy(client, db_conn):
    _seed_four_accounts(db_conn)
    row = client.get("/v1/positions", headers=AUTH).json()["positions"][0]
    for key in ("quantity", "side", "instrument_symbol", "status"):
        assert key in row
    protection = row["protection"]
    assert "protection_policy" not in protection
    assert "protection_policy" not in row
    assert protection["status"] in {"protected", "partial", "unprotected"}


def test_t1_3_tp_only_is_partial(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn, "account-a")
        _insert_open_position(
            db_conn, account_id="account-a", symbol="ETHUSDT", side="long", quantity="1.25"
        )
        _insert_mirror(
            db_conn,
            account_id="account-a",
            algo_orders=[
                {
                    "symbol": "ETHUSDT",
                    "side": "SELL",
                    "type": "TAKE_PROFIT_MARKET",
                    "trigger_price": "2800",
                    "quantity": "1.25",
                    "client_order_id": BOT_CLIENT_ID,
                }
            ],
        )
    row = client.get("/v1/positions", headers=AUTH).json()["positions"][0]
    assert row["protection"]["status"] == "partial"
    assert row["protection"]["stop_loss"] == []
    assert len(row["protection"]["take_profits"]) == 1
    assert "protection_policy" not in row["protection"]


def test_t1_3_no_protection_is_unprotected(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn, "account-a")
        _insert_open_position(
            db_conn, account_id="account-a", symbol="ETHUSDT", side="short", quantity="2"
        )
        _insert_mirror(db_conn, account_id="account-a")
    row = client.get("/v1/positions", headers=AUTH).json()["positions"][0]
    assert row["protection"]["status"] == "unprotected"
    assert row["protection"]["stop_loss"] == []
    assert row["protection"]["take_profits"] == []


def test_t1_3_sl_only_in_algo_orders_is_not_unprotected(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn, "account-a")
        _insert_open_position(
            db_conn, account_id="account-a", symbol="ETHUSDT", side="long", quantity="1.25"
        )
        _insert_mirror(
            db_conn,
            account_id="account-a",
            open_orders=[],
            algo_orders=[
                {
                    "symbol": "ETHUSDT",
                    "side": "SELL",
                    "type": "STOP_MARKET",
                    "trigger_price": "2400",
                    "quantity": "1.25",
                    "order_id": "algo-sl-1",
                    "client_order_id": BOT_CLIENT_ID,
                }
            ],
        )
    row = client.get("/v1/positions", headers=AUTH).json()["positions"][0]
    assert row["protection"]["status"] != "unprotected"
    assert row["protection"]["status"] == "protected"
    assert row["protection"]["stop_loss"]
    assert row["protection"]["stop_loss"][0]["is_bot_order"] is True
    assert "quantity" in row
    assert "side" in row
