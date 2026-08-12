from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.nautilus_actors import (  # noqa: E402
    CommandPollerActor,
    ExecutionProjectionActor,
    IntentPublisherActor,
)
from runtime.control_plane_session import (  # noqa: E402
    NodeControlPlaneSession,
    SubmissionResult,
)
from execution_domain.control_plane import (  # noqa: E402
    CommandType,
    NodeCommand,
    TradingState,
)
from projection.actor import (  # noqa: E402
    ProjectionActor,
    ProjectionIngestOutcome,
    ProjectionIngestResult,
)
from projection.event_mapper import ProjectionConfig  # noqa: E402
from projection.spool import JsonExecutionSpool  # noqa: E402


SLOW_ITEM_SECONDS = 0.05
MAX_CALLBACK_SECONDS = 0.01


class _IntentClient:
    def __init__(self) -> None:
        self.poll_calls = 0

    def poll_once(self, *, limit: int, wait_ms: int) -> int:
        del limit, wait_ms
        self.poll_calls += 1
        return 0


class _PublisherFanout:
    def __init__(self) -> None:
        self._publishers: list[Any] = []

    def attach(self, publisher: Any) -> None:
        self._publishers.append(publisher)

    def publish(self, intent: Any) -> None:
        for publisher in self._publishers:
            publisher.publish(intent)


class _SessionIntentClient:
    def __init__(self) -> None:
        self._publisher = _PublisherFanout()
        self.delivery_thread_id: int | None = None

    def deliver(self, intent: Any) -> None:
        self.delivery_thread_id = get_ident()
        self._publisher.publish(intent)


class _RecordingSession:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.execution_events: list[Any] = []

    def start(self) -> None:
        self.started = True

    def stop(self, deadline: float) -> bool:
        del deadline
        self.stopped = True
        return True

    def submit_execution_event(self, event: Any) -> str:
        self.execution_events.append(event)
        return "accepted"

    def snapshot(self) -> object:
        return SimpleNamespace()


class _BackpressuredSession(_RecordingSession):
    def submit_execution_event(self, event: Any) -> SubmissionResult:
        self.execution_events.append(event)
        return SubmissionResult.BACKPRESSURED


class _Lifecycle:
    def __init__(self) -> None:
        self.runtime_generation = "runtime-a"
        self.reconciliation_generation = 7
        self.lease_generation = 11
        self.applied_states: list[TradingState] = []

    def record_actor_tick(self) -> None:
        return

    def apply_operator_state(self, state: TradingState, reason: str) -> None:
        del reason
        self.applied_states.append(state)

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

    def build_heartbeat(
        self,
        exchange_evidence: dict[str, Any] | None = None,
    ) -> object:
        del exchange_evidence
        return object()


class _ControlPlane:
    def heartbeat(self, node_id: str, heartbeat: object) -> None:
        del node_id, heartbeat

    def poll_commands(self, node_id: str, after_sequence: int | None):
        del node_id, after_sequence
        return ()

    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class _SessionCommandControlPlane(_ControlPlane):
    def __init__(self) -> None:
        self._polled = False
        self.acked = Event()

    def poll_commands(self, node_id: str, after_sequence: int | None):
        del node_id, after_sequence
        if self._polled:
            return ()
        self._polled = True
        return (
            SimpleNamespace(
                command_id="command-1",
                type=SimpleNamespace(value="halt"),
                args={},
            ),
        )

    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.acked.set()


class _SlowCommandActor(CommandPollerActor):
    def __init__(self, session: _RecordingSession) -> None:
        super().__init__(
            _ControlPlane(),
            _Lifecycle(),
            "node-a",
            account_id="account-a",
            control_plane_session=session,
            command_apply_max_items=16,
            command_apply_time_budget_seconds=0.005,
        )
        self.applied: list[str] = []

    def _apply(self, cmd: Any):
        time.sleep(SLOW_ITEM_SECONDS)
        self.applied.append(str(cmd.command_id))
        return "completed", None

    def _record_actor_tick(self) -> None:
        return

    def _harvest_heartbeat(self) -> None:
        return

    def _harvest_acks(self) -> None:
        return

    def _submit_heartbeat(self) -> None:
        return

    def _submit_commands(self) -> None:
        return

    def _submit_acks(self) -> None:
        return

    def _evaluate_poll_staleness(self) -> None:
        return


