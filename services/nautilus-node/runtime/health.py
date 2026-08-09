from __future__ import annotations

from dataclasses import dataclass

from .lifecycle import NodeLifecycle


@dataclass(frozen=True)
class HealthResponse:
    status_code: int
    body: dict[str, object]


class HealthService:
    """Separate liveness and readiness surfaces for process supervisors."""

    def __init__(self, lifecycle: NodeLifecycle) -> None:
        self._lifecycle = lifecycle

    def liveness(self) -> HealthResponse:
        return HealthResponse(
            status_code=200,
            body={
                "live": True,
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
