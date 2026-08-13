from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.nautilus_actors import CommandPollerActor  # noqa: E402
from app.node import (  # noqa: E402
    ACTOR_WATCHDOG_EXIT_CODE,
    _build_runtime_fatal_callback,
)
from execution_domain.contracts import ReconciliationState  # noqa: E402
from execution_domain.control_plane import (  # noqa: E402
    CommandType,
    NodeCommand,
    TradingState,
)
from persistence.redis_namespace_lease import (  # noqa: E402
    NamespaceLeaseGuard,
    RedisNamespaceLeaseLost,
)
from runtime.lifecycle import DependencyName, NodeLifecycle  # noqa: E402
from runtime.reconciliation import (  # noqa: E402
    ReconciliationDatasetSummary,
    ReconciliationProof,
)


LEASE_REFRESH_INTERVAL_SECONDS = 60.0
MAX_CALLBACK_SECONDS = 0.1
REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"


class _Lease:
    def __init__(
        self,
        events: list[str],
        *,
        acquire_error: Exception | None = None,
        refresh_error: Exception | None = None,
        block_refresh: bool = False,
        fencing_token: int = 1,
        redis_fencing_epoch: str = REDIS_FENCING_EPOCH,
    ) -> None:
        self._events = events
        self._acquire_error = acquire_error
        self._refresh_error = refresh_error
        self._block_refresh = block_refresh
        self._fencing_token = fencing_token
        self._redis_fencing_epoch = redis_fencing_epoch
        self.refresh_started = Event()
        self.refresh_release = Event()

    def acquire(self) -> Any:
        self._events.append("lease.acquire")
        if self._acquire_error is not None:
            raise self._acquire_error
        return SimpleNamespace(
            redis_fencing_epoch=self._redis_fencing_epoch,
            fencing_token=self._fencing_token,
        )

    def refresh(self) -> Any:
        self._events.append("lease.refresh")
        self.refresh_started.set()
        if self._block_refresh:
            self.refresh_release.wait(timeout=1.0)
        if self._refresh_error is not None:
            raise self._refresh_error
        return SimpleNamespace(
            redis_fencing_epoch=self._redis_fencing_epoch,
            fencing_token=self._fencing_token,
        )

    def release(self) -> None:
        self._events.append("lease.release")


class _ControlPlane:
    def __init__(self) -> None:
        self.heartbeat_calls = 0
        self.command_calls = 0

    def heartbeat(self, node_id: str, heartbeat: Any) -> None:
        del node_id, heartbeat
        self.heartbeat_calls += 1

    def poll_commands(
        self,
        node_id: str,
        after_sequence: int | None,
    ) -> tuple[Any, ...]:
        del node_id, after_sequence
        self.command_calls += 1
        return ()

    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class _BlockingHeartbeatControlPlane(_ControlPlane):
    def __init__(self) -> None:
        super().__init__()
        self.heartbeat_started = Event()
        self.heartbeat_release = Event()

    def heartbeat(self, node_id: str, heartbeat: Any) -> None:
        del node_id, heartbeat
        self.heartbeat_started.set()
        self.heartbeat_release.wait(timeout=1.0)
        self.heartbeat_calls += 1


class _HeartbeatCaptureControlPlane(_ControlPlane):
    def __init__(self) -> None:
        super().__init__()
        self.heartbeats: list[Any] = []

    def heartbeat(self, node_id: str, heartbeat: Any) -> None:
        del node_id
        self.heartbeats.append(heartbeat)
        self.heartbeat_calls += 1


class _FailingEvidenceProvider:
    def snapshot(self) -> dict[str, Any]:
        raise RuntimeError("Binance 429 backoff active")


