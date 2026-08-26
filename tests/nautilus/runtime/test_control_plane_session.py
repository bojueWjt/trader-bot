from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from data_client.approved_intent_client import (  # noqa: E402
    ApprovedIntentDataClient,
    JsonIntentOffsetStore,
)
from app.nautilus_actors import (  # noqa: E402
    ExecutionProjectionActor,
    IntentPublisherActor,
)
from app.node import _flush_projection_session_event  # noqa: E402
from execution_domain.contracts import (  # noqa: E402
    ApprovedTradeIntentV1,
    IntentAction,
    RiskBudget,
)
from execution_domain.control_plane import (  # noqa: E402
    IntentAckStatus,
    IntentBatch,
    IntentItem,
    TradingState,
)
from runtime.control_plane_session import (  # noqa: E402
    NodeControlPlaneSession,
    SubmissionResult,
)


NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


def test_snapshot_declares_small_interface_lane_capacities() -> None:
    session = NodeControlPlaneSession()

    health = session.snapshot()

    assert health.started is False
    assert health.process_liveness is False
    assert health.lanes["heartbeat"].capacity == 1
    assert health.lanes["heartbeat"].last_success_age_ms is False
    assert health.lanes["command_poll"].capacity == 1
    assert health.lanes["command_ack"].capacity == 256
    assert health.lanes["intent_fetch"].capacity == 1
    assert health.lanes["intent_delivery"].capacity == 256
    assert health.lanes["execution_event"].capacity == 1024
    assert callable(session.start)
    assert callable(session.stop)
    assert callable(session.submit_execution_event)
    assert callable(session.snapshot)


def test_session_rejects_non_callable_fatal_termination_hook() -> None:
    with pytest.raises(
        TypeError,
        match="fatal termination hook must be callable",
    ):
        NodeControlPlaneSession(fatal_termination_hook="exit")


def test_blocked_intent_fetch_does_not_stall_heartbeat_command_or_ack() -> None:
    intent_fetch_started = Event()
    release_intent_fetch = Event()
    heartbeat_progress = Event()
    command_acknowledged = Event()
    heartbeat_calls = 0
    heartbeat_lock = Lock()
    command_polled = False
    lane_threads: dict[str, int] = {}

    def heartbeat() -> None:
        nonlocal heartbeat_calls
        lane_threads["heartbeat"] = get_ident()
        with heartbeat_lock:
            heartbeat_calls += 1
            if heartbeat_calls >= 2:
                heartbeat_progress.set()

    def command_poll(capacity: int) -> tuple[str, ...]:
        nonlocal command_polled
        del capacity
        lane_threads["command_poll"] = get_ident()
        if command_polled:
            return ()
        command_polled = True
        return ("halt-1",)

    def command_apply(command: str) -> str:
        lane_threads["command_delivery"] = get_ident()
        return f"ack:{command}"

    def command_ack(ack: str) -> None:
        lane_threads["command_ack"] = get_ident()
        assert ack == "ack:halt-1"
        command_acknowledged.set()

    def intent_fetch(capacity: int) -> tuple[Any, ...]:
        del capacity
        lane_threads["intent_fetch"] = get_ident()
        intent_fetch_started.set()
        release_intent_fetch.wait(timeout=1.0)
        return ()

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        command_poll=command_poll,
        command_apply=command_apply,
        command_ack=command_ack,
        intent_fetch=intent_fetch,
        heartbeat_interval_seconds=0.01,
        command_poll_interval_seconds=0.01,
        intent_fetch_interval_seconds=0.01,
    )

    session.start()

    assert intent_fetch_started.wait(timeout=1.0)
    assert heartbeat_progress.wait(timeout=1.0)
    assert command_acknowledged.wait(timeout=1.0)
    assert len(set(lane_threads.values())) == len(lane_threads)

    release_intent_fetch.set()
    assert session.stop(time.monotonic() + 1.0) is True


