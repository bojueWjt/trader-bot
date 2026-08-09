from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.nautilus_actors import ExecutionProjectionActor  # noqa: E402
from projection.actor import ProjectionActor  # noqa: E402
from projection.event_mapper import ProjectionConfig  # noqa: E402
from projection.spool import JsonExecutionSpool  # noqa: E402
from runtime.control_plane_session import NodeControlPlaneSession  # noqa: E402

MAX_CALLBACK_SECONDS = 0.01


def test_projection_core_ingest_is_durable_without_inline_flush(
    tmp_path: Path,
) -> None:
    sink = _RecordingSink()
    spool = JsonExecutionSpool(tmp_path / "execution-events.json")
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        sink,
        spool,
    )

    result = projection.ingest_event(_execution_event("event-1"))

    assert result.outcome.value == "DURABLE"
    assert result.event_id
    assert spool.pending_count == 1
    assert sink.calls == []


def test_projection_sink_http_failure_is_recoverable_degradation(
    tmp_path: Path,
) -> None:
    sink = _FailingSink()
    health = _ProjectionHealth()
    spool = JsonExecutionSpool(tmp_path / "execution-events-http.json")
    projection = ProjectionActor(
        ProjectionConfig(
            node_id="node-a",
            account_id="account-a",
            lag_degrade_threshold_ms=10**12,
        ),
        sink,
        spool,
        health=health,
    )

    event_id = projection.on_event(_execution_event("event-http"))

    assert event_id
    assert spool.pending_count == 1
    assert health.degraded == [
        "control-plane execution-event sink unavailable"
    ]
    assert health.failed.is_set() is False


def test_projection_session_sink_failure_retries_same_spooled_event(
    tmp_path: Path,
) -> None:
    sink = _ToggleFailingSink()
    health = _ProjectionHealth()
    spool = JsonExecutionSpool(tmp_path / "execution-events-session.json")
    projection = ProjectionActor(
        ProjectionConfig(
            node_id="node-a",
            account_id="account-a",
            lag_degrade_threshold_ms=10**12,
        ),
        sink,
        spool,
        health=health,
    )
    actor_holder: dict[str, ExecutionProjectionActor] = {}
    session = NodeControlPlaneSession(
        execution_event_sink=lambda event: actor_holder[
            "actor"
        ].session_flush_execution_event(event),
        retry_budget=1,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0,
        circuit_reset_seconds=0.01,
    )
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        worker_shutdown_wait_seconds=0.5,
    )
    actor_holder["actor"] = actor
    actor.on_start()

    assert actor.on_event(_execution_event("event-session-http")) is True
    assert _wait_until(lambda: spool.pending_count == 1)
    assert _wait_until(
        lambda: bool(
            session.snapshot().lanes["execution_event"].failure
        )
    )

    failed_lane = session.snapshot().lanes["execution_event"]
    assert failed_lane.success_count == 0
    assert failed_lane.error_count >= 1
    assert sink.calls >= 1
    assert spool.pending_count == 1

    sink.fail = False

    assert _wait_until(
        lambda: (
            spool.pending_count == 0
            and session.snapshot().lanes[
                "execution_event"
            ].success_count
            >= 1
        )
    )
    recovered_lane = session.snapshot().lanes["execution_event"]
    assert spool.pending_count == 0
    assert recovered_lane.success_count >= 1
    assert actor.on_stop() is True


def test_projection_lag_degradation_survives_same_flush_until_low_lag_progress(
    tmp_path: Path,
) -> None:
    event_time = datetime.fromtimestamp(1_786_000_000, tz=timezone.utc)
    now_value = [event_time + timedelta(milliseconds=6)]
    health = _ProjectionHealth()
    projection = ProjectionActor(
        ProjectionConfig(
            node_id="node-a",
            account_id="account-a",
            lag_degrade_threshold_ms=5,
        ),
        _RecordingSink(),
        JsonExecutionSpool(tmp_path / "execution-events-lag.json"),
        now=lambda: now_value[0],
        health=health,
    )

    projection.on_event(_execution_event("event-high-lag"))

    assert health.states[-1][0] == "degraded"
    assert health.degraded[-1] == "projection lag 6ms exceeds 5ms"

    now_value[0] = event_time + timedelta(milliseconds=1)
    projection.on_event(_execution_event("event-low-lag"))

    assert health.states[-1] == ("ready", "")


