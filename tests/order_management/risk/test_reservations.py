from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg2

from risk_config import RiskConfig
from reservations import (
    ReservationRequest,
    consume_reservation,
    expire_reservations,
    load_active_reservations,
    release_reservation,
    reserve_risk,
)


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_reserve_release_expire_consume_and_recover_from_db(db_conn) -> None:
    _insert_account(db_conn, "acct-om3-res", equity="10000", available="9000")

    held = reserve_risk(
        db_conn,
        ReservationRequest(
            account_id="acct-om3-res",
            instrument_id="BTCUSDT-PERP.BINANCE",
            intent_id="intent-1",
            idempotency_key="idem-1",
            risk_amount=Decimal("100"),
            notional=Decimal("1000"),
            ttl_seconds=30,
            now=NOW,
        ),
        config=RiskConfig(max_total_open_risk_pct=Decimal("0.10")),
    )

    assert held.status == "held"
    assert held.reservation_id is not None

    active = load_active_reservations(db_conn, account_id="acct-om3-res", now=NOW)
    assert [r.reservation_id for r in active] == [held.reservation_id]

    released = release_reservation(db_conn, held.reservation_id, now=NOW)
    assert released.status == "released"

    expired = reserve_risk(
        db_conn,
        ReservationRequest(
            account_id="acct-om3-res",
            instrument_id="ETHUSDT",
            intent_id="intent-2",
            idempotency_key="idem-2",
            risk_amount=Decimal("50"),
            notional=Decimal("500"),
            ttl_seconds=1,
            now=NOW - timedelta(seconds=2),
        ),
        config=RiskConfig(max_total_open_risk_pct=Decimal("0.10")),
    )
    assert expire_reservations(db_conn, now=NOW) == 1
    assert load_active_reservations(db_conn, account_id="acct-om3-res", now=NOW) == []

    consumed = reserve_risk(
        db_conn,
        ReservationRequest(
            account_id="acct-om3-res",
            instrument_id="SOLUSDT",
            intent_id="intent-3",
            idempotency_key="idem-3",
            risk_amount=Decimal("25"),
            notional=Decimal("250"),
            ttl_seconds=30,
            now=NOW,
        ),
        config=RiskConfig(max_total_open_risk_pct=Decimal("0.10")),
    )
    assert consume_reservation(db_conn, consumed.reservation_id, now=NOW).status == "consumed"
    assert expired.status == "held"


def test_concurrent_reservations_cannot_over_allocate_account_budget(risk_db_url) -> None:
    setup = psycopg2.connect(risk_db_url)
    try:
        _clean_account(setup, "acct-om3-concurrent")
        _insert_account(setup, "acct-om3-concurrent", equity="10000", available="9000")
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results = []

    def worker(name: str) -> None:
        conn = psycopg2.connect(risk_db_url)
        try:
            barrier.wait(timeout=5)
            result = reserve_risk(
                conn,
                ReservationRequest(
                    account_id="acct-om3-concurrent",
                    instrument_id="BTCUSDT",
                    intent_id=f"intent-{name}",
                    idempotency_key=f"idem-{name}",
                    risk_amount=Decimal("700"),
                    notional=Decimal("7000"),
                    ttl_seconds=60,
                    now=NOW,
                ),
                config=RiskConfig(max_total_open_risk_pct=Decimal("0.10")),
            )
            results.append(result.status)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results) == ["held", "rejected"]

    verify = psycopg2.connect(risk_db_url)
    try:
        with verify.cursor() as cur:
            cur.execute(
                """
                SELECT coalesce(sum(risk_amount), 0)
                FROM risk_reservations
                WHERE account_id='acct-om3-concurrent' AND status='held'
                """
            )
            assert cur.fetchone()[0] == Decimal("700")
    finally:
        _clean_account(verify, "acct-om3-concurrent")
        verify.close()


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


def _clean_account(conn, account_id: str) -> None:
    conn.rollback()  # close any open transaction so autocommit can be toggled
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_reservations WHERE account_id=%s", (account_id,))
            cur.execute("DELETE FROM positions_projection WHERE account_id=%s", (account_id,))
            cur.execute("DELETE FROM accounts_projection WHERE account_id=%s", (account_id,))
    finally:
        conn.autocommit = False

