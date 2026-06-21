from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from psycopg2.extras import Json

from db.connection import transaction
import risk_state

from .db_helpers import record_reconciliation_finding
from .identifiers import canonical_instrument_key


class ProtectionRepairer(Protocol):
    def repair_stop(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProtectionRecoveryResult:
    status: str
    repaired_count: int
    unrepaired_count: int


class ProtectionRecovery:
    def __init__(self, *, repairer: ProtectionRepairer, failure_mode: str = "REDUCING") -> None:
        self._repairer = repairer
        self._failure_mode = failure_mode

    def recover(
        self,
        conn,
        *,
        account_id: str,
        now: datetime,
        venue_working_orders: list[dict[str, Any]] | None = None,
    ) -> ProtectionRecoveryResult:
        del now
        duplicate_count = self._handle_duplicate_venue_protection(
            conn,
            account_id=account_id,
            venue_working_orders=venue_working_orders or [],
        )
        if duplicate_count:
            return ProtectionRecoveryResult("unsafe", 0, duplicate_count)

        repaired = 0
        unrepaired = 0
        for position in _open_positions(conn, account_id):
            if _has_active_stop(conn, account_id, position["position_key"]):
                continue
            payload = {
                "account_id": account_id,
                "position_key": position["position_key"],
                "instrument_id": position["instrument_id"],
                "venue_symbol": position["venue_symbol"],
                "quantity": str(position["quantity"]),
            }
            try:
                repair = self._repairer.repair_stop(payload)
            except Exception as exc:
                unrepaired += 1
                _mark_unsafe(
                    conn,
                    account_id=account_id,
                    position=position,
                    mode=self._failure_mode,
                    finding_type="protection_repair_failed",
                    payload={**payload, "error": repr(exc)},
                )
                continue
            _record_repaired_stop(conn, account_id=account_id, position=position, repair=repair)
            repaired += 1

        return ProtectionRecoveryResult(
            "unsafe" if unrepaired else "recovered",
            repaired,
            unrepaired,
        )

    def _handle_duplicate_venue_protection(
        self,
        conn,
        *,
        account_id: str,
        venue_working_orders: list[dict[str, Any]],
    ) -> int:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for order in venue_working_orders:
            status = str(order.get("status") or "working").lower()
            if status not in {"working", "accepted", "open", "submitted"}:
                continue
            role = str(order.get("lifecycle_role") or order.get("role") or "")
            if role not in {"stop_loss", "take_profit"}:
                continue
            key = (str(order.get("position_key")), role)
            grouped.setdefault(key, []).append(order)

        duplicate_count = 0
        positions = {position["position_key"]: position for position in _open_positions(conn, account_id)}
        for (position_key, role), orders in grouped.items():
            if len(orders) <= 1:
                continue
            duplicate_count += 1
            position = positions.get(position_key)
            if position is None:
                position = {
                    "position_key": position_key,
                    "instrument_id": str(orders[0].get("instrument_id") or "UNKNOWN"),
                    "venue_symbol": str(orders[0].get("venue_symbol") or "UNKNOWN"),
                    "quantity": Decimal("0"),
                }
            _mark_unsafe(
                conn,
                account_id=account_id,
                position=position,
                mode=self._failure_mode,
                finding_type="duplicate_protection",
                payload={"lifecycle_role": role, "orders": orders},
            )
        return duplicate_count


def _open_positions(conn, account_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT position_id, instrument_id, quantity
            FROM positions_projection
            WHERE account_id=%s
              AND status IN ('open', 'reducing', 'external')
              AND quantity > 0
            ORDER BY position_id
            """,
            (account_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "position_key": row[0],
            "instrument_id": row[1],
            "venue_symbol": canonical_instrument_key(row[1]),
            "quantity": Decimal(str(row[2])),
        }
        for row in rows
    ]


def _has_active_stop(conn, account_id: str, position_key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM protective_orders_projection
            WHERE account_id=%s AND position_key=%s
              AND lifecycle_role='stop_loss' AND active
            """,
            (account_id, position_key),
        )
        return cur.fetchone() is not None


def _record_repaired_stop(
    conn,
    *,
    account_id: str,
    position: dict[str, Any],
    repair: dict[str, Any],
) -> None:
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO protective_orders_projection (
                protective_order_projection_id, account_id, position_key,
                venue_symbol, lifecycle_role, client_order_id, venue_order_id,
                status, active, quantity, updated_at, payload
            )
            VALUES (%s,%s,%s,%s,'stop_loss',%s,%s,%s,true,%s,now(),%s)
            ON CONFLICT (account_id, position_key, lifecycle_role) WHERE active
            DO NOTHING
            """,
            (
                str(uuid4()),
                account_id,
                position["position_key"],
                position["venue_symbol"],
                repair.get("client_order_id"),
                repair.get("venue_order_id"),
                repair.get("status", "working"),
                position["quantity"],
                Json({"source": "protection_recovery"}),
            ),
        )


def _mark_unsafe(
    conn,
    *,
    account_id: str,
    position: dict[str, Any],
    mode: str,
    finding_type: str,
    payload: dict[str, Any],
) -> None:
    instrument_id = canonical_instrument_key(position["instrument_id"])
    with transaction(conn), conn.cursor() as cur:
        risk_state.set_mode(cur, account_id, instrument_id, mode)
        record_reconciliation_finding(
            conn,
            account_id=account_id,
            finding_type=finding_type,
            severity="critical",
            position_key=position["position_key"],
            payload=payload,
            run_reason="protection_recovery",
        )

