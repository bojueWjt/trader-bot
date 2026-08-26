from __future__ import annotations

import json
from typing import Any

import pytest

import read_api


ACCOUNT_ID = "account-a"
NODE_ID = "nautilus-node-account-a"
NODE_TOKEN = "node-a-token"
CLIENT_ORDER_ID = "B3562ddc2a0e74f509dc34253ab80beef01"


def test_node_open_orders_are_owned_nonterminal_and_account_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = RecordingConnection(
        [
            {
                "client_order_id": CLIENT_ORDER_ID,
                "venue_order_id": "25082516000001",
                "instrument_id": "PENGUUSDT-PERP.BINANCE",
                "status": "accepted",
                "side": "long",
                "order_type": "LIMIT",
                "reduce_only": False,
                "payload": {
                    "position_side": "BOTH",
                    "time_in_force": "GTC",
                    "reduce_only": False,
                    "tags": [
                        "intent_id="
                        "3562ddc2-a0e7-4f50-9dc3-4253ab80beef"
                    ],
                },
            }
        ]
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_ID: {
                    "account_id": ACCOUNT_ID,
                    "token": NODE_TOKEN,
                }
            }
        ),
    )
    monkeypatch.setattr(
        read_api,
        "_database_connection",
        lambda _database_url: connection,
    )

    response = read_api.node_orders(
        NODE_ID,
        ACCOUNT_ID,
        "open",
        f"Bearer {NODE_TOKEN}",
        NODE_ID,
        ACCOUNT_ID,
    )

    assert response == {
        "account_id": ACCOUNT_ID,
        "orders": [
            {
                "client_order_id": CLIENT_ORDER_ID,
                "venue_order_id": "25082516000001",
                "instrument_id": "PENGUUSDT-PERP.BINANCE",
                "status": "accepted",
                "side": "long",
                "order_type": "LIMIT",
                "reduce_only": False,
                "position_side": "BOTH",
                "time_in_force": "GTC",
                "tags": [
                    "intent_id="
                    "3562ddc2-a0e7-4f50-9dc3-4253ab80beef"
                ],
            }
        ],
    }
    assert connection.cursor_instance.params == (
        ACCOUNT_ID,
        read_api._TERMINAL_ORDER_STATES,
    )
    assert "account_id=%s" in connection.cursor_instance.query
    assert "client_order_id" in connection.cursor_instance.query
    assert connection.closed is True


class RecordingCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.query = ""
        self.params: tuple[Any, ...] = ()

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(
        self,
        query: str,
        params: tuple[Any, ...],
    ) -> None:
        self.query = query
        self.params = params

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class RecordingConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cursor_instance = RecordingCursor(rows)
        self.closed = False

    def cursor(self, **_kwargs: Any) -> RecordingCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True
