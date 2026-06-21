from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAUTILUS_NODE = ROOT / "services" / "nautilus-node"
if str(NAUTILUS_NODE) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_NODE))

from observability import derive_trace_id as node_trace_id
from observability import structured_log_line as node_log_line
from order_management.execution_jobs import build_execution_job_request
from order_management.metrics import (
    MetricsRegistry,
    derive_trace_id,
    inject_trace_context,
    structured_log_line,
)
from order_management.outbox import enqueue_order_management_event


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, row: tuple[str, ...] | None = None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, row: tuple[str, ...] | None = None) -> None:
        self.cursor_obj = FakeCursor(row)

    def cursor(self):
        return self.cursor_obj


def test_trace_ids_are_stable_and_correlate_intent_job_outbox_order_event_payload() -> None:
    request_id = "req-om8-trace-1"
    idempotency_key = "a" * 64
    expected_trace_id = derive_trace_id(request_id=request_id, idempotency_key=idempotency_key)
    assert node_trace_id(request_id=request_id, idempotency_key=idempotency_key) == expected_trace_id

    intent_row = {
        "intent_id": "11111111-1111-4111-8111-111111111111",
        "account_id": "acct-om8-trace",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "action": "open_position",
        "idempotency_key": idempotency_key,
        "order_plan": {"side": "buy", "type": "market"},
        "risk_budget": {"max_notional": "1000", "risk_amount": "25", "max_leverage": "5"},
        "valid_until": NOW,
    }
    request = build_execution_job_request(intent_row, now=NOW, request_id=request_id)

    assert request.payload["trace_id"] == expected_trace_id
    assert request.payload["intent"]["trace_id"] == expected_trace_id

    outbox_event_id = "22222222-2222-4222-8222-222222222222"
    conn = FakeConnection(row=(outbox_event_id,))
    inserted = enqueue_order_management_event(
        conn,
        aggregate_type="execution_job",
        aggregate_id=request.execution_job_id,
        event_type="order.execution_job.created",
        idempotency_key=request.idempotency_key,
        request_id=request.request_id,
        payload={
            "execution_job_id": request.execution_job_id,
            "intent_id": request.intent_id,
        },
    )

    sql, params = conn.cursor_obj.calls[0]
    assert inserted == outbox_event_id
    assert "trace_id" in sql
    assert params is not None
    payload = params[4].adapted
    assert payload["request_id"] == request_id
    assert payload["trace_id"] == expected_trace_id
    assert params[5] == expected_trace_id

    order_event_payload = inject_trace_context(
        {"event_type": "OrderAccepted"},
        request_id=request.request_id,
        idempotency_key=request.idempotency_key,
    )
    assert order_event_payload["trace_id"] == expected_trace_id
    command_payload = inject_trace_context(
        {"command_type": "cancel_all"},
        request_id=request.request_id,
        idempotency_key=request.idempotency_key,
        command_id="cmd-om8-trace",
    )
    assert command_payload["trace_id"] == expected_trace_id


def test_metrics_registry_records_latency_failure_and_command_duration_snapshots() -> None:
    registry = MetricsRegistry()

    registry.record_intent_latency(
        "submit",
        intent_approved_at=NOW,
        observed_at=NOW.replace(second=3),
        labels={"account_id": "acct-om8"},
    )
    registry.record_failure("reject", labels={"stage": "order"})
    registry.record_failure("retry", labels={"stage": "execution_job"}, amount=2)
    registry.record_protection_install_latency(0.75, labels={"account_id": "acct-om8"})
    registry.record_reconciliation_drift(labels={"severity": "error"})
    registry.record_command_duration("cancel_all", 2.5, status="completed")

    snapshot = registry.snapshot()

    submit = snapshot["histograms"]["intent_to_submit_seconds"]["series"][0]
    assert submit["labels"] == {"account_id": "acct-om8"}
    assert submit["count"] == 1
    assert submit["sum"] == 3.0
    assert submit["min"] == 3.0
    assert submit["max"] == 3.0
    assert snapshot["counters"]["failure.reject.total"]["series"][0]["value"] == 1
    assert snapshot["counters"]["failure.retry.total"]["series"][0]["value"] == 2
    assert snapshot["counters"]["reconciliation.drift.total"]["series"][0]["labels"] == {"severity": "error"}
    command = snapshot["histograms"]["command.cancel_all.duration_seconds"]["series"][0]
    assert command["labels"] == {"status": "completed"}
    assert command["sum"] == 2.5


def test_structured_logs_are_json_and_redact_secret_values() -> None:
    secret = "sk_live_DO_NOT_LEAK"
    line = structured_log_line(
        "order.submitted",
        level="INFO",
        trace_id="33333333-3333-4333-8333-333333333333",
        api_key=secret,
        nested={
            "access_token": secret,
            "headers": {"Authorization": f"Bearer {secret}"},
            "safe": "visible",
        },
    )
    node_line = node_log_line("node.command", token=secret)

    assert secret not in line
    assert secret not in node_line
    data = json.loads(line)
    assert data["event"] == "order.submitted"
    assert data["level"] == "INFO"
    assert data["api_key"] == "[REDACTED]"
    assert data["nested"]["access_token"] == "[REDACTED]"
    assert data["nested"]["headers"]["Authorization"] == "[REDACTED]"
    assert data["nested"]["safe"] == "visible"
