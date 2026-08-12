from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.node import (  # noqa: E402
    _configure_runtime_incident_reporter,
    _mark_runtime_dependency_failed,
    _stop_background_workers,
)
from execution_domain.control_plane import (  # noqa: E402
    IncidentSeverity,
)


class _ControlPlane:
    def __init__(self) -> None:
        self.reports: list[tuple[str, Any]] = []

    def report_incident(self, node_id: str, report: Any) -> object:
        self.reports.append((node_id, report))
        return object()


class _Lifecycle:
    def __init__(self) -> None:
        self.failures: list[tuple[Any, str]] = []

    def mark_dependency_failed(
        self,
        dependency: Any,
        reason: str,
    ) -> None:
        self.failures.append((dependency, reason))


class _Health:
    def __init__(self) -> None:
        self.providers: dict[str, Any] = {}

    def register_provider(self, name: str, provider: Any) -> None:
        self.providers[name] = provider


def test_live_dependency_failure_reports_deduplicated_incident() -> None:
    control_plane = _ControlPlane()
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            account_id="account-a",
            node_id="node-a",
            binance=SimpleNamespace(environment="live"),
        ),
        control_plane=control_plane,
        lifecycle=_Lifecycle(),
        health=_Health(),
        background_workers=[],
        incident_reporter=None,
    )
    _configure_runtime_incident_reporter(runtime)

    _mark_runtime_dependency_failed(
        runtime,
        "redis",
        "maxmemory critical window exceeded",
    )

    worker = runtime.background_workers[0]
    assert worker.wait_empty(timeout_seconds=1.0) is True
    assert len(control_plane.reports) == 1
    node_id, report = control_plane.reports[0]
    assert node_id == "node-a"
    assert report.account_id == "account-a"
    assert report.severity is IncidentSeverity.P1
    assert report.reason == "redis_runtime_failure"
    assert report.summary == (
        "redis failed: maxmemory critical window exceeded"
    )
    assert "production_incident_reporter" in runtime.health.providers

    _stop_background_workers(runtime)
