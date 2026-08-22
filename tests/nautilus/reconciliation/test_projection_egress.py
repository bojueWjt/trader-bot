from __future__ import annotations

import base64
import json
import signal
import subprocess
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

from app.nautilus_actors import ExecutionProjectionActor  # noqa: E402
from projection.actor import (  # noqa: E402
    LifecycleProjectionHealth,
    ProjectionActor,
    ProjectionIngestOutcome,
    ProjectionIngestResult,
)
from projection.contracts import ExecutionEventEnvelopeV1  # noqa: E402
from projection.event_mapper import ProjectionConfig  # noqa: E402
from projection.spool import (  # noqa: E402
    JsonExecutionSpool,
    SpoolCapacityError,
    SpoolCorruptionError,
)


class _BlockingProjection:
    def __init__(self, *, block: bool = True) -> None:
        self.block = block
        self.started = Event()
        self.release = Event()
        self.events: list[Any] = []
        self.degraded_reasons: list[str] = []
        self.halted_reasons: list[str] = []
        self.cleared = 0

    def flush(self) -> list[str]:
        return []

    def on_event(self, event: Any) -> str:
        self.started.set()
        if self.block:
            self.release.wait(timeout=1.0)
        self.events.append(event)
        return str(event)

    def mark_egress_degraded(self, reason: str) -> None:
        self.degraded_reasons.append(reason)

    def clear_egress_degraded(self) -> None:
        self.cleared += 1

    def halt_egress(self, reason: str) -> None:
        self.halted_reasons.append(reason)


class _ProjectionHeartbeatLifecycle:
    def __init__(self) -> None:
        self.heartbeat_health_degraded_reasons: tuple[str, ...] = ()

    def record_projection_progress(
        self,
        projection_lag_ms: int,
        last_event_id: str | None = None,
    ) -> None:
        del projection_lag_ms, last_event_id

    def mark_dependency_ready(self, dependency: Any) -> None:
        del dependency

    def mark_dependency_failed(
        self,
        dependency: Any,
        reason: str,
    ) -> None:
        del dependency, reason

    def record_projection_degraded(self, reason: str) -> None:
        self.heartbeat_health_degraded_reasons = (reason,)

    def clear_projection_degraded(self) -> None:
        self.heartbeat_health_degraded_reasons = ()


class _BlockingSink:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.events: list[ExecutionEventEnvelopeV1] = []

    def post_events(
        self,
        node_id: str,
        events: list[ExecutionEventEnvelopeV1],
    ) -> list[str]:
        del node_id
        self.started.set()
        self.release.wait(timeout=1.0)
        self.events.extend(events)
        return [event.event_id for event in events]


class _FailingSpool:
    pending_count = 0
    is_degraded = False
    usage_ratio = 0.0

    def append_once(self, envelope: ExecutionEventEnvelopeV1) -> bool:
        del envelope
        raise OSError("wal fsync failed")

    def pending_events(
        self,
        limit: int | None = None,
    ) -> list[ExecutionEventEnvelopeV1]:
        del limit
        return []

    def mark_acked(self, event_ids: list[str]) -> None:
        del event_ids


class _BlockingDurableIngressProjection:
    def __init__(self) -> None:
        self.ingest_started = Event()
        self.release_ingest = Event()
        self.persisted: list[str] = []
        self.degraded_reasons: list[str] = []
        self.halted_reasons: list[str] = []

    def ingest_event(self, event: Any) -> ProjectionIngestResult:
        if not self.persisted:
            self.ingest_started.set()
            self.release_ingest.wait(timeout=1.0)
        value = str(event)
        self.persisted.append(value)
        return ProjectionIngestResult(
            outcome=ProjectionIngestOutcome.DURABLE,
            event_id=value,
        )

    def flush(self) -> None:
        return

    def mark_egress_degraded(self, reason: str) -> None:
        self.degraded_reasons.append(reason)

    def clear_egress_degraded(self) -> None:
        return

    def halt_egress(self, reason: str) -> None:
        self.halted_reasons.append(reason)