class _Lifecycle:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.ready_dependencies: list[Any] = []
        self.failed_dependencies: list[tuple[Any, str]] = []
        self.open_orders_provider: Any = None

    def set_open_orders_provider(self, provider: Any) -> None:
        self._events.append("lifecycle.open_orders_provider")
        self.open_orders_provider = provider

    def build_heartbeat(self) -> object:
        return object()

    def mark_dependency_ready(self, dependency: Any) -> None:
        self.ready_dependencies.append(dependency)

    def mark_dependency_failed(self, dependency: Any, reason: str) -> None:
        self.failed_dependencies.append((dependency, reason))


class _RiskLifecycle(_Lifecycle):
    runtime_generation = "runtime-a"
    reconciliation_generation = 3

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.redis_fencing_epoch = ""
        self.lease_generation = 0
        self.applied_states: list[TradingState] = []

    def configure_lease(
        self,
        *,
        redis_fencing_epoch: str,
        generation: int,
        freshness_seconds: float,
    ) -> None:
        del freshness_seconds
        self.redis_fencing_epoch = redis_fencing_epoch
        self.lease_generation = generation
        self._events.append(f"lease.configured:{generation}")

    def record_lease_refresh(
        self,
        *,
        redis_fencing_epoch: str,
        generation: int,
    ) -> None:
        assert redis_fencing_epoch == self.redis_fencing_epoch
        assert generation == self.lease_generation
        self._events.append(f"lease.recorded:{generation}")

    def validate_risk_generation(
        self,
        *,
        runtime_generation: str,
        reconciliation_generation: int,
        lease_generation: int,
    ) -> None:
        assert runtime_generation == self.runtime_generation
        assert reconciliation_generation == self.reconciliation_generation
        assert lease_generation == self.lease_generation

    def apply_operator_state(self, state: TradingState, reason: str) -> None:
        del reason
        self.applied_states.append(state)


class _Watchdog:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def start(self) -> None:
        self._events.append("watchdog.start")

    def record_tick(self) -> None:
        self._events.append("watchdog.tick")

    def stop(self) -> None:
        self._events.append("watchdog.stop")


def test_on_start_acquires_lease_before_runtime_lanes_and_on_stop_releases() -> None:
    events: list[str] = []
    lifecycle = _Lifecycle(events)
    actor = _actor(
        lifecycle=lifecycle,
        lease=_Lease(events),
        watchdog=_Watchdog(events),
    )

    actor.on_start()

    assert events[0] == "lease.acquire"
    assert events.count("lease.acquire") == 1
    assert DependencyName.REDIS in lifecycle.ready_dependencies
    assert events.index("lease.acquire") < events.index("watchdog.start")

    actor.on_stop()

    assert events.count("lease.release") == 1
    assert events.index("watchdog.stop") < events.index("lease.release")


def test_process_guard_is_shared_without_actor_reacquire_or_release() -> None:
    events: list[str] = []
    lease = _Lease(events)
    guard = NamespaceLeaseGuard(
        lease,
        refresh_interval_seconds=LEASE_REFRESH_INTERVAL_SECONDS,
    )
    guard.acquire()
    actor = _actor(
        lifecycle=_Lifecycle(events),
        lease=guard,
        watchdog=_Watchdog(events),
    )

    actor.on_start()
    actor.on_stop()

    assert events.count("lease.acquire") == 1
    assert events.count("lease.release") == 0
    assert actor._lease_executor is None

    guard.close()

    assert events.count("lease.release") == 1


def test_on_start_acquire_failure_marks_redis_failed_and_aborts_startup() -> None:
    events: list[str] = []
    lifecycle = _Lifecycle(events)
    actor = _actor(
        lifecycle=lifecycle,
        lease=_Lease(events, acquire_error=RedisNamespaceLeaseLost("held")),
        watchdog=_Watchdog(events),
    )

    with pytest.raises(
        RuntimeError,
        match="Redis namespace lease acquisition failed",
    ):
        actor.on_start()

    assert events == ["lease.acquire"]
    assert lifecycle.failed_dependencies == [
        (
            DependencyName.REDIS,
            "Redis namespace lease acquisition failed",
        )
    ]