class _SessionCommandActor(CommandPollerActor):
    def __init__(
        self,
        control_plane: _SessionCommandControlPlane,
        session: NodeControlPlaneSession,
    ) -> None:
        super().__init__(
            control_plane,
            _Lifecycle(),
            "node-a",
            account_id="account-a",
            control_plane_session=session,
        )
        self.apply_thread_id: int | None = None

    def _apply(self, cmd: Any):
        del cmd
        self.apply_thread_id = get_ident()
        return "completed", None


class _BlockingEvidenceProvider:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def snapshot(self) -> dict[str, Any]:
        self.started.set()
        self.release.wait(timeout=1.0)
        return {
            "positions": [],
            "regular_orders": [],
            "algo_orders": [],
            "fetched_at": SimpleNamespace(),
        }


class _BlockingDurableProjection:
    def __init__(self) -> None:
        self.ingest_started = Event()
        self.startup_flushed = Event()
        self.flush_started = Event()
        self.release_flush = Event()
        self.calls: list[str] = []
        self.flush_calls = 0

    def ingest_event(self, event: Any) -> ProjectionIngestResult:
        self.calls.append(f"ingest:{event}")
        self.ingest_started.set()
        return ProjectionIngestResult(
            outcome=ProjectionIngestOutcome.DURABLE,
            event_id=str(event),
        )

    def flush(self) -> None:
        self.flush_calls += 1
        self.calls.append("flush")
        if self.flush_calls == 1:
            self.startup_flushed.set()
            return
        self.flush_started.set()
        self.release_flush.wait(timeout=1.0)


class _DeadlineBlockingDurableProjection:
    def __init__(self) -> None:
        self.ingest_started = Event()
        self.release_ingest = Event()
        self.halted_reasons: list[str] = []

    def ingest_event(self, event: Any) -> ProjectionIngestResult:
        self.ingest_started.set()
        self.release_ingest.wait(timeout=1.0)
        return ProjectionIngestResult(
            outcome=ProjectionIngestOutcome.DURABLE,
            event_id=str(event),
        )

    def flush(self) -> None:
        return

    def halt_egress(self, reason: str) -> None:
        self.halted_reasons.append(reason)


class _ProjectionSink:
    def post_events(self, node_id: str, events: list[Any]) -> list[str]:
        del node_id
        return [str(event.event_id) for event in events]


class _BlockingProjectionSink(_ProjectionSink):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def post_events(self, node_id: str, events: list[Any]) -> list[str]:
        self.started.set()
        self.release.wait(timeout=2.0)
        return super().post_events(node_id, events)


class _JournalCommandActor(CommandPollerActor):
    def _record_actor_tick(self) -> None:
        return

    def _harvest_namespace_lease_refresh(self) -> None:
        return

    def _harvest_heartbeat(self) -> None:
        return

    def _harvest_commands(self) -> None:
        return

    def _drain_terminal_command_results(self) -> int:
        return 0

    def _harvest_terminal_verification(self) -> None:
        return

    def _replay_terminal_commands(self) -> None:
        return

    def _harvest_acks(self) -> None:
        return

    def _submit_namespace_lease_refresh(self) -> None:
        return

    def _submit_heartbeat(self) -> None:
        return

    def _submit_commands(self) -> None:
        return

    def _submit_acks(self) -> None:
        return

    def _submit_terminal_verification(self) -> None:
        return

    def _evaluate_poll_staleness(self) -> None:
        return


def test_intent_timer_callback_only_touches_local_session_state() -> None:
    client = _IntentClient()
    session = _RecordingSession()
    actor = IntentPublisherActor(
        client,
        control_plane_session=session,
    )
    actor.on_start()

    started_at = time.monotonic()
    actor._on_poll_timer()
    elapsed = time.monotonic() - started_at

    assert elapsed < MAX_CALLBACK_SECONDS
    assert client.poll_calls == 0
    assert session.started is True
    actor.on_stop()


