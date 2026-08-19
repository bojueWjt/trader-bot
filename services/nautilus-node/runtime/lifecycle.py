from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Event, Lock, RLock, Thread
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol
from uuid import uuid4

from config.node_config import NodeConfig
from execution_domain.contracts import ReconciliationState
from execution_domain.control_plane import ControlPlaneClient, Heartbeat, TradingState

from .live_canary_execution import (
    live_open_gate_denial,
    normalize_live_open_gate,
)
from .reconciliation import (
    ReconciliationProof,
    ReconciliationProofSnapshot,
    ReconciliationProofStatus,
    evaluate_reconciliation_proof,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class DependencyName(str, Enum):
    INSTRUMENTS = "instruments"
    REDIS = "redis"
    CONTROL_PLANE = "control_plane"
    INTENT_STREAM = "intent_stream"
    COMMAND_STREAM = "command_stream"
    RECONCILIATION = "reconciliation"
    PROJECTION = "projection"


@dataclass(frozen=True)
class ReadinessStatus:
    ready: bool
    missing: tuple[DependencyName, ...]
    reconciliation: ReconciliationProofSnapshot


class ActorTickWatchdog:
    """Independent event-loop watchdog with no network or actor dependencies."""

    def __init__(
        self,
        lifecycle: "NodeLifecycle",
        *,
        stale_after_seconds: float,
        restart_after_seconds: float,
        check_interval_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        on_stale: Callable[[float], None] | None = None,
        on_restart_required: Callable[[float], None] | None = None,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if restart_after_seconds <= stale_after_seconds:
            raise ValueError(
                "restart_after_seconds must be greater than stale_after_seconds"
            )
        if check_interval_seconds <= 0:
            raise ValueError("check_interval_seconds must be positive")
        self._lifecycle = lifecycle
        self._stale_after_seconds = stale_after_seconds
        self._restart_after_seconds = restart_after_seconds
        self._check_interval_seconds = check_interval_seconds
        self._monotonic = monotonic
        self._on_stale = on_stale
        self._on_restart_required = on_restart_required
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._last_tick_at = monotonic()
        self._stale_signaled = False
        self._restart_signaled = False

    def start(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._stop.clear()
        with self._lock:
            self._last_tick_at = self._monotonic()
            self._stale_signaled = False
            self._restart_signaled = False
        thread = Thread(
            target=self._run,
            name="nautilus-actor-tick-watchdog",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def record_tick(self) -> None:
        with self._lock:
            self._last_tick_at = self._monotonic()
            self._stale_signaled = False
        self._lifecycle.record_actor_tick()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=self._check_interval_seconds + 0.1)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._check_interval_seconds):
            self._check_once()

    def _check_once(self) -> None:
        now = self._monotonic()
        stale_callback_age = None
        callback_age = None
        with self._lock:
            age = max(now - self._last_tick_at, 0.0)
            stale_signaled = self._stale_signaled
            restart_signaled = self._restart_signaled
            if age >= self._stale_after_seconds:
                self._stale_signaled = True
            if age >= self._restart_after_seconds:
                self._restart_signaled = True
            self._lifecycle.record_actor_tick_age(age)
            if age >= self._stale_after_seconds and not stale_signaled:
                self._lifecycle.mark_actor_tick_stale(age)
                stale_callback_age = age
            if age >= self._restart_after_seconds and not restart_signaled:
                self._lifecycle.mark_restart_required(age)
                callback_age = age
        if stale_callback_age is not None:
            callback = self._on_stale
            if callback is not None:
                callback(stale_callback_age)
        if callback_age is None:
            return
        callback = self._on_restart_required
        if callback is not None:
            callback(callback_age)


class NodeLifecycle:
    """Pure lifecycle state machine for a single account/process."""

    def __init__(
        self,
        config: NodeConfig,
        clock: Optional[Clock] = None,
        control_plane: Optional[ControlPlaneClient] = None,
        *,
        release_id: str | None = None,
        reconciliation_proof_max_age: timedelta | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._clock = clock or SystemClock()
        self._control_plane = control_plane
        configured_release_id = release_id
        if configured_release_id is None:
            configured_release_id = os.environ.get("TRADER_RELEASE_ID", "")
        self._release_id = configured_release_id.strip()
        self._release_identity = {
            "image_digest": os.environ.get(
                "TRADER_RELEASE_IMAGE_DIGEST",
                "",
            ).strip(),
            "config_sha256": os.environ.get(
                "TRADER_RELEASE_CONFIG_SHA256",
                "",
            ).strip(),
            "dependency_lock_sha256": os.environ.get(
                "TRADER_RELEASE_DEPENDENCY_LOCK_SHA256",
                "",
            ).strip(),
            "schema_epoch": os.environ.get(
                "TRADER_RELEASE_SCHEMA_EPOCH",
                "",
            ).strip(),
        }
        binance_config = getattr(config, "binance", None)
        environment = str(
            getattr(binance_config, "environment", "")
        ).lower()
        if environment == "live":
            missing_identity = [
                field_name
                for field_name, value in {
                    "release_id": self._release_id,
                    **self._release_identity,
                }.items()
                if not value
            ]
            if missing_identity:
                raise ValueError(
                    "live release identity is incomplete: "
                    + ", ".join(sorted(missing_identity))
                )
        self._runtime_generation = uuid4().hex
        self._heartbeat_sequence = 0
        self._monotonic = monotonic
        self._reconciliation_proof_max_age = reconciliation_proof_max_age
        if self._reconciliation_proof_max_age is None:
            self._reconciliation_proof_max_age = _default_reconciliation_proof_max_age(
                config
            )
        if self._reconciliation_proof_max_age <= timedelta(0):
            raise ValueError("reconciliation_proof_max_age must be positive")
        self._state_lock = RLock()
        self._ready_dependencies: set[DependencyName] = set()
        requested_initial_state = os.environ.get(
            "NAUTILUS_INITIAL_TRADING_STATE",
            "HALTED",
        )
        self._trading_state = TradingState(requested_initial_state)
        if environment == "live":
            self._trading_state = TradingState.HALTED
        self._halt_reason = "startup"
        self._last_control_plane_ok_at: Optional[datetime] = None
        self._last_event_id: Optional[str] = None
        self._projection_lag_ms = 0
        self._health_degraded_reasons: dict[str, str] = {}
        self._reconciliation_state = ReconciliationState.DEGRADED
        self._reconciliation_proof: ReconciliationProof | None = None
        self._last_reconciliation_completed_at: datetime | None = None
        self._reconciliation_generation = 0
        self._reconciliation_in_flight = False
        self._lease_required = False
        self._redis_fencing_epoch: str | bool = False
        self._lease_generation: Any = 0
        self._lease_last_refresh_at: float | None = None
        self._lease_freshness_seconds = 0.0
        self._open_orders_provider: (
            Callable[[], Iterable[dict[str, Any]]] | None
        ) = None
        self._actor_tick_age_seconds = 0.0
        self._actor_tick_failed = False
        self._restart_required = False
        self._rollout_phase: str | None = None
        self._live_open_gate: dict[str, Any] | bool = False

    @property
    def trading_state(self) -> TradingState:
        with self._state_lock:
            self._halt_if_lease_stale_locked()
            return self._trading_state

    @property
    def rollout_phase(self) -> str | None:
        with self._state_lock:
            return self._rollout_phase

    @property
    def live_open_gate(self) -> dict[str, Any] | bool:
        with self._state_lock:
            if self._live_open_gate is False:
                return False
            return dict(self._live_open_gate)

    @property
    def runtime_generation(self) -> str:
        return self._runtime_generation

    @property
    def reconciliation_generation(self) -> int:
        with self._state_lock:
            return self._reconciliation_generation

    @property
    def reconciliation_in_flight(self) -> bool:
        with self._state_lock:
            return self._reconciliation_in_flight

    @property
    def lease_generation(self) -> Any:
        with self._state_lock:
            return self._lease_generation

    @property
    def halt_reason(self) -> str:
        with self._state_lock:
            return self._halt_reason

    @property
    def actor_tick_age_seconds(self) -> float:
        with self._state_lock:
            return self._actor_tick_age_seconds

    @property
    def restart_required(self) -> bool:
        with self._state_lock:
            return self._restart_required

    @property
    def readiness(self) -> ReadinessStatus:
        with self._state_lock:
            reconciliation = self._refresh_reconciliation_locked()
            missing = tuple(
                dependency
                for dependency in DependencyName
                if dependency is not DependencyName.RECONCILIATION
                and dependency not in self._ready_dependencies
            )
            return ReadinessStatus(
                ready=not missing,
                missing=missing,
                reconciliation=reconciliation,
            )

    @property
    def reconciliation(self) -> ReconciliationProofSnapshot:
        with self._state_lock:
            return self._refresh_reconciliation_locked()

    def mark_dependency_ready(self, dependency: DependencyName) -> None:
        with self._state_lock:
            if dependency is DependencyName.RECONCILIATION:
                return
            if (
                dependency
                in {
                    DependencyName.REDIS,
                    DependencyName.COMMAND_STREAM,
                }
                and self._restart_required
            ):
                return
            if (
                dependency is DependencyName.COMMAND_STREAM
                and self._actor_tick_failed
            ):
                return
            self._ready_dependencies.add(dependency)
            if dependency is DependencyName.CONTROL_PLANE:
                self._last_control_plane_ok_at = self._clock.now()

    def mark_dependency_failed(self, dependency: DependencyName, reason: str) -> None:
        with self._state_lock:
            self._ready_dependencies.discard(dependency)
            if dependency is DependencyName.REDIS:
                self._restart_required = True
            if dependency is DependencyName.RECONCILIATION:
                self._reconciliation_proof = None
                self._reconciliation_in_flight = False
                self._reconciliation_state = ReconciliationState.DEGRADED
                return
            self._halt(f"{dependency.value} failed: {reason}")

    def record_projection_degraded(self, reason: str) -> None:
        normalized_reason = str(reason).strip()
        if not normalized_reason:
            raise ValueError("projection degraded reason is required")
        with self._state_lock:
            self._health_degraded_reasons["projection"] = normalized_reason

    def clear_projection_degraded(self) -> None:
        with self._state_lock:
            self._health_degraded_reasons.pop("projection", None)

    def begin_reconciliation(self, *, halt_active: bool = False) -> int:
        with self._state_lock:
            self._reconciliation_generation += 1
            self._reconciliation_proof = None
            self._reconciliation_in_flight = True
            self._ready_dependencies.discard(DependencyName.RECONCILIATION)
            self._reconciliation_state = ReconciliationState.DEGRADED
            return self._reconciliation_generation

    def record_reconciliation_proof(self, proof: ReconciliationProof) -> None:
        with self._state_lock:
            if proof.generation != self._reconciliation_generation:
                return
            self._reconciliation_proof = proof
            self._last_reconciliation_completed_at = proof.completed_at
            self._reconciliation_in_flight = False
            snapshot = self._refresh_reconciliation_locked()
            if snapshot.status is ReconciliationProofStatus.HEALTHY:
                return

    def configure_lease(
        self,
        *,
        redis_fencing_epoch: str,
        generation: Any,
        freshness_seconds: float,
    ) -> None:
        normalized_epoch = str(redis_fencing_epoch or "").strip()
        if not normalized_epoch:
            raise ValueError("redis fencing epoch is required")
        if generation in (None, "", 0, False):
            raise ValueError("lease generation is required")
        if freshness_seconds <= 0:
            raise ValueError("lease freshness_seconds must be positive")
        with self._state_lock:
            self._lease_required = True
            self._redis_fencing_epoch = normalized_epoch
            self._lease_generation = generation
            self._lease_freshness_seconds = float(freshness_seconds)
            self._lease_last_refresh_at = self._monotonic()

    def record_lease_refresh(
        self,
        *,
        redis_fencing_epoch: str,
        generation: Any,
    ) -> None:
        normalized_epoch = str(redis_fencing_epoch or "").strip()
        with self._state_lock:
            if not self._lease_required:
                raise RuntimeError("runtime lease is not configured")
            if normalized_epoch != self._redis_fencing_epoch:
                self._halt("Redis fencing epoch changed")
                raise RuntimeError("redis fencing epoch does not match runtime")
            if generation != self._lease_generation:
                self._halt("Redis namespace lease generation changed")
                raise RuntimeError("lease generation does not match runtime")
            self._lease_last_refresh_at = self._monotonic()

    def invalidate_lease(self, reason: str) -> None:
        with self._state_lock:
            self._lease_last_refresh_at = None
            self._halt(reason)

    def validate_risk_generation(
        self,
        *,
        runtime_generation: str,
        reconciliation_generation: int,
        lease_generation: Any,
    ) -> None:
        with self._state_lock:
            if runtime_generation != self._runtime_generation:
                raise RuntimeError("runtime generation does not match command")
            if not self._lease_required:
                return
            if lease_generation != self._lease_generation:
                raise RuntimeError("lease generation does not match command")
            self._assert_lease_fresh_locked()

    def apply_operator_state(self, state: TradingState, reason: str) -> None:
        with self._state_lock:
            if state is TradingState.ACTIVE and self._restart_required:
                raise RuntimeError("cannot switch ACTIVE while restart is required")
            if state is TradingState.ACTIVE:
                self._assert_lease_fresh_locked()
            if state is TradingState.ACTIVE and not self.readiness.ready:
                raise RuntimeError("cannot switch ACTIVE before readiness is true")
            self._trading_state = state
            if state is TradingState.HALTED:
                self._halt_reason = reason
            elif state is TradingState.ACTIVE:
                self._halt_reason = ""

    def force_halt(self, reason: str) -> None:
        normalized_reason = str(reason).strip()
        if not normalized_reason:
            normalized_reason = "runtime safety halt"
        with self._state_lock:
            self._halt(normalized_reason)

    def build_heartbeat(
        self,
        open_orders: Iterable[dict[str, Any]] | None = None,
        exchange_evidence: Mapping[str, Any] | None = None,
    ) -> Heartbeat:
        with self._state_lock:
            provider = self._open_orders_provider
        snapshot = open_orders
        if snapshot is None and provider is not None:
            try:
                snapshot = provider()
            except Exception:
                snapshot = None
        normalized_open_orders = None
        if snapshot is not None:
            normalized_open_orders = tuple(dict(item) for item in snapshot)
        positions = None
        regular_orders = None
        algo_orders = None
        positions_snapshot_at = None
        regular_orders_snapshot_at = None
        algo_orders_snapshot_at = None
        if exchange_evidence is not None:
            positions = _heartbeat_evidence_rows(exchange_evidence, "positions")
            regular_orders = _heartbeat_evidence_rows(
                exchange_evidence,
                "regular_orders",
            )
            algo_orders = _heartbeat_evidence_rows(
                exchange_evidence,
                "algo_orders",
            )
            fetched_at = _heartbeat_evidence_timestamp(exchange_evidence)
            positions_snapshot_at = fetched_at
            regular_orders_snapshot_at = fetched_at
            algo_orders_snapshot_at = fetched_at
            normalized_open_orders = regular_orders
        with self._state_lock:
            reconciliation = self._refresh_reconciliation_locked()
            reconciliation_completed_at = reconciliation.completed_at
            if reconciliation.status is ReconciliationProofStatus.IN_FLIGHT:
                reconciliation_completed_at = (
                    self._last_reconciliation_completed_at
                )
            self._heartbeat_sequence += 1
            return Heartbeat(
                account_id=self.config.account_id,
                ts=self._clock.now(),
                trading_state=self._trading_state,
                readiness=self.readiness.ready,
                projection_lag_ms=self._projection_lag_ms,
                reconciliation_state=self._reconciliation_state,
                health_degraded_reasons=tuple(
                    sorted(self._health_degraded_reasons.values())
                ),
                redis_fencing_epoch=(
                    str(self._redis_fencing_epoch)
                    if self._redis_fencing_epoch is not False
                    else None
                ),
                runtime_generation=self._runtime_generation,
                lease_fencing_token=int(self._lease_generation or 0),
                heartbeat_sequence=self._heartbeat_sequence,
                last_event_id=self._last_event_id,
                release_id=self._release_id or None,
                image_digest=self._release_identity["image_digest"] or None,
                config_sha256=self._release_identity["config_sha256"] or None,
                dependency_lock_sha256=(
                    self._release_identity["dependency_lock_sha256"] or None
                ),
                schema_epoch=self._release_identity["schema_epoch"] or None,
                positions=positions,
                regular_orders=regular_orders,
                algo_orders=algo_orders,
                positions_snapshot_at=positions_snapshot_at,
                regular_orders_snapshot_at=regular_orders_snapshot_at,
                algo_orders_snapshot_at=algo_orders_snapshot_at,
                reconciliation_completed_at=reconciliation_completed_at,
                open_orders=normalized_open_orders,
            )

    def build_writer_bootstrap_heartbeat(self) -> Heartbeat:
        with self._state_lock:
            self._heartbeat_sequence += 1
            return Heartbeat(
                account_id=self.config.account_id,
                ts=self._clock.now(),
                trading_state=self._trading_state,
                readiness=self.readiness.ready,
                projection_lag_ms=self._projection_lag_ms,
                reconciliation_state=self._reconciliation_state,
                health_degraded_reasons=tuple(
                    sorted(self._health_degraded_reasons.values())
                ),
                redis_fencing_epoch=(
                    str(self._redis_fencing_epoch)
                    if self._redis_fencing_epoch is not False
                    else None
                ),
                runtime_generation=self._runtime_generation,
                lease_fencing_token=int(self._lease_generation or 0),
                heartbeat_sequence=self._heartbeat_sequence,
                last_event_id=self._last_event_id,
            )

    def set_open_orders_provider(
        self,
        provider: Callable[[], Iterable[dict[str, Any]]],
    ) -> None:
        """Any heartbeat sender (health checker, poller) then carries the open-order
        snapshot automatically — single source, no per-caller wiring."""
        with self._state_lock:
            self._open_orders_provider = provider

    def record_actor_tick(self) -> None:
        with self._state_lock:
            self._actor_tick_age_seconds = 0.0
            if not self._restart_required:
                self._actor_tick_failed = False

    def record_actor_tick_age(self, age_seconds: float) -> None:
        with self._state_lock:
            self._actor_tick_age_seconds = max(float(age_seconds), 0.0)

    def mark_actor_tick_stale(self, age_seconds: float) -> None:
        with self._state_lock:
            self._actor_tick_age_seconds = max(float(age_seconds), 0.0)
            self._actor_tick_failed = True
            self._ready_dependencies.discard(DependencyName.COMMAND_STREAM)
            self._halt(
                f"actor event loop tick stale ({self._actor_tick_age_seconds:.3f}s)"
            )

    def mark_restart_required(self, age_seconds: float) -> None:
        with self._state_lock:
            self._actor_tick_age_seconds = max(float(age_seconds), 0.0)
            self._restart_required = True
            self._ready_dependencies.discard(DependencyName.COMMAND_STREAM)
            self._halt(
                "actor event loop restart required "
                f"({self._actor_tick_age_seconds:.3f}s stale)"
            )

    def send_heartbeat(self, open_orders=None) -> None:
        if self._control_plane is None:
            raise RuntimeError("control-plane client is not configured")
        beat = self.build_heartbeat(open_orders=open_orders)
        receipt = self._control_plane.heartbeat(
            self.config.node_id,
            beat,
        )
        live = (
            str(self.config.binance.environment).strip().lower()
            == "live"
        )
        if _heartbeat_receipt_requires_halt(receipt, live=live):
            reason = _heartbeat_receipt_halt_reason(receipt)
            self.mark_dependency_failed(
                DependencyName.CONTROL_PLANE,
                reason,
            )
            raise RuntimeError(reason)
        self.record_heartbeat_receipt(receipt)
        with self._state_lock:
            self._last_control_plane_ok_at = self._clock.now()

    def record_heartbeat_receipt(self, receipt: Any) -> None:
        gate = getattr(receipt, "release_gate", None)
        raw_live_open_gate = {
            "mode": getattr(gate, "live_open_mode", None),
            "release_id": getattr(gate, "release_id", None),
            "rollout_phase": getattr(gate, "rollout_phase", None),
            "phase_version": getattr(gate, "phase_version", None),
        }
        live_open_gate = normalize_live_open_gate(raw_live_open_gate)
        rollout_phase = ""
        if live_open_gate is not False:
            rollout_phase = str(live_open_gate["rollout_phase"])
        with self._state_lock:
            self._rollout_phase = rollout_phase or None
            self._live_open_gate = live_open_gate

    def validate_live_open_gate(
        self,
        raw_gate: Any,
        *,
        require_normal: bool,
    ) -> None:
        with self._state_lock:
            trusted_gate = self._live_open_gate
        denial = live_open_gate_denial(
            raw_gate,
            trusted_gate=trusted_gate,
            expected_release_id=self._release_id,
            require_normal=require_normal,
        )
        if denial is not None:
            raise RuntimeError(denial)

    def record_projection_progress(
        self, projection_lag_ms: int, last_event_id: Optional[str] = None
    ) -> None:
        with self._state_lock:
            if projection_lag_ms < 0:
                raise ValueError("projection_lag_ms must be non-negative")
            self._projection_lag_ms = projection_lag_ms
            if last_event_id is not None:
                self._last_event_id = last_event_id

    def evaluate_safety(self) -> None:
        now = self._clock.now()
        with self._state_lock:
            was_active = self._trading_state is TradingState.ACTIVE
            reconciliation = self._refresh_reconciliation_locked(now=now)
            if (
                was_active
                and reconciliation.status is not ReconciliationProofStatus.HEALTHY
            ):
                return
            last_control_plane_ok_at = self._last_control_plane_ok_at
            control_plane_ready = (
                DependencyName.CONTROL_PLANE in self._ready_dependencies
            )
        if last_control_plane_ok_at is None:
            if control_plane_ready:
                with self._state_lock:
                    self._halt("control-plane heartbeat missing")
            return

        if (
            now - last_control_plane_ok_at
            > self.config.control_plane.heartbeat_timeout
        ):
            with self._state_lock:
                self._ready_dependencies.discard(DependencyName.CONTROL_PLANE)
                self._halt("control-plane heartbeat stale")
            return

        if self._control_plane is None:
            return
        snapshot_at = self._control_plane.latest_snapshot_generated_at(
            self.config.account_id
        )
        if snapshot_at is None:
            with self._state_lock:
                if DependencyName.PROJECTION in self._ready_dependencies:
                    self._ready_dependencies.discard(DependencyName.PROJECTION)
                    self._halt("control-plane snapshot missing")
            return
        if now - snapshot_at > self.config.control_plane.snapshot_stale_after:
            with self._state_lock:
                self._ready_dependencies.discard(DependencyName.PROJECTION)
                self._halt("control-plane snapshot stale")

    def _refresh_reconciliation_locked(
        self,
        *,
        now: datetime | None = None,
    ) -> ReconciliationProofSnapshot:
        evaluated_at = now
        if evaluated_at is None:
            evaluated_at = self._clock.now()
        snapshot = evaluate_reconciliation_proof(
            self._reconciliation_proof,
            expected_account_id=self.config.account_id,
            expected_node_id=self.config.node_id,
            expected_release_id=self._release_id,
            now=evaluated_at,
            max_age=self._reconciliation_proof_max_age,
            expected_generation=self._reconciliation_generation,
            in_flight=self._reconciliation_in_flight,
        )
        if snapshot.status is ReconciliationProofStatus.HEALTHY:
            self._ready_dependencies.add(DependencyName.RECONCILIATION)
            self._reconciliation_state = ReconciliationState.HEALTHY
            return snapshot
        self._ready_dependencies.discard(DependencyName.RECONCILIATION)
        if snapshot.status in {
            ReconciliationProofStatus.IDENTITY_MISMATCH,
            ReconciliationProofStatus.GENERATION_MISMATCH,
            ReconciliationProofStatus.RELEASE_IDENTITY_MISSING,
            ReconciliationProofStatus.UNHEALTHY,
            ReconciliationProofStatus.INVALID_TIME,
        }:
            self._reconciliation_state = ReconciliationState.DEGRADED
            return snapshot
        self._reconciliation_state = ReconciliationState.DEGRADED
        return snapshot

    def _halt(self, reason: str) -> None:
        self._trading_state = TradingState.HALTED
        self._halt_reason = reason
        # A silent halt cost 2h of debugging on 2026-07-10: every halt must be
        # loud. print reaches docker logs regardless of logging config.
        print(f"[NodeLifecycle] TRADING HALTED: {reason}", flush=True)

    def _halt_if_lease_stale_locked(self) -> None:
        if self._trading_state is not TradingState.ACTIVE:
            return
        if not self._lease_is_stale_locked():
            return
        self._halt("Redis namespace lease freshness expired")

    def _assert_lease_fresh_locked(self) -> None:
        if not self._lease_required:
            return
        if not self._lease_is_stale_locked():
            return
        if self._trading_state is TradingState.ACTIVE:
            self._halt("Redis namespace lease freshness expired")
        raise RuntimeError("Redis namespace lease freshness expired")

    def _lease_is_stale_locked(self) -> bool:
        if not self._lease_required:
            return False
        refreshed_at = self._lease_last_refresh_at
        if refreshed_at is None:
            return True
        age = max(self._monotonic() - refreshed_at, 0.0)
        return age > self._lease_freshness_seconds


def _heartbeat_evidence_rows(
    evidence: Mapping[str, Any],
    field_name: str,
) -> tuple[dict[str, Any], ...]:
    raw_rows = evidence.get(field_name)
    if not isinstance(raw_rows, (list, tuple)):
        raise ValueError(f"exchange evidence {field_name} must be a collection")
    rows = []
    for item in raw_rows:
        if not isinstance(item, Mapping):
            raise ValueError(
                f"exchange evidence {field_name} entries must be objects"
            )
        rows.append(dict(item))
    return tuple(rows)


def _heartbeat_receipt_requires_halt(
    receipt: Any,
    *,
    live: bool,
) -> bool:
    if receipt is None:
        return live
    gate = getattr(receipt, "release_gate", None)
    status = str(
        getattr(gate, "status", "") or ""
    ).strip()
    if status != "pass":
        return True
    if not live:
        return False
    raw_live_open_gate = {
        "mode": getattr(gate, "live_open_mode", None),
        "release_id": getattr(gate, "release_id", None),
        "rollout_phase": getattr(gate, "rollout_phase", None),
        "phase_version": getattr(gate, "phase_version", None),
    }
    return normalize_live_open_gate(raw_live_open_gate) is False


def _heartbeat_receipt_halt_reason(receipt: Any) -> str:
    if receipt is None:
        return "heartbeat receipt is missing"
    gate = getattr(receipt, "release_gate", None)
    status = str(
        getattr(gate, "status", "") or ""
    ).strip()
    if status != "pass":
        return f"heartbeat release gate failed: {status or 'missing'}"
    raw_live_open_gate = {
        "mode": getattr(gate, "live_open_mode", None),
        "release_id": getattr(gate, "release_id", None),
        "rollout_phase": getattr(gate, "rollout_phase", None),
        "phase_version": getattr(gate, "phase_version", None),
    }
    if normalize_live_open_gate(raw_live_open_gate) is False:
        return "heartbeat live open gate is invalid"
    return "heartbeat receipt requires sticky HALT"


def _heartbeat_evidence_timestamp(
    evidence: Mapping[str, Any],
) -> datetime:
    fetched_at = evidence.get("fetched_at")
    if not isinstance(fetched_at, datetime):
        raise ValueError("exchange evidence fetched_at must be a datetime")
    if fetched_at.tzinfo is None:
        raise ValueError("exchange evidence fetched_at must be timezone-aware")
    return fetched_at.astimezone(timezone.utc)


def _default_reconciliation_proof_max_age(config: NodeConfig) -> timedelta:
    configured = os.environ.get("NAUTILUS_RECONCILIATION_PROOF_MAX_AGE_SECONDS")
    if configured:
        try:
            seconds = float(configured)
        except ValueError as exc:
            raise ValueError(
                "NAUTILUS_RECONCILIATION_PROOF_MAX_AGE_SECONDS must be numeric"
            ) from exc
        if seconds <= 0:
            raise ValueError(
                "NAUTILUS_RECONCILIATION_PROOF_MAX_AGE_SECONDS must be positive"
            )
        return timedelta(seconds=seconds)
    return timedelta(minutes=max(config.reconciliation.interval_mins * 2, 1))


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
