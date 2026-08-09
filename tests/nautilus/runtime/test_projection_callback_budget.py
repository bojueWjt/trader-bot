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

from app.nautilus_actors import (  # noqa: E402
    ExecutionProjectionActor,
    _ProjectionDegradationCause,
)
from projection.actor import (  # noqa: E402
    ProjectionActor,
    ProjectionSinkUnavailable,
)
from projection.event_mapper import ProjectionConfig  # noqa: E402
from projection.spool import JsonExecutionSpool  # noqa: E402
from execution_domain.http_client import ControlPlaneHttpError  # noqa: E402
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


def test_projection_filters_order_initialized_without_halting_durable_lane(
    tmp_path: Path,
) -> None:
    sink = _RecordingSink()
    spool = JsonExecutionSpool(
        tmp_path / "execution-events-filtered.json"
    )
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        sink,
        spool,
    )
    fatal_reasons: list[str] = []
    degraded_reasons: list[str] = []
    recovered: list[bool] = []
    actor = ExecutionProjectionActor(
        projection,
        fatal_callback=fatal_reasons.append,
        degraded_callback=degraded_reasons.append,
        recovered_callback=lambda: recovered.append(True),
    )
    actor.on_start()

    accepted = actor.on_event(
        {
            "event_type": "OrderInitialized",
            "client_order_id": "restored-protection-order",
            "instrument_id": "GOOGLUSDT-PERP.BINANCE",
            "ts_event": 1_786_000_000_000_000_000,
        }
    )

    assert accepted is True
    assert _wait_until(
        lambda: bool(actor.halted_reason or actor.degraded_reason)
    )
    assert actor.halted_reason == ""
    assert "filtered subscribed event" in actor.degraded_reason
    assert degraded_reasons == [actor.degraded_reason]
    assert fatal_reasons == []
    assert spool.pending_count == 0

    assert actor.on_event(_execution_event("event-after-filter")) is True
    assert _wait_until(lambda: bool(sink.calls))
    assert actor.halted_reason == ""
    assert _wait_until(lambda: actor.degraded_reason == "")
    assert recovered == [True]
    assert fatal_reasons == []
    assert actor.on_stop() is True


def test_projection_halted_core_remains_sticky_fatal(
    tmp_path: Path,
) -> None:
    projection = ProjectionActor(
        ProjectionConfig(node_id="node-a", account_id="account-a"),
        _RecordingSink(),
        JsonExecutionSpool(
            tmp_path / "execution-events-halted-core.json"
        ),
    )
    projection.halt_egress("durable spool unavailable")
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event(
        {
            "event_type": "OrderInitialized",
            "client_order_id": "event-after-core-halt",
            "instrument_id": "GOOGLUSDT-PERP.BINANCE",
            "ts_event": 1_786_000_000_000_000_000,
        }
    ) is True
    assert _wait_until(lambda: bool(fatal_reasons))
    assert actor.halted_reason == (
        "execution projection durable ingress is halted"
    )
    assert fatal_reasons == [actor.halted_reason]
    assert actor.on_event("event-after-wrapper-halt") is False
    actor.on_stop()


def test_filtered_event_retries_pending_session_wake_before_next_ingest() -> None:
    projection = _BlockingSecondFilteredProjection()
    session = _BackpressuredAfterStartupSession()
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
    )
    actor.on_start()

    assert actor.on_event("persisted-before-filter") is True
    assert _wait_until(lambda: actor.degraded_reason != "")
    submissions_before_filter = len(session.submitted)

    assert actor.on_event("filtered-first") is True
    assert actor.on_event("filtered-second") is True
    assert projection.second_filtered_started.wait(timeout=1.0)

    assert len(session.submitted) > submissions_before_filter

    projection.release_second_filtered.set()
    actor.on_stop()


