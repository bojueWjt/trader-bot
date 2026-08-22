from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from threading import RLock
from typing import Any

from .lifecycle import NodeLifecycle


@dataclass(frozen=True)
class HealthResponse:
    status_code: int
    body: dict[str, object]


class HealthService:
    """Separate liveness and readiness surfaces for process supervisors."""

    def __init__(self, lifecycle: NodeLifecycle) -> None:
        self._lifecycle = lifecycle
        self._provider_lock = RLock()
        self._providers: dict[str, Callable[[], Any]] = {}

    def register_provider(
        self,
        name: str,
        provider: Callable[[], Any],
    ) -> None:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("health provider name is required")
        if not callable(provider):
            raise TypeError("health provider must be callable")
        with self._provider_lock:
            self._providers[normalized_name] = provider

    def liveness(self) -> HealthResponse:
        restart_required = self._lifecycle.restart_required
        providers, provider_errors = self._provider_snapshots()
        provider_liveness_issues, provider_readiness_issues = (
            _provider_health_issues(providers)
        )
        del provider_readiness_issues
        live = (
            not restart_required
            and not provider_errors
            and not provider_liveness_issues
        )
        return HealthResponse(
            status_code=200 if live else 503,
            body={
                "live": live,
                "account_id": self._lifecycle.config.account_id,
                "node_id": self._lifecycle.config.node_id,
                "trading_state": self._lifecycle.trading_state.value,
                "actor_tick_age_seconds": self._lifecycle.actor_tick_age_seconds,
                "restart_required": restart_required,
                "providers": providers,
                "health_provider_errors": provider_errors,
                "health_provider_issues": provider_liveness_issues,
            },
        )

    def readiness(self) -> HealthResponse:
        readiness = self._lifecycle.readiness
        reconciliation = readiness.reconciliation
        restart_required = self._lifecycle.restart_required
        providers, provider_errors = self._provider_snapshots()
        provider_liveness_issues, provider_readiness_issues = (
            _provider_health_issues(providers)
        )
        del provider_liveness_issues
        ready = (
            readiness.ready
            and not restart_required
            and not provider_errors
            and not provider_readiness_issues
        )
        return HealthResponse(
            status_code=200 if ready else 503,
            body={
                "ready": ready,
                # Identity fields let deploy-side quiesce checks assert
                # they are talking to the intended account's node.
                "account_id": self._lifecycle.config.account_id,
                "node_id": self._lifecycle.config.node_id,
                "missing": [dependency.value for dependency in readiness.missing],
                "trading_state": self._lifecycle.trading_state.value,
                "halt_reason": self._lifecycle.halt_reason,
                "actor_tick_age_seconds": self._lifecycle.actor_tick_age_seconds,
                "restart_required": restart_required,
                "reconciliation_status": reconciliation.status.value,
                "reconciliation_proof_age_seconds": (
                    reconciliation.proof_age_seconds
                ),
                "reconciliation_proof_fresh": reconciliation.fresh,
                "providers": providers,
                "health_provider_errors": provider_errors,
                "health_provider_issues": provider_readiness_issues,
            },
        )

    def _provider_snapshots(
        self,
    ) -> tuple[dict[str, object], dict[str, str]]:
        with self._provider_lock:
            providers = tuple(self._providers.items())
        snapshots: dict[str, object] = {}
        errors: dict[str, str] = {}
        for name, provider in providers:
            try:
                snapshots[name] = _health_value(provider())
            except Exception as exc:
                detail = str(exc).strip()
                if not detail:
                    detail = type(exc).__name__
                errors[name] = detail
        return snapshots, errors


def _provider_health_issues(
    providers: Mapping[str, object],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    liveness: dict[str, list[str]] = {}
    readiness: dict[str, list[str]] = {}
    for name, raw_snapshot in providers.items():
        if not isinstance(raw_snapshot, Mapping):
            continue
        live_issues: list[str] = []
        ready_issues: list[str] = []
        if raw_snapshot.get("process_liveness") is False:
            live_issues.append("process_liveness=false")
            ready_issues.append("process_liveness=false")
        if raw_snapshot.get("running") is False:
            ready_issues.append("running=false")
        if raw_snapshot.get("halted") is True:
            ready_issues.append("halted=true")
        lanes = raw_snapshot.get("lanes")
        if isinstance(lanes, Mapping):
            for lane_name, raw_lane in lanes.items():
                if not isinstance(raw_lane, Mapping):
                    continue
                circuit_state = raw_lane.get("circuit_state")
                if circuit_state not in (None, "closed"):
                    ready_issues.append(
                        f"{lane_name}.circuit_state={circuit_state}"
                    )
                queue_pressure = raw_lane.get("queue_pressure")
                if queue_pressure not in (None, "normal"):
                    ready_issues.append(
                        f"{lane_name}.queue_pressure={queue_pressure}"
                    )
                fatal_failure = raw_lane.get("fatal_failure")
                if fatal_failure:
                    ready_issues.append(
                        f"{lane_name}.fatal_failure={fatal_failure}"
                    )
        if live_issues:
            liveness[name] = live_issues
        if ready_issues:
            readiness[name] = ready_issues
    return liveness, readiness


def _health_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _health_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _health_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_health_value(item) for item in value]
    if isinstance(value, list):
        return [_health_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_health_value(item) for item in value)
    return value
