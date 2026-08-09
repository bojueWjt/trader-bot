from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app import run_node  # noqa: E402
from app.nautilus_actors import (  # noqa: E402
    CommandPollerActor,
    ExecutionProjectionActor,
    IntentPublisherActor,
)
from execution_domain.control_plane import (  # noqa: E402
    CommandType,
    NodeCommand,
    TradingState,
)
from runtime.bounded_task_worker import BoundedTaskWorker  # noqa: E402
from runtime.control_plane_session import NodeControlPlaneSession  # noqa: E402


class _LeaseGuard:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(
        self,
        *,
        stop_results: tuple[bool, ...] = (True,),
    ) -> None:
        self._stop_results = list(stop_results)
        self.started = False
        self.stop_calls = 0

    def start(self) -> None:
        self.started = True

    def stop(self, deadline: float) -> bool:
        del deadline
        self.stop_calls += 1
        if len(self._stop_results) > 1:
            return self._stop_results.pop(0)
        return self._stop_results[0]


class _Node:
    def __init__(self, *, fail_stage: str = "") -> None:
        self._fail_stage = fail_stage
        self.built = False
        self.ran = False
        self.stopped = False
        self.disposed = False

    def build(self) -> None:
        self.built = True
        if self._fail_stage == "build":
            raise RuntimeError("node build failed")

    def run(self) -> None:
        self.ran = True
        if self._fail_stage == "run":
            raise RuntimeError("node run failed")

    def stop(self) -> None:
        self.stopped = True

    def dispose(self) -> None:
        self.disposed = True


class _Server:
    def __init__(self) -> None:
        self.served = Event()
        self.shutdown_called = False
        self.closed = False

    def serve_forever(self) -> None:
        self.served.set()

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        self.closed = True


class _RedisSafetyTimeoutGuard:
    def __init__(self) -> None:
        self._stop_calls = 0
        self.running = True

    def stop(self) -> bool:
        self._stop_calls += 1
        if self._stop_calls == 1:
            raise RuntimeError(
                "Redis runtime safety thread exceeded shutdown deadline"
            )
        return False

    def snapshot(self) -> dict[str, bool]:
        return {"running": self.running}


class _RedisSafetyGuard:
    def stop(self) -> bool:
        return True


class _RedisClient:
    def __init__(self, *, close_failures: int = 0) -> None:
        self.close_calls = 0
        self._close_failures = close_failures

    def close(self) -> None:
        self.close_calls += 1
        if self._close_failures > 0:
            self._close_failures -= 1
            raise RuntimeError("redis client close failed")


def test_blocked_intent_lane_preserves_actor_and_peer_lane_progress() -> None:
    lifecycle = _ProgressLifecycle()
    control_plane = _ProgressControlPlane()
    intent_client = _ProgressIntentClient()
    projection = _ProgressProjection()
    fetch_started = Event()
    release_fetch = Event()
    actor_ticks: list[float] = []
    pump_stop = Event()
    fetched = False
    actor_holder: dict[str, Any] = {}

    def fetch_intents(capacity: int) -> tuple[Any, ...]:
        nonlocal fetched
        del capacity
        if fetched:
            return ()
        fetch_started.set()
        release_fetch.wait(timeout=2.0)
        fetched = True
        return (SimpleNamespace(account_id="account-a"),)

    session = NodeControlPlaneSession(
        heartbeat=lambda: actor_holder[
            "command"
        ].session_send_heartbeat(),
        command_poll=lambda capacity: actor_holder[
            "command"
        ].session_poll_commands(capacity),
        command_apply=lambda command: actor_holder[
            "command"
        ].session_apply_command(command),
        command_ack=lambda acknowledgement: actor_holder[
            "command"
        ].session_ack_command(acknowledgement),
        intent_fetch=fetch_intents,
        intent_deliver=intent_client.deliver,
        execution_event_sink=lambda event: actor_holder[
            "projection"
        ].session_flush_execution_event(event),
        heartbeat_interval_seconds=0.01,
        command_poll_interval_seconds=0.01,
        intent_fetch_interval_seconds=0.01,
        operation_timeout_seconds=1.0,
        retry_budget=1,
    )
    intent_actor = IntentPublisherActor(
        intent_client,
        lifecycle=lifecycle,
        control_plane_session=session,
        manage_control_plane_session=False,
        stale_after_seconds=1.0,
    )
    intent_actor.publish = intent_client.record_published  # type: ignore[method-assign]
    projection_actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        manage_control_plane_session=False,
    )
    command_actor = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=session,
        manage_control_plane_session=False,
        stale_after_seconds=1.0,
    )
    actor_holder.update(
        {
            "command": command_actor,
            "intent": intent_actor,
            "projection": projection_actor,
        }
    )

    def pump_actor_callbacks() -> None:
        while not pump_stop.is_set():
            actor_ticks.append(time.monotonic())
            command_actor._on_poll_timer()
            intent_actor._on_poll_timer()
            time.sleep(0.005)

    intent_actor.on_start()
    projection_actor.on_start()
    command_actor.on_start()
    session.start()
    pump = Thread(
        target=pump_actor_callbacks,
        name="account-stall.actor-pump",
    )
    pump.start()
    try:
        assert fetch_started.wait(timeout=1.0)
        started_at = time.monotonic()
        assert projection_actor.on_event("fill-1") is True
        assert time.monotonic() - started_at < 0.01
        assert _wait_for(
            lambda: (
                lifecycle.heartbeat_count >= 2
                and control_plane.command_poll_count >= 1
                and control_plane.command_ack_count >= 1
                and projection.flush_count >= 1
            ),
            timeout=1.0,
        )
        blocked_snapshot = session.snapshot()
        assert actor_ticks
        assert time.monotonic() - actor_ticks[-1] < 0.1
        assert (
            blocked_snapshot.lanes["heartbeat"].last_success_at
            is not False
        )
        assert (
            blocked_snapshot.lanes["command_poll"].last_success_at
            is not False
        )
        assert (
            blocked_snapshot.lanes["command_ack"].last_success_at
            is not False
        )
        assert (
            blocked_snapshot.lanes["execution_event"].last_success_at
            is not False
        )
        assert (
            blocked_snapshot.lanes["intent_fetch"].last_success_at
            is False
        )

        release_fetch.set()
        assert _wait_for(
            lambda: bool(intent_client.published),
            timeout=1.0,
        )
        recovered_snapshot = session.snapshot()
        assert (
            recovered_snapshot.lanes["intent_fetch"].last_success_at
            is not False
        )
        assert intent_client.published[0].account_id == "account-a"
        assert lifecycle.failed_dependencies == []
    finally:
        release_fetch.set()
        assert session.stop(time.monotonic() + 1.0)
        pump_stop.set()
        pump.join(timeout=1.0)
        intent_actor.on_stop()
        projection_actor.on_stop()
        command_actor.on_stop()

    assert pump.is_alive() is False