def test_intent_cursor_publish_and_ack_run_on_delivery_worker(
    tmp_path: Path,
) -> None:
    intent = _intent()
    source = _SingleIntentSource(intent)
    publisher = _ThreadRecordingPublisher()
    offset_store = _ThreadRecordingOffsetStore(tmp_path / "intent-offset.json")
    client = ApprovedIntentDataClient(
        account_id="account-a",
        node_id="node-a",
        source=source,
        publisher=publisher,
        offset_store=offset_store,
        now=lambda: NOW,
        trading_state=lambda: TradingState.ACTIVE,
    )
    session = NodeControlPlaneSession(
        intent_fetch=lambda capacity: client.fetch_once(
            limit=capacity,
            wait_ms=0,
        ),
        intent_deliver=client.deliver,
        intent_fetch_interval_seconds=0.01,
    )

    session.start()

    assert source.acknowledged.wait(timeout=1.0)
    assert client.state.last_cursor == "cursor-1"
    assert source.fetch_thread_id is not None
    assert publisher.thread_id is not None
    assert offset_store.save_thread_id == publisher.thread_id
    assert source.ack_thread_id == publisher.thread_id
    assert source.fetch_thread_id != publisher.thread_id
    assert session.stop(time.monotonic() + 1.0) is True


def test_intent_startup_replay_completes_before_first_fetch() -> None:
    calls: list[str] = []
    fetched = Event()

    def replay() -> int:
        calls.append("replay")
        return 1

    def fetch(capacity: int) -> tuple[Any, ...]:
        del capacity
        calls.append("fetch")
        fetched.set()
        return ()

    session = NodeControlPlaneSession(
        intent_replay=replay,
        intent_fetch=fetch,
        intent_fetch_interval_seconds=0.01,
    )

    session.start()

    assert fetched.wait(timeout=1.0)
    assert calls[0:2] == ["replay", "fetch"]
    assert calls.count("replay") == 1
    assert session.stop(time.monotonic() + 1.0) is True


def test_startup_takeover_heartbeat_completes_before_replay_and_fetch() -> None:
    calls: list[str] = []
    heartbeat_started = Event()
    release_heartbeat = Event()
    replay_started = Event()
    fetched = Event()

    def heartbeat() -> None:
        calls.append("heartbeat_started")
        heartbeat_started.set()
        release_heartbeat.wait(timeout=1.0)
        calls.append("heartbeat_completed")

    def replay() -> int:
        calls.append("replay")
        replay_started.set()
        return 1

    def fetch(capacity: int) -> tuple[Any, ...]:
        del capacity
        calls.append("fetch")
        fetched.set()
        return ()

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        intent_replay=replay,
        intent_fetch=fetch,
        heartbeat_interval_seconds=1.0,
        intent_fetch_interval_seconds=0.01,
    )
    starter = Thread(target=session.start)
    starter.start()

    assert heartbeat_started.wait(timeout=1.0)
    replay_crossed_barrier = replay_started.wait(timeout=0.05)
    fetch_crossed_barrier = fetched.is_set()
    release_heartbeat.set()
    starter.join(timeout=1.0)
    assert starter.is_alive() is False
    assert fetched.wait(timeout=1.0)
    assert replay_crossed_barrier is False
    assert fetch_crossed_barrier is False
    assert calls.index("heartbeat_completed") < calls.index("replay")
    assert calls.index("replay") < calls.index("fetch")
    assert session.stop(time.monotonic() + 1.0) is True


def test_startup_heartbeat_failure_blocks_replay_and_fetch() -> None:
    replayed = Event()
    fetched = Event()
    fatal_reasons: list[str] = []

    def heartbeat() -> None:
        raise RuntimeError("writer takeover rejected")

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        intent_replay=lambda: replayed.set(),
        intent_fetch=lambda _capacity: fetched.set(),
        retry_budget=1,
        fatal_termination_hook=fatal_reasons.append,
    )

    session.start()

    assert _wait_until(lambda: bool(fatal_reasons))
    assert replayed.is_set() is False
    assert fetched.is_set() is False
    assert fatal_reasons == ["writer takeover rejected"]


