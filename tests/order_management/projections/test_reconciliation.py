from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from order_management.reconciliation import Reconciler


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_startup_reconciliation_gate_requires_clean_run_before_active(db_conn) -> None:
    reconciler = Reconciler(now=lambda: NOW)
    _seed_local_state(db_conn)

    clean = reconciler.run_startup(
        db_conn,
        account_id="acct-om2",
        venue_snapshot={
            "orders": [{"client_order_id": "coid-1", "status": "accepted"}],
            "positions": [{"position_key": "acct-om2:BTCUSDT", "quantity": "1", "status": "open"}],
            "account": {"equity": "1000", "free": "900", "margin": "100"},
        },
    )
    drift = reconciler.run_startup(
        db_conn,
        account_id="acct-om2",
        venue_snapshot={
            "orders": [{"client_order_id": "coid-1", "status": "accepted"}],
            "positions": [{"position_key": "acct-om2:BTCUSDT", "quantity": "2", "status": "open"}],
            "account": {"equity": "1000", "free": "900", "margin": "100"},
        },
    )

    assert clean.active_allowed is True
    assert drift.active_allowed is False
    assert drift.status == "failed"


def test_periodic_reconciliation_persists_run_and_findings(db_conn) -> None:
    reconciler = Reconciler(now=lambda: NOW)
    _seed_local_state(db_conn)

    result = reconciler.run_periodic(
        db_conn,
        account_id="acct-om2",
        venue_snapshot={
            "orders": [{"client_order_id": "coid-1", "status": "filled"}],
            "positions": [{"position_key": "acct-om2:BTCUSDT", "quantity": "1", "status": "open"}],
            "account": {"equity": "999", "free": "900", "margin": "100"},
        },
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, reason FROM reconciliation_runs")
        run_status, reason = cur.fetchone()
        cur.execute("SELECT finding_type FROM reconciliation_findings ORDER BY finding_type")
        finding_types = [row[0] for row in cur.fetchall()]

    assert result.status == "failed"
    assert run_status == "failed"
    assert reason == "periodic"
    assert finding_types == ["account_drift", "order_drift"]


def _seed_local_state(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_projection (
                order_projection_id, account_id, instrument_id, venue_symbol,
                client_order_id, status, side, quantity, filled_quantity, payload
            )
            VALUES (
                '00000000-0000-0000-0000-000000000101',
                'acct-om2', 'BTCUSDT-PERP.BINANCE', 'BTCUSDT',
                'coid-1', 'accepted', 'long', 1, 0, '{}'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity, status, payload
            )
            VALUES ('acct-om2', 'acct-om2:BTCUSDT', 'BTCUSDT', 'long', %s, 'open', '{}')
            """,
            (Decimal("1"),),
        )
        cur.execute(
            """
            INSERT INTO accounts_projection (
                account_id, currency, equity, margin, available_balance, payload
            )
            VALUES ('acct-om2', 'USDT', 1000, 100, 900, '{}')
            """
        )
    conn.commit()