def test_projection_callback_only_enqueues_while_durable_ingest_blocks() -> None:
    projection = _DurableProjection(block_ingest=True)
    actor = ExecutionProjectionActor(
        projection,
        durable_ingress_deadline_seconds=1.0,
    )
    actor.on_start()

    started_at = time.monotonic()
    accepted = actor.on_event("fill-1")
    elapsed = time.monotonic() - started_at

    assert accepted is True
    assert elapsed < MAX_CALLBACK_SECONDS
    assert projection.ingest_started.wait(timeout=1.0)
    assert projection.ingest_thread_id != get_ident()

    projection.release_ingest.set()
    assert projection.ingest_completed.wait(timeout=1.0)
    actor.on_stop()


def test_projection_slow_spool_fsync_keeps_callback_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spool = JsonExecutionSpool(tmp_path / "slow-execution-events.json")
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        _RecordingSink(),
        spool,
    )
    session = _AcceptingSession()
    actor = ExecutionProjectionActor(
        projection,
        event_queue_capacity=16,
        durable_ingress_deadline_seconds=1.0,
        control_plane_session=session,
    )
    original_fsync = __import__(
        "projection.spool",
        fromlist=["os"],
    ).os.fsync

    def slow_fsync(fd: int) -> None:
        time.sleep(0.01)
        original_fsync(fd)

    monkeypatch.setattr("projection.spool.os.fsync", slow_fsync)
    actor.on_start()

    callback_samples = []
    for index in range(8):
        started_at = time.monotonic()
        assert actor.on_event(_execution_event(f"event-{index}")) is True
        callback_samples.append(time.monotonic() - started_at)

    assert max(callback_samples) < MAX_CALLBACK_SECONDS
    assert _wait_until(lambda: spool.pending_count == 8, timeout=2.0)
    actor.on_stop()


def test_projection_durable_ingest_precedes_session_lane_flush() -> None:
    projection = _DurableProjection()
    actor_holder: dict[str, ExecutionProjectionActor] = {}

    def flush_event(event: Any) -> None:
        projection.calls.append(f"lane:{event}")
        actor_holder["actor"].session_flush_execution_event(event)

    session = NodeControlPlaneSession(
        execution_event_sink=flush_event,
    )
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
    )
    actor_holder["actor"] = actor
    actor.on_start()
    assert _wait_until(lambda: projection.flush_count >= 1)
    projection.calls.clear()

    assert actor.on_event("fill-1") is True
    assert _wait_until(
        lambda: projection.calls
        == ["ingest:fill-1", "lane:fill-1", "flush"]
    )

    actor.on_stop()
    assert projection.ingest_thread_id is not None
    assert projection.flush_thread_id is not None
    assert projection.ingest_thread_id != get_ident()
    assert projection.flush_thread_id != get_ident()
    assert projection.flush_thread_id != projection.ingest_thread_id


def test_projection_queue_full_is_sticky_fatal() -> None:
    projection = _DurableProjection(block_ingest=True)
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        event_queue_capacity=1,
        durable_ingress_deadline_seconds=1.0,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event("fill-1") is True
    assert projection.ingest_started.wait(timeout=1.0)
    assert actor.on_event("fill-2") is True
    assert actor.on_event("fill-3") is False

    assert "queue capacity exceeded" in actor.halted_reason
    assert fatal_reasons == [actor.halted_reason]
    assert actor.on_event("fill-after-fatal") is False

    projection.release_ingest.set()
    assert _wait_until(lambda: len(projection.ingested) == 2)
    actor.on_stop()


def test_projection_durable_ingress_deadline_is_sticky_fatal() -> None:
    projection = _DurableProjection(block_ingest=True)
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        durable_ingress_deadline_seconds=0.03,
        worker_shutdown_wait_seconds=0.2,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event("fill-timeout") is True
    assert projection.ingest_started.wait(timeout=1.0)
    assert _wait_until(lambda: bool(fatal_reasons), timeout=0.3)

    assert "durable ingress deadline exceeded" in fatal_reasons[0]
    assert actor.halted_reason == fatal_reasons[0]
    assert actor.on_event("fill-after-timeout") is False

    projection.release_ingest.set()
    actor.on_stop()


def test_projection_durable_ingress_error_is_sticky_fatal() -> None:
    projection = _DurableProjection(ingest_error=RuntimeError("disk offline"))
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event("fill-error") is True
    assert _wait_until(lambda: bool(fatal_reasons))

    assert "durable ingress failed" in fatal_reasons[0]
    assert "disk offline" in fatal_reasons[0]
    assert actor.on_event("fill-after-error") is False
    actor.on_stop()