def test_projection_degradation_causes_isolate_session_wake_from_filtered_recovery(
) -> None:
    projection = _ControlledSpoolProjection()
    session = _BackpressuredAfterStartupSession()
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
    )
    actor.on_start()

    assert actor.on_event("durable-before-filter") is True
    assert _wait_until(
        lambda: "session wake backpressured" in actor.degraded_reason
    )

    assert actor.on_event("filtered-after-backpressure") is True
    assert projection.filtered.wait(timeout=1.0)
    assert _wait_until(
        lambda: (
            "filtered subscribed event" in actor.degraded_reason
            and "session wake backpressured" in actor.degraded_reason
        )
    )

    session.release_backpressure.set()
    assert session.wake_accepted.wait(timeout=1.0)
    assert _wait_until(
        lambda: "session wake backpressured" not in actor.degraded_reason
    )
    assert actor._session_wake_pending.is_set() is False
    assert "filtered subscribed event" in actor.degraded_reason

    projection.allow_drain.set()
    assert actor.on_event("durable-after-filter") is True
    assert _wait_until(lambda: projection.spool.pending_count >= 2)
    actor.session_flush_execution_event(False)
    assert _wait_until(lambda: actor.degraded_reason == "")
    assert actor.on_stop() is True


def test_projection_degradation_causes_isolate_spool_flush_from_filtered_recovery(
) -> None:
    projection = _ControlledSpoolProjection()
    actor = ExecutionProjectionActor(projection)
    actor.on_start()

    assert actor.on_event("durable-before-filter") is True
    assert projection.flush_with_pending.wait(timeout=1.0)
    assert _wait_until(
        lambda: "spool flush made no progress" in actor.degraded_reason
    )

    assert actor.on_event("filtered-after-spool-stall") is True
    assert projection.filtered.wait(timeout=1.0)
    assert _wait_until(
        lambda: (
            "filtered subscribed event" in actor.degraded_reason
            and "spool flush made no progress" in actor.degraded_reason
        )
    )

    projection.allow_drain.set()
    actor.session_flush_execution_event(False)

    assert projection.spool.pending_count == 0
    assert "spool flush made no progress" not in actor.degraded_reason
    assert "filtered subscribed event" in actor.degraded_reason

    assert actor.on_event("deduped-after-filter") is True
    assert _wait_until(lambda: actor.degraded_reason == "")
    assert actor.on_stop() is True


def test_projection_spool_flush_exception_is_sticky_fatal(
) -> None:
    projection = _ControlledSpoolProjection()
    actor = ExecutionProjectionActor(projection)
    actor.on_start()
    projection.fail_flush.set()

    assert actor.on_event("durable-before-flush-error") is True
    assert projection.flush_with_pending.wait(timeout=1.0)
    assert _wait_until(
        lambda: "durable spool flush failed" in actor.halted_reason
    )
    assert actor.degraded_reason == ""
    assert actor.on_event("durable-after-flush-error") is False
    actor.on_stop()


def test_projection_halt_clears_all_degradation_causes_and_stays_sticky() -> None:
    projection = _FilterableDurableProjection()
    session = _BackpressuredAfterStartupSession()
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
    )
    actor.on_start()

    assert actor.on_event("durable-before-halt") is True
    assert _wait_until(
        lambda: "session wake backpressured" in actor.degraded_reason
    )
    assert actor.on_event("filtered-before-halt") is True
    assert projection.filtered.wait(timeout=1.0)
    assert _wait_until(
        lambda: (
            "filtered subscribed event" in actor.degraded_reason
            and "session wake backpressured" in actor.degraded_reason
        )
    )

    actor._halt_egress("explicit final projection halt")

    assert actor.halted_reason == "explicit final projection halt"
    assert actor.degraded_reason == ""
    assert actor.on_event("durable-after-halt") is False

    session.release_backpressure.set()
    actor.session_flush_execution_event(False)

    assert actor.halted_reason == "explicit final projection halt"
    assert actor.degraded_reason == ""
    assert actor.on_stop() is True


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


