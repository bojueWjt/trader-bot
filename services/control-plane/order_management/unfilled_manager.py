from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json

from db.connection import transaction


TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "rejected", "denied", "cancelled", "expired", "failed", "lost"}
)


@dataclass(frozen=True)
class UnfilledSettings:
    unfilled_timeout_seconds: int
    reprice_interval_seconds: int = 0
    max_reprices: int = 0


@dataclass(frozen=True)
class UnfilledOrder:
    order_projection_id: str
    client_order_id: str
    status: str
    submitted_at: datetime
    cancel_requested_at: datetime | None = None
    reprice_count: int = 0


@dataclass(frozen=True)
class UnfilledDecision:
    action: str
    reason: str
    next_status: str | None = None
    reprice_count: int | None = None


def decide_unfilled_action(
    order: UnfilledOrder,
    settings: UnfilledSettings,
    *,
    now: datetime | None = None,
) -> UnfilledDecision:
    now = _aware(now or datetime.now(timezone.utc))
    submitted_at = _aware(order.submitted_at)
    timed_out = (now - submitted_at).total_seconds() >= settings.unfilled_timeout_seconds
    if order.status == "pending_cancel":
        return UnfilledDecision("wait_for_cancel_terminal", "cancel_not_terminal")
    if not timed_out and order.status not in TERMINAL_ORDER_STATUSES:
        return UnfilledDecision("wait", "timeout_not_reached")
    if order.status not in TERMINAL_ORDER_STATUSES:
        return UnfilledDecision("request_cancel", "unfilled_timeout", next_status="pending_cancel")
    if order.status in {"filled", "rejected", "denied", "failed", "lost"}:
        return UnfilledDecision("terminal", f"order_{order.status}", next_status=order.status)
    if order.reprice_count >= settings.max_reprices:
        return UnfilledDecision("terminal", "max_reprices_exceeded", next_status="expired")
    return UnfilledDecision(
        "reprice",
        "cancel_terminal_confirmed",
        reprice_count=order.reprice_count + 1,
    )


def build_reprice_link_payload(
    *,
    parent_client_order_id: str,
    child_client_order_id: str,
    reprice_count: int,
) -> dict[str, Any]:
    return {
        "parent_client_order_id": parent_client_order_id,
        "child_client_order_id": child_client_order_id,
        "reprice_count": reprice_count,
    }


def record_reprice_link(
    conn,
    *,
    account_id: str,
    parent_order_projection_id: str,
    child_order_projection_id: str,
    lifecycle_role: str = "entry",
    position_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    link_id = str(uuid4())
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO order_links (
                order_link_id, account_id, parent_order_projection_id,
                child_order_projection_id, link_type, lifecycle_role,
                position_key, payload
            )
            VALUES (%s,%s,%s,%s,'reprice',%s,%s,%s)
            ON CONFLICT (parent_order_projection_id, child_order_projection_id, link_type)
            DO UPDATE SET payload=order_links.payload || EXCLUDED.payload
            RETURNING order_link_id::text
            """,
            (
                link_id,
                account_id,
                parent_order_projection_id,
                child_order_projection_id,
                lifecycle_role,
                position_key,
                Json(payload or {}),
            ),
        )
        row = cur.fetchone()
        return row[0]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
