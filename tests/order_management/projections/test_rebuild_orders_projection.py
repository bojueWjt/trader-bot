"""WP-A tests for scripts/rebuild_orders_projection.py.

Contract (docs/plans/2026-08-28-execution-state-arch-migration.md, WP-A item 5):
rebuild orders_projection from execution_events per account; default run is a
dry-run that emits a diff summary and writes nothing; --apply performs the
rebuild. Structure mirrors scripts/rebuild_positions_projection.py (JSON report
on stdout, --db-url / DATABASE_URL, --apply flag).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg2.extras import Json


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "rebuild_orders_projection.py"

ACCOUNT_A = "account-a"
ACCOUNT_B = "account-b"
INSTRUMENT_ID = "ATOMUSDT-PERP.BINANCE"

T1 = datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 8, 28, 9, 0, 5, tzinfo=timezone.utc)
T3 = datetime(2026, 8, 28, 9, 0, 10, tzinfo=timezone.utc)


def test_rebuild_script_exists() -> None:
    assert SCRIPT.exists(), (
        "scripts/rebuild_orders_projection.py is part of the WP-A contract"
    )


def test_dry_run_reports_differences_without_writing(
    db_conn,
    projection_db_url: str,
) -> None:
    filled_cid, open_cid = _seed_events(db_conn)
    stale_projection_id = _insert_stale_projection_row(db_conn, filled_cid)
    db_conn.commit()

    result = _run_script(projection_db_url)

    assert result.returncode == 0, result.stdout + result.stderr
    report = _parse_report(result.stdout)
    assert _difference_count(report) >= 1, (
        "dry-run must report the divergence between execution_events "
        f"and the stale projection; report={report}"
    )

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_projection_id::text, status, filled_quantity
            FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            """,
            (ACCOUNT_A, filled_cid),
        )
        stale_row = cur.fetchone()
        cur.execute(
            """
            SELECT COUNT(*) FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            """,
            (ACCOUNT_A, open_cid),
        )
        open_count = cur.fetchone()[0]

    assert stale_row == (stale_projection_id, "submitted", Decimal("0")), (
        "dry-run must not modify orders_projection"
    )
    assert open_count == 0, "dry-run must not create orders_projection rows"


def test_apply_rebuilds_orders_projection_from_events(
    db_conn,
    projection_db_url: str,
) -> None:
    filled_cid, open_cid = _seed_events(db_conn)
    _insert_stale_projection_row(db_conn, filled_cid)
    db_conn.commit()

    result = _run_script(projection_db_url, "--apply")

    assert result.returncode == 0, result.stdout + result.stderr

    db_conn.rollback()
    rows = _projection_rows(db_conn, ACCOUNT_A)
    assert filled_cid in rows
    assert rows[filled_cid]["status"] == "filled"
    assert rows[filled_cid]["filled_quantity"] == Decimal("5")
    assert rows[filled_cid]["instrument_id"] == INSTRUMENT_ID
    assert open_cid in rows
    assert rows[open_cid]["status"] == "accepted"

    other_account_rows = _projection_rows(db_conn, ACCOUNT_B)
    assert len(other_account_rows) == 1
    (other_row,) = other_account_rows.values()
    assert other_row["status"] == "accepted"
    assert other_row["instrument_id"] == INSTRUMENT_ID


def test_apply_is_idempotent(
    db_conn,
    projection_db_url: str,
) -> None:
    _seed_events(db_conn)
    db_conn.commit()

    first = _run_script(projection_db_url, "--apply")
    assert first.returncode == 0, first.stdout + first.stderr
    db_conn.rollback()
    rows_after_first = _projection_rows(db_conn, ACCOUNT_A)
    # Release the read transaction: the apply path takes an ACCESS EXCLUSIVE
    # lock on orders_projection and would block behind our ACCESS SHARE lock.
    db_conn.rollback()

    second = _run_script(projection_db_url, "--apply")
    assert second.returncode == 0, second.stdout + second.stderr
    db_conn.rollback()
    rows_after_second = _projection_rows(db_conn, ACCOUNT_A)

    assert rows_after_first == rows_after_second


# --- helpers ---------------------------------------------------------------


