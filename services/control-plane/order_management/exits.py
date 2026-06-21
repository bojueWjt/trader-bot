from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Protocol

from db.connection import transaction


class ExitExchange(Protocol):
    def actual_position_quantity(self, *, account_id: str, position_key: str) -> Decimal: ...

    def submit_reduce_only_market(self, *, account_id: str, position_key: str, quantity: Decimal) -> None: ...

    def cancel_order(self, client_order_id: str) -> None: ...


@dataclass(frozen=True)
class CloseResult:
    status: str
    position_key: str
    submitted_quantity: Decimal = Decimal("0")
    reason: str | None = None


def partial_close(
    conn,
    *,
    account_id: str,
    position_key: str,
    exchange: ExitExchange,
    now: datetime,
    fraction: Decimal | None = None,
    quantity: Decimal | None = None,
) -> CloseResult:
    del now
    actual = Decimal(str(exchange.actual_position_quantity(account_id=account_id, position_key=position_key)))
    close_quantity = _close_quantity(actual=actual, fraction=fraction, quantity=quantity)
    if close_quantity is None:
        return CloseResult("denied", position_key, reason="quantity_required")
    if close_quantity <= 0:
        return CloseResult("denied", position_key, reason="quantity_must_be_positive")
    if close_quantity > actual:
        return CloseResult("denied", position_key, reason="quantity_exceeds_actual_position")

    exchange.submit_reduce_only_market(
        account_id=account_id,
        position_key=position_key,
        quantity=close_quantity,
    )
    remaining = Decimal(str(exchange.actual_position_quantity(account_id=account_id, position_key=position_key)))
    with transaction(conn):
        _update_position_quantity(conn, account_id, position_key, remaining)
        _recompute_active_protection_quantities(conn, account_id, position_key, remaining)
    return CloseResult("submitted", position_key, submitted_quantity=close_quantity)


def close_position(
    conn,
    account_id: str,
    position_key: str,
    *,
    exchange: ExitExchange,
    reconcile: Callable[[], bool] | Callable[..., bool],
    now: datetime,
) -> CloseResult:
    del now
    actual = Decimal(str(exchange.actual_position_quantity(account_id=account_id, position_key=position_key)))
    if actual > 0:
        exchange.submit_reduce_only_market(
            account_id=account_id,
            position_key=position_key,
            quantity=actual,
        )

    final_quantity = Decimal(str(exchange.actual_position_quantity(account_id=account_id, position_key=position_key)))
    if final_quantity != 0:
        return CloseResult("verifying", position_key, submitted_quantity=actual, reason="position_not_flat")

    residuals = _active_protection_client_order_ids(conn, account_id, position_key)
    for client_order_id in residuals:
        exchange.cancel_order(client_order_id)
    with transaction(conn):
        _update_position_quantity(conn, account_id, position_key, Decimal("0"))
        _clear_active_protection(conn, account_id, position_key)

    reconciled = _call_reconcile(reconcile)
    if not reconciled:
        return CloseResult(
            "verifying",
            position_key,
            submitted_quantity=actual,
            reason="final_reconciliation_failed",
        )
    return CloseResult("completed", position_key, submitted_quantity=actual)


def _close_quantity(
    *,
    actual: Decimal,
    fraction: Decimal | None,
    quantity: Decimal | None,
) -> Decimal | None:
    if (fraction is None) == (quantity is None):
        return None
    if fraction is not None:
        return actual * Decimal(str(fraction))
    return Decimal(str(quantity))


def _update_position_quantity(conn, account_id: str, position_key: str, quantity: Decimal) -> None:
    status = "closed" if quantity == 0 else "reducing"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE positions_projection
            SET quantity=%s, status=%s, updated_at=now()
            WHERE account_id=%s AND position_id=%s
            """,
            (quantity, status, account_id, position_key),
        )


def _recompute_active_protection_quantities(
    conn,
    account_id: str,
    position_key: str,
    remaining: Decimal,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE protective_orders_projection
            SET quantity=LEAST(quantity, %s), updated_at=now()
            WHERE account_id=%s
              AND position_key=%s
              AND active
              AND lifecycle_role='stop_loss'
            """,
            (remaining, account_id, position_key),
        )
        cur.execute(
            """
            SELECT protective_order_projection_id, quantity
            FROM protective_orders_projection
            WHERE account_id=%s
              AND position_key=%s
              AND active
              AND lifecycle_role='take_profit'
            ORDER BY updated_at, client_order_id
            FOR UPDATE
            """,
            (account_id, position_key),
        )
        rows = cur.fetchall()
        allocated = Decimal("0")
        for projection_id, quantity in rows:
            available = max(Decimal("0"), remaining - allocated)
            adjusted = min(Decimal(str(quantity)), available)
            allocated += adjusted
            cur.execute(
                """
                UPDATE protective_orders_projection
                SET quantity=%s, active=(%s > 0), updated_at=now()
                WHERE protective_order_projection_id=%s
                """,
                (adjusted, adjusted, projection_id),
            )


def _active_protection_client_order_ids(conn, account_id: str, position_key: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT client_order_id
            FROM protective_orders_projection
            WHERE account_id=%s AND position_key=%s AND active
              AND lifecycle_role IN ('stop_loss', 'take_profit')
            ORDER BY lifecycle_role, client_order_id
            """,
            (account_id, position_key),
        )
        return [row[0] for row in cur.fetchall() if row[0]]


def _clear_active_protection(conn, account_id: str, position_key: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE protective_orders_projection
            SET active=false, status='cancelled', updated_at=now()
            WHERE account_id=%s AND position_key=%s AND active
              AND lifecycle_role IN ('stop_loss', 'take_profit')
            """,
            (account_id, position_key),
        )


def _call_reconcile(reconcile: Callable[..., bool]) -> bool:
    try:
        return bool(reconcile())
    except TypeError:
        return bool(reconcile())

