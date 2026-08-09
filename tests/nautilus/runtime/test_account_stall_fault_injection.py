from __future__ import annotations

import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app import run_node  # noqa: E402
from runtime.bounded_task_worker import BoundedTaskWorker  # noqa: E402


class _LeaseGuard:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Session:
    def stop(self, deadline: float) -> bool:
        del deadline
        return True


class _Node:
    def stop(self) -> None:
        return

    def dispose(self) -> None:
        return


def test_cleanup_retains_lease_when_bounded_worker_stop_returns_false() -> None:
    started = Event()
    release = Event()

    def block(_task: object) -> None:
        started.set()
        release.wait(timeout=5.0)

    worker: BoundedTaskWorker[object] = BoundedTaskWorker(
        "account-a.writer",
        block,
        capacity=1,
        task_timeout_seconds=False,
    )
    original_stop = worker.stop
    worker.stop = lambda: original_stop(timeout_seconds=0.01)  # type: ignore[method-assign]
    worker.start()
    assert worker.submit(object()) is True
    assert started.wait(timeout=1.0)

    lease_guard = _LeaseGuard()
    runtime = SimpleNamespace(
        control_plane_session=_Session(),
        trading_node=_Node(),
        background_workers=[worker],
        redis_runtime_safety_guard=None,
        redis_runtime_safety_client=None,
        namespace_lease_guard=lease_guard,
    )

    try:
        with pytest.raises(RuntimeError, match="runtime cleanup failed"):
            run_node._cleanup_runtime(runtime, False)
    finally:
        release.set()
        original_stop(timeout_seconds=1.0)

    assert lease_guard.closed is False
