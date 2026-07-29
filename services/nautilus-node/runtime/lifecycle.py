from __future__ import annotations

import os
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
        # HALTED is the safe default (PLAN: live off by default). Testnet acceptance
        # may override until the operator RESUME command path is wired into the node.
        self._trading_state = TradingState(
            os.environ.get("NAUTILUS_INITIAL_TRADING_STATE", "HALTED")
        )
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
            account_id=self.config.account_id,
            ts=self._clock.now(),
            trading_state=self._trading_state,
            readiness=self.readiness.ready,
            projection_lag_ms=self._projection_lag_ms,
            reconciliation_state=self._reconciliation_state,
            last_event_id=self._last_event_id,
        )

    def set_open_orders_provider(self, provider) -> None:
        """Any heartbeat sender (health checker, poller) then carries the open-order
        snapshot automatically — single source, no per-caller wiring."""
        self._open_orders_provider = provider

    def send_heartbeat(self, open_orders=None) -> None:
        if self._control_plane is None:
            raise RuntimeError("control-plane client is not configured")
        beat = self.build_heartbeat()
        if open_orders is None:
            provider = getattr(self, "_open_orders_provider", None)
            if provider is not None:
                try:
                    open_orders = provider()
                except Exception:
                    open_orders = None
        if open_orders is not None:
            try:
                beat = {**beat, "open_orders": open_orders}
            except TypeError:
                pass
        self._control_plane.heartbeat(self.config.node_id, beat)
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
        # A silent halt cost 2h of debugging on 2026-07-10: every halt must be
        # loud. print reaches docker logs regardless of logging config.
        print(f"[NodeLifecycle] TRADING HALTED: {reason}", flush=True)


class TradingLifecycle:
    """Small command gate used by command routing and lifecycle matrix tests."""

    _OPENING_ACTIONS = frozenset({"open_position", "add_position"})
    _MANAGEMENT_ACTIONS = frozenset(
        {
            "partial_close",
            "close_position",
            "move_stop_loss",
            "move_stop_to_entry",
            "replace_take_profits",
            "cancel",
            "cancel_all",
            "close_all",
        }
    )

    def __init__(
        self,
        initial_state: TradingState | str = TradingState.ACTIVE,
    ) -> None:
        self._trading_state = self._coerce_state(initial_state)
        self._reason = ""

    @property
    def trading_state(self) -> TradingState:
        return self._trading_state

    @property
    def reason(self) -> str:
        return self._reason

    def apply_operator_state(
        self,
        state: TradingState | str,
        reason: str,
    ) -> None:
        self._trading_state = self._coerce_state(state)
        self._reason = reason

    def action_allowed(self, action: str) -> bool:
        if self._trading_state is TradingState.ACTIVE:
            return action in self._OPENING_ACTIONS | self._MANAGEMENT_ACTIONS
        return action in self._MANAGEMENT_ACTIONS

    @staticmethod
    def _coerce_state(state: TradingState | str) -> TradingState:
        if isinstance(state, TradingState):
            return state
        return TradingState(str(state))