def test_writer_bootstrap_blocks_network_lanes_until_registration() -> None:
    calls: list[str] = []
    bootstrap_started = Event()
    release_bootstrap = Event()
    heartbeat_sent = Event()
    replayed = Event()
    fetched = Event()

    def writer_bootstrap() -> None:
        calls.append("bootstrap_started")
        bootstrap_started.set()
        release_bootstrap.wait(timeout=1.0)
        calls.append("bootstrap_completed")

    def heartbeat() -> None:
        calls.append("heartbeat")
        heartbeat_sent.set()

    def replay() -> int:
        calls.append("replay")
        replayed.set()
        return 0

    def fetch(capacity: int) -> tuple[Any, ...]:
        del capacity
        calls.append("fetch")
        fetched.set()
        return ()

    session = NodeControlPlaneSession(
        writer_bootstrap=writer_bootstrap,
        heartbeat=heartbeat,
        intent_replay=replay,
        intent_fetch=fetch,
        heartbeat_interval_seconds=0.01,
        intent_fetch_interval_seconds=0.01,
    )
    session.start()

    assert bootstrap_started.wait(timeout=1.0)
    time.sleep(0.05)
    assert heartbeat_sent.is_set() is False
    assert replayed.is_set() is False
    assert fetched.is_set() is False

    release_bootstrap.set()

    assert heartbeat_sent.wait(timeout=1.0)
    assert replayed.wait(timeout=1.0)
    assert fetched.wait(timeout=1.0)
    bootstrap_completed = calls.index("bootstrap_completed")
    assert bootstrap_completed < calls.index("heartbeat")
    assert bootstrap_completed < calls.index("replay")
    assert bootstrap_completed < calls.index("fetch")
    assert session.stop(time.monotonic() + 1.0) is True


def test_startup_heartbeat_hard_deadline_triggers_fatal_fence() -> None:
    heartbeat_started = Event()
    release_heartbeat = Event()
    fatal_reasons: list[str] = []

    def heartbeat() -> None:
        heartbeat_started.set()
        release_heartbeat.wait()

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        retry_budget=1,
        operation_timeout_seconds=0.02,
        fatal_termination_hook=fatal_reasons.append,
    )

    started_at = time.monotonic()
    session.start()
    start_elapsed = time.monotonic() - started_at

    assert start_elapsed < 0.01
    assert heartbeat_started.wait(timeout=1.0)
    assert _wait_until(
        lambda: bool(fatal_reasons),
        timeout=0.3,
    )
    health = session.snapshot()
    assert health.process_liveness is False
    assert health.lanes["heartbeat"].fatal_failure
    assert fatal_reasons == [
        "heartbeat operation exceeded 0.020s deadline"
    ]

    release_heartbeat.set()
    assert _wait_until(
        lambda: session._startup_failure
        == "heartbeat operation exceeded 0.020s deadline"
    )


def test_startup_replay_waits_for_actor_without_blocking_node_start() -> None:
    client = _ReplayIntentClient()
    replay_completed = Event()
    session = NodeControlPlaneSession(
        intent_replay=lambda: client.replay_pending(
            replay_completed
        ),
    )
    actor = _ReplayIntentActor(client, session)

    started_at = time.monotonic()
    session.start()
    start_elapsed = time.monotonic() - started_at

    assert start_elapsed < 0.01
    assert client.replay_started.wait(timeout=1.0)
    assert replay_completed.is_set() is False

    actor.on_start()
    deadline = time.monotonic() + 1.0
    while not replay_completed.is_set() and time.monotonic() < deadline:
        actor._on_poll_timer()
        time.sleep(0.001)

    assert replay_completed.is_set()
    assert actor.published == ["replayed-intent"]
    assert session.stop(time.monotonic() + 1.0) is True
    actor.on_stop()


def test_execution_event_submission_is_non_blocking_and_backpressured() -> None:
    sink_started = Event()
    release_sink = Event()
    received: list[str] = []

    def sink(event: str) -> None:
        received.append(event)
        sink_started.set()
        release_sink.wait(timeout=1.0)

    session = NodeControlPlaneSession(
        execution_event_sink=sink,
        execution_event_capacity=1,
    )
    session.start()

    started_at = time.monotonic()
    first = session.submit_execution_event("event-1")
    elapsed = time.monotonic() - started_at

    assert first is SubmissionResult.ACCEPTED
    assert elapsed < 0.01
    assert sink_started.wait(timeout=1.0)
    assert session.submit_execution_event("event-2") is SubmissionResult.ACCEPTED
    assert (
        session.submit_execution_event("event-3")
        is SubmissionResult.BACKPRESSURED
    )

    release_sink.set()
    assert _wait_until(lambda: received == ["event-1", "event-2"])
    assert session.stop(time.monotonic() + 1.0) is True


