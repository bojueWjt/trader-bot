from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from db.connection import transaction

from .db_helpers import record_reconciliation_finding


WatchdogDispatcher = Callable[[str, dict[str, Any]], Any]


@dataclass(frozen=True)
class ProtectionWatchdogResult:
    missing_count: int
    findings: list[dict[str, Any]]


class ProtectionWatchdog:
    def __init__(self, dispatcher: WatchdogDispatcher | None = None) -> None:
        self._dispatcher = dispatcher

    def check(
        self,
        conn,
        *,
        account_id: str,
        venue_working_orders: list[dict[str, Any]],
        policy: str,
        now: datetime,
    ) -> ProtectionWatchdogResult:
        del now
        findings: list[dict[str, Any]] = []
        venue_keys = {
            (
                str(order.get("position_key")),
                str(order.get("lifecycle_role") or order.get("role") or ""),
            )
            for order in venue_working_orders
            if str(order.get("status") or "working").lower() in {"working", "accepted", "open"}
        }
        with transaction(conn):
            for position in _open_positions(conn, account_id):
                expected = _expected_protection(conn, account_id, position["position_key"])
                missing_roles = _missing_roles(expected, venue_keys, position["position_key"])
                for role in missing_roles:
                    finding = record_reconciliation_finding(
                        conn,
                        account_id=account_id,
                        finding_type="missing_protection",
                        severity="error",
                        position_key=position["position_key"],
                        payload={
                            "position_key": position["position_key"],
                            "venue_symbol": position["venue_symbol"],
                            "lifecycle_role": role,
                            "quantity": str(position["quantity"]),
                        },
                        run_reason="protection_watchdog",
                    )
                    findings.append(finding)
                    if self._dispatcher is not None:
                        self._dispatcher(policy, finding)
        return ProtectionWatchdogResult(
            missing_count=len(findings),
            findings=findings,
        )


def _open_positions(conn, account_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT position_id, instrument_id, quantity
            FROM positions_projection
            WHERE account_id=%s
              AND status IN ('open', 'reducing', 'external')
              AND quantity > 0
            """,
            (account_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "position_key": row[0],
            "venue_symbol": str(row[1]).split(".", 1)[0].split("-", 1)[0].upper(),
            "quantity": row[2],
        }
        for row in rows
    ]


def _expected_protection(conn, account_id: str, position_key: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lifecycle_role, status
            FROM protective_orders_projection
            WHERE account_id=%s AND position_key=%s AND active
            """,
            (account_id, position_key),
        )
        rows = cur.fetchall()
    return [{"lifecycle_role": row[0], "status": row[1]} for row in rows]


def _missing_roles(
    expected: list[dict[str, Any]],
    venue_keys: set[tuple[str, str]],
    position_key: str,
) -> list[str]:
    if not expected:
        return ["stop_loss"]
    missing = []
    for protection in expected:
        role = str(protection["lifecycle_role"])
        if (position_key, role) not in venue_keys:
            missing.append(role)
    return missing
