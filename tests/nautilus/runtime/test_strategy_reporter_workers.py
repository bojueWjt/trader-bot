from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"


def _load_isolated_nautilus():
    saved_path = list(sys.path)
    saved_app = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))
    sys.path.insert(0, str(SERVICE_ROOT))
    try:
        from runtime.exchange_cancel_adapter import (  # noqa: E402
            TerminalExchangeRequest,
        )

        spec = importlib.util.spec_from_file_location(
            "_g1_nautilus_node_reporter",
            SERVICE_ROOT / "app" / "node.py",
        )
        if spec is None or spec.loader is None:
            raise ImportError("nautilus node.py is unavailable")
        node_mod = importlib.util.module_from_spec(spec)
        sys.modules["_g1_nautilus_node_reporter"] = node_mod
        spec.loader.exec_module(node_mod)
        return SimpleNamespace(
            TerminalExchangeRequest=TerminalExchangeRequest,
            build_denial_reporter=node_mod._build_denial_reporter,
            build_live_canary_risk_reporter=node_mod._build_live_canary_risk_reporter,
            build_protection_event_reporter=node_mod._build_protection_event_reporter,
            build_terminal_exchange_worker=node_mod._build_terminal_exchange_worker,
            stop_background_workers=node_mod._stop_background_workers,
        )
    finally:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                if name not in saved_app:
                    sys.modules.pop(name, None)
        sys.modules.update(saved_app)
        sys.path[:] = saved_path


_nautilus = _load_isolated_nautilus()
TerminalExchangeRequest = _nautilus.TerminalExchangeRequest
_build_denial_reporter = _nautilus.build_denial_reporter
_build_live_canary_risk_reporter = _nautilus.build_live_canary_risk_reporter
_build_protection_event_reporter = _nautilus.build_protection_event_reporter
_build_terminal_exchange_worker = _nautilus.build_terminal_exchange_worker
_stop_background_workers = _nautilus.stop_background_workers


class _Lifecycle:
    def __init__(self) -> None:
        self.failures: list[tuple[str, str]] = []
        self.halts: list[str] = []

    def mark_dependency_failed(self, dependency, reason: str) -> None:
        value = str(getattr(dependency, "value", dependency))
        self.failures.append((value, reason))

    def force_halt(self, reason: str) -> None:
        self.halts.append(reason)


class _Health:
    def __init__(self) -> None:
        self.providers: dict[str, object] = {}

    def register_provider(self, name: str, provider) -> None:
        self.providers[name] = provider


class _BlockingControlPlane:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.acks: list[dict[str, object]] = []

    def ack_intent(self, **kwargs) -> None:
        self.started.set()
        self.release.wait(timeout=1.0)
        self.acks.append(kwargs)


class _Projection:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.ingested: list[object] = []
        self.flush_count = 0

    def ingest_event(self, event: object) -> object:
        self.started.set()
        self.release.wait(timeout=1.0)
        self.ingested.append(event)
        if isinstance(event, dict):
            event_id = event.get("event_id") or event.get("event_key") or "raw"
        else:
            event_id = getattr(event, "event_id", None) or "raw"
        return SimpleNamespace(outcome="DURABLE", event_id=str(event_id))

    def flush(self) -> list[str]:
        self.flush_count += 1
        return []


class _BlockingMirror:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def refresh(self, **_kwargs):
        self.started.set()
        self.release.wait(timeout=1)
        return ()


class _Adapter:
    def cancel(self, *_args, **_kwargs):
        raise AssertionError("no cancel request expected")


class _TerminalStrategy:
    def __init__(self) -> None:
        self.results: list[object] = []

    def enqueue_terminal_exchange_result(self, result: object) -> None:
        self.results.append(result)


def _runtime(
    control_plane,
    projection,
    *,
    exchange_state_mirror=None,
    exchange_cancel_adapter=None,
):
    return SimpleNamespace(
        config=SimpleNamespace(
            account_id="account-a",
            node_id="node-a",
        ),
        control_plane=control_plane,
        lifecycle=_Lifecycle(),
        health=_Health(),
        projection_actor=projection,
        background_workers=[],
        incident_reporter=None,
        exchange_state_mirror=exchange_state_mirror,
        exchange_cancel_adapter=exchange_cancel_adapter,
    )