def test_stop_drains_accepted_execution_events_before_deadline() -> None:
    first_started = Event()
    release_first = Event()
    received: list[str] = []
    stop_results: list[bool] = []

    def sink(event: str) -> None:
        received.append(event)
        if event == "event-1":
            first_started.set()
            release_first.wait(timeout=1.0)

    session = NodeControlPlaneSession(
        execution_event_sink=sink,
        execution_event_capacity=2,
    )
    session.start()
    assert session.submit_execution_event("event-1") is SubmissionResult.ACCEPTED
    assert first_started.wait(timeout=1.0)
    assert session.submit_execution_event("event-2") is SubmissionResult.ACCEPTED

    stopper = Thread(
        target=lambda: stop_results.append(
            session.stop(time.monotonic() + 1.0)
        )
    )
    stopper.start()
    release_first.set()
    stopper.join(timeout=1.0)

    assert stopper.is_alive() is False
    assert stop_results == [True]
    assert received == ["event-1", "event-2"]


def test_lane_retry_budget_uses_exponential_backoff_and_opens_circuit() -> None:
    attempts: list[float] = []
    failures: list[tuple[str, str]] = []
    heartbeat_calls = 0

    def heartbeat() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            return
        attempts.append(time.monotonic())
        raise TimeoutError("heartbeat deadline exceeded")

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        heartbeat_interval_seconds=1.0,
        retry_budget=3,
        retry_base_delay_seconds=0.005,
        retry_max_delay_seconds=0.02,
        retry_jitter_ratio=0.0,
        circuit_reset_seconds=1.0,
        failure_callback=lambda lane, reason: failures.append(
            (lane, reason)
        ),
    )
    session.start()

    assert _wait_until(
        lambda: session.snapshot().lanes["heartbeat"].circuit_state
        == "open"
    )
    lane = session.snapshot().lanes["heartbeat"]

    assert len(attempts) == 3
    assert attempts[1] - attempts[0] >= 0.004
    assert attempts[2] - attempts[1] >= 0.009
    assert lane.retry_count == 2
    assert lane.timeout_count == 3
    assert lane.error_count == 3
    assert lane.circuit_open_count == 1
    assert lane.consecutive_failures == 3
    assert failures == []
    assert session.stop(time.monotonic() + 1.0) is True


def test_transient_http_500_does_not_halt_before_stream_failure_budget() -> None:
    failures: list[tuple[str, str]] = []
    heartbeat_calls = 0
    http_500 = RuntimeError(
        "GET /v1/nodes/nautilus-node-account-a/intents"
        "?account_id=account-a failed with HTTP 500: Internal Server Error"
    )

    def heartbeat() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            return
        raise http_500

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        heartbeat_interval_seconds=1.0,
        retry_budget=3,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.002,
        retry_jitter_ratio=0.0,
        circuit_reset_seconds=1.0,
        stream_failure_halt_after_seconds=30.0,
        failure_callback=lambda lane, reason: failures.append(
            (lane, reason)
        ),
    )
    session.start()

    assert _wait_until(
        lambda: session.snapshot().lanes["heartbeat"].circuit_state
        == "open"
    )
    assert failures == []
    assert session.stop(time.monotonic() + 1.0) is True


def test_transient_http_500_halts_after_stream_failure_budget() -> None:
    failures: list[tuple[str, str]] = []
    heartbeat_calls = 0
    http_500 = RuntimeError(
        "GET /v1/nodes/nautilus-node-account-a/intents"
        "?account_id=account-a failed with HTTP 500: Internal Server Error"
    )

    def heartbeat() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            return
        raise http_500

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        heartbeat_interval_seconds=1.0,
        retry_budget=1,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0.0,
        circuit_reset_seconds=1.0,
        stream_failure_halt_after_seconds=0.0,
        failure_callback=lambda lane, reason: failures.append(
            (lane, reason)
        ),
    )
    session.start()

    assert _wait_until(lambda: bool(failures))
    assert failures == [("heartbeat", str(http_500))]
    assert session.stop(time.monotonic() + 1.0) is True


