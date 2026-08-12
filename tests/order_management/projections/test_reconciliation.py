from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from order_management import reconciliation as reconciliation_module
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


def test_reconciliation_reports_every_nonzero_local_position_missing_at_venue(
    monkeypatch,
) -> None:
    rows = [
        (f"acct-om2:GHOST-{index}", Decimal(1), "open")
        for index in range(8)
    ]
    findings = _position_findings(
        monkeypatch,
        local_rows=rows,
        venue_positions=[],
    )

    assert len(findings) == 8
    assert {finding["finding_type"] for finding in findings} == {
        "position_drift"
    }
    assert {
        finding["payload"]["drift_kind"] for finding in findings
    } == {"venue_missing"}
    assert {
        finding["payload"]["missing_side"] for finding in findings
    } == {"venue"}


def test_reconciliation_reports_nonzero_venue_position_missing_locally(
    monkeypatch,
) -> None:
    findings = _position_findings(
        monkeypatch,
        local_rows=[],
        venue_positions=[
            {
                "position_key": "acct-om2:ETHUSDT",
                "quantity": "2",
                "status": "open",
            }
        ],
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding["finding_type"] == "position_drift"
    assert finding["position_key"] == "acct-om2:ETHUSDT"
    assert finding["payload"] == {
        "drift_kind": "local_missing",
        "missing_side": "local",
        "local_quantity": "0",
        "venue_quantity": "2",
        "local_status": "missing",
        "venue_status": "open",
    }


def test_reconciliation_treats_missing_closed_or_zero_positions_as_flat(
    monkeypatch,
) -> None:
    findings = _position_findings(
        monkeypatch,
        local_rows=[
            ("acct-om2:LOCAL-CLOSED", Decimal(0), "closed"),
            ("acct-om2:LOCAL-ZERO", Decimal(0), "open"),
        ],
        venue_positions=[
            {
                "position_key": "acct-om2:VENUE-CLOSED",
                "quantity": "0",
                "status": "closed",
            },
            {
                "position_key": "acct-om2:VENUE-ZERO",
                "quantity": "0",
                "status": "open",
            },
        ],
    )

    assert findings == []


def test_reconciliation_reports_missing_nonzero_positions_even_if_status_is_closed(
    monkeypatch,
) -> None:
    findings = _position_findings(
        monkeypatch,
        local_rows=[
            ("acct-om2:LOCAL-CLOSED-NONZERO", Decimal(1), "closed"),
        ],
        venue_positions=[
            {
                "position_key": "acct-om2:VENUE-CLOSED-NONZERO",
                "quantity": "2",
                "status": "closed",
            },
        ],
    )

    assert len(findings) == 2
    assert {
        finding["payload"]["drift_kind"] for finding in findings
    } == {"venue_missing", "local_missing"}


def _position_findings(
    monkeypatch,
    *,
    local_rows: list[tuple[str, Decimal, str]],
    venue_positions: list[dict[str, str]],
) -> list[dict]:
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, query, params):
            del query, params

        def fetchall(self):
            return local_rows

    class Connection:
        def cursor(self):
            return Cursor()

    def record_finding(conn, **kwargs):
        del conn
        return kwargs

    monkeypatch.setattr(
        reconciliation_module,
        "record_reconciliation_finding",
        record_finding,
    )
    return reconciliation_module._position_findings(
        Connection(),
        "acct-om2",
        {"positions": venue_positions},
        "run-1",
    )


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