class _RecordingDurableProjection:
    def __init__(self) -> None:
        self.persisted: list[str] = []
        self.halted_reasons: list[str] = []

    def ingest_event(self, event: Any) -> ProjectionIngestResult:
        value = str(event)
        self.persisted.append(value)
        return ProjectionIngestResult(
            outcome=ProjectionIngestOutcome.DURABLE,
            event_id=value,
        )

    def flush(self) -> None:
        return

    def halt_egress(self, reason: str) -> None:
        self.halted_reasons.append(reason)


class _StalledDurableProjection:
    def __init__(self) -> None:
        self.spool = SimpleNamespace(pending_count=1)
        self.degraded_reasons: list[str] = []
        self.halted_reasons: list[str] = []

    def ingest_event(self, event: Any) -> ProjectionIngestResult:
        del event
        return ProjectionIngestResult(
            outcome=ProjectionIngestOutcome.DURABLE,
            event_id="probe",
        )

    def flush(self) -> list[str]:
        return []

    def mark_egress_degraded(self, reason: str) -> None:
        self.degraded_reasons.append(reason)

    def clear_egress_degraded(self) -> None:
        return

    def halt_egress(self, reason: str) -> None:
        self.halted_reasons.append(reason)


class _NonDrainingSession:
    def __init__(self) -> None:
        self.submissions: list[Any] = []

    def submit_execution_event(self, event: Any) -> str:
        self.submissions.append(event)
        return "accepted"


class _ReplayBus:
    def __init__(self, event: Any) -> None:
        self.event = event
        self.accepted: list[bool] = []
        self.unsubscribed: list[str] = []

    def subscribe(self, *, topic: str, handler: Any) -> None:
        del topic
        self.accepted.append(bool(handler(self.event)))

    def unsubscribe(self, *, topic: str, handler: Any) -> None:
        del handler
        self.unsubscribed.append(topic)


class _FailingSubscribeBus:
    def subscribe(self, *, topic: str, handler: Any) -> None:
        del topic
        del handler
        raise RuntimeError("injected execution subscription failure")


class _FailingUnsubscribeBus(_ReplayBus):
    def unsubscribe(self, *, topic: str, handler: Any) -> None:
        del topic
        del handler
        raise RuntimeError("injected execution unsubscribe failure")


class _BusExecutionProjectionActor(ExecutionProjectionActor):
    def __init__(self, projection: Any, bus: Any, **kwargs: Any) -> None:
        self._test_bus = bus
        super().__init__(projection, **kwargs)

    def _subscription_targets(self) -> tuple[Any, ...]:
        return (self._test_bus,)


def test_projection_event_is_durable_before_async_sender_can_block(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-events.wal"
    sink = _BlockingSink()
    spool = JsonExecutionSpool(path)
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        sink,
        spool,
    )
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        event_queue_capacity=2,
        worker_shutdown_wait_seconds=0.1,
        callback_time_budget_seconds=0.1,
        durable_ingress_deadline_seconds=0.5,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event(_raw_order_event(1)) is True
    assert sink.started.wait(timeout=0.2)
    assert spool.pending_count == 1

    assert actor.on_event(_raw_order_event(2)) is True
    assert _wait_until(lambda: spool.pending_count == 2)
    restarted = JsonExecutionSpool(path)
    assert restarted.pending_count == 2
    assert fatal_reasons == []

    sink.release.set()
    assert _wait_until(lambda: spool.pending_count == 0)
    actor.on_stop()


def test_coalesced_projection_wake_drains_all_durable_batches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-events.wal"
    sink = _BlockingSink()
    spool = JsonExecutionSpool(path)
    projection = ProjectionActor(
        ProjectionConfig(
            node_id="node-a",
            account_id="account-a",
            max_flush_batch_size=1,
        ),
        sink,
        spool,
    )
    actor = ExecutionProjectionActor(
        projection,
        event_queue_capacity=4,
        worker_shutdown_wait_seconds=0.1,
        callback_time_budget_seconds=0.1,
        durable_ingress_deadline_seconds=0.5,
    )
    actor.on_start()

    assert actor.on_event(_raw_order_event(1)) is True
    assert sink.started.wait(timeout=0.2)
    assert actor.on_event(_raw_order_event(2)) is True
    assert actor.on_event(_raw_order_event(3)) is True
    assert _wait_until(lambda: spool.pending_count == 3)

    sink.release.set()

    assert _wait_until(lambda: spool.pending_count == 0)
    assert len(sink.events) == 3
    actor.on_stop()