def test_lane_hard_deadline_triggers_fatal_termination_and_fast_stop() -> None:
    heartbeat_started = Event()
    release_heartbeat = Event()
    failures: list[tuple[str, str]] = []
    fatal_reasons: list[str] = []
    heartbeat_calls = 0

    def heartbeat() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            return
        heartbeat_started.set()
        release_heartbeat.wait()

    session = NodeControlPlaneSession(
        heartbeat=heartbeat,
        heartbeat_interval_seconds=1.0,
        retry_budget=1,
        circuit_reset_seconds=1.0,
        operation_timeout_seconds=0.02,
        failure_callback=lambda lane, reason: failures.append(
            (lane, reason)
        ),
        fatal_termination_hook=fatal_reasons.append,
    )
    session.start()

    assert heartbeat_started.wait(timeout=1.0)
    assert _wait_until(
        lambda: bool(fatal_reasons),
        timeout=0.3,
    )
    health = session.snapshot()
    lane = health.lanes["heartbeat"]
    assert lane.timeout_count == 1
    assert lane.error_count == 1
    assert lane.in_flight_age_ms
    assert lane.fatal_failure
    assert health.process_liveness is False
    assert health.ready is False
    assert fatal_reasons == [
        "heartbeat operation exceeded 0.020s deadline"
    ]
    assert failures == [
        (
            "heartbeat",
            "heartbeat operation exceeded 0.020s deadline",
        )
    ]
    time.sleep(0.03)
    assert len(fatal_reasons) == 1

    started_at = time.monotonic()
    assert session.stop(time.monotonic() + 1.0) is False
    assert time.monotonic() - started_at < 0.1

    release_heartbeat.set()


def test_action_returning_after_hard_deadline_triggers_fatal_fence() -> None:
    fatal_reasons: list[str] = []
    session = NodeControlPlaneSession(
        retry_budget=1,
        operation_timeout_seconds=0.01,
        fatal_termination_hook=fatal_reasons.append,
    )
    lane = session._lanes["heartbeat"]

    succeeded = session._execute_with_retry(
        lane,
        lambda: time.sleep(0.02),
        drain_on_stop=False,
    )

    assert succeeded is False
    assert fatal_reasons == [
        "heartbeat operation exceeded 0.010s deadline"
    ]
    health = lane.snapshot(time.monotonic())
    assert health.fatal_failure
    assert health.timeout_count == 1


def test_execution_queue_reports_degraded_then_fatal_at_capacity() -> None:
    sink_started = Event()
    release_sink = Event()
    failures: list[tuple[str, str]] = []

    def sink(event: str) -> None:
        del event
        sink_started.set()
        release_sink.wait(timeout=1.0)

    session = NodeControlPlaneSession(
        execution_event_sink=sink,
        execution_event_capacity=5,
        queue_degraded_ratio=0.8,
        failure_callback=lambda lane, reason: failures.append(
            (lane, reason)
        ),
    )
    session.start()
    assert session.submit_execution_event("worker-blocker") is SubmissionResult.ACCEPTED
    assert sink_started.wait(timeout=1.0)

    for index in range(4):
        assert (
            session.submit_execution_event(f"queued-{index}")
            is SubmissionResult.ACCEPTED
        )

    degraded = session.snapshot().lanes["execution_event"]
    assert degraded.queue_depth == 4
    assert degraded.queue_pressure == "degraded"
    assert degraded.queue_usage_ratio == 0.8
    assert failures == []

    assert session.submit_execution_event("queue-full") is SubmissionResult.ACCEPTED
    full = session.snapshot().lanes["execution_event"]
    assert full.queue_depth == 5
    assert full.queue_pressure == "full"
    assert full.fatal_failure
    assert failures == [
        (
            "execution_event",
            "execution_event queue capacity exceeded",
        )
    ]

    assert (
        session.submit_execution_event("rejected-after-full")
        is SubmissionResult.BACKPRESSURED
    )
    rejected = session.snapshot().lanes["execution_event"]
    assert rejected.fatal_failure
    assert failures == [
        (
            "execution_event",
            "execution_event queue capacity exceeded",
        )
    ]
    assert (
        session.submit_execution_event("rejected-after-halt")
        is SubmissionResult.BACKPRESSURED
    )

    release_sink.set()
    assert session.stop(time.monotonic() + 1.0) is True


