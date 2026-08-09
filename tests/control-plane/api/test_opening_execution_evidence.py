from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

import read_api


NODE_ID = "nautilus-node-account-a"
ACCOUNT_ID = "account-a"
TOKEN = "node-a-token"
FILLED_CLIENT_ORDER_ID = "B1111111111111111111111111111111101"
REJECTED_CLIENT_ORDER_ID = "B2222222222222222222222222222222201"
AMBIGUOUS_CLIENT_ORDER_ID = "B3333333333333333333333333333333301"
HISTORY_CLIENT_ORDER_ID = "B4444444444444444444444444444444401"
OTHER_ACCOUNT_CLIENT_ORDER_ID = "B5555555555555555555555555555555501"


def test_exchange_state_returns_account_scoped_historical_opening_evidence(
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    mirror_row = {
        "account_id": ACCOUNT_ID,
        "updated_at": now,
        "stale": False,
        "payload": {
            "source": "binance_fapi",
            "fetched_at": now.isoformat(),
            "open_orders": [],
            "algo_orders": [],
            "recent_order_history": [
                {
                    "symbol": "BTCUSDT",
                    "client_order_id": HISTORY_CLIENT_ORDER_ID,
                    "venue_order_id": "venue-history",
                    "status": "FILLED",
                    "executed_quantity": "0.02",
                }
            ],
            "recent_algo_order_history": [],
        },
    }
    projection_rows = [
        _projection_row(
            account_id=ACCOUNT_ID,
            client_order_id=FILLED_CLIENT_ORDER_ID,
            status="filled",
            filled_quantity="0.01",
            venue_order_id="venue-filled",
        ),
        _projection_row(
            account_id=ACCOUNT_ID,
            client_order_id=AMBIGUOUS_CLIENT_ORDER_ID,
            status="submitted",
            filled_quantity="0",
            venue_order_id=None,
        ),
        _projection_row(
            account_id="account-b",
            client_order_id=OTHER_ACCOUNT_CLIENT_ORDER_ID,
            status="filled",
            filled_quantity="1",
            venue_order_id="venue-other-account",
        ),
    ]
    event_rows = [
        {
            "account_id": ACCOUNT_ID,
            "client_order_id": REJECTED_CLIENT_ORDER_ID,
            "venue_order_id": None,
            "trade_id": None,
            "event_type": "OrderRejected",
            "ts_event": now,
            "payload": {"instrument_id": "BTCUSDT-PERP.BINANCE"},
        }
    ]
    conn = _FakeConnection(
        mirror_row=mirror_row,
        projection_rows=projection_rows,
        event_rows=event_rows,
    )
    _configure(monkeypatch, conn)

    response = _client().get(
        f"/v1/nodes/{NODE_ID}/exchange-state",
        params={"account_id": ACCOUNT_ID},
        headers=_headers(),
    )

    assert response.status_code == 200
    surface = response.json()["opening_execution_evidence"]
    assert surface["authoritative"] is True
    evidence = {item["client_order_id"]: item for item in surface["items"]}
    assert evidence[FILLED_CLIENT_ORDER_ID]["state"] == "confirmed_executed"
    assert evidence[REJECTED_CLIENT_ORDER_ID]["state"] == "definitively_absent"
    assert evidence[AMBIGUOUS_CLIENT_ORDER_ID]["state"] == "unknown"
    assert evidence[HISTORY_CLIENT_ORDER_ID]["state"] == "confirmed_executed"
    assert OTHER_ACCOUNT_CLIENT_ORDER_ID not in evidence
    assert all(item["account_id"] == ACCOUNT_ID for item in surface["items"])


def test_stale_exchange_state_keeps_durable_evidence_authoritative(
    monkeypatch,
) -> None:
    client_order_id = "B6666666666666666666666666666666601"
    conn = _FakeConnection(
        mirror_row={
            "account_id": ACCOUNT_ID,
            "updated_at": datetime.now(timezone.utc),
            "stale": True,
            "payload": {"open_orders": [], "algo_orders": []},
        },
        projection_rows=[
            _projection_row(
                account_id=ACCOUNT_ID,
                client_order_id=client_order_id,
                status="filled",
                filled_quantity="0.01",
                venue_order_id="venue-stale-mirror",
            )
        ],
        event_rows=[],
    )
    _configure(monkeypatch, conn)

    response = _client().get(
        f"/v1/nodes/{NODE_ID}/exchange-state",
        params={"account_id": ACCOUNT_ID},
        headers=_headers(),
    )

    assert response.status_code == 200
    surface = response.json()["opening_execution_evidence"]
    assert surface["authoritative"] is True
    assert surface["reason"] == (
        "durable_evidence_only_exchange_mirror_stale"
    )
    assert surface["items"][0]["client_order_id"] == client_order_id
    assert surface["items"][0]["state"] == "confirmed_executed"


def test_projection_query_failure_uses_fresh_exchange_evidence(
    monkeypatch,
) -> None:
    client_order_id = "B7777777777777777777777777777777701"
    conn = _FakeConnection(
        mirror_row={
            "account_id": ACCOUNT_ID,
            "updated_at": datetime.now(timezone.utc),
            "stale": False,
            "payload": {
                "open_orders": [],
                "algo_orders": [],
                "recent_order_history": [
                    {
                        "symbol": "BTCUSDT",
                        "client_order_id": client_order_id,
                        "venue_order_id": "venue-query-failed",
                        "status": "FILLED",
                        "executed_quantity": "0.01",
                    }
                ],
            },
        },
        projection_rows=[],
        event_rows=[],
        fail_projection_query=True,
    )
    _configure(monkeypatch, conn)

    response = _client().get(
        f"/v1/nodes/{NODE_ID}/exchange-state",
        params={"account_id": ACCOUNT_ID},
        headers=_headers(),
    )

    assert response.status_code == 200
    surface = response.json()["opening_execution_evidence"]
    assert surface["authoritative"] is True
    assert surface["reason"] == (
        "fresh_exchange_evidence_durable_query_failed"
    )
    assert surface["items"][0]["client_order_id"] == client_order_id
    assert surface["items"][0]["state"] == "confirmed_executed"
    assert conn.rollback_count == 1


def test_stale_mirror_and_durable_query_failure_are_unknown(
    monkeypatch,
) -> None:
    conn = _FakeConnection(
        mirror_row={
            "account_id": ACCOUNT_ID,
            "updated_at": datetime.now(timezone.utc),
            "stale": True,
            "payload": {"open_orders": [], "algo_orders": []},
        },
        projection_rows=[],
        event_rows=[],
        fail_projection_query=True,
    )
    _configure(monkeypatch, conn)

    response = _client().get(
        f"/v1/nodes/{NODE_ID}/exchange-state",
        params={"account_id": ACCOUNT_ID},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["opening_execution_evidence"] == {
        "authoritative": False,
        "reason": "opening_evidence_unavailable",
        "items": [],
    }


class _FakeConnection:
    def __init__(
        self,
        *,
        mirror_row: dict[str, Any],
        projection_rows: list[dict[str, Any]],
        event_rows: list[dict[str, Any]],
        fail_projection_query: bool = False,
    ) -> None:
        self.mirror_row = mirror_row
        self.projection_rows = projection_rows
        self.event_rows = event_rows
        self.fail_projection_query = fail_projection_query
        self.rollback_count = 0
        self.closed = False

    def cursor(self, **_kwargs):
        return _FakeCursor(self)

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn
        self.rows: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, _params=None) -> None:
        if "to_regclass" in sql:
            self.rows = [{"present": True}]
            return
        if "FROM exchange_state_mirror" in sql:
            self.rows = [self.conn.mirror_row]
            return
        if "FROM orders_projection" in sql:
            if self.conn.fail_projection_query:
                raise read_api.psycopg2.OperationalError("projection unavailable")
            self.rows = self.conn.projection_rows
            return
        if "FROM execution_events" in sql:
            self.rows = self.conn.event_rows
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        if not self.rows:
            return None
        return self.rows[0]

    def fetchall(self):
        return list(self.rows)


def _projection_row(
    *,
    account_id: str,
    client_order_id: str,
    status: str,
    filled_quantity: str,
    venue_order_id: str | None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "account_id": account_id,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "client_order_id": client_order_id,
        "venue_order_id": venue_order_id,
        "status": status,
        "filled_quantity": filled_quantity,
        "ts_event": now,
        "updated_at": now,
        "payload": {},
    }


def _configure(monkeypatch, conn: _FakeConnection) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_ID: {
                    "account_id": ACCOUNT_ID,
                    "token": TOKEN,
                }
            }
        ),
    )
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)


def _client() -> TestClient:
    return TestClient(read_api.app)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-Node-Id": NODE_ID,
        "X-Account-Id": ACCOUNT_ID,
    }
