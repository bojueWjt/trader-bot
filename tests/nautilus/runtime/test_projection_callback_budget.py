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

from app.nautilus_actors import ExecutionProjectionActor  # noqa: E402
from projection.actor import ProjectionActor  # noqa: E402
from projection.event_mapper import ProjectionConfig  # noqa: E402
from projection.spool import JsonExecutionSpool  # noqa: E402
from runtime.control_plane_session import NodeControlPlaneSession  # noqa: E402

MAX_CALLBACK_SECONDS = 0.05


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


def test_projection_session_backpressure_after_ingest_is_sticky_fatal() -> None:
    projection = _DurableProjection()
    session = _BackpressuredAfterStartupSession()
    fatal_reasons: list[str] = []
    actor = ExecutionProjectionActor(
        projection,
        control_plane_session=session,
        fatal_callback=fatal_reasons.append,
    )
    actor.on_start()

    assert actor.on_event("fill-backpressured") is True
    assert _wait_until(lambda: bool(fatal_reasons))

    assert projection.ingested == ["fill-backpressured"]
    assert session.submitted == [False, "fill-backpressured"]
    assert "session wake backpressured" in fatal_reasons[0]
    assert actor.on_event("fill-after-backpressure") is False
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

    def start(self) -> None:
        self.started = True

    def stop(self, deadline: float) -> bool:
        del deadline
        self.stopped = True
        return True

    def submit_execution_event(self, event: Any) -> str:
        self.submitted.append(event)
        if event is False:
            return "accepted"
        return "backpressured"


class _AcceptingSession(_BackpressuredAfterStartupSession):
    def submit_execution_event(self, event: Any) -> str:
        self.submitted.append(event)
        return "accepted"


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
