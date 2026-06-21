from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from psycopg2.extras import Json

from order_management.account_reducer import AccountProjectionReducer, get_account_balance
from order_management.position_reducer import PositionProjectionReducer


BASE_TS = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_position_reducer_uses_canonical_identity_for_both_and_external(db_conn) -> None:
    reducer = PositionProjectionReducer()

    reducer.apply_event(
        db_conn,
        _position_event(
            "pos-both",
            "PositionOpened",
            0,
            payload={"quantity": "2", "position_side": "BOTH"},
        ),
    )
    reducer.apply_event(
        db_conn,
        _position_event(
            "pos-external",
            "PositionOpened",
            1,
            instrument_id="ETHUSDT-PERP.BINANCE",
            payload={"quantity": "1", "ownership": "EXTERNAL"},
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT position_id, status FROM positions_projection "
            "WHERE account_id='acct-om2' ORDER BY position_id"
        )
        rows = cur.fetchall()

    assert rows == [
        ("acct-om2:BTCUSDT", "open"),
        ("acct-om2:ETHUSDT", "external"),
    ]


def test_position_close_merges_to_single_closed_record_and_clears_protection(db_conn) -> None:
    reducer = PositionProjectionReducer()
    reducer.apply_event(db_conn, _position_event("pos-open", "PositionOpened", 0))
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO protective_orders_projection (
                protective_order_projection_id, account_id, position_key, venue_symbol,
                lifecycle_role, client_order_id, status, active, quantity, payload
            )
            VALUES (
                '00000000-0000-0000-0000-000000000001',
                'acct-om2', 'acct-om2:BTCUSDT', 'BTCUSDT',
                'stop_loss', 'stop-1', 'working', true, 1, %s
            )
            """,
            (Json({}),),
        )
    db_conn.commit()

    reducer.apply_event(
        db_conn,
        _position_event(
            "pos-close",
            "PositionClosed",
            3,
            instrument_id="BTCUSDT",
            payload={"quantity": "0"},
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT position_id, quantity, status FROM positions_projection "
            "WHERE account_id='acct-om2'"
        )
        position_rows = cur.fetchall()
        cur.execute(
            "SELECT active FROM protective_orders_projection "
            "WHERE account_id='acct-om2' AND position_key='acct-om2:BTCUSDT'"
        )
        active = cur.fetchone()[0]

    assert position_rows == [("acct-om2:BTCUSDT", Decimal("0"), "closed")]
    assert active is False


def test_account_reducer_persists_queryable_balances(db_conn) -> None:
    reducer = AccountProjectionReducer()
    reducer.apply_event(
        db_conn,
        {
            "event_id": "acct-state",
            "schema_version": "1.0",
            "node_id": "node-1",
            "account_id": "acct-om2",
            "event_type": "AccountState",
            "ts_event": BASE_TS,
            "payload": {
                "currency": "USDT",
                "equity": "1000.55",
                "free": "800.25",
                "margin": "200.30",
                "balance": "1000.55",
            },
        },
    )

    balance = get_account_balance(db_conn, "acct-om2")

    assert balance == {
        "account_id": "acct-om2",
        "currency": "USDT",
        "equity": Decimal("1000.55"),
        "free": Decimal("800.25"),
        "margin": Decimal("200.30"),
        "balance": Decimal("1000.55"),
    }


def _position_event(
    event_id: str,
    event_type: str,
    seconds: int,
    *,
    instrument_id: str = "BTCUSDT-PERP.BINANCE",
    payload: dict | None = None,
) -> dict:
    body = {
        "instrument_id": instrument_id,
        "side": "long",
        "quantity": "1",
        "avg_entry_price": "50000",
        "mark_price": "51000",
        "unrealized_pnl": "100",
    }
    body.update(payload or {})
    return {
        "event_id": event_id,
        "schema_version": "1.0",
        "node_id": "node-1",
        "account_id": "acct-om2",
        "event_type": event_type,
        "ts_event": BASE_TS + timedelta(seconds=seconds),
        "payload": body,
    }
