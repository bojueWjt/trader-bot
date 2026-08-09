from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .lifecycle import NodeLifecycle


@dataclass(frozen=True)
class HealthResponse:
    status_code: int
    body: dict[str, object]


class HealthService:
    """Separate liveness and readiness surfaces for process supervisors."""

    def __init__(
        self,
        lifecycle: NodeLifecycle,
        process_liveness: Callable[[], bool] | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._process_liveness = process_liveness

    def set_process_liveness_provider(
        self,
        provider: Callable[[], bool],
    ) -> None:
        self._process_liveness = provider

    def liveness(self) -> HealthResponse:
        live = self._resolve_process_liveness()
        return HealthResponse(
            status_code=200 if live else 503,
            body={
                "live": live,
                "account_id": self._lifecycle.config.account_id,
                "node_id": self._lifecycle.config.node_id,
                "trading_state": self._lifecycle.trading_state.value,
            },
        )

    def readiness(self) -> HealthResponse:
        readiness = self._lifecycle.readiness
        return HealthResponse(
            status_code=200 if readiness.ready else 503,
            body={
                "ready": readiness.ready,
                "missing": [dependency.value for dependency in readiness.missing],
                "degraded": [
                    {
                        "dependency": dependency.value,
                        "reason": reason,
                    }
                    for dependency, reason in readiness.degraded
                ],
                "trading_state": self._lifecycle.trading_state.value,
                "halt_reason": self._lifecycle.halt_reason,
            },
        )

    def _resolve_process_liveness(self) -> bool:
        provider = self._process_liveness
        if provider is None:
            return True
        try:
            return bool(provider())
        except Exception:
            return False