def test_projection_sink_wrapper_preserves_failure_classification(
    tmp_path: Path,
) -> None:
    failure = _ClassifiedSinkError(
        "HTTP 409 lease owner conflict",
        status_code=409,
        is_fence_conflict=True,
        permanent=True,
        fatal=True,
    )
    sink = _ClassifiedFailingSink(failure)
    spool = JsonExecutionSpool(
        tmp_path / "execution-events-classified.json"
    )
    projection = ProjectionActor(
        ProjectionConfig(
            node_id="node-a",
            account_id="account-a",
            lag_degrade_threshold_ms=10**12,
        ),
        sink,
        spool,
    )
    result = projection.ingest_event(
        _execution_event("event-classified")
    )

    assert result.outcome.value == "DURABLE"
    with pytest.raises(ProjectionSinkUnavailable) as captured:
        projection.flush_for_session()

    wrapped = captured.value
    assert wrapped.status_code == 409
    assert wrapped.is_fence_conflict is True
    assert wrapped.permanent is True
    assert wrapped.fatal is True
    assert wrapped.__cause__ is failure
    assert spool.pending_count == 1


def test_projection_session_permanent_422_is_nonfatal_degradation(
    tmp_path: Path,
) -> None:
    failure = ControlPlaneHttpError(
        "HTTP 422 permanent schema rejection",
        status_code=422,
    )
    sink = _ClassifiedFailingSink(failure)
    spool = JsonExecutionSpool(
        tmp_path / "execution-events-permanent-failure.json"
    )
    projection = ProjectionActor(
        ProjectionConfig(
            node_id="node-a",
            account_id="account-a",
            lag_degrade_threshold_ms=10**12,
        ),
        sink,
        spool,
    )
    actor_holder: dict[str, ExecutionProjectionActor] = {}
    fatal_reasons: list[str] = []
    session = NodeControlPlaneSession(
        execution_event_sink=lambda event: actor_holder[
            "actor"
        ].session_flush_execution_event(event),
        retry_budget=3,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0,
        fatal_termination_hook=fatal_reasons.append,
    )
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        worker_shutdown_wait_seconds=0.5,
    )
    actor_holder["actor"] = actor
    actor.on_start()

    assert actor.on_event(
        _execution_event("event-session-permanent")
    ) is True
    assert _wait_until(lambda: sink.calls == 1)
    assert _wait_until(
        lambda: (
            session.snapshot().lanes["execution_event"].failure
            == "control-plane execution-event sink unavailable"
        )
    )

    degraded = session.snapshot()
    lane = degraded.lanes["execution_event"]
    assert degraded.process_liveness is True
    assert degraded.degraded is True
    assert lane.fatal_failure is False
    assert lane.queue_depth == 0
    assert fatal_reasons == []
    assert session.wait_for_termination(timeout=0.01) is False
    assert sink.calls == 1
    assert spool.pending_count == 1

    sink.failure = None
    assert actor.on_event(
        _execution_event("event-session-after-permanent")
    ) is True
    assert _wait_until(lambda: spool.pending_count == 0)
    assert actor.on_stop() is True


