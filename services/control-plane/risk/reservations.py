from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json, RealDictCursor

_CONTROL_PLANE = Path(__file__).resolve().parents[1]
_DB = _CONTROL_PLANE / "db"
for _path in (_CONTROL_PLANE, _DB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from connection import transaction  # noqa: E402
from order_management.identifiers import canonical_account_id, canonical_instrument_key  # noqa: E402

from account_budget import load_account_budget  # noqa: E402
from risk_config import RiskConfig, decimal_value  # noqa: E402


ACTIVE_STATUSES = ("held", "reserved")


@dataclass(frozen=True)
class ReservationRequest:
    account_id: str
    instrument_id: str
    intent_id: str
    idempotency_key: str
    risk_amount: Decimal
    notional: Decimal
    ttl_seconds: int
    now: datetime
    margin_amount: Decimal = Decimal("0")
    position_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", canonical_account_id(self.account_id))
        object.__setattr__(self, "instrument_id", canonical_instrument_key(self.instrument_id))
        object.__setattr__(self, "risk_amount", decimal_value(self.risk_amount))
        object.__setattr__(self, "notional", decimal_value(self.notional))
        object.__setattr__(self, "margin_amount", decimal_value(self.margin_amount))
        object.__setattr__(self, "ttl_seconds", int(self.ttl_seconds))
        object.__setattr__(self, "now", _aware(self.now))


@dataclass(frozen=True)
class ReservationResult:
    status: str
    reason: str
    reservation_id: str | None
    account_id: str
    instrument_id: str | None
    risk_amount: Decimal
    notional: Decimal
    expires_at: datetime | None = None
    remaining_budget: Decimal | None = None


def reserve_risk(
    conn,
    request: ReservationRequest,
    *,
    config: RiskConfig | None = None,
) -> ReservationResult:
    config = config or RiskConfig()
    with transaction(conn):
        _lock_account(conn, request.account_id)
        _expire_account_reservations(conn, request.account_id, request.now)
        existing = _existing_by_idempotency(conn, request.account_id, request.idempotency_key)
        if existing is not None:
            return _row_result(existing, reason="idempotent reservation")

        _lock_account_projection(conn, request.account_id)
        budget = load_account_budget(conn, request.account_id, now=request.now)
        if not budget.can_take_new_risk:
            return ReservationResult(
                "needs_review",
                "account budget stale or unavailable",
                None,
                request.account_id,
                request.instrument_id,
                request.risk_amount,
                request.notional,
                remaining_budget=Decimal("0"),
            )
        active_risk = _active_risk_amount(conn, request.account_id, request.now)
        max_budget = budget.equity * config.max_total_open_risk_pct
        remaining = max(max_budget - active_risk, Decimal("0"))
        if request.risk_amount > remaining:
            return ReservationResult(
                "rejected",
                "risk reservation budget exceeded",
                None,
                request.account_id,
                request.instrument_id,
                request.risk_amount,
                request.notional,
                remaining_budget=remaining,
            )

        reservation_id = str(uuid4())
        expires_at = request.now + timedelta(seconds=request.ttl_seconds)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO risk_reservations (
                    risk_reservation_id, account_id, position_key, venue_symbol,
                    idempotency_key, status, notional, risk_amount, margin_amount,
                    expires_at, payload
                )
                VALUES (%s,%s,%s,%s,%s,'held',%s,%s,%s,%s,%s)
                RETURNING risk_reservation_id::text, account_id, venue_symbol,
                          status, notional, risk_amount, expires_at
                """,
                (
                    reservation_id,
                    request.account_id,
                    request.position_key,
                    request.instrument_id,
                    request.idempotency_key,
                    request.notional,
                    request.risk_amount,
                    request.margin_amount,
                    expires_at,
                    Json({"intent_id": request.intent_id, "instrument_id": request.instrument_id}),
                ),
            )
            row = cur.fetchone()
        return _row_result(row, reason="risk reserved", remaining_budget=remaining - request.risk_amount)


def release_reservation(conn, reservation_id: str, *, now: datetime | None = None) -> ReservationResult:
    return _set_status(conn, reservation_id, "released", now=now)


def consume_reservation(conn, reservation_id: str, *, now: datetime | None = None) -> ReservationResult:
    return _set_status(conn, reservation_id, "consumed", now=now)


def expire_reservations(conn, *, now: datetime | None = None) -> int:
    now = _aware(now or datetime.now(timezone.utc))
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE risk_reservations
            SET status='expired', updated_at=now()
            WHERE status = ANY(%s) AND expires_at IS NOT NULL AND expires_at <= %s
            """,
            (list(ACTIVE_STATUSES), now),
        )
        return cur.rowcount


