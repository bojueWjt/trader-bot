from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from db.connection import transaction

from .db_helpers import (
    create_reconciliation_run,
    decimal_or_none,
    record_reconciliation_finding,
    update_reconciliation_run,
)


@dataclass(frozen=True)
class ReconciliationResult:
    reconciliation_run_id: str
    status: str
    active_allowed: bool
    findings: list[dict[str, Any]]


class Reconciler:
    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run_startup(
        self,
        conn,
        *,
        account_id: str,
        venue_snapshot: dict[str, Any],
    ) -> ReconciliationResult:
        return self._run(conn, account_id=account_id, venue_snapshot=venue_snapshot, reason="startup")

    def run_periodic(
        self,
        conn,
        *,
        account_id: str,
        venue_snapshot: dict[str, Any],
    ) -> ReconciliationResult:
        return self._run(conn, account_id=account_id, venue_snapshot=venue_snapshot, reason="periodic")

    def _run(
        self,
        conn,
        *,
        account_id: str,
        venue_snapshot: dict[str, Any],
        reason: str,
    ) -> ReconciliationResult:
        del self
        with transaction(conn):
            run_id = create_reconciliation_run(
                conn,
                account_id=account_id,
                status="running",
                reason=reason,
                completed=False,
                payload={"venue_snapshot": venue_snapshot},
            )
            findings = _collect_findings(
                conn,
                account_id=account_id,
                venue_snapshot=venue_snapshot,
                run_id=run_id,
            )
            status = "passed" if not findings else "failed"
            update_reconciliation_run(
                conn,
                run_id,
                status=status,
                payload={"finding_count": len(findings)},
            )
            return ReconciliationResult(
                reconciliation_run_id=run_id,
                status=status,
                active_allowed=(reason != "startup" or not findings),
                findings=findings,
            )


def _collect_findings(
    conn,
    *,
    account_id: str,
    venue_snapshot: dict[str, Any],
    run_id: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    findings.extend(_order_findings(conn, account_id, venue_snapshot, run_id))
    findings.extend(_position_findings(conn, account_id, venue_snapshot, run_id))
    findings.extend(_account_findings(conn, account_id, venue_snapshot, run_id))
    return findings


def _order_findings(conn, account_id: str, venue_snapshot: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    venue_orders = {
        str(order.get("client_order_id")): str(order.get("status"))
        for order in venue_snapshot.get("orders", [])
        if order.get("client_order_id")
    }
    findings: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_projection_id::text, client_order_id, status
            FROM orders_projection
            WHERE account_id=%s
            """,
            (account_id,),
        )
        rows = cur.fetchall()
    for order_projection_id, client_order_id, local_status in rows:
        venue_status = venue_orders.get(client_order_id)
        if venue_status is not None and venue_status != local_status:
            findings.append(
                record_reconciliation_finding(
                    conn,
                    reconciliation_run_id=run_id,
                    account_id=account_id,
                    finding_type="order_drift",
                    severity="error",
                    order_projection_id=order_projection_id,
                    payload={
                        "client_order_id": client_order_id,
                        "local_status": local_status,
                        "venue_status": venue_status,
                    },
                )
            )
    return findings


def _position_findings(conn, account_id: str, venue_snapshot: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    venue_positions = {
        str(position.get("position_key")): position
        for position in venue_snapshot.get("positions", [])
        if position.get("position_key")
    }
    findings: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT position_id, quantity, status
            FROM positions_projection
            WHERE account_id=%s
            """,
            (account_id,),
        )
        rows = cur.fetchall()
    local_positions = {
        str(position_key): (
            decimal_or_none(local_quantity) or Decimal(0),
            str(local_status),
        )
        for position_key, local_quantity, local_status in rows
    }
    for position_key, (local_quantity, local_status) in local_positions.items():
        venue_position = venue_positions.get(position_key)
        if venue_position is None:
            if local_quantity == 0:
                continue
            findings.append(
                _record_position_drift(
                    conn,
                    account_id=account_id,
                    run_id=run_id,
                    position_key=position_key,
                    local_quantity=local_quantity,
                    venue_quantity=Decimal(0),
                    local_status=local_status,
                    venue_status="missing",
                    drift_kind="venue_missing",
                    missing_side="venue",
                )
            )
            continue
        venue_quantity = decimal_or_none(venue_position.get("quantity")) or Decimal(0)
        venue_status = str(venue_position.get("status"))
        if venue_quantity != local_quantity or venue_status != local_status:
            findings.append(
                _record_position_drift(
                    conn,
                    account_id=account_id,
                    run_id=run_id,
                    position_key=position_key,
                    local_quantity=local_quantity,
                    venue_quantity=venue_quantity,
                    local_status=local_status,
                    venue_status=venue_status,
                )
            )
    for position_key, venue_position in venue_positions.items():
        if position_key in local_positions:
            continue
        venue_quantity = decimal_or_none(venue_position.get("quantity")) or Decimal(0)
        if venue_quantity == 0:
            continue
        findings.append(
            _record_position_drift(
                conn,
                account_id=account_id,
                run_id=run_id,
                position_key=position_key,
                local_quantity=Decimal(0),
                venue_quantity=venue_quantity,
                local_status="missing",
                venue_status=str(venue_position.get("status")),
                drift_kind="local_missing",
                missing_side="local",
            )
        )
    return findings


def _record_position_drift(
    conn,
    *,
    account_id: str,
    run_id: str,
    position_key: str,
    local_quantity: Decimal,
    venue_quantity: Decimal,
    local_status: str,
    venue_status: str,
    drift_kind: str | None = None,
    missing_side: str | None = None,
) -> dict[str, Any]:
    payload = {
        "local_quantity": str(local_quantity),
        "venue_quantity": str(venue_quantity),
        "local_status": local_status,
        "venue_status": venue_status,
    }
    if drift_kind:
        payload["drift_kind"] = drift_kind
    if missing_side:
        payload["missing_side"] = missing_side
    return record_reconciliation_finding(
        conn,
        reconciliation_run_id=run_id,
        account_id=account_id,
        finding_type="position_drift",
        severity="error",
        position_key=position_key,
        payload=payload,
    )


def _account_findings(conn, account_id: str, venue_snapshot: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    account = venue_snapshot.get("account") or {}
    if not account:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT equity, available_balance, margin
            FROM accounts_projection
            WHERE account_id=%s
            """,
            (account_id,),
        )
        row = cur.fetchone()
    if row is None:
        return [
            record_reconciliation_finding(
                conn,
                reconciliation_run_id=run_id,
                account_id=account_id,
                finding_type="account_missing",
                severity="error",
                payload={"venue_account": account},
            )
        ]
    local_equity, local_free, local_margin = row
    venue_equity = decimal_or_none(account.get("equity")) or Decimal("0")
    venue_free = decimal_or_none(account.get("free")) or Decimal("0")
    venue_margin = decimal_or_none(account.get("margin")) or Decimal("0")
    if (local_equity, local_free, local_margin) == (venue_equity, venue_free, venue_margin):
        return []
    return [
        record_reconciliation_finding(
            conn,
            reconciliation_run_id=run_id,
            account_id=account_id,
            finding_type="account_drift",
            severity="error",
            payload={
                "local_equity": str(local_equity),
                "venue_equity": str(venue_equity),
                "local_free": str(local_free),
                "venue_free": str(venue_free),
                "local_margin": str(local_margin),
                "venue_margin": str(venue_margin),
            },
        )
    ]