def test_projection_wal_write_failure_is_fatal_fail_closed() -> None:
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        _BlockingSink(),
        _FailingSpool(),  # type: ignore[arg-type]
    )
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event(_raw_order_event(1)) is True
    assert _wait_until(lambda: bool(actor.halted_reason))
    assert "durable ingress failed" in actor.halted_reason
    assert fatal_reasons == [actor.halted_reason]

    actor.on_stop()


def test_projection_watchdog_tracks_egress_progress_not_ingress_progress() -> None:
    projection = _StalledDurableProjection()
    stalled: list[float] = []
    actor = ExecutionProjectionActor(
        projection,
        progress_stalled_callback=stalled.append,
        progress_probe_interval_seconds=0.01,
        progress_stale_after_seconds=0.05,
        worker_shutdown_wait_seconds=0.1,
    )
    actor.on_start()

    assert _wait_until(lambda: bool(stalled), timeout=0.3)
    assert actor.progress_snapshot()["stalled"] is True
    assert projection.degraded_reasons

    actor.on_stop()


def test_projection_callback_only_enqueues_while_worker_handles_slow_egress() -> None:
    projection = _BlockingProjection()
    actor = ExecutionProjectionActor(
        projection,
        event_queue_capacity=4,
        worker_shutdown_wait_seconds=0.1,
    )
    actor.on_start()

    started_at = time.monotonic()
    accepted = actor.on_event("slow-event")
    elapsed = time.monotonic() - started_at

    assert accepted is True
    assert elapsed < 0.01
    assert projection.started.wait(timeout=0.2)
    assert projection.events == []

    projection.release.set()
    assert _wait_until(lambda: projection.events == ["slow-event"])
    actor.on_stop()


def test_projection_queue_reports_degraded_then_sticky_halt_at_capacity() -> None:
    projection = _BlockingProjection()
    actor = ExecutionProjectionActor(
        projection,
        event_queue_capacity=5,
        queue_degraded_ratio=0.8,
        worker_shutdown_wait_seconds=0.1,
    )
    actor.on_start()
    assert actor.on_event("worker-blocker") is True
    assert projection.started.wait(timeout=0.2)

    for index in range(4):
        assert actor.on_event(f"queued-{index}") is True
    assert projection.degraded_reasons
    assert projection.halted_reasons == []

    assert actor.on_event("queue-full") is True
    assert projection.halted_reasons
    assert actor.halted_reason
    assert actor.on_event("rejected-after-halt") is False

    projection.release.set()
    actor.on_stop()
    assert actor.halted_reason


def test_projection_worker_stop_obeys_deadline_and_marks_sticky_halt() -> None:
    projection = _BlockingProjection()
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        event_queue_capacity=2,
        worker_shutdown_wait_seconds=0.02,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()
    assert actor.on_event("blocked") is True
    assert projection.started.wait(timeout=0.2)

    started_at = time.monotonic()
    actor.on_stop()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.08
    assert "shutdown deadline" in actor.halted_reason
    assert fatal_reasons == [actor.halted_reason]
    projection.release.set()


