from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Protocol, Sequence

from .contracts import ExecutionEventEnvelopeV1
from .event_mapper import ProjectionConfig, ProjectionEventMapper
from .spool import JsonExecutionSpool


class ExecutionEventSink(Protocol):
    def post_events(
        self, node_id: str, events: Sequence[ExecutionEventEnvelopeV1]
    ) -> list[str]: ...


class ProjectionHealth(Protocol):
    def record_projection_progress(
        self, projection_lag_ms: int, last_event_id: str | None = None
    ) -> None: ...

    def mark_projection_ready(self) -> None: ...

    def mark_projection_failed(self, reason: str) -> None: ...


class ProjectionIngestOutcome(str, Enum):
    DURABLE = "DURABLE"
    DEDUPED = "DEDUPED"
    IGNORED = "IGNORED"


@dataclass(frozen=True)
class ProjectionIngestResult:
    outcome: ProjectionIngestOutcome
    event_id: str | None


class ProjectionActor:
    """Nautilus Actor-shaped projection component.

    TODO(host-verify): Confirm the concrete Nautilus base class import and config
    type for deployment. Local tests exercise the pure ``on_event`` logic.
    """

    def __init__(
        self,
        config: ProjectionConfig,
        sink: ExecutionEventSink,
        spool: JsonExecutionSpool,
        now: Callable[[], datetime] | None = None,
        mapper: ProjectionEventMapper | None = None,
        health: ProjectionHealth | None = None,
    ) -> None:
        self.config = config
        self._sink = sink
        self.spool = spool
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._mapper = mapper or ProjectionEventMapper(config, now=self._now)
        self._health = health
        self._spool_lock = RLock()
        self._egress_halted_reason = ""

    @property
    def egress_halted_reason(self) -> str:
        return self._egress_halted_reason

    def on_event(self, event: Any) -> str | None:
        result = self.ingest_event(event)
        if result.outcome is ProjectionIngestOutcome.IGNORED:
            return None
        self.flush()
        return result.event_id

    def ingest_event(self, event: Any) -> ProjectionIngestResult:
        envelope = self._mapper.to_envelope(event)
        if envelope is None:
            return ProjectionIngestResult(
                outcome=ProjectionIngestOutcome.IGNORED,
                event_id=None,
            )
        with self._spool_lock:
            appended = self.spool.append_once(envelope)
        if appended:
            self._record_projection_progress(envelope)
            outcome = ProjectionIngestOutcome.DURABLE
        else:
            outcome = ProjectionIngestOutcome.DEDUPED
        return ProjectionIngestResult(
            outcome=outcome,
            event_id=envelope.event_id,
        )

    def flush(self) -> list[str]:
        with self._spool_lock:
            pending = self.spool.pending_events(
                limit=self.config.max_flush_batch_size
            )
        if not pending:
            return []
        try:
            acked = self._sink.post_events(self.config.node_id, pending)
        except Exception:
            self._mark_projection_failed("control-plane execution-event sink unavailable")
            return []
        with self._spool_lock:
            self.spool.mark_acked(acked)
            pending_count = self.spool.pending_count
        if acked:
            last_event_id = acked[-1]
            last_event = _find_event(pending, last_event_id)
            if last_event is not None:
                self._record_projection_progress(last_event)
        if pending_count == 0:
            self._mark_projection_ready()
        return acked

    def halt_egress(self, reason: str) -> None:
        if self._egress_halted_reason:
            return
        self._egress_halted_reason = reason
        self._mark_projection_failed(reason)

    def _record_projection_progress(self, envelope: ExecutionEventEnvelopeV1) -> None:
        lag_ms = _lag_ms(now=self._now(), ts_event=envelope.ts_event)
        if self._health is not None:
            self._health.record_projection_progress(lag_ms, envelope.event_id)
            if lag_ms > self.config.lag_degrade_threshold_ms:
                self._health.mark_projection_failed(
                    "projection lag "
                    f"{lag_ms}ms exceeds {self.config.lag_degrade_threshold_ms}ms"
                )
            else:
                self._health.mark_projection_ready()

    def _mark_projection_ready(self) -> None:
        if self._health is None:
            return
        if self._egress_halted_reason:
            return
        self._health.mark_projection_ready()

    def _mark_projection_failed(self, reason: str) -> None:
        if self._health is not None:
            self._health.mark_projection_failed(reason)


class LifecycleProjectionHealth:
    """Adapter from projection lag to ``NodeLifecycle`` readiness/heartbeat."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle

    def record_projection_progress(
        self, projection_lag_ms: int, last_event_id: str | None = None
    ) -> None:
        self._lifecycle.record_projection_progress(projection_lag_ms, last_event_id)

    def mark_projection_ready(self) -> None:
        try:
            from runtime.lifecycle import DependencyName
        except ModuleNotFoundError:
            return
        self._lifecycle.mark_dependency_ready(DependencyName.PROJECTION)

    def mark_projection_failed(self, reason: str) -> None:
        try:
            from runtime.lifecycle import DependencyName
        except ModuleNotFoundError:
            return
        self._lifecycle.mark_dependency_failed(DependencyName.PROJECTION, reason)


def _find_event(
    events: Sequence[ExecutionEventEnvelopeV1], event_id: str
) -> ExecutionEventEnvelopeV1 | None:
    for event in events:
        if event.event_id == event_id:
            return event
    return None


def _lag_ms(*, now: datetime, ts_event: datetime) -> int:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    if ts_event.tzinfo is None:
        ts_event = ts_event.replace(tzinfo=timezone.utc)
    else:
        ts_event = ts_event.astimezone(timezone.utc)
    return max(0, int((now - ts_event).total_seconds() * 1000))
