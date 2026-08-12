from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

import persistence.redis_namespace_lease as lease_module  # noqa: E402
from persistence.redis_namespace_lease import (  # noqa: E402
    NamespaceLeaseGuard,
    RedisNamespaceLeaseError,
    RedisNamespaceLeaseLost,
)


class _Lease:
    def __init__(
        self,
        *,
        refresh_error: Exception | None = None,
        block_refresh: bool = False,
    ) -> None:
        self.acquire_calls = 0
        self.refresh_calls = 0
        self.release_calls = 0
        self.refresh_started = threading.Event()
        self.refresh_release = threading.Event()
        self._refresh_error = refresh_error
        self._block_refresh = block_refresh

    def acquire(self) -> Any:
        self.acquire_calls += 1
        return SimpleNamespace(fencing_token=41)

    def refresh(self) -> Any:
        self.refresh_calls += 1
        self.refresh_started.set()
        if self._block_refresh:
            self.refresh_release.wait(timeout=1.0)
        if self._refresh_error is not None:
            raise self._refresh_error
        return SimpleNamespace(fencing_token=41)

    def release(self) -> bool:
        self.release_calls += 1
        return True


def test_guard_acquires_once_and_refreshes_independently() -> None:
    lease = _Lease()
    guard = NamespaceLeaseGuard(
        lease,
        refresh_interval_seconds=0.01,
    )

    first = guard.acquire()
    second = guard.acquire()

    assert first is second
    assert lease.acquire_calls == 1
    assert lease.refresh_started.wait(timeout=1.0)
    assert guard.is_healthy is True

    guard.close()

    assert guard.is_running is False
    assert lease.release_calls == 1


def test_guard_refresh_failure_notifies_once_and_releases_on_close() -> None:
    lease = _Lease(refresh_error=RuntimeError("redis unavailable"))
    failures: list[str] = []
    guard = NamespaceLeaseGuard(
        lease,
        refresh_interval_seconds=0.01,
        lease_lost_callback=failures.append,
    )
    guard.acquire()

    assert _wait_until(lambda: bool(guard.failure_reason))
    assert guard.failure_reason == (
        "Redis namespace lease refresh failed: redis unavailable"
    )
    assert failures == [guard.failure_reason]
    assert guard.is_healthy is False
    with pytest.raises(
        RedisNamespaceLeaseLost,
        match="redis unavailable",
    ):
        guard.acquire()

    guard.close()

    assert lease.release_calls == 1


def test_guard_treats_server_expiry_as_terminal_lease_loss() -> None:
    lease = _Lease(
        refresh_error=RedisNamespaceLeaseLost(
            "namespace lease refresh failed closed: expired"
        )
    )
    failures: list[str] = []
    guard = NamespaceLeaseGuard(
        lease,
        refresh_interval_seconds=0.01,
        lease_lost_callback=failures.append,
    )
    guard.acquire()

    assert _wait_until(lambda: bool(guard.failure_reason))
    assert "expired" in guard.failure_reason
    assert failures == [guard.failure_reason]
    assert guard.is_healthy is False

    guard.close()

    assert lease.release_calls == 1


def test_guard_releases_lease_when_renewal_thread_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _Lease()

    class _FailingThread:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def start(self) -> None:
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(lease_module, "Thread", _FailingThread)
    guard = NamespaceLeaseGuard(lease)

    with pytest.raises(
        RedisNamespaceLeaseError,
        match="failed to start",
    ):
        guard.acquire()

    assert lease.acquire_calls == 1
    assert lease.release_calls == 1


def test_guard_attempts_release_when_refresh_thread_misses_join_deadline() -> None:
    lease = _Lease(block_refresh=True)
    guard = NamespaceLeaseGuard(
        lease,
        refresh_interval_seconds=0.01,
        thread_join_timeout_seconds=0.02,
    )
    guard.acquire()
    assert lease.refresh_started.wait(timeout=1.0)

    started_at = time.monotonic()
    with pytest.raises(
        RedisNamespaceLeaseError,
        match="did not stop",
    ):
        guard.close()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.1
    assert lease.release_calls == 1

    lease.refresh_release.set()
    assert _wait_until(lambda: guard.is_running is False)


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())