def load_active_reservations(
    conn,
    *,
    account_id: str | None = None,
    now: datetime | None = None,
) -> list[ReservationResult]:
    now = _aware(now or datetime.now(timezone.utc))
    expire_reservations(conn, now=now)
    params: list[Any] = [list(ACTIVE_STATUSES)]
    where = "status = ANY(%s)"
    if account_id is not None:
        where += " AND account_id=%s"
        params.append(canonical_account_id(account_id))
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT risk_reservation_id::text, account_id, venue_symbol,
                   status, notional, risk_amount, expires_at
            FROM risk_reservations
            WHERE {where}
            ORDER BY created_at, risk_reservation_id
            """,
            params,
        )
        return [_row_result(row, reason="active reservation") for row in cur.fetchall()]


def _set_status(
    conn,
    reservation_id: str,
    status: str,
    *,
    now: datetime | None,
) -> ReservationResult:
    now = _aware(now or datetime.now(timezone.utc))
    released_sql = ", released_at=%s" if status == "released" else ""
    params: list[Any] = [status]
    if status == "released":
        params.append(now)
    params.append(reservation_id)
    with transaction(conn), conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            UPDATE risk_reservations
            SET status=%s, updated_at=now(){released_sql}
            WHERE risk_reservation_id=%s
            RETURNING risk_reservation_id::text, account_id, venue_symbol,
                      status, notional, risk_amount, expires_at
            """,
            params,
        )
        row = cur.fetchone()
    if row is None:
        return ReservationResult(status, "reservation not found", None, "", None, Decimal("0"), Decimal("0"))
    return _row_result(row, reason=f"reservation {status}")


def _lock_account(conn, account_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s), 0)", (account_id,))


def _lock_account_projection(conn, account_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts_projection WHERE account_id=%s FOR UPDATE", (account_id,))


def _existing_by_idempotency(conn, account_id: str, idempotency_key: str) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT risk_reservation_id::text, account_id, venue_symbol,
                   status, notional, risk_amount, expires_at
            FROM risk_reservations
            WHERE account_id=%s AND idempotency_key=%s
            FOR UPDATE
            """,
            (account_id, idempotency_key),
        )
        return cur.fetchone()


def _active_risk_amount(conn, account_id: str, now: datetime) -> Decimal:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT risk_amount
            FROM risk_reservations
            WHERE account_id=%s
              AND status = ANY(%s)
              AND (expires_at IS NULL OR expires_at > %s)
            FOR UPDATE
            """,
            (account_id, list(ACTIVE_STATUSES), now),
        )
        return sum((decimal_value(row["risk_amount"]) for row in cur.fetchall()), Decimal("0"))


def _expire_account_reservations(conn, account_id: str, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE risk_reservations
            SET status='expired', updated_at=now()
            WHERE account_id=%s
              AND status = ANY(%s)
              AND expires_at IS NOT NULL
              AND expires_at <= %s
            """,
            (account_id, list(ACTIVE_STATUSES), now),
        )


def _row_result(
    row: dict[str, Any],
    *,
    reason: str,
    remaining_budget: Decimal | None = None,
) -> ReservationResult:
    return ReservationResult(
        status=row["status"],
        reason=reason,
        reservation_id=row["risk_reservation_id"],
        account_id=row["account_id"],
        instrument_id=row["venue_symbol"],
        risk_amount=decimal_value(row["risk_amount"]),
        notional=decimal_value(row["notional"]),
        expires_at=row["expires_at"],
        remaining_budget=remaining_budget,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