def test_projection_consumer_ready_waits_for_ingress_worker() -> None:
    projection = _DurableProjection()
    actor = ExecutionProjectionActor(
        projection,
        worker_shutdown_wait_seconds=1.0,
    )
    release_worker = Event()
    original_run_worker = actor._run_worker
    startup_errors: list[BaseException] = []

    def delayed_run_worker() -> None:
        release_worker.wait(timeout=1.0)
        original_run_worker()

    def start_actor() -> None:
        try:
            actor.on_start()
        except BaseException as exc:
            startup_errors.append(exc)

    actor._run_worker = delayed_run_worker
    assert actor.control_plane_consumer_ready is False
    starter = Thread(target=start_actor)
    starter.start()
    assert _wait_until(lambda: actor._worker_thread is not None)
    assert actor._worker_started.is_set() is False
    assert actor.control_plane_consumer_ready is False

    release_worker.set()
    starter.join(timeout=1.0)

    assert starter.is_alive() is False
    assert startup_errors == []
    assert actor._worker_started.is_set()
    assert actor.control_plane_consumer_ready is True
    assert actor.on_stop() is True
    assert actor.control_plane_consumer_ready is False


def test_projection_halt_serializes_admission_with_queue_publish() -> None:
    projection = _DurableProjection()
    actor = ExecutionProjectionActor(projection)
    actor.on_start()
    admission_queue = _ProjectionAdmissionBarrierQueue()
    actor._event_queue = admission_queue
    accepted: list[bool] = []
    halt_finished = Event()

    def admit() -> None:
        accepted.append(bool(actor.on_event("fill-racing-halt")))

    def halt() -> None:
        actor._halt_egress("explicit projection halt")
        halt_finished.set()

    publisher = Thread(target=admit)
    publisher.start()
    assert admission_queue.put_started.wait(timeout=1.0)
    halter = Thread(target=halt)
    halter.start()

    assert halt_finished.wait(timeout=0.03) is False
    admission_queue.release_put.set()
    publisher.join(timeout=1.0)
    halter.join(timeout=1.0)

    assert publisher.is_alive() is False
    assert halter.is_alive() is False
    assert accepted == [True]
    assert actor.halted_reason == "explicit projection halt"
    assert actor.on_event("fill-after-halt") is False
    actor.on_stop()


def test_projection_stop_serializes_admission_and_drains_accepted_event() -> None:
    projection = _DurableProjection()
    actor = ExecutionProjectionActor(
        projection,
        worker_shutdown_wait_seconds=0.5,
    )
    actor.on_start()
    assert actor.control_plane_consumer_ready is True
    admission_queue = _ProjectionAdmissionBarrierQueue()
    actor._event_queue = admission_queue
    accepted: list[bool] = []
    stop_results: list[bool] = []
    stop_finished = Event()

    def admit() -> None:
        accepted.append(bool(actor.on_event("fill-racing-stop")))

    def stop() -> None:
        stop_results.append(bool(actor.on_stop()))
        stop_finished.set()

    publisher = Thread(target=admit)
    publisher.start()
    assert admission_queue.put_started.wait(timeout=1.0)
    stopper = Thread(target=stop)
    stopper.start()

    assert stop_finished.wait(timeout=0.03) is False
    assert _wait_until(
        lambda: not actor.control_plane_consumer_ready
    )
    admission_queue.release_put.set()
    publisher.join(timeout=1.0)
    stopper.join(timeout=1.0)

    assert publisher.is_alive() is False
    assert stopper.is_alive() is False
    assert accepted == [True]
    assert stop_results == [True]
    assert projection.ingested == ["fill-racing-stop"]
    assert admission_queue.empty()
    assert actor.on_event("fill-after-stop") is False


def test_projection_core_halt_is_final_health_and_admission_barrier() -> None:
    spool = _BlockingProjectionSpool()
    health = _ProjectionHealth()
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        _RecordingSink(),
        spool,
        health=health,
    )
    ingest_results: list[Any] = []

    def ingest() -> None:
        ingest_results.append(
            projection.ingest_event(_execution_event("event-before-halt"))
        )

    ingestion = Thread(target=ingest)
    ingestion.start()
    assert spool.append_started.wait(timeout=1.0)
    halt = Thread(target=projection.halt_egress, args=("durable hard halt",))
    halt.start()
    assert health.failed.wait(timeout=0.03) is False

    spool.release_append.set()
    ingestion.join(timeout=1.0)
    halt.join(timeout=1.0)

    assert ingestion.is_alive() is False
    assert halt.is_alive() is False
    assert ingest_results[0].outcome.value == "DURABLE"
    assert health.states[-1] == ("failed", "durable hard halt")
    ready_after_failure = False
    failure_seen = False
    for state, _reason in health.states:
        if state == "failed":
            failure_seen = True
            continue
        if failure_seen and state == "ready":
            ready_after_failure = True
    assert ready_after_failure is False

    rejected = projection.ingest_event(
        _execution_event("event-after-halt")
    )

    assert rejected.outcome.value == "IGNORED"
    assert spool.append_count == 1


