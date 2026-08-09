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
    def __init__(
        self,
        *,
        stop_results: tuple[bool, ...] = (True,),
    ) -> None:
        self._stop_results = list(stop_results)

    def stop(self, deadline: float) -> bool:
        del deadline
        if len(self._stop_results) > 1:
            return self._stop_results.pop(0)
        return self._stop_results[0]


class _Node:
    def stop(self) -> None:
        return

    def dispose(self) -> None:
        return


class _RedisSafetyTimeoutGuard:
    def __init__(self) -> None:
        self._stop_calls = 0
        self.running = True

    def stop(self) -> bool:
        self._stop_calls += 1
        if self._stop_calls == 1:
            raise RuntimeError(
                "Redis runtime safety thread exceeded shutdown deadline"
            )
        return False

    def snapshot(self) -> dict[str, bool]:
        return {"running": self.running}


class _RedisSafetyGuard:
    def stop(self) -> bool:
        return True


class _RedisClient:
    def __init__(self, *, close_failures: int = 0) -> None:
        self.close_calls = 0
        self._close_failures = close_failures

    def close(self) -> None:
        self.close_calls += 1
        if self._close_failures > 0:
            self._close_failures -= 1
            raise RuntimeError("redis client close failed")


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


def test_cleanup_retries_failed_session_before_releasing_lease() -> None:
    lease_guard = _LeaseGuard()
    session = _Session(stop_results=(False, True))
    runtime = _runtime(
        lease_guard=lease_guard,
        session=session,
    )

    with pytest.raises(RuntimeError, match="runtime cleanup failed"):
        run_node._cleanup_runtime(runtime, False)

    assert runtime.control_plane_session is session
    assert lease_guard.closed is False

    run_node._cleanup_runtime(runtime, False)

    assert runtime.control_plane_session is None
    assert lease_guard.closed is True


def test_cleanup_retries_timed_out_redis_guard_after_thread_stops() -> None:
    lease_guard = _LeaseGuard()
    redis_guard = _RedisSafetyTimeoutGuard()
    redis_client = _RedisClient()
    runtime = _runtime(
        lease_guard=lease_guard,
        redis_guard=redis_guard,
        redis_client=redis_client,
    )

    with pytest.raises(RuntimeError, match="runtime cleanup failed"):
        run_node._cleanup_runtime(runtime, False)

    assert runtime.redis_runtime_safety_guard is redis_guard
    assert runtime.redis_runtime_safety_client is redis_client
    assert redis_client.close_calls == 0
    assert lease_guard.closed is False

    redis_guard.running = False
    run_node._cleanup_runtime(runtime, False)

    assert runtime.redis_runtime_safety_guard is None
    assert runtime.redis_runtime_safety_client is None
    assert redis_client.close_calls == 1
    assert lease_guard.closed is True


def test_cleanup_retries_failed_redis_client_after_guard_stops() -> None:
    lease_guard = _LeaseGuard()
    redis_guard = _RedisSafetyGuard()
    redis_client = _RedisClient(close_failures=1)
    runtime = _runtime(
        lease_guard=lease_guard,
        redis_guard=redis_guard,
        redis_client=redis_client,
    )

    with pytest.raises(RuntimeError, match="runtime cleanup failed"):
        run_node._cleanup_runtime(runtime, False)

    assert runtime.redis_runtime_safety_guard is None
    assert runtime.redis_runtime_safety_client is redis_client
    assert redis_client.close_calls == 1
    assert lease_guard.closed is False

    run_node._cleanup_runtime(runtime, False)

    assert runtime.redis_runtime_safety_client is None
    assert redis_client.close_calls == 2
    assert lease_guard.closed is True


def _runtime(
    *,
    lease_guard: _LeaseGuard,
    session: _Session | None = None,
    redis_guard: object | None = None,
    redis_client: object | None = None,
) -> SimpleNamespace:
    active_session = session
    if active_session is None:
        active_session = _Session()
    return SimpleNamespace(
        control_plane_session=active_session,
        trading_node=_Node(),
        background_workers=[],
        redis_runtime_safety_guard=redis_guard,
        redis_runtime_safety_client=redis_client,
        namespace_lease_guard=lease_guard,
    )
