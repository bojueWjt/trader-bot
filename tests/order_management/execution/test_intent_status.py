from __future__ import annotations

from datetime import datetime, timezone

from order_management.intent_status import IntentStatusUpdate, build_timeline_item, classify_intent_outcome


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_node_denial_reject_failure_and_fill_are_distinguishable() -> None:
    denied = classify_intent_outcome({"event_type": "OrderDenied", "payload": {"reason": "risk_guard"}})
    rejected = classify_intent_outcome({"event_type": "OrderRejected", "payload": {"reason": "exchange_filter"}})
    failed = classify_intent_outcome({"event_type": "OrderSubmitFailed", "payload": {"reason": "adapter_error"}})
    completed = classify_intent_outcome({"event_type": "OrderFilled", "payload": {"leaves_qty": "0"}})

    assert denied == IntentStatusUpdate(intent_status="denied", job_status="denied", reason="risk_guard")
    assert rejected == IntentStatusUpdate(intent_status="rejected", job_status="rejected", reason="exchange_filter")
    assert failed == IntentStatusUpdate(intent_status="failed", job_status="failed", reason="adapter_error")
    assert completed == IntentStatusUpdate(intent_status="completed", job_status="completed", reason="filled")


def test_timeline_item_contains_dashboard_data() -> None:
    item = build_timeline_item(
        intent_id="intent-1",
        execution_job_id="job-1",
        status_update=IntentStatusUpdate("rejected", "rejected", "exchange_filter"),
        event_id="event-1",
        event_type="OrderRejected",
        observed_at=NOW,
        payload={"reason": "exchange_filter"},
    )

    assert item["intent_id"] == "intent-1"
    assert item["execution_job_id"] == "job-1"
    assert item["intent_status"] == "rejected"
    assert item["job_status"] == "rejected"
    assert item["reason"] == "exchange_filter"
    assert item["observed_at"] == "2026-06-21T12:00:00+00:00"

