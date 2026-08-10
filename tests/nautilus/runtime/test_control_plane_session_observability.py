from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from threading import Event, Lock
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from runtime.control_plane_session import NodeControlPlaneSession  # noqa: E402


class _FenceConflictError(RuntimeError):
    status_code = 409
    is_fence_conflict = True


def test_structured_logs_cover_start_fatal_and_stop_without_sensitive_data(
) -> None:
    records: list[dict[str, Any]] = []
    sensitive_error = (
        "token=secret-token environment=prod-private order=order-42"
    )
    failure = _FenceConflictError(sensitive_error)

    def record(entry: Mapping[str, Any]) -> None:
        records.append(dict(entry))

    def reject_event(event: Any) -> None:
        del event
        raise failure

    session = NodeControlPlaneSession(
        execution_event_sink=reject_event,
        operation_timeout_seconds=0.1,
        retry_budget=1,
        log_callback=record,
    )

    session.start()
    assert _wait_until(lambda: session.snapshot().ready)
    result = session.submit_execution_event(
        {
            "payload": "private-order-body",
            "token": "payload-token",
            "order_id": "order-42",
        }
    )

    assert result.value == "accepted"
    assert session.wait_for_termination(timeout=1.0) is True
    assert session.stop(time.monotonic() + 1.0) is True

    events = [recorded["event"] for recorded in records]
    assert events.count("control_plane_session.started") == 1
    assert events.count("control_plane_session.fatal") == 1
    assert events.count("control_plane_session.stop_requested") == 1
    assert events.count("control_plane_session.stopped") == 1

    fatal = _single_event(records, "control_plane_session.fatal")
    assert fatal["lane"] == "execution_event"
    assert fatal["level"] == "CRITICAL"

    serialized = json.dumps(records, sort_keys=True)
    assert sensitive_error not in serialized
    assert "private-order-body" not in serialized
    assert "payload-token" not in serialized
    assert "order-42" not in serialized
    assert "prod-private" not in serialized


def test_structured_logs_cover_lane_failure_retry_circuit_and_recovery(
) -> None:
    records: list[dict[str, Any]] = []
    recovered = Event()
    calls = 0

    def record(entry: Mapping[str, Any]) -> None:
        records.append(dict(entry))

    def heartbeat() -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise TimeoutError(
                "token=lane-secret payload=private-body"
            )
        recovered.set()

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        heartbeat_interval_seconds=0.005,
        retry_budget=2,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0,
        circuit_reset_seconds=0.005,
        operation_timeout_seconds=0.1,
        log_callback=record,
    )

    session.start()
    assert recovered.wait(timeout=1.0)
    assert _wait_until(
        lambda: session.snapshot().lanes["heartbeat"].success_count >= 4
    )
    assert session.stop(time.monotonic() + 1.0) is True

    failure = _single_event(
        records,
        "control_plane_session.lane_failure",
    )
    retry = _single_event(
        records,
        "control_plane_session.lane_retry",
    )
    circuit = _single_event(
        records,
        "control_plane_session.lane_circuit_open",
    )
    recovery = _single_event(
        records,
        "control_plane_session.lane_recovered",
    )

    assert failure["lane"] == "heartbeat"
    assert failure["error_type"] == "TimeoutError"
    assert retry["retry_count"] == 1
    assert retry["retry_delay_ms"] == 1.0
    assert circuit["circuit_state"] == "open"
    assert circuit["error_count"] == 2
    assert circuit["timeout_count"] == 2
    assert circuit["circuit_open_count"] == 1
    assert recovery["circuit_state"] == "closed"
    assert recovery["success_count"] == 1
    assert recovery["previous_circuit_state"] == "half_open"

    timeout_records = _events(
        records,
        "control_plane_session.lane_timeout",
    )
    assert [
        record["timeout_count"]
        for record in timeout_records
    ] == [1, 2]

    events = [recorded["event"] for recorded in records]
    assert "control_plane_session.lane_success" not in events
    serialized = json.dumps(records, sort_keys=True)
    assert "lane-secret" not in serialized
    assert "private-body" not in serialized