def test_projection_session_stop_failure_retains_retry_identity() -> None:
    projection = _DurableProjection()
    session = _RetryableStopSession()
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        worker_shutdown_wait_seconds=0.1,
    )
    actor.on_start()

    first_stopped = actor.on_stop()

    assert first_stopped is False
    assert actor._session_started is True
    assert session.stop_calls == 1

    second_stopped = actor.on_stop()

    assert second_stopped is True
    assert actor._session_started is False
    assert session.stop_calls == 2


def test_projection_session_backpressure_after_ingest_is_recoverable() -> None:
    projection = _DurableProjection()
    session = _BackpressuredAfterStartupSession()
    fatal_reasons: list[str] = []
    degraded_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        fatal_callback=fatal_reasons.append,
        degraded_callback=degraded_reasons.append,
    )
    actor.on_start()

    assert actor.on_event("fill-backpressured") is True
    assert _wait_until(lambda: bool(degraded_reasons))

    assert projection.ingested == ["fill-backpressured"]
    assert session.submitted[:2] == [False, "fill-backpressured"]
    assert "session wake backpressured" in degraded_reasons[0]
    assert fatal_reasons == []
    assert actor.halted_reason == ""
    assert actor.on_event("fill-during-backpressure") is True

    session.release_backpressure.set()
    assert session.wake_accepted.wait(timeout=1.0)
    assert _wait_until(lambda: actor.degraded_reason == "")
    actor.on_stop()


def test_projection_session_queue_full_after_ingest_is_sticky_fatal() -> None:
    projection = _DurableProjection()
    session = _FullExecutionEventSession()
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event("fill-queue-full") is True
    assert _wait_until(lambda: bool(fatal_reasons))

    assert projection.ingested == ["fill-queue-full"]
    assert "queue capacity exceeded" in fatal_reasons[0]
    assert actor.halted_reason == fatal_reasons[0]
    assert actor.on_event("fill-after-queue-full") is False
    actor.on_stop()


def test_projection_stop_with_pending_ingress_is_sticky_fatal() -> None:
    projection = _DurableProjection(block_ingest=True)
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        durable_ingress_deadline_seconds=1.0,
        worker_shutdown_wait_seconds=0.02,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()
    assert actor.on_event("fill-pending") is True
    assert projection.ingest_started.wait(timeout=1.0)

    actor.on_stop()

    assert fatal_reasons
    assert "pending" in actor.halted_reason
    assert actor.on_event("fill-after-stop") is False

    projection.release_ingest.set()
    assert projection.ingest_completed.wait(timeout=1.0)
    actor.on_stop()


def test_legacy_projection_without_session_preserves_direct_return() -> None:
    projection = _LegacyProjection()
    actor = ExecutionProjectionActor(projection)
    actor.on_start()

    result = actor.on_event("legacy-event")

    assert result == "legacy-event"
    assert projection.events == ["legacy-event"]
    assert projection.thread_id == get_ident()
    actor.on_stop()


def _execution_event(client_order_id: str) -> dict[str, Any]:
    return {
        "event_type": "OrderAccepted",
        "client_order_id": client_order_id,
        "venue_order_id": f"venue-{client_order_id}",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "ts_event": 1_786_000_000_000_000_000,
    }


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


class _RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def post_events(self, node_id: str, events: Any) -> list[str]:
        batch = tuple(events)
        self.calls.append((node_id, batch))
        return [str(event.event_id) for event in batch]


class _FailingSink(_RecordingSink):
    def post_events(self, node_id: str, events: Any) -> list[str]:
        del node_id, events
        raise RuntimeError("HTTP 503")


class _ToggleFailingSink:
    def __init__(self) -> None:
        self.fail = True
        self.calls = 0

    def post_events(self, node_id: str, events: Any) -> list[str]:
        del node_id
        self.calls += 1
        if self.fail:
            raise RuntimeError("HTTP 503")
        return [str(event.event_id) for event in events]