def test_projection_session_sink_fence_conflict_is_fatal(
    tmp_path: Path,
) -> None:
    failure = _ClassifiedSinkError(
        "HTTP 409 stale writer",
        status_code=409,
        is_fence_conflict=True,
        permanent=True,
    )
    sink = _ClassifiedFailingSink(failure)
    spool = JsonExecutionSpool(
        tmp_path / "execution-events-fence-conflict.json"
    )
    projection = ProjectionActor(
        ProjectionConfig(
            node_id="node-a",
            account_id="account-a",
            lag_degrade_threshold_ms=10**12,
        ),
        sink,
        spool,
    )
    actor_holder: dict[str, ExecutionProjectionActor] = {}
    fatal_reasons: list[str] = []
    session = NodeControlPlaneSession(
        execution_event_sink=lambda event: actor_holder[
            "actor"
        ].session_flush_execution_event(event),
        retry_budget=3,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0,
        fatal_termination_hook=fatal_reasons.append,
    )
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        worker_shutdown_wait_seconds=0.5,
    )
    actor_holder["actor"] = actor
    actor.on_start()

    assert actor.on_event(
        _execution_event("event-session-fence")
    ) is True
    assert session.wait_for_termination(timeout=1.0) is True

    fatal = session.snapshot()
    lane = fatal.lanes["execution_event"]
    assert fatal.process_liveness is False
    assert lane.fatal_failure == (
        "control-plane execution-event sink unavailable"
    )
    assert fatal_reasons == [lane.fatal_failure]
    assert sink.calls == 1
    assert spool.pending_count == 1

    sink.failure = None
    actor.session_flush_execution_event(False)
    assert spool.pending_count == 0
    assert actor.on_stop() is True


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
    fatal_reasons: list[str] = []
    session = NodeControlPlaneSession(
        execution_event_sink=lambda event: actor_holder[
            "actor"
        ].session_flush_execution_event(event),
        retry_budget=1,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0,
        circuit_reset_seconds=0.01,
        fatal_termination_hook=fatal_reasons.append,
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
    assert session.snapshot().process_liveness is True
    assert fatal_reasons == []

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
    assert fatal_reasons == []
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


def test_wrapper_recovery_does_not_clear_active_projection_lag(
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
        JsonExecutionSpool(tmp_path / "execution-events-wrapper-lag.json"),
        now=lambda: now_value[0],
        health=health,
    )
    actor = ExecutionProjectionActor(
        projection,
        recovered_callback=projection.mark_ready_if_healthy,
    )
    actor.on_start()

    assert actor.on_event(
        {
            "event_type": "OrderInitialized",
            "client_order_id": "filtered-before-lag",
            "instrument_id": "GOOGLUSDT-PERP.BINANCE",
            "ts_event": int(event_time.timestamp() * 1_000_000_000),
        }
    ) is True
    assert _wait_until(lambda: actor.degraded_reason != "")

    assert actor.on_event(_execution_event("event-wrapper-lag")) is True
    assert _wait_until(lambda: actor.degraded_reason == "")

    assert ("ready", "") not in health.states
    assert health.degraded
    assert set(health.degraded) == {"projection lag 6ms exceeds 5ms"}
    assert actor.on_stop() is True


def test_older_flush_cannot_recover_later_filtered_degradation(
    tmp_path: Path,
) -> None:
    sink = _BlockingFirstSink()
    health = _ProjectionHealth()
    spool = JsonExecutionSpool(
        tmp_path / "execution-events-filtered-flush-race.json"
    )
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
    actor = ExecutionProjectionActor(
        projection,
        degraded_callback=health.mark_projection_degraded,
        recovered_callback=projection.mark_ready_if_healthy,
    )
    actor.on_start()

    assert actor.on_event(_execution_event("durable-before-filter")) is True
    assert sink.first_post_started.wait(timeout=1.0)

    assert actor.on_event(
        {
            "event_type": "OrderInitialized",
            "client_order_id": "filtered-after-flush-start",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "ts_event": 1_786_000_000_000_000_000,
        }
    ) is True
    assert _wait_until(
        lambda: "filtered subscribed event" in actor.degraded_reason
    )
    assert health.states[-1][0] == "degraded"

    sink.release_first_post.set()
    assert _wait_until(lambda: spool.pending_count == 0)

    assert "filtered subscribed event" in actor.degraded_reason
    assert health.states[-1][0] == "degraded"

    assert actor.on_event(
        _execution_event("durable-recovery-candidate")
    ) is True
    assert _wait_until(lambda: actor.degraded_reason == "")

    assert health.states[-1] == ("ready", "")
    assert actor.on_stop() is True


def test_filtered_recovery_callback_precedes_new_degradation() -> None:
    projection = _DurableProjection()
    callback_order: list[str] = []
    recovery_started = Event()
    release_recovery = Event()

    def recovered_callback() -> None:
        recovery_started.set()
        release_recovery.wait(timeout=1.0)
        callback_order.append("recovered")

    actor = ExecutionProjectionActor(
        projection,
        degraded_callback=lambda reason: callback_order.append(
            "degraded"
        ),
        recovered_callback=recovered_callback,
    )
    cause = _ProjectionDegradationCause.FILTERED
    reason = "execution projection filtered subscribed event: OrderInitialized"

    actor._degrade_egress(cause, reason)
    actor._register_filtered_recovery_candidate()
    recovery = Thread(target=actor._complete_filtered_recovery)
    recovery.start()

    assert recovery_started.wait(timeout=1.0)
    degradation = Thread(
        target=lambda: actor._degrade_egress(cause, reason)
    )
    degradation.start()
    time.sleep(0.02)
    assert degradation.is_alive() is True

    release_recovery.set()
    recovery.join(timeout=1.0)
    degradation.join(timeout=1.0)

    assert recovery.is_alive() is False
    assert degradation.is_alive() is False
    assert callback_order[-2:] == ["recovered", "degraded"]
    assert actor.degraded_reason == reason
    assert actor._filtered_degradation_epoch == 2


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

    assert rejected.outcome.value == "HALTED"
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


def test_projection_session_queue_full_after_ingest_is_recoverable() -> None:
    projection = _DurableProjection()
    session = _FullExecutionEventSession()
    fatal_reasons: list[str] = []
    degraded_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        fatal_callback=fatal_reasons.append,
        degraded_callback=degraded_reasons.append,
    )
    actor.on_start()

    assert actor.on_event("fill-queue-full") is True
    assert _wait_until(lambda: bool(degraded_reasons))

    assert projection.ingested == ["fill-queue-full"]
    assert "session wake backpressured" in degraded_reasons[0]
    assert fatal_reasons == []
    assert actor.halted_reason == ""
    assert actor.on_event("fill-after-queue-full") is True
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