def test_projection_shutdown_records_unflushed_position_with_shared_session(
    tmp_path: Path,
) -> None:
    spool = JsonExecutionSpool(tmp_path / "shutdown-events.wal")
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        _BlockingSink(),
        spool,
    )
    session = _NonDrainingSession()
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        worker_shutdown_wait_seconds=0.05,
        fatal_callback=fatal_reasons.append,
        control_plane_session=session,
        manage_control_plane_session=False,
    )
    actor.on_start()
    assert actor.on_event(_raw_order_event(1)) is True
    assert _wait_until(lambda: spool.pending_count == 1)

    started_at = time.monotonic()
    actor.on_stop()
    elapsed = time.monotonic() - started_at

    position = projection.shutdown_unflushed_position
    pending = spool.pending_events()
    assert elapsed < 0.15
    assert position is not False
    assert position["pending_count"] == 1
    assert position["first_event_id"] == pending[0].event_id
    assert position["last_event_id"] == pending[-1].event_id
    assert "shutdown deadline" in position["reason"]
    assert fatal_reasons == [actor.halted_reason]
    assert len(session.submissions) >= 2


def test_projection_filtered_subscribed_event_keeps_durable_lane_running(
    tmp_path: Path,
) -> None:
    lifecycle = _ProjectionHeartbeatLifecycle()
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        _BlockingSink(),
        JsonExecutionSpool(tmp_path / "filtered-events.wal"),
        health=LifecycleProjectionHealth(lifecycle),
    )
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    accepted = actor.on_event(
        {
            "event_type": "OrderInitialized",
            "client_order_id": "filtered-client",
            "ts_event": 1_786_000_000_000_000_000,
        }
    )

    assert accepted is True
    assert _wait_until(lambda: bool(projection.egress_degraded_reason))
    assert "filtered subscribed event" in projection.egress_degraded_reason
    assert lifecycle.heartbeat_health_degraded_reasons == (
        "execution projection filtered subscribed event: OrderInitialized",
    )
    assert actor.halted_reason == ""
    assert fatal_reasons == []

    assert actor.on_event(_raw_order_event(1)) is True
    assert _wait_until(lambda: projection.spool.pending_count == 1)
    assert lifecycle.heartbeat_health_degraded_reasons == ()
    actor.on_stop()


def test_projection_silently_ignores_manual_and_out_of_scope_events(
    tmp_path: Path,
) -> None:
    lifecycle = _ProjectionHeartbeatLifecycle()
    projection = ProjectionActor(
        ProjectionConfig(
            node_id="node-a",
            account_id="account-a",
            allowed_instrument_ids=frozenset(
                {"SOLUSDT-PERP.BINANCE"}
            ),
            require_robot_order_ownership=True,
        ),
        _BlockingSink(),
        JsonExecutionSpool(tmp_path / "ownership-events.wal"),
        health=LifecycleProjectionHealth(lifecycle),
    )
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    manual_submit = {
        "event_type": "OrderSubmitted",
        "client_order_id": "manual-sol-order",
        "instrument_id": "SOLUSDT-PERP.BINANCE",
        "ts_event": 1_786_000_000_000_000_000,
    }
    manual_cancel = {
        "event_type": "OrderCanceled",
        "client_order_id": "manual-sol-order",
        "instrument_id": "SOLUSDT-PERP.BINANCE",
        "ts_event": 1_786_000_000_000_000_000,
    }
    out_of_scope_robot_order = {
        "event_type": "OrderFilled",
        "client_order_id": "B" + ("a" * 32) + "01",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "ts_event": 1_786_000_000_000_000_000,
    }
    manual_position = {
        "event_type": "PositionClosed",
        "position_id": "SOLUSDT-LONG-EXTERNAL",
        "instrument_id": "SOLUSDT-PERP.BINANCE",
        "ts_event": 1_786_000_000_000_000_000,
    }

    assert actor.on_event(manual_submit) is True
    assert actor.on_event(manual_cancel) is True
    assert actor.on_event(out_of_scope_robot_order) is True
    assert actor.on_event(manual_position) is True
    assert _wait_until(lambda: actor._event_queue.unfinished_tasks == 0)
    assert projection.spool.pending_count == 0
    assert projection.egress_degraded_reason == ""
    assert lifecycle.heartbeat_health_degraded_reasons == ()
    assert actor.halted_reason == ""
    assert fatal_reasons == []
    actor.on_stop()


