from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from runtime.control_plane_session import NodeControlPlaneSession  # noqa: E402
from runtime.health import HealthService  # noqa: E402


def test_consumer_gate_wait_does_not_start_io_or_operation_deadlines() -> None:
    consumers_ready = Event()
    calls: list[str] = []
    fatal_reasons: list[str] = []
    session = NodeControlPlaneSession(
        heartbeat=lambda: calls.append("heartbeat"),
        command_poll=lambda capacity: _record_poll(
            calls,
            "command",
            capacity,
        ),
        intent_replay=lambda: calls.append("replay"),
        intent_fetch=lambda capacity: _record_poll(
            calls,
            "intent",
            capacity,
        ),
        consumer_ready=consumers_ready.is_set,
        heartbeat_interval_seconds=0.01,
        command_poll_interval_seconds=0.01,
        intent_fetch_interval_seconds=0.01,
        operation_timeout_seconds=0.02,
        fatal_termination_hook=fatal_reasons.append,
    )

    session.start()
    time.sleep(0.08)
    waiting = session.snapshot()

    assert waiting.started is True
    assert waiting.consumers_ready is False
    assert waiting.process_liveness is True
    assert waiting.ready is False
    assert (
        waiting.configuration["consumer_freeze_threshold_seconds"]
        == 60.0
    )
    assert calls == []
    assert fatal_reasons == []
    assert all(
        lane.in_flight_age_ms is False
        for lane in waiting.lanes.values()
    )

    consumers_ready.set()
    assert _wait_until(
        lambda: {"heartbeat", "command", "intent", "replay"}
        <= set(calls),
    )
    running = session.snapshot()

    assert running.consumers_ready is True
    assert running.process_liveness is True
    assert fatal_reasons == []
    assert session.stop(time.monotonic() + 1.0) is True


def test_explicit_consumer_ready_signal_releases_startup_gate() -> None:
    heartbeat_sent = Event()
    session = NodeControlPlaneSession(
        heartbeat=heartbeat_sent.set,
        consumer_ready=lambda: False,
        heartbeat_interval_seconds=0.01,
        operation_timeout_seconds=0.02,
    )

    session.start()
    time.sleep(0.05)

    assert session.snapshot().consumers_ready is False
    assert heartbeat_sent.is_set() is False

    session.mark_consumers_ready()

    assert heartbeat_sent.wait(timeout=1.0)
    assert session.snapshot().consumers_ready is True
    assert session.stop(time.monotonic() + 1.0) is True


def test_startup_heartbeat_http_failure_degrades_then_recovers() -> None:
    recover = Event()
    failed = Event()
    succeeded = Event()
    fatal_reasons: list[str] = []

    def heartbeat() -> None:
        if not recover.is_set():
            failed.set()
            raise RuntimeError("heartbeat HTTP 503")
        succeeded.set()

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        heartbeat_interval_seconds=0.005,
        retry_budget=1,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0,
        circuit_reset_seconds=0.005,
        operation_timeout_seconds=0.1,
        fatal_termination_hook=fatal_reasons.append,
    )

    session.start()
    assert failed.wait(timeout=1.0)
    assert _wait_until(lambda: session.snapshot().degraded)
    degraded = session.snapshot()

    assert degraded.process_liveness is True
    assert degraded.lanes["heartbeat"].failure == "heartbeat HTTP 503"
    assert degraded.lanes["heartbeat"].circuit_state == "open"
    assert fatal_reasons == []
    assert session.wait_for_termination(timeout=0.01) is False

    recover.set()
    assert succeeded.wait(timeout=1.0)
    assert _wait_until(
        lambda: (
            session.snapshot().lanes["heartbeat"].last_success_at
            is not False
        )
    )
    recovered = session.snapshot()

    assert recovered.process_liveness is True
    assert recovered.degraded is False
    assert recovered.lanes["heartbeat"].failure is False
    assert recovered.lanes["heartbeat"].circuit_state == "closed"
    assert fatal_reasons == []
    assert session.stop(time.monotonic() + 1.0) is True


