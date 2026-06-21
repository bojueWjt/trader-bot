from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg2
import pytest

from order_management.commands import request_command_run
from order_management.metrics import derive_trace_id
from order_management.order_reducer import OrderProjectionReducer
from order_management.outbox import enqueue_order_management_event
from reservations import ReservationRequest, reserve_risk
from risk_config import RiskConfig


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def om8_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om8")
    try:
        with psycopg2.connect(url):
            pass
    except psycopg2.OperationalError as exc:
        pytest.skip(f"OM8 DB unavailable at {url}: {exc}")
    return url


@pytest.fixture()
def om8_db_conn(om8_db_url: str):
    conn = psycopg2.connect(om8_db_url)
    try:
        _clean_om8_rows(conn)
        yield conn
    finally:
        conn.rollback()
        _clean_om8_rows(conn)
        conn.close()


def test_outbox_idempotency_replay_same_key_does_not_duplicate() -> None:
    conn = _FakeOutboxConnection()

    first = enqueue_order_management_event(
        conn,
        aggregate_type="order",
        aggregate_id="order-om8",
        event_type="order.submitted",
        idempotency_key="idem-om8",
        request_id="req-om8-outbox",
        payload={"status": "submitted"},
    )
    replay = enqueue_order_management_event(
        conn,
        aggregate_type="order",
        aggregate_id="order-om8",
        event_type="order.submitted",
        idempotency_key="idem-om8",
        request_id="req-om8-outbox",
        payload={"status": "submitted"},
    )

    assert replay == first
    assert len(conn.rows) == 1
    row = conn.rows[first]
    assert row["payload"]["request_id"] == "req-om8-outbox"
    assert row["payload"]["trace_id"] == row["trace_id"]


def test_order_reducer_duplicate_out_of_order_events_preserve_decimal_precision(om8_db_conn) -> None:
    reducer = OrderProjectionReducer()
    accepted = _order_event("evt-om8-accept", "OrderAccepted", 2)
    fill = _order_event(
        "evt-om8-fill-1",
        "OrderFilled",
        3,
        payload={
            "quantity": "1.00000000",
            "filled_qty": "0.33333333",
            "leaves_qty": "0.66666667",
        },
    )
    stale_submit = _order_event("evt-om8-submit", "OrderSubmitted", 1)

    assert reducer.apply_event(om8_db_conn, accepted).applied is True
    assert reducer.apply_event(om8_db_conn, fill).status == "partially_filled"
    assert reducer.apply_event(om8_db_conn, stale_submit).applied is False
    duplicate = reducer.apply_event(om8_db_conn, fill)

    with om8_db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, filled_quantity, updated_from_event_id
            FROM orders_projection
            WHERE account_id='acct-om8-unit' AND client_order_id='coid-om8-unit'
            """
        )
        status, filled_quantity, updated_from_event_id = cur.fetchone()
        cur.execute("SELECT count(*) FROM order_events WHERE account_id='acct-om8-unit'")
        event_count = cur.fetchone()[0]

    assert duplicate.duplicate is True
    assert status == "partially_filled"
    assert filled_quantity == Decimal("0.33333333")
    assert updated_from_event_id == "evt-om8-fill-1"
    assert event_count == 3


def test_command_replay_conflicting_request_with_same_idempotency_key_keeps_original_run(om8_db_conn) -> None:
    first = request_command_run(
        om8_db_conn,
        node_id="om8-unit-node",
        command_type="cancel_all",
        request_id="req-om8-command-a",
        idempotency_key="idem-om8-command",
        payload={"account_id": "acct-om8-unit", "value": "original"},
    )
    replay = request_command_run(
        om8_db_conn,
        node_id="om8-unit-node",
        command_type="cancel_all",
        request_id="req-om8-command-b",
        idempotency_key="idem-om8-command",
        payload={"account_id": "acct-om8-unit", "value": "conflicting"},
    )

    with om8_db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM node_command_runs WHERE node_id='om8-unit-node'")
        count = cur.fetchone()[0]

    assert replay["idempotent"] is True
    assert replay["node_command_run_id"] == first["node_command_run_id"]
    assert replay["request_id"] == "req-om8-command-a"
    assert replay["payload"]["value"] == "original"
    assert replay["payload"]["trace_id"] == derive_trace_id(request_id="req-om8-command-a")
    assert count == 1


def test_reservation_over_allocation_edge_rejects_when_remaining_budget_is_zero(om8_db_conn) -> None:
    _insert_account(om8_db_conn, "acct-om8-unit-res", equity="1000", available="1000")
    config = RiskConfig(max_total_open_risk_pct=Decimal("0.10"))

    first = reserve_risk(
        om8_db_conn,
        ReservationRequest(
            account_id="acct-om8-unit-res",
            instrument_id="BTCUSDT",
            intent_id="intent-om8-res-1",
            idempotency_key="idem-om8-res-1",
            risk_amount=Decimal("100"),
            notional=Decimal("1000"),
            ttl_seconds=60,
            now=NOW,
        ),
        config=config,
    )
    second = reserve_risk(
        om8_db_conn,
        ReservationRequest(
            account_id="acct-om8-unit-res",
            instrument_id="ETHUSDT",
            intent_id="intent-om8-res-2",
            idempotency_key="idem-om8-res-2",
            risk_amount=Decimal("0.01"),
            notional=Decimal("10"),
            ttl_seconds=60,
            now=NOW,
        ),
        config=config,
    )

    with om8_db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT coalesce(sum(risk_amount), 0)
            FROM risk_reservations
            WHERE account_id='acct-om8-unit-res' AND status='held'
            """
        )
        held_risk = cur.fetchone()[0]

    assert first.status == "held"
    assert second.status == "rejected"
    assert second.remaining_budget == Decimal("0")
    assert held_risk == Decimal("100")