def test_denial_reporter_keeps_strategy_callback_under_10ms() -> None:
    control_plane = _BlockingControlPlane()
    runtime = _runtime(control_plane, _Projection())
    reporter = _build_denial_reporter(runtime)
    intent = SimpleNamespace(intent_id=uuid4())
    denial = SimpleNamespace(reason="risk", detail="blocked")

    started_at = time.monotonic()
    reporter(intent, denial)
    elapsed = time.monotonic() - started_at

    try:
        assert elapsed < 0.01
        assert control_plane.started.wait(timeout=1.0)
        assert control_plane.acks == []
        control_plane.release.set()
        worker = runtime.background_workers[0]
        assert worker.wait_empty(timeout_seconds=1.0)
        assert len(control_plane.acks) == 1
    finally:
        control_plane.release.set()
        _stop_background_workers(runtime)


def test_protection_reporter_moves_spool_and_network_work_off_callback() -> None:
    projection = _Projection()
    runtime = _runtime(_BlockingControlPlane(), projection)
    reporter = _build_protection_event_reporter(runtime)
    event = {
        "event_type": "ProtectionUpdated",
        "event_key": "entry-1",
        "instrument_id": "SOLUSDT-PERP.BINANCE",
        "intent_id": str(uuid4()),
        "client_order_id": "protection-1",
        "payload": {"role": "stop_loss"},
    }

    started_at = time.monotonic()
    accepted = reporter(event)
    elapsed = time.monotonic() - started_at

    try:
        assert accepted is True
        assert elapsed < 0.01
        assert projection.started.wait(timeout=1.0)
        assert projection.ingested == []
        projection.release.set()
        worker = runtime.background_workers[0]
        assert worker.wait_empty(timeout_seconds=1.0)
        assert len(projection.ingested) == 1
        assert isinstance(projection.ingested[0], dict)
        assert projection.flush_count == 1
    finally:
        projection.release.set()
        _stop_background_workers(runtime)


def test_live_canary_risk_reporter_moves_fsync_and_decision_off_callback() -> None:
    class _RiskStrategy:
        def __init__(self) -> None:
            self.started = Event()
            self.release = Event()
            self.tasks: list[dict[str, object]] = []
            self.decisions: list[object] = []

        def process_live_canary_risk_task(self, task):
            self.started.set()
            self.release.wait(timeout=1.0)
            self.tasks.append(task)
            return ("close-decision",)

        def publish_live_canary_loss_decision(self, decision) -> None:
            self.decisions.append(decision)

    runtime = _runtime(_BlockingControlPlane(), _Projection())
    strategy = _RiskStrategy()
    reporter = _build_live_canary_risk_reporter(runtime, strategy)

    started_at = time.monotonic()
    accepted = reporter({"kind": "fill", "fill": {"fill_id": "trade-1"}})
    elapsed = time.monotonic() - started_at

    try:
        assert accepted is True
        assert elapsed < 0.01
        assert strategy.started.wait(timeout=1.0)
        assert strategy.tasks == []
        strategy.release.set()
        worker = runtime.background_workers[0]
        assert worker.wait_empty(timeout_seconds=1.0)
        assert strategy.tasks[0]["kind"] == "fill"
        assert strategy.decisions == ["close-decision"]
        assert "live_canary_risk_worker" in runtime.health.providers
    finally:
        strategy.release.set()
        _stop_background_workers(runtime)


def test_terminal_exchange_worker_registers_health_and_full_queue_halts() -> None:
    mirror = _BlockingMirror()
    runtime = _runtime(
        _BlockingControlPlane(),
        _Projection(),
        exchange_state_mirror=mirror,
        exchange_cancel_adapter=_Adapter(),
    )
    strategy = _TerminalStrategy()
    worker = _build_terminal_exchange_worker(runtime, strategy)

    try:
        first = TerminalExchangeRequest(
            request_id="active",
            account_id="account-a",
            operation="refresh",
            purpose="health-test",
            deadline_monotonic=worker.new_deadline(),
        )
        assert worker.submit(first) is True
        assert mirror.started.wait(timeout=1)
        for index in range(64):
            request = TerminalExchangeRequest(
                request_id=f"queued-{index}",
                account_id="account-a",
                operation="refresh",
                purpose="health-test",
                deadline_monotonic=worker.new_deadline(),
            )
            assert worker.submit(request) is True
        overflow = TerminalExchangeRequest(
            request_id="overflow",
            account_id="account-a",
            operation="refresh",
            purpose="health-test",
            deadline_monotonic=worker.new_deadline(),
        )

        assert worker.submit(overflow) is False
        assert runtime.lifecycle.halts
        provider = runtime.health.providers[
            "terminal_exchange_worker"
        ]
        health = provider()
        lane = health["lanes"]["terminal_exchange"]
        assert health["process_liveness"] is False
        assert lane["queue_pressure"] == "degraded"
        assert lane["circuit_state"] == "open"
        assert lane["fatal_failure"]
    finally:
        mirror.release.set()
        _stop_background_workers(runtime)
