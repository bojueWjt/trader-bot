from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from execution_domain.idempotency import RequestId, execution_job_key, intent_idempotency_key
from order_management.outbox import (
    deterministic_outbox_event_id,
    enqueue_order_management_event,
    fetch_command_result_by_request_id,
)


class FakeCursor:
    def __init__(self, row=None):
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
    def __init__(self, row=None):
        self.cursor_obj = FakeCursor(row)

    def cursor(self):
        return self.cursor_obj


def test_intent_idempotency_key_matches_decision_gateway_material_order():
    decision_id = "22222222-2222-4222-8222-222222222222"

    key = intent_idempotency_key(decision_id, "open_position", "BTCUSDT", "acct-1")

    expected = hashlib.sha256(
        f"{decision_id}|open_position|BTCUSDT|acct-1".encode("utf-8")
    ).hexdigest()
    assert key == expected
    assert len(key) == 64


def test_execution_job_key_is_stable_for_same_intent_replay():
    first = execution_job_key(
        "11111111-1111-4111-8111-111111111111",
        "open_position",
        "BTCUSDT-PERP.BINANCE",
        "acct-1",
    )
    replay = execution_job_key(
        "11111111-1111-4111-8111-111111111111",
        "open_position",
        "BTCUSDT-PERP.BINANCE",
        "acct-1",
    )

    assert replay == first
    assert execution_job_key("intent-2", "open_position", "BTCUSDT", "acct-1") != first


def test_request_id_helper_validates_and_derives_deterministically():
    assert str(RequestId.from_value(" req-1 ")) == "req-1"
    assert RequestId.for_material("command", "acct-1", "close_all") == RequestId.for_material(
        "command", "acct-1", "close_all"
    )
    assert str(RequestId.for_material("command", "acct-1", "close_all")).startswith("req_")

    with pytest.raises(ValueError, match="request_id"):
        RequestId.from_value("  ")


def test_deterministic_outbox_event_id_uses_event_identity():
    event_id = deterministic_outbox_event_id("order", "order-1", "order.submitted", "idem-1")

    material = "order|order-1|order.submitted|idem-1"
    expected = UUID(hex=hashlib.sha256(material.encode("utf-8")).hexdigest()[:32])
    assert event_id == expected
    assert deterministic_outbox_event_id("order", "order-1", "order.submitted", "idem-1") == event_id
    assert deterministic_outbox_event_id("order", "order-1", "order.filled", "idem-1") != event_id


def test_enqueue_order_management_event_uses_deterministic_id_and_conflict_noop():
    expected_id = deterministic_outbox_event_id(
        "order", "order-1", "order.submitted", "idem-1"
    )
    conn = FakeConnection(row=(str(expected_id),))

    inserted = enqueue_order_management_event(
        conn,
        aggregate_type="order",
        aggregate_id="order-1",
        event_type="order.submitted",
        idempotency_key="idem-1",
        request_id="req-1",
        payload={"status": "submitted"},
    )

    sql, params = conn.cursor_obj.calls[0]
    assert inserted == str(expected_id)
    assert "ON CONFLICT (outbox_event_id) DO NOTHING" in sql
    assert params[:4] == (str(expected_id), "order", "order-1", "order.submitted")
    assert params[4].adapted == {"status": "submitted", "request_id": "req-1"}


def test_enqueue_replay_returns_same_id_when_insert_conflicts():
    expected_id = deterministic_outbox_event_id(
        "settings", "global", "settings.changed", "idem-2"
    )
    conn = FakeConnection(row=None)

    inserted = enqueue_order_management_event(
        conn,
        aggregate_type="settings",
        aggregate_id="global",
        event_type="settings.changed",
        idempotency_key="idem-2",
        request_id="req-2",
    )

    assert inserted == str(expected_id)


def test_fetch_command_result_by_request_id_returns_terminal_outbox_event():
    conn = FakeConnection(
        row=(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "published",
            "command",
            "cmd-1",
            "command.completed",
            {"request_id": "req-3", "result": "completed"},
            None,
        )
    )

    result = fetch_command_result_by_request_id(conn, "req-3")

    sql, params = conn.cursor_obj.calls[0]
    assert "payload->>'request_id' = %s" in sql
    assert "command.completed" in sql
    assert params == ("req-3",)
    assert result == {
        "outbox_event_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "status": "published",
        "aggregate_type": "command",
        "aggregate_id": "cmd-1",
        "event_type": "command.completed",
        "payload": {"request_id": "req-3", "result": "completed"},
        "error": None,
    }