def test_session_startup_wake_drains_every_projection_spool_batch() -> None:
    projection = _BatchProjection(pending_count=3)
    progress: list[bool] = []
    runtime = SimpleNamespace(
        projection_actor=projection,
        projection_egress_progress_callback=lambda: progress.append(True),
    )
    session = NodeControlPlaneSession(
        execution_event_sink=lambda event: _flush_projection_session_event(
            runtime,
            event,
        ),
        execution_event_capacity=1,
    )
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
    )

    actor.on_start()

    assert _wait_until(lambda: projection.spool.pending_count == 0)
    assert projection.flush_calls == 3
    assert progress
    actor.on_stop()


def _intent() -> ApprovedTradeIntentV1:
    intent_id = uuid4()
    return ApprovedTradeIntentV1(
        schema_version="1.0",
        intent_id=intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        account_id="account-a",
        instrument_id="BTCUSDT-PERP.BINANCE",
        action=IntentAction.ADD_POSITION,
        order_plan={"type": "market", "side": "buy", "quantity": "0.001"},
        risk_budget=RiskBudget(
            risk_fraction=0.01,
            max_notional=100.0,
            max_leverage=2.0,
        ),
        valid_until=NOW + timedelta(minutes=5),
        idempotency_key=sha256(str(intent_id).encode("ascii")).hexdigest(),
        approved_at=NOW - timedelta(seconds=5),
    )


class _SingleIntentSource:
    def __init__(self, intent: ApprovedTradeIntentV1) -> None:
        self._intent = intent
        self.fetch_thread_id: int | None = None
        self.ack_thread_id: int | None = None
        self.acknowledged = Event()

    def fetch_intents(
        self,
        account_id: str,
        after_cursor: str | None,
        limit: int,
        wait_ms: int = 0,
    ) -> IntentBatch:
        del account_id, limit, wait_ms
        self.fetch_thread_id = get_ident()
        if after_cursor is not None:
            return IntentBatch(items=[], next_cursor=after_cursor)
        return IntentBatch(
            items=[IntentItem(cursor="cursor-1", intent=self._intent)],
            next_cursor="cursor-1",
        )

    def ack_intent(
        self,
        account_id: str,
        node_id: str,
        intent_id: Any,
        status: IntentAckStatus,
        detail: str | None = None,
    ) -> None:
        del account_id, node_id, intent_id, detail
        assert status is IntentAckStatus.ACCEPTED
        self.ack_thread_id = get_ident()
        self.acknowledged.set()


class _ThreadRecordingPublisher:
    def __init__(self) -> None:
        self.thread_id: int | None = None

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        del intent
        self.thread_id = get_ident()


class _ThreadRecordingOffsetStore(JsonIntentOffsetStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.save_thread_id: int | None = None

    def save(self, state: Any) -> None:
        self.save_thread_id = get_ident()
        super().save(state)


class _BatchProjection:
    def __init__(self, pending_count: int) -> None:
        self.spool = SimpleNamespace(pending_count=pending_count)
        self.flush_calls = 0

    def ingest_event(self, event: Any) -> None:
        del event

    def flush(self) -> list[str]:
        self.flush_calls += 1
        if self.spool.pending_count <= 0:
            return []
        self.spool.pending_count -= 1
        return [f"event-{self.flush_calls}"]


class _ReplayPublisherFanout:
    def __init__(self) -> None:
        self._publishers: list[Any] = []

    def attach(self, publisher: Any) -> None:
        self._publishers.append(publisher)

    def publish(self, intent: Any) -> None:
        for publisher in self._publishers:
            publisher.publish(intent)


class _ReplayIntentClient:
    def __init__(self) -> None:
        self._publisher = _ReplayPublisherFanout()
        self.replay_started = Event()

    def replay_pending(self, completed: Event) -> int:
        self.replay_started.set()
        self._publisher.publish("replayed-intent")
        completed.set()
        return 1


class _ReplayIntentActor(IntentPublisherActor):
    def __init__(
        self,
        client: _ReplayIntentClient,
        session: NodeControlPlaneSession,
    ) -> None:
        super().__init__(
            client,
            control_plane_session=session,
            manage_control_plane_session=False,
        )
        self.published: list[Any] = []

    def publish(self, intent: Any) -> None:
        self.published.append(intent)


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())
