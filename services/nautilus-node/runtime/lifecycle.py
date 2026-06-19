from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Protocol

from config.node_config import NodeConfig
from execution_domain.contracts import ReconciliationState
from execution_domain.control_plane import ControlPlaneClient, Heartbeat, TradingState


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class DependencyName(str, Enum):
    INSTRUMENTS = "instruments"
    REDIS = "redis"
    CONTROL_PLANE = "control_plane"
    RECONCILIATION = "reconciliation"
    PROJECTION = "projection"


@dataclass(frozen=True)
class ReadinessStatus:
    ready: bool
    missing: tuple[DependencyName, ...]


class NodeLifecycle:
    """Pure lifecycle state machine for a single account/process."""

    def __init__(
        self,
        config: NodeConfig,
        clock: Optional[Clock] = None,
        control_plane: Optional[ControlPlaneClient] = None,
    ) -> None:
        self.config = config
        self._clock = clock or SystemClock()
        self._control_plane = control_plane
        self._ready_dependencies: set[DependencyName] = set()
        self._trading_state = TradingState.HALTED
        self._halt_reason = "startup"
        self._last_control_plane_ok_at: Optional[datetime] = None
        self._last_event_id: Optional[str] = None
        self._projection_lag_ms = 0
        self._reconciliation_state = ReconciliationState.DEGRADED

    @property
    def trading_state(self) -> TradingState:
        return self._trading_state

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def readiness(self) -> ReadinessStatus:
        missing = tuple(
            dependency
            for dependency in DependencyName
            if dependency not in self._ready_dependencies
        )
        return ReadinessStatus(ready=not missing, missing=missing)

    def mark_dependency_ready(self, dependency: DependencyName) -> None:
        self._ready_dependencies.add(dependency)
        if dependency is DependencyName.CONTROL_PLANE:
            self._last_control_plane_ok_at = self._clock.now()
        if dependency is DependencyName.RECONCILIATION:
            self._reconciliation_state = ReconciliationState.HEALTHY

    def mark_dependency_failed(self, dependency: DependencyName, reason: str) -> None:
        self._ready_dependencies.discard(dependency)
        if dependency is DependencyName.RECONCILIATION:
            self._reconciliation_state = ReconciliationState.FAILED
        self._halt(f"{dependency.value} failed: {reason}")

    def apply_operator_state(self, state: TradingState, reason: str) -> None:
        if state is TradingState.ACTIVE and not self.readiness.ready:
            raise RuntimeError("cannot switch ACTIVE before readiness is true")
        self._trading_state = state
        if state is TradingState.HALTED:
            self._halt_reason = reason
        elif state is TradingState.ACTIVE:
            self._halt_reason = ""

    def build_heartbeat(self) -> Heartbeat:
        return Heartbeat(
            ts=self._clock.now(),
            trading_state=self._trading_state,
            readiness=self.readiness.ready,
            projection_lag_ms=self._projection_lag_ms,
            reconciliation_state=self._reconciliation_state,
            last_event_id=self._last_event_id,
        )

    def send_heartbeat(self) -> None:
        if self._control_plane is None:
            raise RuntimeError("control-plane client is not configured")
        self._control_plane.heartbeat(self.config.node_id, self.build_heartbeat())
        self._last_control_plane_ok_at = self._clock.now()

    def record_projection_progress(
        self, projection_lag_ms: int, last_event_id: Optional[str] = None
    ) -> None:
        if projection_lag_ms < 0:
            raise ValueError("projection_lag_ms must be non-negative")
        self._projection_lag_ms = projection_lag_ms
        if last_event_id is not None:
            self._last_event_id = last_event_id

    def evaluate_safety(self) -> None:
        now = self._clock.now()
        if self._last_control_plane_ok_at is None:
            if DependencyName.CONTROL_PLANE in self._ready_dependencies:
                self._halt("control-plane heartbeat missing")
            return

        if now - self._last_control_plane_ok_at > self.config.control_plane.heartbeat_timeout:
            self._ready_dependencies.discard(DependencyName.CONTROL_PLANE)
            self._halt("control-plane heartbeat stale")
            return

        if self._control_plane is None:
            return
        snapshot_at = self._control_plane.latest_snapshot_generated_at(
            self.config.account_id
        )
        if snapshot_at is None:
            if DependencyName.PROJECTION in self._ready_dependencies:
                self._ready_dependencies.discard(DependencyName.PROJECTION)
                self._halt("control-plane snapshot missing")
            return
        if now - snapshot_at > self.config.control_plane.snapshot_stale_after:
            self._ready_dependencies.discard(DependencyName.PROJECTION)
            self._halt("control-plane snapshot stale")

    def _halt(self, reason: str) -> None:
        self._trading_state = TradingState.HALTED
        self._halt_reason = reason
