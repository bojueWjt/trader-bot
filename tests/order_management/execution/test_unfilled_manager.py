from __future__ import annotations

from datetime import datetime, timedelta, timezone

from order_management.unfilled_manager import (
    UnfilledOrder,
    UnfilledSettings,
    build_reprice_link_payload,
    decide_unfilled_action,
)


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_unfilled_timeout_requests_cancel_before_reprice() -> None:
    order = _order(status="working", submitted_at=NOW - timedelta(seconds=61))

    decision = decide_unfilled_action(order, UnfilledSettings(unfilled_timeout_seconds=60), now=NOW)

    assert decision.action == "request_cancel"
    assert decision.next_status == "pending_cancel"


def test_pending_cancel_waits_until_terminal_before_reprice() -> None:
    order = _order(status="pending_cancel", submitted_at=NOW - timedelta(seconds=61), cancel_requested_at=NOW - timedelta(seconds=10))

    decision = decide_unfilled_action(order, UnfilledSettings(unfilled_timeout_seconds=60), now=NOW)

    assert decision.action == "wait_for_cancel_terminal"
    assert decision.next_status is None


def test_cancelled_order_reprices_with_parent_child_trace() -> None:
    order = _order(status="cancelled", submitted_at=NOW - timedelta(seconds=61), reprice_count=1)

    decision = decide_unfilled_action(order, UnfilledSettings(unfilled_timeout_seconds=60, max_reprices=2), now=NOW)
    payload = build_reprice_link_payload(parent_client_order_id="parent", child_client_order_id="child", reprice_count=2)

    assert decision.action == "reprice"
    assert decision.reprice_count == 2
    assert payload["parent_client_order_id"] == "parent"
    assert payload["child_client_order_id"] == "child"
    assert payload["reprice_count"] == 2


def test_max_reprices_returns_explicit_terminal() -> None:
    order = _order(status="cancelled", submitted_at=NOW - timedelta(seconds=61), reprice_count=2)

    decision = decide_unfilled_action(order, UnfilledSettings(unfilled_timeout_seconds=60, max_reprices=2), now=NOW)

    assert decision.action == "terminal"
    assert decision.next_status == "expired"
    assert decision.reason == "max_reprices_exceeded"


def _order(**overrides) -> UnfilledOrder:
    values = {
        "order_projection_id": "order-1",
        "client_order_id": "client-1",
        "status": "working",
        "submitted_at": NOW,
        "cancel_requested_at": None,
        "reprice_count": 0,
    }
    values.update(overrides)
    return UnfilledOrder(**values)

