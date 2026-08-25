from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from order_management.protection_watchdog import ProtectionWatchdog
from psycopg2.extras import Json

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_missing_protection_creates_reconciliation_finding(db_conn) -> None:
    _seed_position(db_conn)
    _seed_robot_fill(db_conn)
    dispatched: list[tuple[str, dict]] = []
    watchdog = ProtectionWatchdog(dispatcher=lambda policy, finding: dispatched.append((policy, finding)))

    result = watchdog.check(
        db_conn,
        account_id="acct-om2",
        venue_working_orders=[],
        policy="repair",
        now=NOW,
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT finding_type, severity, position_key FROM reconciliation_findings"
        )
        finding_type, severity, position_key = cur.fetchone()

    assert result.missing_count == 1
    assert finding_type == "missing_protection"
    assert severity == "error"
    assert position_key == "acct-om2:BTCUSDT"
    assert dispatched == [("repair", result.findings[0])]


def test_missing_protection_policy_dispatches_reducing_and_halt(db_conn) -> None:
    _seed_position(db_conn)
    _seed_robot_fill(db_conn)
    dispatched: list[tuple[str, dict]] = []
    watchdog = ProtectionWatchdog(dispatcher=lambda policy, finding: dispatched.append((policy, finding)))

    watchdog.check(db_conn, account_id="acct-om2", venue_working_orders=[], policy="REDUCING", now=NOW)
    watchdog.check(db_conn, account_id="acct-om2", venue_working_orders=[], policy="HALT", now=NOW)

    assert [policy for policy, _ in dispatched] == ["REDUCING", "HALT"]


def test_rebaseline_removes_manual_position_from_watchdog(db_conn) -> None:
    _seed_position(db_conn)
    _seed_robot_fill(db_conn)
    _seed_rebaseline(db_conn)
    dispatched: list[tuple[str, dict]] = []
    watchdog = ProtectionWatchdog(
        dispatcher=lambda policy, finding: dispatched.append(
            (policy, finding)
        )
    )

    result = watchdog.check(
        db_conn,
        account_id="acct-om2",
        venue_working_orders=[],
        policy="repair",
        now=NOW,
    )

    assert result.missing_count == 0
    assert dispatched == []


def _seed_position(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity, status, updated_at, payload
            )
            VALUES ('acct-om2', 'acct-om2:BTCUSDT', 'BTCUSDT', 'long', %s, 'open', %s, '{}')
            """,
            (Decimal(1), NOW),
        )
    conn.commit()


def _seed_robot_fill(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO execution_events (
                execution_event_row_id,
                event_id,
                node_id,
                account_id,
                client_order_id,
                event_type,
                ts_event,
                payload
            )
            VALUES (%s, %s, 'node-om2', 'acct-om2', %s,
                    'OrderFilled', %s, %s)
            """,
            (
                str(uuid4()),
                str(uuid4()),
                "B" + ("a" * 32) + "01",
                NOW,
                Json(
                    {
                        "instrument_id": "BTCUSDT",
                        "order_side": 1,
                        "last_qty": "1",
                    }
                ),
            ),
        )
    conn.commit()


def _seed_rebaseline(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO execution_events (
                execution_event_row_id,
                event_id,
                node_id,
                account_id,
                event_type,
                ts_event,
                payload
            )
            VALUES (%s, %s, 'operator-ownership-ledger', 'acct-om2',
                    'OwnershipRebaseline', %s, %s)
            """,
            (
                str(uuid4()),
                f"ownership-rebaseline:{uuid4()}",
                NOW.replace(microsecond=1),
                Json(
                    {
                        "ownership_schema_version": (
                            "ownership-rebaseline/v1"
                        ),
                        "symbol": "BTCUSDT",
                        "baseline_quantity": "0",
                        "exchange_quantity_at_baseline": "1",
                        "manual_quantity_at_baseline": "1",
                        "reason": "test manual domain",
                        "adjudicated_by": "test-user",
                        "request_id": str(uuid4()),
                    }
                ),
            ),
        )
    conn.commit()