def test_fatal_deadline_publishes_termination_and_process_dead() -> None:
    blocked = Event()
    release = Event()
    fatal_reasons: list[str] = []

    def sink(event: Any) -> None:
        del event
        blocked.set()
        release.wait(timeout=1.0)

    session = NodeControlPlaneSession(
        execution_event_sink=sink,
        operation_timeout_seconds=0.02,
        retry_budget=1,
        fatal_termination_hook=fatal_reasons.append,
    )
    session.start()
    assert _wait_until(lambda: session.snapshot().ready)
    session.submit_execution_event("fill-1")

    assert blocked.wait(timeout=1.0)
    assert session.wait_for_termination(timeout=1.0) is True
    fatal = session.snapshot()

    assert fatal.process_liveness is False
    assert fatal.ready is False
    assert fatal_reasons == [
        "execution_event operation exceeded 0.020s deadline"
    ]

    release.set()
    assert session.stop(time.monotonic() + 1.0) is True


def test_queue_capacity_fatal_publishes_termination_and_process_dead() -> None:
    blocked = Event()
    release = Event()
    fatal_reasons: list[str] = []

    def sink(event: Any) -> None:
        del event
        blocked.set()
        release.wait(timeout=1.0)

    session = NodeControlPlaneSession(
        execution_event_sink=sink,
        execution_event_capacity=1,
        operation_timeout_seconds=1.0,
        fatal_termination_hook=fatal_reasons.append,
    )
    session.start()
    assert _wait_until(lambda: session.snapshot().ready)
    assert session.submit_execution_event("fill-1").value == "accepted"
    assert blocked.wait(timeout=1.0)

    assert session.submit_execution_event("fill-2").value == "accepted"
    assert session.submit_execution_event("fill-3").value == "backpressured"
    assert session.wait_for_termination(timeout=1.0) is True
    fatal = session.snapshot()

    assert fatal.process_liveness is False
    assert fatal.ready is False
    assert fatal_reasons == [
        "execution_event queue capacity exceeded"
    ]

    release.set()
    assert session.stop(time.monotonic() + 1.0) is True


def test_consumer_progress_advances_without_false_freeze_then_stalls_hard() -> None:
    clock = _ManualClock()
    progress = {"value": 0.0}
    fatal_reasons: list[str] = []
    session = NodeControlPlaneSession(
        consumer_ready=lambda: True,
        consumer_progress=lambda: progress["value"],
        consumer_freeze_threshold_seconds=0.05,
        monotonic_clock=clock.now,
        operation_timeout_seconds=0.02,
        fatal_termination_hook=fatal_reasons.append,
    )
    session.start()
    assert _wait_until(lambda: session.snapshot().ready)

    for _ in range(5):
        clock.advance(0.04)
        progress["value"] += 1.0
        time.sleep(0.01)
        assert session.wait_for_termination(timeout=0) is False
        assert session.snapshot().process_liveness is True

    clock.advance(0.051)

    assert session.wait_for_termination(timeout=1.0) is True
    assert session.snapshot().process_liveness is False
    assert fatal_reasons == [
        "control-plane consumer progress frozen for more than 0.050s"
    ]
    assert session.stop(time.monotonic() + 1.0) is True


def test_stopped_session_publishes_termination_and_process_dead() -> None:
    session = NodeControlPlaneSession()
    session.start()
    assert _wait_until(lambda: session.snapshot().ready)

    assert session.stop(time.monotonic() + 1.0) is True

    stopped = session.snapshot()
    assert session.wait_for_termination(timeout=0) is True
    assert stopped.stopped is True
    assert stopped.process_liveness is False
    assert stopped.ready is False


def test_health_liveness_fails_closed_from_session_process_state() -> None:
    lifecycle = SimpleNamespace(
        config=SimpleNamespace(
            account_id="account-a",
            node_id="node-a",
        ),
        trading_state=SimpleNamespace(value="ACTIVE"),
    )
    process_live = {"value": True}
    health = HealthService(
        lifecycle,
        process_liveness=lambda: process_live["value"],
    )

    live = health.liveness()
    assert live.status_code == 200
    assert live.body["live"] is True

    process_live["value"] = False
    dead = health.liveness()
    assert dead.status_code == 503
    assert dead.body["live"] is False


def _record_poll(
    calls: list[str],
    name: str,
    capacity: int,
) -> tuple[Any, ...]:
    assert capacity > 0
    calls.append(name)
    return ()


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


class _ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)