def test_heartbeat_lane_refreshes_lease_at_sixty_second_boundary() -> None:
    events: list[str] = []
    lease = _Lease(events)
    actor = _actor(
        lifecycle=_Lifecycle(events),
        lease=lease,
        watchdog=_Watchdog(events),
    )
    actor.on_start()

    actor._last_namespace_lease_refresh_at = (
        time.monotonic() - LEASE_REFRESH_INTERVAL_SECONDS + 1.0
    )
    actor._on_poll_timer()
    assert _wait_until(_heartbeat_done(actor))
    assert events.count("lease.refresh") == 0

    actor._last_namespace_lease_refresh_at = (
        time.monotonic() - LEASE_REFRESH_INTERVAL_SECONDS - 0.01
    )
    actor._on_poll_timer()
    assert _wait_until(_heartbeat_done(actor))

    try:
        assert events.count("lease.refresh") == 1
    finally:
        actor.on_stop()


@pytest.mark.parametrize(
    "refresh_error",
    [
        RuntimeError("redis unavailable"),
        RedisNamespaceLeaseLost("fenced"),
    ],
    ids=["redis-failure", "fencing-loss"],
)
def test_refresh_failure_halts_sticky_marks_redis_failed_and_exits_75(
    refresh_error: Exception,
) -> None:
    events: list[str] = []
    lease = _Lease(events, refresh_error=refresh_error)
    lifecycle = _active_lifecycle()
    exit_codes: list[int] = []
    fatal_reasons: list[str] = []
    fatal = _build_runtime_fatal_callback(exit_process=exit_codes.append)

    def record_fatal(reason: str) -> None:
        fatal_reasons.append(reason)
        fatal(reason)

    actor = _actor(
        lifecycle=lifecycle,
        lease=lease,
        watchdog=_Watchdog(events),
        namespace_lease_lost_callback=record_fatal,
    )
    actor.on_start()
    actor._last_namespace_lease_refresh_at = (
        time.monotonic() - LEASE_REFRESH_INTERVAL_SECONDS
    )

    actor._on_poll_timer()
    assert _wait_until(_heartbeat_done(actor))
    actor._on_poll_timer()

    try:
        reason = f"Redis namespace lease refresh failed: {refresh_error}"
        assert lifecycle.trading_state is TradingState.HALTED
        assert lifecycle.halt_reason == f"redis failed: {reason}"
        assert DependencyName.REDIS in lifecycle.readiness.missing
        assert actor._stopped.is_set()
        assert fatal_reasons == [reason]
        assert exit_codes == [ACTOR_WATCHDOG_EXIT_CODE]

        lifecycle.mark_dependency_ready(DependencyName.REDIS)

        assert lifecycle.trading_state is TradingState.HALTED
        assert lifecycle.halt_reason == f"redis failed: {reason}"
    finally:
        actor.on_stop()


def test_blocked_heartbeat_does_not_delay_namespace_lease_refresh() -> None:
    events: list[str] = []
    lease = _Lease(events)
    control_plane = _BlockingHeartbeatControlPlane()
    actor = _actor(
        lifecycle=_Lifecycle(events),
        lease=lease,
        watchdog=_Watchdog(events),
        control_plane=control_plane,
    )
    actor.on_start()
    actor._last_namespace_lease_refresh_at = (
        time.monotonic() - LEASE_REFRESH_INTERVAL_SECONDS
    )

    actor._on_poll_timer()

    assert control_plane.heartbeat_started.wait(timeout=1.0)
    assert lease.refresh_started.wait(timeout=1.0)
    assert _wait_until(lambda: actor._lease_future is not None)
    assert _wait_until(lambda: bool(actor._lease_future.done()))
    assert actor._heartbeat_future is not None
    assert actor._heartbeat_future.done() is False

    control_plane.heartbeat_release.set()
    actor._on_poll_timer()
    actor.on_stop()


