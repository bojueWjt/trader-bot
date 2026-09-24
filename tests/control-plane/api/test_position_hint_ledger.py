from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import psycopg2

import read_api
from repository import ProjectionWriter


def test_closed_flat_uses_position_id_suffix_and_forces_qty_zero():
    hint = read_api._normalize_position_hint(
        {
            "account_id": "account-d",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "position_id": "BTCUSDT-PERP.BINANCE-SHORT",
            "side": 1,
            "quantity": "0.235",
        },
        "PositionClosed",
    )
    assert hint is not None
    assert hint["side"] == "short"
    assert hint["quantity"] == 0.0
    assert hint["status"] == "closed"
    assert hint["position_id"] == "BTCUSDT-PERP.BINANCE-SHORT"


def test_closed_flat_without_suffix_does_not_collapse_to_long():
    hint = read_api._normalize_position_hint(
        {
            "account_id": "account-d",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "side": "FLAT",
            "quantity": "1",
        },
        "PositionClosed",
    )
    assert hint is None


def test_hedge_long_and_short_stay_isolated():
    long_hint = read_api._normalize_position_hint(
        {
            "instrument_id": "ASTERUSDT-PERP.BINANCE",
            "position_id": "ASTERUSDT-PERP.BINANCE-LONG",
            "side": 2,
            "quantity": "10",
        },
        "PositionOpened",
    )
    short_closed = read_api._normalize_position_hint(
        {
            "instrument_id": "ASTERUSDT-PERP.BINANCE",
            "position_id": "ASTERUSDT-PERP.BINANCE-SHORT",
            "side": 1,
            "quantity": "10",
        },
        "PositionClosed",
    )
    assert long_hint["position_id"] == "ASTERUSDT-PERP.BINANCE-LONG"
    assert long_hint["status"] == "open"
    assert short_closed["position_id"] == "ASTERUSDT-PERP.BINANCE-SHORT"
    assert short_closed["quantity"] == 0.0


def test_out_of_order_older_opened_does_not_overwrite_newer_qty(migrated_db):
    later = datetime(2026, 9, 23, 14, 13, 39, tzinfo=timezone.utc)
    earlier = datetime(2026, 9, 23, 14, 10, 46, tzinfo=timezone.utc)
    position_id = "BTCUSDT-PERP.BINANCE-LONG"
    with psycopg2.connect(migrated_db) as conn:
        writer = ProjectionWriter(conn)
        writer.upsert_position_projection(
            {
                "account_id": "account-d",
                "position_id": position_id,
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "side": "long",
                "quantity": 0.235,
                "status": "open",
                "ts_event": later,
            }
        )
        writer.upsert_position_projection(
            {
                "account_id": "account-d",
                "position_id": position_id,
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "side": "long",
                "quantity": 0.087,
                "status": "open",
                "ts_event": earlier,
            }
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT quantity, status FROM positions_projection "
                "WHERE account_id=%s AND position_id=%s",
                ("account-d", position_id),
            )
            quantity, status = cur.fetchone()
    assert quantity == Decimal("0.235")
    assert status == "open"


def test_missing_qty_on_open_is_rejected():
    hint = read_api._normalize_position_hint(
        {
            "account_id": "account-d",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "position_id": "BTCUSDT-PERP.BINANCE-LONG",
            "side": "long",
        },
        "PositionOpened",
    )
    assert hint is None


def test_missing_side_is_not_invented_long():
    hint = read_api._normalize_position_hint(
        {
            "account_id": "account-d",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "quantity": "1",
        },
        "PositionOpened",
    )
    assert hint is None


def test_contradictory_id_and_side_rejected():
    hint = read_api._normalize_position_hint(
        {
            "account_id": "account-d",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "position_id": "BTCUSDT-PERP.BINANCE-LONG",
            "side": "short",
            "quantity": "1",
        },
        "PositionOpened",
    )
    assert hint is None


def test_nonfinite_qty_rejected():
    hint = read_api._normalize_position_hint(
        {
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "position_id": "BTCUSDT-PERP.BINANCE-LONG",
            "side": "long",
            "quantity": "inf",
        },
        "PositionOpened",
    )
    assert hint is None


def test_zero_mark_price_and_unrealized_pnl_are_kept():
    hint = read_api._position_projection_from_event(
        {
            "account_id": "account-d",
            "event_id": "e-zero",
            "event_type": "PositionOpened",
            "payload": {
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "position_id": "BTCUSDT-PERP.BINANCE-LONG",
                "side": "long",
                "quantity": "1",
                "mark_price": 0,
                "unrealized_pnl": 0,
            },
        }
    )
    assert hint is not None
    assert hint["mark_price"] == 0.0
    assert hint["unrealized_pnl"] == 0.0


def test_invalid_position_hint_records_projection_failure():
    class _Writer:
        def __init__(self):
            self.failures = []
            self.upserts = []

        def record_projection_failure(self, **kwargs):
            self.failures.append(kwargs)

        def upsert_position_projection(self, hint):
            self.upserts.append(hint)

        def upsert_projection_watermark(self, **kwargs):
            return None

    writer = _Writer()
    read_api._derive_projection_from_event(
        writer,
        {
            "account_id": "account-d",
            "event_id": "e-bad",
            "event_type": "PositionOpened",
            "payload": {
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "side": "long",
            },
        },
    )
    assert writer.upserts == []
    assert writer.failures
    assert writer.failures[0]["projector"] == "positions"


def test_same_ts_closed_wins_over_open(migrated_db):
    ts = datetime(2026, 9, 23, 16, 2, 9, tzinfo=timezone.utc)
    position_id = "BTCUSDT-PERP.BINANCE-LONG"
    with psycopg2.connect(migrated_db) as conn:
        writer = ProjectionWriter(conn)
        writer.upsert_position_projection(
            {
                "account_id": "account-d",
                "position_id": position_id,
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "side": "long",
                "quantity": 0.235,
                "status": "open",
                "ts_event": ts,
            }
        )
        writer.upsert_position_projection(
            {
                "account_id": "account-d",
                "position_id": position_id,
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "side": "long",
                "quantity": 0,
                "status": "closed",
                "ts_event": ts,
            }
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT quantity, status FROM positions_projection "
                "WHERE account_id=%s AND position_id=%s",
                ("account-d", position_id),
            )
            quantity, status = cur.fetchone()
    assert quantity == Decimal("0")
    assert status == "closed"