class _ProjectionAdmissionBarrierQueue(Queue[Any]):
    def __init__(self) -> None:
        super().__init__()
        self.put_started = Event()
        self.release_put = Event()

    def put_nowait(self, item: Any) -> None:
        self.put_started.set()
        self.release_put.wait(timeout=1.0)
        super().put_nowait(item)


class _BlockingProjectionSpool:
    def __init__(self) -> None:
        self.append_started = Event()
        self.release_append = Event()
        self.append_count = 0
        self.pending_count = 0

    def append_once(self, envelope: Any) -> bool:
        del envelope
        self.append_started.set()
        self.release_append.wait(timeout=1.0)
        self.append_count += 1
        self.pending_count += 1
        return True

    def pending_events(self, limit: int) -> list[Any]:
        del limit
        return []

    def mark_acked(self, event_ids: Any) -> None:
        del event_ids


class _ProjectionHealth:
    def __init__(self) -> None:
        self.states: list[tuple[str, str]] = []
        self.degraded: list[str] = []
        self.failed = Event()

    def record_projection_progress(
        self,
        projection_lag_ms: int,
        last_event_id: str | None = None,
    ) -> None:
        del projection_lag_ms, last_event_id

    def mark_projection_ready(self) -> None:
        self.states.append(("ready", ""))

    def mark_projection_degraded(self, reason: str) -> None:
        self.degraded.append(reason)
        self.states.append(("degraded", reason))

    def mark_projection_failed(self, reason: str) -> None:
        self.states.append(("failed", reason))
        self.failed.set()


class _DurableProjection:
    def __init__(
        self,
        *,
        block_ingest: bool = False,
        ingest_error: Exception | None = None,
    ) -> None:
        self._block_ingest = block_ingest
        self._ingest_error = ingest_error
        self.ingest_started = Event()
        self.ingest_completed = Event()
        self.release_ingest = Event()
        self.ingested: list[Any] = []
        self.calls: list[str] = []
        self.halted_reasons: list[str] = []
        self.flush_count = 0
        self.ingest_thread_id: int | None = None
        self.flush_thread_id: int | None = None

    def ingest_event(self, event: Any) -> Any:
        self.ingest_thread_id = get_ident()
        self.ingest_started.set()
        if self._block_ingest:
            self.release_ingest.wait(timeout=2.0)
        if self._ingest_error is not None:
            raise self._ingest_error
        self.ingested.append(event)
        self.calls.append(f"ingest:{event}")
        self.ingest_completed.set()
        return SimpleNamespace(outcome="DURABLE", event_id=str(event))

    def flush(self) -> list[str]:
        self.flush_thread_id = get_ident()
        self.flush_count += 1
        self.calls.append("flush")
        return []

    def halt_egress(self, reason: str) -> None:
        self.halted_reasons.append(reason)


class _BackpressuredAfterStartupSession:
    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.started = False
        self.stopped = False
        self.release_backpressure = Event()
        self.wake_accepted = Event()

    def start(self) -> None:
        self.started = True

    def stop(self, deadline: float) -> bool:
        del deadline
        self.stopped = True
        return True

    def submit_execution_event(self, event: Any) -> str:
        self.submitted.append(event)
        if len(self.submitted) == 1 and event is False:
            return "accepted"
        if not self.release_backpressure.is_set():
            return "backpressured"
        self.wake_accepted.set()
        return "accepted"


class _AcceptingSession(_BackpressuredAfterStartupSession):
    def submit_execution_event(self, event: Any) -> str:
        self.submitted.append(event)
        return "accepted"


class _RetryableStopSession(_AcceptingSession):
    def __init__(self) -> None:
        super().__init__()
        self.stop_calls = 0

    def stop(self, deadline: float) -> bool:
        del deadline
        self.stop_calls += 1
        return self.stop_calls >= 2


class _FullExecutionEventSession(_BackpressuredAfterStartupSession):
    def snapshot(self) -> Any:
        execution_lane = SimpleNamespace(
            failure="execution_event queue capacity exceeded",
            fatal_failure="execution_event queue capacity exceeded",
            circuit_state="closed",
            queue_pressure="full",
        )
        return SimpleNamespace(
            stopped=False,
            lanes={"execution_event": execution_lane},
        )


class _LegacyProjection:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.thread_id: int | None = None

    def on_event(self, event: Any) -> str:
        self.thread_id = get_ident()
        self.events.append(event)
        return str(event)

    def flush(self) -> list[str]:
        return []
