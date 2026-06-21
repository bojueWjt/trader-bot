from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ACTIVE_ORDER_STATUSES = frozenset({"accepted", "working", "partially_filled", "submitted"})


@dataclass(frozen=True)
class RecoveryJob:
    execution_job_id: str
    status: str
    client_order_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason: str
    next_status: str | None = None


def decide_recovery_action(
    job: RecoveryJob,
    *,
    local_orders: list[dict[str, Any]],
    reconciliation_clean: bool | None,
) -> RecoveryDecision:
    accepted = [
        order
        for order in local_orders
        if job.client_order_id is not None
        and str(order.get("client_order_id")) == job.client_order_id
        and str(order.get("status")) in ACTIVE_ORDER_STATUSES
    ]
    if accepted:
        return RecoveryDecision("do_not_resubmit", "accepted_order_exists", next_status="executing")
    if reconciliation_clean is None:
        return RecoveryDecision("reconcile_first", "unknown_submit_state", next_status="needs_review")
    if not reconciliation_clean:
        return RecoveryDecision("needs_review", "reconciliation_failed", next_status="needs_review")
    if str((job.payload.get("submit") or {}).get("state")) == "unknown":
        return RecoveryDecision("needs_review", "network_uncertain_after_clean_reconcile", next_status="needs_review")
    return RecoveryDecision("resume", "safe_to_resume", next_status="pending")
