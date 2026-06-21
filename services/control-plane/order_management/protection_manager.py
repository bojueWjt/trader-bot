from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Protocol
from uuid import uuid4

from psycopg2.extras import Json

from db.connection import transaction
import risk_state

from .db_helpers import record_reconciliation_finding
from .identifiers import canonical_instrument_key
from strategy.intent_execution_planner import InstrumentSpec
from strategy.protection import PositionProtectionSnapshot, StopProtectionSpec, build_stop_order_plan


class ProtectionSubmitter(Protocol):
    def submit_stop(self, plan) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProtectionInstallRequest:
    account_id: str
    position_key: str
    intent_id: str
    stop: StopProtectionSpec
    instrument: InstrumentSpec
    entry_filled_at: datetime
    now: datetime
    threshold_seconds: int


@dataclass(frozen=True)
class ProtectionInstallResult:
    status: str
    position_key: str
    client_order_id: str | None = None
    risk_mode: str | None = None
    reason: str | None = None


class ProtectionManager:
    def __init__(
        self,
        *,
        submitter: ProtectionSubmitter,
        alert_sink: Callable[[dict[str, Any]], Any] | None = None,
        failure_mode: str = "REDUCING",
    ) -> None:
        self._submitter = submitter
        self._alert_sink = alert_sink
        self._failure_mode = failure_mode

    def install_after_entry_fill(
        self,
        conn,
        request: ProtectionInstallRequest,
    ) -> ProtectionInstallResult:
        if (_aware(request.now) - _aware(request.entry_filled_at)).total_seconds() > request.threshold_seconds:
            return self._fail(conn, request, reason="protection_install_timeout")

        position = _load_position(conn, request.account_id, request.position_key)
        if position is None:
            return self._fail(conn, request, reason="position_missing")
        if position["quantity"] <= 0:
            return self._fail(conn, request, reason="position_flat")

        plan = build_stop_order_plan(
            intent_id=request.intent_id,
            account_id=request.account_id,
            instrument_id=position["instrument_id"],
            position=PositionProtectionSnapshot(
                position_key=request.position_key,
                side=position["side"],
                quantity=position["quantity"],
                entry_price=position["avg_entry_price"],
            ),
            stop=request.stop,
            instrument=request.instrument,
        )
        try:
            submitted = self._submitter.submit_stop(plan)
        except Exception as exc:
            return self._fail(conn, request, reason="protection_install_failed", error=repr(exc))

        client_order_id = str(submitted.get("client_order_id") or plan.client_order_id)
        with transaction(conn), conn.cursor() as cur:
            cur.execute(
                """
                UPDATE protective_orders_projection
                SET active=false, status='replaced', updated_at=now()
                WHERE account_id=%s AND position_key=%s AND lifecycle_role='stop_loss' AND active
                """,
                (request.account_id, request.position_key),
            )
            cur.execute(
                """
                INSERT INTO protective_orders_projection (
                    protective_order_projection_id, account_id, position_key,
                    venue_symbol, lifecycle_role, client_order_id, venue_order_id,
                    status, active, quantity, price, trigger_price, updated_at, payload
                )
                VALUES (%s,%s,%s,%s,'stop_loss',%s,%s,%s,true,%s,%s,%s,now(),%s)
                """,
                (
                    str(uuid4()),
                    request.account_id,
                    request.position_key,
                    canonical_instrument_key(position["instrument_id"]),
                    client_order_id,
                    submitted.get("venue_order_id"),
                    submitted.get("status", "submitted"),
                    Decimal(plan.quantity),
                    Decimal(plan.price) if plan.price is not None else None,
                    Decimal(plan.trigger_price) if plan.trigger_price is not None else None,
                    Json({"intent_id": request.intent_id, "source": "protection_manager"}),
                ),
            )
        return ProtectionInstallResult(
            status="submitted",
            position_key=request.position_key,
            client_order_id=client_order_id,
        )

    def _fail(
        self,
        conn,
        request: ProtectionInstallRequest,
        *,
        reason: str,
        error: str | None = None,
    ) -> ProtectionInstallResult:
        mode = self._failure_mode
        position = _load_position(conn, request.account_id, request.position_key)
        instrument_id = canonical_instrument_key(
            (position or {}).get("instrument_id") or request.instrument.instrument_id
        )
        payload = {
            "type": reason,
            "position_key": request.position_key,
            "intent_id": request.intent_id,
        }
        if error is not None:
            payload["error"] = error
        with transaction(conn), conn.cursor() as cur:
            risk_state.set_mode(cur, request.account_id, instrument_id, mode)
            record_reconciliation_finding(
                conn,
                account_id=request.account_id,
                finding_type=reason,
                severity="critical",
                position_key=request.position_key,
                payload=payload,
                run_reason="protection_manager",
            )
        if self._alert_sink is not None:
            self._alert_sink(payload)
        return ProtectionInstallResult(
            status="failed",
            position_key=request.position_key,
            risk_mode=mode,
            reason=reason,
        )


def _load_position(conn, account_id: str, position_key: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT instrument_id, side, quantity, avg_entry_price
            FROM positions_projection
            WHERE account_id=%s AND position_id=%s
            """,
            (account_id, position_key),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "instrument_id": row[0],
        "side": row[1],
        "quantity": Decimal(str(row[2])),
        "avg_entry_price": Decimal(str(row[3])) if row[3] is not None else None,
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