def test_structured_logs_cover_queue_pressure_capacity_and_recovery(
) -> None:
    records: list[dict[str, Any]] = []
    blocked = Event()
    release = Event()

    def record(entry: Mapping[str, Any]) -> None:
        records.append(dict(entry))

    def sink(event: Any) -> None:
        del event
        blocked.set()
        release.wait(timeout=1.0)

    session = NodeControlPlaneSession(
        execution_event_sink=sink,
        execution_event_capacity=2,
        queue_degraded_ratio=0.5,
        operation_timeout_seconds=1.0,
        log_callback=record,
    )

    session.start()
    assert _wait_until(lambda: session.snapshot().ready)
    assert session.submit_execution_event("private-hold-order").value == (
        "accepted"
    )
    assert blocked.wait(timeout=1.0)
    assert session.submit_execution_event("private-queued-order-1").value == (
        "accepted"
    )
    assert session.submit_execution_event("private-queued-order-2").value == (
        "accepted"
    )
    assert session.submit_execution_event("private-overflow-order").value == (
        "backpressured"
    )

    capacity = _single_event(
        records,
        "control_plane_session.queue_capacity_exceeded",
    )
    assert capacity["lane"] == "execution_event"
    assert capacity["queue_depth"] == 2
    assert capacity["queue_capacity"] == 2
    assert capacity["queue_pressure"] == "full"

    release.set()
    assert _wait_until(
        lambda: (
            session.snapshot().lanes["execution_event"].queue_depth == 0
            and session.snapshot().degraded is False
        )
    )
    assert session.stop(time.monotonic() + 1.0) is True

    pressure_records = [
        record
        for record in records
        if record.get("event")
        == "control_plane_session.queue_pressure"
        and record.get("lane") == "execution_event"
    ]
    pressure_states = [
        str(record["queue_pressure"])
        for record in pressure_records
    ]
    assert "degraded" in pressure_states
    assert "full" in pressure_states
    assert pressure_states[-1] == "normal"

    serialized = json.dumps(records, sort_keys=True)
    assert "private-hold-order" not in serialized
    assert "private-queued-order-1" not in serialized
    assert "private-overflow-order" not in serialized


def test_structured_health_logs_are_rate_limited_and_include_lane_progress(
) -> None:
    records: list[dict[str, Any]] = []
    progress_checked = Event()
    clock = _ManualClock(100.0)

    def record(entry: Mapping[str, Any]) -> None:
        records.append(dict(entry))

    def consumer_progress() -> float:
        progress_checked.set()
        return 7.0

    session = NodeControlPlaneSession(
        heartbeat=lambda: None,
        heartbeat_interval_seconds=60.0,
        operation_timeout_seconds=0.02,
        consumer_progress=consumer_progress,
        consumer_freeze_threshold_seconds=10.0,
        health_log_interval_seconds=1.0,
        monotonic_clock=clock.now,
        log_callback=record,
    )

    session.start()
    assert progress_checked.wait(timeout=1.0)
    assert _wait_until(
        lambda: session.snapshot().lanes["heartbeat"].success_count == 1
    )

    clock.advance(0.5)
    time.sleep(0.03)
    assert _events(
        records,
        "control_plane_session.health",
    ) == []

    clock.advance(0.6)
    assert _wait_until(
        lambda: len(
            _events(records, "control_plane_session.health")
        )
        == 1
    )
    first = _single_event(
        records,
        "control_plane_session.health",
    )

    assert first["consumer_progress_age_ms"] == 1100.0
    assert first["process_liveness"] is True
    assert first["ready"] is True
    assert set(first["lanes"]) == {
        "heartbeat",
        "command_poll",
        "command_delivery",
        "command_ack",
        "intent_fetch",
        "intent_delivery",
        "execution_event",
    }
    heartbeat = first["lanes"]["heartbeat"]
    assert heartbeat["queue_depth"] == 0
    assert heartbeat["queue_capacity"] == 1
    assert heartbeat["circuit_state"] == "closed"
    assert heartbeat["error_count"] == 0
    assert heartbeat["timeout_count"] == 0
    assert heartbeat["success_count"] == 1
    assert heartbeat["retry_count"] == 0
    assert isinstance(heartbeat["last_success_age_ms"], float)

    time.sleep(0.04)
    assert len(
        _events(records, "control_plane_session.health")
    ) == 1
    clock.advance(1.1)
    assert _wait_until(
        lambda: len(
            _events(records, "control_plane_session.health")
        )
        == 2
    )
    assert session.stop(time.monotonic() + 1.0) is True


class _ManualClock:
    def __init__(self, value: float) -> None:
        self._value = float(value)
        self._lock = Lock()

    def now(self) -> float:
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += float(seconds)


def _events(
    records: list[dict[str, Any]],
    event: str,
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("event") == event
    ]


def _single_event(
    records: list[dict[str, Any]],
    event: str,
) -> dict[str, Any]:
    matching = [
        record
        for record in records
        if record.get("event") == event
    ]
    assert len(matching) == 1
    return matching[0]


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return False
