from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from psycopg2.extras import Json

from order_management.protection_recovery import ProtectionRecovery


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_recovery_repairs_missing_stop_idempotently(db_conn) -> None:
    _seed_position(db_conn, account_id="acct-om5-recover")
    repairer = _Repairer()
    recovery = ProtectionRecovery(repairer=repairer)

    first = recovery.recover(db_conn, account_id="acct-om5-recover", now=NOW)
    second = recovery.recover(db_conn, account_id="acct-om5-recover", now=NOW)

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT lifecycle_role, client_order_id, active
            FROM protective_orders_projection
            WHERE account_id='acct-om5-recover'
            """
        )
        rows = cur.fetchall()

    assert first.repaired_count == 1
    assert second.repaired_count == 0
    assert repairer.calls == ["acct-om5-recover:BTCUSDT"]
    assert rows == [("stop_loss", "repaired-stop-1", True)]


def test_recovery_halts_when_duplicate_venue_protection_cannot_be_safely_repaired(db_conn) -> None:
    _seed_position(db_conn, account_id="acct-om5-dup")
    recovery = ProtectionRecovery(repairer=_Repairer(), failure_mode="HALTED")

    result = recovery.recover(
        db_conn,
        account_id="acct-om5-dup",
        now=NOW,
        venue_working_orders=[
            {"position_key": "acct-om5-dup:BTCUSDT", "lifecycle_role": "stop_loss", "client_order_id": "stop-a"},
            {"position_key": "acct-om5-dup:BTCUSDT", "lifecycle_role": "stop_loss", "client_order_id": "stop-b"},
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT state->>'mode' FROM risk_state WHERE account_id='acct-om5-dup' AND instrument_id='BTCUSDT'"
        )
        mode = cur.fetchone()[0]
        cur.execute("SELECT finding_type FROM reconciliation_findings WHERE account_id='acct-om5-dup'")
        finding_type = cur.fetchone()[0]

    assert result.status == "unsafe"
    assert result.unrepaired_count == 1
    assert mode == "HALTED"
    assert finding_type == "duplicate_protection"


def test_recovery_stays_reducing_when_repair_fails(db_conn) -> None:
    _seed_position(db_conn, account_id="acct-om5-repair-fail")
    recovery = ProtectionRecovery(repairer=_FailingRepairer(), failure_mode="REDUCING")

    result = recovery.recover(db_conn, account_id="acct-om5-repair-fail", now=NOW)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT state->>'mode' FROM risk_state WHERE account_id='acct-om5-repair-fail' AND instrument_id='BTCUSDT'"
        )
        mode = cur.fetchone()[0]

    assert result.status == "unsafe"
    assert result.unrepaired_count == 1
    assert mode == "REDUCING"


def _seed_position(conn, *, account_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity,
                avg_entry_price, status, updated_at, payload
            )
            VALUES (%s, %s, 'BTCUSDT-PERP.BINANCE', 'long', 1, 100, 'open', %s, %s)
            """,
            (account_id, f"{account_id}:BTCUSDT", NOW, Json({})),
        )
    conn.commit()


class _Repairer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def repair_stop(self, payload: dict) -> dict:
        self.calls.append(payload["position_key"])
        return {"client_order_id": "repaired-stop-1", "venue_order_id": "venue-repaired-1", "status": "working"}


class _FailingRepairer:
    def repair_stop(self, payload: dict) -> dict:
        raise RuntimeError("cannot repair safely")