def test_projection_halted_core_is_sticky_fatal(
    tmp_path: Path,
) -> None:
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        _BlockingSink(),
        JsonExecutionSpool(tmp_path / "halted-events.wal"),
    )
    projection.halt_egress("durable spool unavailable")
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event(_raw_order_event(1)) is True
    assert _wait_until(lambda: bool(fatal_reasons))
    assert actor.halted_reason == (
        "execution projection durable ingress is halted"
    )
    assert fatal_reasons == [actor.halted_reason]
    assert actor.on_event(_raw_order_event(2)) is False
    actor.on_stop()


def test_projection_full_queue_drains_durable_events_before_single_fatal() -> None:
    projection = _BlockingDurableIngressProjection()
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        event_queue_capacity=2,
        durable_ingress_deadline_seconds=0.5,
        worker_shutdown_wait_seconds=0.5,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event("worker-blocker") is True
    assert projection.ingest_started.wait(timeout=0.2)
    assert actor.on_event("queued-1") is True
    assert actor.on_event("queued-2") is True
    assert actor.halted_reason
    assert fatal_reasons == []
    assert actor.on_event("overflow") is False

    projection.release_ingest.set()

    assert _wait_until(lambda: bool(fatal_reasons), timeout=0.5)
    assert projection.persisted == [
        "worker-blocker",
        "queued-1",
        "queued-2",
    ]
    assert len(fatal_reasons) == 1
    actor.on_stop()
    assert len(fatal_reasons) == 1


def test_projection_subscribe_synchronous_replay_runs_after_startup_barrier() -> None:
    projection = _RecordingDurableProjection()
    bus = _ReplayBus("subscribe-replay")
    actor = _BusExecutionProjectionActor(
        projection,
        bus,
        event_topics=("events.execution.replay",),
        worker_shutdown_wait_seconds=0.2,
    )

    actor.on_start()

    assert bus.accepted == [True]
    assert _wait_until(
        lambda: projection.persisted == ["subscribe-replay"]
    )
    assert actor.halted_reason == ""
    actor.on_stop()
    assert bus.unsubscribed == ["events.execution.replay"]


def test_projection_subscription_failure_rolls_back_started_lanes() -> None:
    projection = _RecordingDurableProjection()
    fatal_reasons: list[str] = []
    actor = _BusExecutionProjectionActor(
        projection,
        _FailingSubscribeBus(),
        event_topics=("events.execution.failure",),
        worker_shutdown_wait_seconds=0.1,
        fatal_callback=fatal_reasons.append,
    )

    with pytest.raises(
        RuntimeError,
        match="execution subscription failure",
    ):
        actor.on_start()

    worker = actor._worker_thread
    deadline_worker = actor._deadline_thread
    egress_worker = actor._egress_thread
    assert worker is None or worker.is_alive() is False
    assert deadline_worker is None or deadline_worker.is_alive() is False
    assert egress_worker is None or egress_worker.is_alive() is False
    assert len(fatal_reasons) == 1


def test_projection_unsubscribe_failure_halts_egress() -> None:
    projection = _RecordingDurableProjection()
    bus = _FailingUnsubscribeBus("subscribe-replay")
    fatal_reasons: list[str] = []
    actor = _BusExecutionProjectionActor(
        projection,
        bus,
        event_topics=("events.execution.replay",),
        worker_shutdown_wait_seconds=0.2,
        fatal_callback=fatal_reasons.append,
    )

    actor.on_start()
    actor.on_stop()

    assert "subscription rollback deadline exceeded" in actor.halted_reason
    assert fatal_reasons == [actor.halted_reason]


def test_json_spool_rejects_growth_atomically_at_byte_capacity(
    tmp_path: Path,
) -> None:
    spool = JsonExecutionSpool(
        tmp_path / "events.json",
        max_bytes=1_970,
        degraded_ratio=0.8,
    )
    observed_degraded = False

    for index in range(100):
        before_count = spool.pending_count
        try:
            spool.append_once(_envelope(index))
        except SpoolCapacityError:
            assert spool.pending_count == before_count
            break
        observed_degraded = observed_degraded or spool.is_degraded
    else:
        pytest.fail("spool capacity was not enforced")

    assert observed_degraded
    assert spool.size_bytes <= spool.max_bytes