def test_cleanup_retains_lease_when_bounded_worker_stop_returns_false() -> None:
    started = Event()
    release = Event()

    def block(_task: object) -> None:
        started.set()
        release.wait(timeout=5.0)

    worker: BoundedTaskWorker[object] = BoundedTaskWorker(
        "account-a.writer",
        block,
        capacity=1,
        task_timeout_seconds=False,
    )
    original_stop = worker.stop
    worker.stop = lambda: original_stop(timeout_seconds=0.01)  # type: ignore[method-assign]
    worker.start()
    assert worker.submit(object()) is True
    assert started.wait(timeout=1.0)

    lease_guard = _LeaseGuard()
    runtime = SimpleNamespace(
        control_plane_session=_Session(),
        trading_node=_Node(),
        background_workers=[worker],
        redis_runtime_safety_guard=None,
        redis_runtime_safety_client=None,
        namespace_lease_guard=lease_guard,
    )

    try:
        with pytest.raises(RuntimeError, match="runtime cleanup failed"):
            run_node._cleanup_runtime(runtime, False)
    finally:
        release.set()
        original_stop(timeout_seconds=1.0)

    assert lease_guard.closed is False


def test_cleanup_retries_failed_session_before_releasing_lease() -> None:
    lease_guard = _LeaseGuard()
    session = _Session(stop_results=(False, True))
    runtime = _runtime(
        lease_guard=lease_guard,
        session=session,
    )

    with pytest.raises(RuntimeError, match="runtime cleanup failed"):
        run_node._cleanup_runtime(runtime, False)

    assert runtime.control_plane_session is session
    assert lease_guard.closed is False

    run_node._cleanup_runtime(runtime, False)

    assert runtime.control_plane_session is None
    assert lease_guard.closed is True


def test_cleanup_retries_timed_out_redis_guard_after_thread_stops() -> None:
    lease_guard = _LeaseGuard()
    redis_guard = _RedisSafetyTimeoutGuard()
    redis_client = _RedisClient()
    runtime = _runtime(
        lease_guard=lease_guard,
        redis_guard=redis_guard,
        redis_client=redis_client,
    )

    with pytest.raises(RuntimeError, match="runtime cleanup failed"):
        run_node._cleanup_runtime(runtime, False)

    assert runtime.redis_runtime_safety_guard is redis_guard
    assert runtime.redis_runtime_safety_client is redis_client
    assert redis_client.close_calls == 0
    assert lease_guard.closed is False

    redis_guard.running = False
    run_node._cleanup_runtime(runtime, False)

    assert runtime.redis_runtime_safety_guard is None
    assert runtime.redis_runtime_safety_client is None
    assert redis_client.close_calls == 1
    assert lease_guard.closed is True


def test_cleanup_retries_failed_redis_client_after_guard_stops() -> None:
    lease_guard = _LeaseGuard()
    redis_guard = _RedisSafetyGuard()
    redis_client = _RedisClient(close_failures=1)
    runtime = _runtime(
        lease_guard=lease_guard,
        redis_guard=redis_guard,
        redis_client=redis_client,
    )

    with pytest.raises(RuntimeError, match="runtime cleanup failed"):
        run_node._cleanup_runtime(runtime, False)

    assert runtime.redis_runtime_safety_guard is None
    assert runtime.redis_runtime_safety_client is redis_client
    assert redis_client.close_calls == 1
    assert lease_guard.closed is False

    run_node._cleanup_runtime(runtime, False)

    assert runtime.redis_runtime_safety_client is None
    assert redis_client.close_calls == 2
    assert lease_guard.closed is True