def _seed_events(conn) -> tuple[str, str]:
    """Insert execution events implying:
    - account-a, filled_cid: OrderAccepted then OrderFilled -> filled(5)
    - account-a, open_cid: OrderAccepted only -> accepted (still open)
    - account-b: one accepted order (per-account rebuild coverage)
    Returns (filled_cid, open_cid).
    """
    filled_cid = _client_order_id()
    open_cid = _client_order_id()
    other_cid = _client_order_id()
    _insert_execution_event(
        conn,
        event_type="OrderAccepted",
        account_id=ACCOUNT_A,
        client_order_id=filled_cid,
        ts_event=T1,
        payload=_order_payload(filled_cid),
    )
    _insert_execution_event(
        conn,
        event_type="OrderFilled",
        account_id=ACCOUNT_A,
        client_order_id=filled_cid,
        ts_event=T2,
        payload={
            **_order_payload(filled_cid),
            "filled_qty": "5",
            "leaves_qty": "0",
            "last_qty": "5",
            "avg_px": "4.6",
        },
    )
    _insert_execution_event(
        conn,
        event_type="OrderAccepted",
        account_id=ACCOUNT_A,
        client_order_id=open_cid,
        ts_event=T3,
        payload=_order_payload(open_cid),
    )
    _insert_execution_event(
        conn,
        event_type="OrderAccepted",
        account_id=ACCOUNT_B,
        client_order_id=other_cid,
        ts_event=T1,
        payload=_order_payload(other_cid),
    )
    return filled_cid, open_cid


def _order_payload(client_order_id: str) -> dict[str, Any]:
    return {
        "instrument_id": INSTRUMENT_ID,
        "client_order_id": client_order_id,
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "5",
        "price": "4.5",
    }


def _client_order_id() -> str:
    return f"B{uuid4().hex}01"


def _insert_execution_event(
    conn,
    *,
    event_type: str,
    account_id: str,
    client_order_id: str,
    ts_event: datetime,
    payload: dict[str, Any],
) -> str:
    event_id = str(uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO execution_events (
                execution_event_row_id, event_id, schema_version, node_id,
                account_id, intent_id, client_order_id, venue_order_id,
                trade_id, event_type, ts_event, payload
            )
            VALUES (%s,%s,'1.0',%s,%s,NULL,%s,NULL,NULL,%s,%s,%s)
            """,
            (
                str(uuid4()),
                event_id,
                f"nautilus-node-{account_id}",
                account_id,
                client_order_id,
                event_type,
                ts_event,
                Json(payload),
            ),
        )
    return event_id


def _insert_stale_projection_row(conn, client_order_id: str) -> str:
    """A wrong row left behind by the projection stall: the order filled on
    the exchange but the projection still says submitted/0."""
    order_projection_id = str(uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_projection (
                order_projection_id, account_id, instrument_id,
                client_order_id, status, side, order_type,
                quantity, filled_quantity, price, payload
            )
            VALUES (%s,%s,%s,%s,'submitted','long','LIMIT',5,0,4.5,%s)
            """,
            (
                order_projection_id,
                ACCOUNT_A,
                INSTRUMENT_ID,
                client_order_id,
                Json({"order_kind": "regular"}),
            ),
        )
    return order_projection_id


def _projection_rows(conn, account_id: str) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT client_order_id, status, instrument_id,
                   quantity, filled_quantity
            FROM orders_projection
            WHERE account_id=%s
            ORDER BY client_order_id
            """,
            (account_id,),
        )
        return {
            row[0]: {
                "status": row[1],
                "instrument_id": row[2],
                "quantity": row[3],
                "filled_quantity": row[4],
            }
            for row in cur.fetchall()
        }


def _run_script(database_url: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )


def _parse_report(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    assert text, "dry-run must emit a diff summary on stdout"
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        pytest.fail(f"rebuild report is not JSON: {text[:500]}")
    assert isinstance(report, dict)
    return report


def _difference_count(report: dict[str, Any]) -> int:
    count = report.get("difference_count")
    if isinstance(count, int):
        return count
    differences = report.get("differences")
    if isinstance(differences, list):
        return len(differences)
    counts = report.get("difference_counts")
    if isinstance(counts, dict):
        return sum(int(v) for v in counts.values())
    pytest.fail(
        f"rebuild report has no recognizable difference summary: {report}"
    )