class _FakeOutboxConnection:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.cursor_obj = _FakeOutboxCursor(self)

    def cursor(self):
        return self.cursor_obj


class _FakeOutboxCursor:
    def __init__(self, conn: _FakeOutboxConnection) -> None:
        self.conn = conn
        self._row: tuple[str] | None = None

    def __enter__(self) -> "_FakeOutboxCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        assert params is not None
        outbox_event_id, aggregate_type, aggregate_id, event_type, payload, trace_id = params
        if outbox_event_id in self.conn.rows:
            self._row = None
            return
        self.conn.rows[outbox_event_id] = {
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "event_type": event_type,
            "payload": payload.adapted,
            "trace_id": trace_id,
        }
        self._row = (outbox_event_id,)

    def fetchone(self):
        return self._row


def _order_event(event_id: str, event_type: str, seconds: int, *, payload: dict | None = None) -> dict:
    body = {
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "side": "long",
        "order_type": "limit",
        "quantity": "1.00000000",
        "filled_qty": "0",
        "leaves_qty": "1.00000000",
    }
    body.update(payload or {})
    return {
        "event_id": event_id,
        "schema_version": "1.0",
        "node_id": "node-om8-unit",
        "account_id": "acct-om8-unit",
        "client_order_id": "coid-om8-unit",
        "venue_order_id": "venue-om8-unit",
        "event_type": event_type,
        "ts_event": NOW + timedelta(seconds=seconds),
        "payload": body,
    }


def _insert_account(conn, account_id: str, *, equity: str, available: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts_projection (
                account_id, currency, equity, margin, available_balance,
                projection_lag_ms, last_execution_event_at, updated_at
            )
            VALUES (%s, 'USDT', %s, 0, %s, 0, %s, %s)
            ON CONFLICT (account_id) DO UPDATE SET
                equity=EXCLUDED.equity,
                available_balance=EXCLUDED.available_balance,
                last_execution_event_at=EXCLUDED.last_execution_event_at,
                updated_at=EXCLUDED.updated_at
            """,
            (account_id, equity, available, NOW, NOW),
        )
    conn.commit()


def _clean_om8_rows(conn) -> None:
    conn.rollback()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM node_command_runs WHERE node_id LIKE 'om8-unit%'")
            cur.execute("DELETE FROM order_events WHERE account_id LIKE 'acct-om8-unit%'")
            cur.execute("DELETE FROM reconciliation_findings WHERE account_id LIKE 'acct-om8-unit%'")
            cur.execute("DELETE FROM protective_orders_projection WHERE account_id LIKE 'acct-om8-unit%'")
            cur.execute("DELETE FROM orders_projection WHERE account_id LIKE 'acct-om8-unit%'")
            cur.execute("DELETE FROM risk_reservations WHERE account_id LIKE 'acct-om8-unit%'")
            cur.execute("DELETE FROM positions_projection WHERE account_id LIKE 'acct-om8-unit%'")
            cur.execute("DELETE FROM accounts_projection WHERE account_id LIKE 'acct-om8-unit%'")
            cur.execute("DELETE FROM execution_events WHERE account_id LIKE 'acct-om8-unit%'")
    finally:
        conn.autocommit = False