def test_dry_run_main_releases_runtime_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_guard = _LeaseGuard()
    runtime = _runtime(lease_guard=lease_guard)
    monkeypatch.setattr(
        run_node,
        "build_account_runtime",
        lambda *args, **kwargs: runtime,
    )

    result = run_node.main(
        [
            "--config",
            "node.json",
            "--dry-run",
            "--build-trading-node",
        ]
    )

    assert result == 0
    assert runtime.control_plane_session is None
    assert runtime.trading_node.stopped is True
    assert runtime.trading_node.disposed is True
    assert lease_guard.closed is True


def test_runtime_failure_starts_session_and_cleans_all_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_guard = _LeaseGuard()
    session = _Session()
    node = _Node(fail_stage="run")
    server = _Server()
    runtime = _runtime(
        lease_guard=lease_guard,
        session=session,
        node=node,
    )
    monkeypatch.setattr(
        run_node,
        "build_account_runtime",
        lambda *args, **kwargs: runtime,
    )
    monkeypatch.setattr(
        run_node,
        "build_health_server",
        lambda *args, **kwargs: server,
    )
    monkeypatch.setattr(
        run_node,
        "run_startup_readiness_checks",
        lambda runtime: None,
    )

    with pytest.raises(RuntimeError, match="node run failed"):
        run_node.main(["--config", "node.json"])

    assert server.served.wait(timeout=1.0)
    assert server.shutdown_called is True
    assert server.closed is True
    assert session.started is True
    assert session.stop_calls == 1
    assert node.built is True
    assert node.ran is True
    assert node.stopped is True
    assert node.disposed is True
    assert lease_guard.closed is True


def _runtime(
    *,
    lease_guard: _LeaseGuard,
    session: _Session | None = None,
    redis_guard: object | None = None,
    redis_client: object | None = None,
    node: _Node | None = None,
) -> SimpleNamespace:
    active_session = session
    if active_session is None:
        active_session = _Session()
    active_node = node
    if active_node is None:
        active_node = _Node()
    return SimpleNamespace(
        config=SimpleNamespace(
            account_id="account-a",
            node_id="node-a",
            binance=SimpleNamespace(environment="testnet"),
        ),
        lifecycle=SimpleNamespace(trading_state="HALTED"),
        control_plane_session=active_session,
        trading_node=active_node,
        background_workers=[],
        redis_runtime_safety_guard=redis_guard,
        redis_runtime_safety_client=redis_client,
        namespace_lease_guard=lease_guard,
    )


def _wait_for(predicate: Any, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class _AttachablePublisher:
    def __init__(self) -> None:
        self._publishers: list[Any] = []

    def attach(self, publisher: Any) -> None:
        self._publishers.append(publisher)

    def publish(self, intent: Any) -> None:
        for publisher in self._publishers:
            publisher.publish(intent)


class _ProgressIntentClient:
    def __init__(self) -> None:
        self._publisher = _AttachablePublisher()
        self.published: list[Any] = []

    def deliver(self, item: Any) -> None:
        self._publisher.publish(item)

    def record_published(self, intent: Any) -> None:
        self.published.append(intent)


class _ProgressControlPlane:
    def __init__(self) -> None:
        self.command_poll_count = 0
        self.command_ack_count = 0
        self._command = NodeCommand(
            command_id="progress-command",
            type=CommandType.HALT,
        )

    def poll_commands(
        self,
        node_id: str,
        after: Any,
    ) -> tuple[NodeCommand, ...]:
        del node_id, after
        self.command_poll_count += 1
        if self.command_ack_count:
            return ()
        return (self._command,)

    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.command_ack_count += 1


class _ProgressLifecycle:
    def __init__(self) -> None:
        self.trading_state = TradingState.ACTIVE
        self.heartbeat_count = 0
        self.failed_dependencies: list[tuple[Any, str]] = []

    def send_heartbeat(self) -> None:
        self.heartbeat_count += 1

    def set_open_orders_provider(self, provider: Any) -> None:
        del provider

    def apply_operator_state(
        self,
        state: TradingState,
        reason: str,
    ) -> None:
        del reason
        self.trading_state = state

    def mark_dependency_ready(self, dependency: Any) -> None:
        del dependency

    def mark_dependency_degraded(
        self,
        dependency: Any,
        reason: str,
    ) -> None:
        del dependency, reason

    def mark_dependency_failed(
        self,
        dependency: Any,
        reason: str,
    ) -> None:
        self.failed_dependencies.append((dependency, reason))


class _ProgressProjection:
    def __init__(self) -> None:
        self.ingested: list[Any] = []
        self.flush_count = 0

    def ingest_event(self, event: Any) -> Any:
        self.ingested.append(event)
        return SimpleNamespace(outcome="DURABLE")

    def flush(self) -> list[str]:
        self.flush_count += 1
        return []

    def halt_egress(self, reason: str) -> None:
        raise AssertionError(reason)