def test_successful_lease_refresh_cannot_clear_redis_runtime_safety_failure() -> None:
    events: list[str] = []
    lease = _Lease(events)
    lifecycle = _active_lifecycle()
    actor = _actor(
        lifecycle=lifecycle,
        lease=lease,
        watchdog=_Watchdog(events),
    )
    actor.on_start()
    lifecycle.mark_dependency_failed(
        DependencyName.REDIS,
        "Redis runtime safety stream byte limit exceeded",
    )
    actor._last_namespace_lease_refresh_at = (
        time.monotonic() - LEASE_REFRESH_INTERVAL_SECONDS
    )

    actor._on_poll_timer()
    assert lease.refresh_started.wait(timeout=1.0)
    assert _wait_until(
        lambda: actor._lease_future is not None
        and actor._lease_future.done()
    )
    actor._on_poll_timer()

    try:
        assert lifecycle.restart_required is True
        assert DependencyName.REDIS in lifecycle.readiness.missing
        with pytest.raises(RuntimeError, match="restart is required"):
            lifecycle.apply_operator_state(
                TradingState.ACTIVE,
                reason="operator resume",
            )
    finally:
        actor.on_stop()


def test_exchange_evidence_failure_still_sends_fail_closed_heartbeat() -> None:
    events: list[str] = []
    lifecycle = _Lifecycle(events)
    control_plane = _HeartbeatCaptureControlPlane()
    actor = _actor(
        lifecycle=lifecycle,
        lease=_Lease(events),
        watchdog=_Watchdog(events),
        control_plane=control_plane,
        exchange_evidence_provider=_FailingEvidenceProvider(),
    )
    actor.on_start()

    actor._on_poll_timer()
    assert _wait_until(_heartbeat_done(actor))
    actor._harvest_heartbeat()

    try:
        assert control_plane.heartbeat_calls == 1
        assert len(control_plane.heartbeats) == 1
        assert actor._last_heartbeat_success_at is not None
        # A transient snapshot miss must NOT fail the reconciliation
        # dependency: that destroys the completed reconciliation proof
        # (irrecoverable without a fresh reconciliation). Fail-closed
        # halting is owned by the control-plane 409 path instead.
        assert lifecycle.failed_dependencies == []
        assert actor._exchange_evidence_available is False
        assert (
            actor._exchange_evidence_failure_reason
            == "exchange evidence unavailable: Binance 429 backoff active"
        )
    finally:
        actor.on_stop()


def test_blocking_lease_refresh_keeps_actor_timer_callback_bounded() -> None:
    events: list[str] = []
    lease = _Lease(events, block_refresh=True)
    actor = _actor(
        lifecycle=_Lifecycle(events),
        lease=lease,
        watchdog=_Watchdog(events),
    )
    actor.on_start()
    actor._last_namespace_lease_refresh_at = (
        time.monotonic() - LEASE_REFRESH_INTERVAL_SECONDS
    )

    started_at = time.monotonic()
    actor._on_poll_timer()
    elapsed = time.monotonic() - started_at

    assert lease.refresh_started.wait(timeout=1.0)
    assert elapsed < MAX_CALLBACK_SECONDS

    lease.refresh_release.set()
    assert _wait_until(_heartbeat_done(actor))
    actor.on_stop()


def test_due_lease_refresh_fences_before_pending_resume_is_applied() -> None:
    events: list[str] = []
    lease = _Lease(events, block_refresh=True, fencing_token=19)
    lifecycle = _RiskLifecycle(events)
    actor = _actor(
        lifecycle=lifecycle,
        lease=lease,
        watchdog=_Watchdog(events),
    )
    actor.on_start()
    actor._pending_commands = (
        NodeCommand(
            command_id="resume-after-pause",
            type=CommandType.RESUME,
            issued_at=datetime.now(timezone.utc),
        ),
    )
    actor._last_namespace_lease_refresh_at = (
        time.monotonic() - LEASE_REFRESH_INTERVAL_SECONDS
    )

    actor._on_poll_timer()

    assert lease.refresh_started.wait(timeout=1.0)
    assert lifecycle.applied_states == []

    lease.refresh_release.set()
    assert _wait_until(lambda: bool(actor._lease_future.done()))
    actor._on_poll_timer()

    assert lifecycle.applied_states == [TradingState.ACTIVE]
    assert events.index("lease.recorded:19") < len(events)
    actor.on_stop()


