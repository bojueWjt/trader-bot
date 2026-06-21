from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from order_management.protection_watchdog import ProtectionWatchdog


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_missing_protection_creates_reconciliation_finding(db_conn) -> None:
    _seed_position(db_conn)
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
    dispatched: list[tuple[str, dict]] = []
    watchdog = ProtectionWatchdog(dispatcher=lambda policy, finding: dispatched.append((policy, finding)))

    watchdog.check(db_conn, account_id="acct-om2", venue_working_orders=[], policy="REDUCING", now=NOW)
    watchdog.check(db_conn, account_id="acct-om2", venue_working_orders=[], policy="HALT", now=NOW)

    assert [policy for policy, _ in dispatched] == ["REDUCING", "HALT"]


def _seed_position(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity, status, updated_at, payload
            )
            VALUES ('acct-om2', 'acct-om2:BTCUSDT', 'BTCUSDT', 'long', %s, 'open', %s, '{}')
            """,
            (Decimal("1"), NOW),
        )
    conn.commit()
