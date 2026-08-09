from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from runtime.bounded_task_worker import BoundedTaskWorker  # noqa: E402


def test_submit_returns_before_blocking_handler_finishes() -> None:
    started = Event()
    release = Event()
    handled: list[str] = []

    def handle(value: str) -> None:
        started.set()
        release.wait(timeout=1.0)
        handled.append(value)

    worker = BoundedTaskWorker(
        "test-worker",
        handle,
        capacity=2,
    )
    worker.start()
    try:
        submitted_at = time.monotonic()
        accepted = worker.submit("payload")
        elapsed = time.monotonic() - submitted_at

        assert accepted is True
        assert elapsed < 0.01
        assert started.wait(timeout=1.0)
        assert handled == []
        release.set()
        assert worker.wait_empty(timeout_seconds=1.0)
        assert handled == ["payload"]
    finally:
        release.set()
        worker.stop()


def test_queue_full_fails_closed_through_overflow_callback() -> None:
    started = Event()
    release = Event()
    overflows: list[str] = []

    def handle(value: str) -> None:
        del value
        started.set()
        release.wait(timeout=1.0)

    worker = BoundedTaskWorker(
        "test-worker",
        handle,
        capacity=1,
        on_overflow=overflows.append,
    )
    worker.start()
    try:
        assert worker.submit("active") is True
        assert started.wait(timeout=1.0)
        assert worker.submit("queued") is True
        assert worker.submit("overflow") is False
        assert overflows == ["test-worker queue capacity exceeded"]
    finally:
        release.set()
        worker.stop()


def test_handler_error_is_reported_and_worker_continues() -> None:
    errors: list[str] = []
    handled: list[str] = []

    def handle(value: str) -> None:
        if value == "bad":
            raise RuntimeError("boom")
        handled.append(value)

    worker = BoundedTaskWorker(
        "test-worker",
        handle,
        capacity=2,
        on_error=errors.append,
    )
    worker.start()
    try:
        assert worker.submit("bad") is True
        assert worker.submit("good") is True
        assert worker.wait_empty(timeout_seconds=1.0)
    finally:
        worker.stop()

    assert errors == ["test-worker handler failed: RuntimeError('boom')"]
    assert handled == ["good"]


def test_handler_timeout_is_reported_while_task_remains_blocked() -> None:
    started = Event()
    release = Event()
    errors: list[str] = []
    fatal_reasons: list[str] = []

    def handle(value: str) -> None:
        del value
        started.set()
        release.wait(timeout=1.0)

    worker = BoundedTaskWorker(
        "test-worker",
        handle,
        capacity=1,
        task_timeout_seconds=0.02,
        on_error=errors.append,
        on_timeout=fatal_reasons.append,
    )
    worker.start()
    try:
        assert worker.submit("blocked") is True
        assert started.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while not errors and time.monotonic() < deadline:
            time.sleep(0.001)

        assert errors == [
            "test-worker handler exceeded 0.020s task timeout"
        ]
        assert fatal_reasons == [
            "test-worker handler exceeded 0.020s task timeout"
        ]
        assert worker.snapshot().in_flight is True
    finally:
        release.set()
        worker.stop()


def test_stop_timeout_keeps_running_worker_visible_and_reports_error() -> None:
    started = Event()
    release = Event()
    errors: list[str] = []
    fatal_reasons: list[str] = []

    def handle(value: str) -> None:
        del value
        started.set()
        release.wait(timeout=1.0)

    worker = BoundedTaskWorker(
        "test-worker",
        handle,
        capacity=1,
        task_timeout_seconds=1.0,
        on_error=errors.append,
        on_timeout=fatal_reasons.append,
    )
    worker.start()
    assert worker.submit("blocked") is True
    assert started.wait(timeout=1.0)

    worker.stop(timeout_seconds=0.01)

    assert worker.snapshot().running is True
    assert errors == [
        "test-worker failed to stop within 0.010s"
    ]
    assert fatal_reasons == [
        "test-worker failed to stop within 0.010s"
    ]
    release.set()
    worker.stop(timeout_seconds=1.0)