def test_shutdown_timeout_retains_lease_and_triggers_fatal_fence() -> None:
    events: list[str] = []
    lease = _Lease(events)
    control_plane = _BlockingHeartbeatControlPlane()
    fatal_reasons: list[str] = []
    actor = _actor(
        lifecycle=_Lifecycle(events),
        lease=lease,
        watchdog=_Watchdog(events),
        control_plane=control_plane,
        fatal_callback=fatal_reasons.append,
        worker_shutdown_wait_seconds=0.02,
    )
    actor.on_start()
    actor._on_poll_timer()
    assert control_plane.heartbeat_started.wait(timeout=1.0)

    started_at = time.monotonic()
    actor.on_stop()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.08
    assert "lease.release" not in events
    assert len(fatal_reasons) == 1
    assert "shutdown deadline" in fatal_reasons[0]

    control_plane.heartbeat_release.set()


def test_command_ack_batch_has_hard_upper_bound() -> None:
    events: list[str] = []
    actor = CommandPollerActor(
        control_plane=_ControlPlane(),
        lifecycle=_Lifecycle(events),
        node_id="node-a",
        account_id="account-a",
        max_pending_acks=10_000,
        ack_batch_size=10_000,
    )

    assert actor._ack_batch_size == 64
    actor.on_stop()


def _actor(
    *,
    lifecycle: Any,
    lease: _Lease,
    watchdog: _Watchdog,
    namespace_lease_lost_callback: Any = None,
    control_plane: Any = None,
    fatal_callback: Any = None,
    worker_shutdown_wait_seconds: float = 0.5,
    exchange_evidence_provider: Any = None,
) -> CommandPollerActor:
    selected_control_plane = control_plane
    if selected_control_plane is None:
        selected_control_plane = _ControlPlane()
    actor = CommandPollerActor(
        control_plane=selected_control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        namespace_lease=lease,
        namespace_lease_refresh_interval_seconds=(
            LEASE_REFRESH_INTERVAL_SECONDS
        ),
        namespace_lease_lost_callback=namespace_lease_lost_callback,
        fatal_callback=fatal_callback,
        worker_shutdown_wait_seconds=worker_shutdown_wait_seconds,
        exchange_evidence_provider=exchange_evidence_provider,
    )
    actor._tick_watchdog = watchdog
    return actor


def _active_lifecycle() -> NodeLifecycle:
    config = SimpleNamespace(
        account_id="account-a",
        node_id="node-a",
        reconciliation=SimpleNamespace(interval_mins=5),
    )
    lifecycle = NodeLifecycle(config=config, release_id="release-a")
    for dependency in DependencyName:
        if dependency is DependencyName.RECONCILIATION:
            continue
        lifecycle.mark_dependency_ready(dependency)
    lifecycle.record_reconciliation_proof(
        ReconciliationProof(
            account_id=config.account_id,
            node_id=config.node_id,
            release_id="release-a",
            state=ReconciliationState.HEALTHY,
            orders=ReconciliationDatasetSummary.from_records([]),
            positions=ReconciliationDatasetSummary.from_records([]),
            fills=ReconciliationDatasetSummary.from_records([]),
            completed_at=datetime.now(timezone.utc),
        )
    )
    lifecycle.apply_operator_state(TradingState.ACTIVE, "test setup")
    return lifecycle


def _heartbeat_done(actor: CommandPollerActor):
    def done() -> bool:
        future = actor._heartbeat_future
        return future is not None and future.done()

    return done


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())