def test_session_intent_publish_returns_to_actor_thread() -> None:
    client = _SessionIntentClient()
    fetched = False

    def fetch(capacity: int) -> tuple[str, ...]:
        nonlocal fetched
        del capacity
        if fetched:
            return ()
        fetched = True
        return ("intent-1",)

    session = NodeControlPlaneSession(
        intent_fetch=fetch,
        intent_deliver=client.deliver,
        intent_fetch_interval_seconds=0.01,
    )
    actor = IntentPublisherActor(
        client,
        control_plane_session=session,
    )
    published_thread_ids: list[int] = []
    actor.publish = lambda intent: published_thread_ids.append(get_ident())
    actor.on_start()

    deadline = time.monotonic() + 1.0
    while not published_thread_ids and time.monotonic() < deadline:
        actor._on_poll_timer()
        time.sleep(0.001)

    assert published_thread_ids == [get_ident()]
    assert client.delivery_thread_id is not None
    assert client.delivery_thread_id != published_thread_ids[0]
    actor.on_stop()


def test_command_timer_callback_does_not_apply_commands_inline() -> None:
    session = _RecordingSession()
    actor = _SlowCommandActor(session)
    actor._pending_commands = (SimpleNamespace(command_id="command-1"),)

    started_at = time.monotonic()
    actor._on_poll_timer()
    elapsed = time.monotonic() - started_at

    assert elapsed < MAX_CALLBACK_SECONDS
    assert actor.applied == []
    assert actor.pending_command_count == 1


def test_session_command_apply_returns_to_actor_thread() -> None:
    control_plane = _SessionCommandControlPlane()
    actor_holder: dict[str, _SessionCommandActor] = {}
    session = NodeControlPlaneSession(
        command_poll=lambda capacity: actor_holder[
            "actor"
        ].session_poll_commands(capacity),
        command_apply=lambda command: actor_holder[
            "actor"
        ].session_apply_command(command),
        command_ack=lambda acknowledgement: actor_holder[
            "actor"
        ].session_ack_command(acknowledgement),
        command_poll_interval_seconds=0.01,
    )
    actor = _SessionCommandActor(control_plane, session)
    actor_holder["actor"] = actor
    actor.on_start()

    deadline = time.monotonic() + 1.0
    while not control_plane.acked.is_set() and time.monotonic() < deadline:
        actor._on_poll_timer()
        time.sleep(0.001)

    assert control_plane.acked.is_set()
    assert actor.apply_thread_id == get_ident()
    actor.on_stop()


def test_exchange_evidence_wait_keeps_actor_timer_callback_bounded() -> None:
    provider = _BlockingEvidenceProvider()
    session = _RecordingSession()
    actor = CommandPollerActor(
        _ControlPlane(),
        _Lifecycle(),
        "node-a",
        account_id="account-a",
        exchange_evidence_provider=provider,
        control_plane_session=session,
    )
    actor.on_start()

    started_at = time.monotonic()
    actor._on_poll_timer()
    elapsed = time.monotonic() - started_at

    assert elapsed < MAX_CALLBACK_SECONDS
    assert provider.started.is_set() is False

    provider.release.set()
    actor.on_stop()


def test_projection_durable_ingress_persists_before_worker_flush() -> None:
    projection = _BlockingDurableProjection()
    actor = ExecutionProjectionActor(
        projection,
        callback_time_budget_seconds=0.05,
    )
    actor.on_start()
    assert projection.startup_flushed.wait(timeout=1.0)

    started_at = time.monotonic()
    accepted = actor.on_event("fill-1")
    elapsed = time.monotonic() - started_at

    assert accepted is True
    assert elapsed < MAX_CALLBACK_SECONDS
    assert projection.ingest_started.wait(timeout=1.0)
    assert projection.calls[:2] == ["flush", "ingest:fill-1"]
    assert projection.flush_started.wait(timeout=1.0)

    projection.release_flush.set()
    actor.on_stop()


def test_projection_100_event_slow_fsync_callback_only_queues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spool = JsonExecutionSpool(tmp_path / "execution-events.wal")
    sink = _BlockingProjectionSink()
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        sink,
        spool,
    )
    actor = ExecutionProjectionActor(
        projection,
        event_queue_capacity=128,
        worker_shutdown_wait_seconds=3.0,
        callback_time_budget_seconds=0.005,
        durable_ingress_deadline_seconds=2.0,
    )
    original_fsync = __import__("projection.spool", fromlist=["os"]).os.fsync

    def slow_fsync(fd: int) -> None:
        time.sleep(0.002)
        original_fsync(fd)

    monkeypatch.setattr("projection.spool.os.fsync", slow_fsync)
    actor.on_start()

    callback_samples = []
    accepted_results = []
    for index in range(100):
        started_at = time.monotonic()
        accepted = actor.on_event(
            {
                "event_type": "OrderAccepted",
                "client_order_id": f"client-{index}",
                "venue_order_id": f"venue-{index}",
                "instrument_id": "SOLUSDT-PERP.BINANCE",
                "ts_event": 1_786_000_000_000_000_000 + index,
            }
        )
        callback_samples.append(time.monotonic() - started_at)
        accepted_results.append(accepted)

    assert max(callback_samples) < MAX_CALLBACK_SECONDS
    assert accepted_results == [True] * 100
    assert sink.started.wait(timeout=1.0)
    assert _wait_until(lambda: spool.pending_count == 100, timeout=3.0)
    sink.release.set()
    actor.on_stop()


