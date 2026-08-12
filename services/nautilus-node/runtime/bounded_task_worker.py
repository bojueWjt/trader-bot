from __future__ import annotations

import time
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread, Timer
from typing import Callable, Generic, TypeVar


TaskT = TypeVar("TaskT")


@dataclass(frozen=True)
class BoundedTaskWorkerSnapshot:
    name: str
    running: bool
    in_flight: bool
    queue_depth: int
    queue_capacity: int
    last_error: str


class BoundedTaskWorker(Generic[TaskT]):
    def __init__(
        self,
        name: str,
        handler: Callable[[TaskT], None],
        *,
        capacity: int,
        task_timeout_seconds: float | bool = 5.0,
        on_overflow: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        normalized_name = str(name).strip()
        if not normalized_name:
            raise ValueError("worker name is required")
        if capacity < 1:
            raise ValueError("worker capacity must be positive")
        if task_timeout_seconds is False:
            normalized_task_timeout: float | bool = False
        else:
            if (
                isinstance(task_timeout_seconds, bool)
                or task_timeout_seconds <= 0
            ):
                raise ValueError(
                    "task_timeout_seconds must be positive or False"
                )
            normalized_task_timeout = float(task_timeout_seconds)
        if on_timeout is not None and not callable(on_timeout):
            raise TypeError("timeout handler must be callable")
        self._name = normalized_name
        self._handler = handler
        self._capacity = int(capacity)
        self._task_timeout_seconds = normalized_task_timeout
        self._on_overflow = on_overflow
        self._on_error = on_error
        self._on_timeout = on_timeout
        self._queue: Queue[TaskT] = Queue(maxsize=self._capacity)
        self._stop = Event()
        self._in_flight = Event()
        self._thread: Thread | None = None
        self._state_lock = Lock()
        self._last_error = ""
        self._task_sequence = 0
        self._active_task_sequence = 0
        self._timeout_reported = False

    def start(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._stop.clear()
        thread = Thread(
            target=self._run,
            name=self._name,
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def submit(self, task: TaskT) -> bool:
        if self._stop.is_set():
            self._report_overflow(f"{self._name} is stopped")
            return False
        try:
            self._queue.put_nowait(task)
        except Full:
            self._report_overflow(
                f"{self._name} queue capacity exceeded"
            )
            return False
        return True

    def wait_empty(self, *, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() <= deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.001)
        return self._queue.unfinished_tasks == 0

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout_seconds)
        if thread.is_alive():
            reason = (
                f"{self._name} failed to stop within "
                f"{timeout_seconds:.3f}s"
            )
            self._report_error(reason)
            self._report_timeout(reason)
            return False
        if self._thread is thread:
            self._thread = None
        return True

    def set_timeout_handler(
        self,
        handler: Callable[[str], None] | None,
    ) -> None:
        if handler is not None and not callable(handler):
            raise TypeError("timeout handler must be callable")
        with self._state_lock:
            self._on_timeout = handler

    def snapshot(self) -> BoundedTaskWorkerSnapshot:
        thread = self._thread
        running = thread is not None and thread.is_alive()
        with self._state_lock:
            last_error = self._last_error
        return BoundedTaskWorkerSnapshot(
            name=self._name,
            running=running,
            in_flight=self._in_flight.is_set(),
            queue_depth=self._queue.qsize(),
            queue_capacity=self._capacity,
            last_error=last_error,
        )

    def _run(self) -> None:
        while True:
            if self._stop.is_set() and self._queue.empty():
                return
            try:
                task = self._queue.get(timeout=0.05)
            except Empty:
                continue
            task_sequence, timeout_timer = self._begin_task()
            try:
                self._handler(task)
            except Exception as exc:
                self._report_error(
                    f"{self._name} handler failed: {exc!r}"
                )
            finally:
                self._finish_task(task_sequence, timeout_timer)
                self._queue.task_done()

    def _begin_task(self) -> tuple[int, Timer | None]:
        self._in_flight.set()
        with self._state_lock:
            self._task_sequence += 1
            task_sequence = self._task_sequence
            self._active_task_sequence = task_sequence
        timeout_seconds = self._task_timeout_seconds
        if timeout_seconds is False:
            return task_sequence, None
        timer = Timer(
            timeout_seconds,
            self._report_task_timeout,
            args=(task_sequence, timeout_seconds),
        )
        timer.daemon = True
        timer.start()
        return task_sequence, timer

    def _finish_task(
        self,
        task_sequence: int,
        timeout_timer: Timer | None,
    ) -> None:
        with self._state_lock:
            if self._active_task_sequence == task_sequence:
                self._active_task_sequence = 0
        self._in_flight.clear()
        if timeout_timer is not None:
            timeout_timer.cancel()

    def _report_task_timeout(
        self,
        task_sequence: int,
        timeout_seconds: float,
    ) -> None:
        with self._state_lock:
            active = (
                self._active_task_sequence == task_sequence
                and self._in_flight.is_set()
            )
        if not active:
            return
        reason = (
            f"{self._name} handler exceeded "
            f"{timeout_seconds:.3f}s task timeout"
        )
        self._report_error(reason)
        self._report_timeout(reason)

    def _report_overflow(self, reason: str) -> None:
        callback = self._on_overflow
        if callback is not None:
            callback(reason)

    def _report_error(self, reason: str) -> None:
        with self._state_lock:
            self._last_error = reason
        callback = self._on_error
        if callback is not None:
            callback(reason)

    def _report_timeout(self, reason: str) -> None:
        with self._state_lock:
            if self._timeout_reported:
                return
            self._timeout_reported = True
            callback = self._on_timeout
        if callback is not None:
            callback(reason)
