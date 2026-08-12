from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from app import run_node  # noqa: E402


class _Guard:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> None:
        self._events.append("guard.close")


class _Session:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def start(self) -> None:
        self._events.append("session.start")

    def stop(self, deadline: float) -> bool:
        del deadline
        self._events.append("session.stop")
        return True


class _RedisSafetyGuard:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def stop(self) -> None:
        self._events.append("redis_safety.stop")


class _RedisClient:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> None:
        self._events.append("redis_client.close")


class _Node:
    def __init__(self, events: list[str], fail_stage: str = "") -> None:
        self._events = events
        self._fail_stage = fail_stage

    def build(self) -> None:
        self._events.append("node.build")
        if self._fail_stage == "build":
            raise RuntimeError("build failed")

    def run(self) -> None:
        self._events.append("node.run")
        if self._fail_stage == "run":
            raise RuntimeError("run failed")

    def stop(self) -> None:
        self._events.append("node.stop")

    def dispose(self) -> None:
        self._events.append("node.dispose")


class _Worker:
    def __init__(
        self,
        events: list[str],
        *,
        stopped: bool = True,
    ) -> None:
        self._events = events
        self._stopped = stopped

    def stop(self) -> bool:
        self._events.append("worker.stop")
        return self._stopped


class _Server:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def serve_forever(self) -> None:
        self._events.append("server.serve")

    def shutdown(self) -> None:
        self._events.append("server.shutdown")

    def server_close(self) -> None:
        self._events.append("server.close")


def test_dry_run_with_trading_node_releases_process_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runtime = _runtime(events)
    monkeypatch.setattr(run_node, "build_account_runtime", lambda *args, **kwargs: runtime)

    result = run_node.main(
        [
            "--config",
            "node.json",
            "--dry-run",
            "--build-trading-node",
        ]
    )

    assert result == 0
    assert events == [
        "session.stop",
        "node.stop",
        "node.dispose",
        "redis_safety.stop",
        "redis_client.close",
        "guard.close",
    ]


@pytest.mark.parametrize(
    ("fail_stage", "message"),
    [
        ("readiness", "readiness failed"),
        ("build", "build failed"),
        ("run", "run failed"),
    ],
)
def test_runtime_failures_stop_node_and_release_guard(
    monkeypatch: pytest.MonkeyPatch,
    fail_stage: str,
    message: str,
) -> None:
    events: list[str] = []
    runtime = _runtime(events, fail_stage=fail_stage)
    server = _Server(events)
    monkeypatch.setattr(run_node, "build_account_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(run_node, "build_health_server", lambda *args, **kwargs: server)

    def readiness(_runtime: Any) -> None:
        events.append("readiness")
        if fail_stage == "readiness":
            raise RuntimeError("readiness failed")

    monkeypatch.setattr(run_node, "run_startup_readiness_checks", readiness)

    with pytest.raises(RuntimeError, match=message):
        run_node.main(["--config", "node.json"])

    assert events[-8:] == [
        "server.shutdown",
        "server.close",
        "session.stop",
        "node.stop",
        "node.dispose",
        "redis_safety.stop",
        "redis_client.close",
        "guard.close",
    ]
    if fail_stage == "run":
        assert "session.start" in events
        assert events.index("session.start") < events.index("node.run")
    else:
        assert "session.start" not in events


def test_cleanup_stops_external_writers_before_releasing_lease() -> None:
    events: list[str] = []
    runtime = _runtime(events)
    runtime.background_workers.append(_Worker(events))

    run_node._cleanup_runtime(runtime, False)

    assert events == [
        "session.stop",
        "node.stop",
        "node.dispose",
        "worker.stop",
        "redis_safety.stop",
        "redis_client.close",
        "guard.close",
    ]


def test_cleanup_retains_lease_when_writer_worker_remains_alive() -> None:
    events: list[str] = []
    runtime = _runtime(events)
    runtime.background_workers.append(
        _Worker(events, stopped=False)
    )

    with pytest.raises(
        RuntimeError,
        match="runtime cleanup failed",
    ):
        run_node._cleanup_runtime(runtime, False)

    assert "worker.stop" in events
    assert "guard.close" not in events


def _runtime(events: list[str], *, fail_stage: str = "") -> Any:
    return SimpleNamespace(
        config=SimpleNamespace(
            account_id="account-a",
            node_id="node-a",
            binance=SimpleNamespace(environment="live"),
        ),
        lifecycle=SimpleNamespace(trading_state="HALTED"),
        trading_node=_Node(events, fail_stage=fail_stage),
        namespace_lease_guard=_Guard(events),
        redis_runtime_safety_guard=_RedisSafetyGuard(events),
        redis_runtime_safety_client=_RedisClient(events),
        control_plane_session=_Session(events),
        background_workers=[],
    )