def test_json_spool_bounds_acked_dedupe_history_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.json"
    spool = JsonExecutionSpool(
        path,
        max_bytes=4_096,
        seen_event_limit=3,
    )

    for index in range(10):
        envelope = _envelope(index)
        assert spool.append_once(envelope) is True
        spool.mark_acked([envelope.event_id])

    assert spool.pending_count == 0
    assert spool.seen_event_count == 3
    assert spool.size_bytes <= spool.max_bytes

    restarted = JsonExecutionSpool(
        path,
        max_bytes=4_096,
        seen_event_limit=3,
    )
    assert restarted.append_once(_envelope(9)) is False
    assert restarted.append_once(_envelope(0)) is True


def test_json_spool_compacts_legacy_unbounded_seen_history_on_load(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-events.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "seen_event_ids": [
                    f"event-{index:04d}" for index in range(10)
                ],
                "pending": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    spool = JsonExecutionSpool(
        path,
        max_bytes=4_096,
        seen_event_limit=3,
    )
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert spool.seen_event_count == 3
    assert persisted["seen_event_ids"] == [
        "event-0007",
        "event-0008",
        "event-0009",
    ]
    assert spool.size_bytes == path.stat().st_size


def test_json_spool_truncates_sigkill_torn_tail_and_recovers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kill-torn-events.wal"
    spool = JsonExecutionSpool(path)
    assert spool.append_once(_envelope(1)) is True
    durable_prefix = path.read_bytes()

    tail_path = tmp_path / "tail-record.wal"
    tail_spool = JsonExecutionSpool(tail_path)
    assert tail_spool.append_once(_envelope(2)) is True
    tail_record = tail_path.read_bytes()
    encoded_tail = base64.b64encode(tail_record).decode("ascii")
    child = (
        "import base64, os, signal, sys\n"
        "path = sys.argv[1]\n"
        "record = base64.b64decode(sys.argv[2])\n"
        "with open(path, 'ab', buffering=0) as wal:\n"
        "    wal.write(record[:len(record) // 2])\n"
        "    os.fsync(wal.fileno())\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )

    killed = subprocess.run(
        [sys.executable, "-c", child, str(path), encoded_tail],
        check=False,
    )

    assert killed.returncode == -signal.SIGKILL
    assert path.stat().st_size > len(durable_prefix)
    restarted = JsonExecutionSpool(path)
    assert restarted.pending_count == 1
    assert path.read_bytes() == durable_prefix
    assert restarted.append_once(_envelope(2)) is True


def test_json_spool_middle_record_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "middle-corrupt-events.wal"
    spool = JsonExecutionSpool(path)
    assert spool.append_once(_envelope(1)) is True
    assert spool.append_once(_envelope(2)) is True
    records = path.read_bytes().splitlines(keepends=True)
    assert len(records) == 2
    path.write_bytes(
        records[0]
        + b'{"event":,"op":"append"}\n'
        + records[1]
    )

    with pytest.raises(
        SpoolCorruptionError,
        match="record 2",
    ):
        JsonExecutionSpool(path)


def _wait_until(predicate, timeout: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _envelope(index: int) -> ExecutionEventEnvelopeV1:
    now = datetime.now(timezone.utc)
    return ExecutionEventEnvelopeV1(
        schema_version="1.0",
        event_id=f"event-{index:04d}",
        node_id="node-a",
        account_id="account-a",
        intent_id=None,
        client_order_id=f"client-{index:04d}",
        venue_order_id=f"venue-{index:04d}",
        trade_id=None,
        event_type="OrderAccepted",
        ts_event=now,
        ts_ingest=now,
        payload={"padding": "x" * 160},
    )


def _raw_order_event(index: int) -> dict[str, Any]:
    return {
        "event_type": "OrderAccepted",
        "client_order_id": f"client-{index:04d}",
        "venue_order_id": f"venue-{index:04d}",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "ts_event": 1_786_000_000_000_000_000 + index,
    }