class _BlockingFirstSink(_RecordingSink):
    def __init__(self) -> None:
        super().__init__()
        self.first_post_started = Event()
        self.release_first_post = Event()

    def post_events(self, node_id: str, events: Any) -> list[str]:
        if not self.first_post_started.is_set():
            self.first_post_started.set()
            self.release_first_post.wait(timeout=2.0)
        return super().post_events(node_id, events)


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


class _ClassifiedSinkError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        is_fence_conflict: bool,
        permanent: bool,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.is_fence_conflict = is_fence_conflict
        self.permanent = permanent
        self.fatal = fatal


class _ClassifiedFailingSink:
    def __init__(self, failure: Exception | None) -> None:
        self.failure = failure
        self.calls = 0

    def post_events(self, node_id: str, events: Any) -> list[str]:
        del node_id
        self.calls += 1
        failure = self.failure
        if failure is not None:
            raise failure
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


class _BlockingSecondFilteredProjection(_DurableProjection):
    def __init__(self) -> None:
        super().__init__()
        self.filtered_count = 0
        self.second_filtered_started = Event()
        self.release_second_filtered = Event()

    def ingest_event(self, event: Any) -> Any:
        if str(event).startswith("filtered-"):
            self.filtered_count += 1
            if self.filtered_count == 2:
                self.second_filtered_started.set()
                self.release_second_filtered.wait(timeout=2.0)
            return SimpleNamespace(
                outcome="FILTERED",
                event_id=False,
            )
        return super().ingest_event(event)


class _FilterableDurableProjection(_DurableProjection):
    def __init__(self) -> None:
        super().__init__()
        self.filtered = Event()

    def ingest_event(self, event: Any) -> Any:
        if str(event).startswith("filtered-"):
            self.filtered.set()
            return SimpleNamespace(
                outcome="FILTERED",
                event_id=False,
            )
        if str(event).startswith("deduped-"):
            return SimpleNamespace(
                outcome="DEDUPED",
                event_id=str(event),
            )
        return super().ingest_event(event)


class _ControlledSpoolProjection(_FilterableDurableProjection):
    def __init__(self) -> None:
        super().__init__()
        self.spool = SimpleNamespace(pending_count=0)
        self.allow_drain = Event()
        self.fail_flush = Event()
        self.flush_with_pending = Event()

    def ingest_event(self, event: Any) -> Any:
        result = super().ingest_event(event)
        if result.outcome == "DURABLE":
            self.spool.pending_count += 1
        return result

    def flush(self) -> list[str]:
        super().flush()
        if self.spool.pending_count <= 0:
            return []
        self.flush_with_pending.set()
        if self.fail_flush.is_set():
            raise RuntimeError("recoverable flush failure")
        if self.allow_drain.is_set():
            self.spool.pending_count = 0
        return []


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
            fatal_failure=False,
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