def test_projection_durable_ingress_hard_deadline_triggers_fatal() -> None:
    projection = _DeadlineBlockingDurableProjection()
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        callback_time_budget_seconds=0.005,
        durable_ingress_deadline_seconds=0.03,
        worker_shutdown_wait_seconds=0.1,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    started_at = time.monotonic()
    accepted = actor.on_event("fill-deadline")
    elapsed = time.monotonic() - started_at

    assert accepted is True
    assert elapsed < MAX_CALLBACK_SECONDS
    assert projection.ingest_started.wait(timeout=0.1)
    assert _wait_until(lambda: bool(fatal_reasons), timeout=0.2)
    assert "durable ingress deadline exceeded" in fatal_reasons[0]
    assert actor.halted_reason == fatal_reasons[0]

    projection.release_ingest.set()
    actor.on_stop()


def test_command_journal_fsync_delay_does_not_block_actor_timer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lifecycle = _Lifecycle()
    actor = _JournalCommandActor(
        _ControlPlane(),
        lifecycle,
        "node-a",
        account_id="account-a",
        command_journal_path=tmp_path / "commands.json",
        command_apply_max_items=1,
    )
    commands = tuple(
        NodeCommand(command_id=f"halt-{index}", type=CommandType.HALT)
        for index in range(8)
    )
    actor._command_journal.record_received_many(
        commands,
        actor._command_generation_context(),
    )
    actor._pending_commands = commands
    original_fsync = __import__(
        "app.nautilus_actors",
        fromlist=["os"],
    ).os.fsync
    fsync_started = Event()

    def slow_fsync(fd: int) -> None:
        fsync_started.set()
        time.sleep(SLOW_ITEM_SECONDS)
        original_fsync(fd)

    monkeypatch.setattr("app.nautilus_actors.os.fsync", slow_fsync)

    callback_samples = []
    for _index in range(8):
        fsync_started.clear()
        started_at = time.monotonic()
        actor._on_poll_timer()
        callback_samples.append(time.monotonic() - started_at)
        assert fsync_started.wait(timeout=0.2)

    assert max(callback_samples) < MAX_CALLBACK_SECONDS
    assert lifecycle.applied_states == [TradingState.HALTED] * 8
    durability_tasks = [
        actor._pending_acks[command.command_id].durability
        for command in commands
    ]
    assert _wait_until(
        lambda: all(task.completed.is_set() for task in durability_tasks),
        timeout=2.0,
    )
    actor.on_stop()


def test_projection_actor_submits_to_configured_control_plane_session() -> None:
    projection = _BlockingDurableProjection()
    session = _RecordingSession()
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        callback_time_budget_seconds=0.05,
    )
    actor.on_start()
    assert session.execution_events == [False]

    started_at = time.monotonic()
    accepted = actor.on_event("fill-1")
    elapsed = time.monotonic() - started_at

    assert accepted is True
    assert elapsed < MAX_CALLBACK_SECONDS
    assert _wait_until(
        lambda: session.execution_events == [False, "fill-1"]
    )
    assert session.execution_events == [False, "fill-1"]
    assert projection.ingest_started.is_set() is True
    actor.on_stop()


def test_projection_actor_sticky_halts_when_session_rejects_flush_wake() -> None:
    projection = _BlockingDurableProjection()
    session = _BackpressuredSession()
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        fatal_callback=fatal_reasons.append,
    )

    actor.on_start()

    assert actor.halted_reason
    assert "backpressured" in actor.halted_reason
    assert fatal_reasons == [actor.halted_reason]
    assert actor.on_event("fill-after-halt") is False
    actor.on_stop()


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())
