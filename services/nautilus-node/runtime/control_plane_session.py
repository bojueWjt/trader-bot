from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from enum import Enum
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 2.0
DEFAULT_COMMAND_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_INTENT_FETCH_INTERVAL_SECONDS = 1.0
DEFAULT_COMMAND_DELIVERY_CAPACITY = 128
DEFAULT_COMMAND_ACK_CAPACITY = 256
DEFAULT_INTENT_DELIVERY_CAPACITY = 256
DEFAULT_EXECUTION_EVENT_CAPACITY = 1024
DEFAULT_QUEUE_DEGRADED_RATIO = 0.8
DEFAULT_RETRY_BUDGET = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.05
DEFAULT_RETRY_MAX_DELAY_SECONDS = 1.0
DEFAULT_RETRY_JITTER_RATIO = 0.2
DEFAULT_CIRCUIT_RESET_SECONDS = 5.0
DEFAULT_OPERATION_TIMEOUT_SECONDS = 15.0
DEFAULT_CONSUMER_FREEZE_THRESHOLD_SECONDS = 60.0
DEFAULT_RETRY_DELAY_SECONDS = DEFAULT_RETRY_BASE_DELAY_SECONDS
_POLL_TOKEN = object()


class SubmissionResult(str, Enum):
    ACCEPTED = "accepted"
    BACKPRESSURED = "backpressured"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class QueuePressure(str, Enum):
    NORMAL = "normal"
    DEGRADED = "degraded"
    FULL = "full"


@dataclass(frozen=True)
class LaneHealth:
    name: str
    queue_depth: int
    capacity: int
    queue_usage_ratio: float
    queue_pressure: str
    in_flight_age_ms: float | bool
    last_attempt_at: float | bool
    last_success_at: float | bool
    last_attempt_age_ms: float | bool
    last_success_age_ms: float | bool
    failure: str | bool
    fatal_failure: str | bool
    circuit_state: str
    circuit_open_count: int
    consecutive_failures: int
    retry_count: int
    timeout_count: int
    error_count: int
    success_count: int
    retry_budget: int
    operation_timeout_seconds: float


@dataclass(frozen=True)
class SessionHealth:
    started: bool
    stopped: bool
    consumers_ready: bool
    process_liveness: bool
    ready: bool
    degraded: bool
    failure_reasons: tuple[str, ...]
    configuration: Mapping[str, Any]
    lanes: Mapping[str, LaneHealth]


class _Lane:
    def __init__(
        self,
        name: str,
        capacity: int,
        *,
        pressure_enabled: bool,
        queue_degraded_ratio: float,
        retry_budget: int,
        circuit_reset_seconds: float,
        operation_timeout_seconds: float,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"{name} capacity must be positive")
        self.name = name
        self.queue: Queue[Any] = Queue(maxsize=capacity)
        self.capacity = int(capacity)
        self.pressure_enabled = bool(pressure_enabled)
        self.queue_degraded_ratio = float(queue_degraded_ratio)
        self.retry_budget = int(retry_budget)
        self.circuit_reset_seconds = float(circuit_reset_seconds)
        self.operation_timeout_seconds = float(operation_timeout_seconds)
        self.lock = Lock()
        self.last_attempt_at: float | bool = False
        self.last_success_at: float | bool = False
        self.in_flight_started_at: float | bool = False
        self.failure: str | bool = False
        self.fatal_failure: str | bool = False
        self.circuit_state = CircuitState.CLOSED
        self.circuit_retry_at: float | bool = False
        self.circuit_open_count = 0
        self.consecutive_failures = 0
        self.retry_count = 0
        self.timeout_count = 0
        self.error_count = 0
        self.success_count = 0
        self.deadline_reported = False

    def begin(self) -> bool:
        now = time.monotonic()
        with self.lock:
            half_open = self.circuit_state is CircuitState.HALF_OPEN
            self.last_attempt_at = now
            self.in_flight_started_at = now
            self.deadline_reported = False
            return half_open

    def succeed(self) -> None:
        now = time.monotonic()
        with self.lock:
            self.last_success_at = now
            self.in_flight_started_at = False
            self.failure = False
            self.circuit_state = CircuitState.CLOSED
            self.circuit_retry_at = False
            self.consecutive_failures = 0
            self.success_count += 1
            self.deadline_reported = False

    def fail(self, exc: BaseException) -> tuple[str, bool]:
        detail = _exception_detail(exc)
        with self.lock:
            self.in_flight_started_at = False
            if self.deadline_reported:
                return str(self.failure), True
            self.failure = detail
            self.consecutive_failures += 1
            self.error_count += 1
            if isinstance(exc, TimeoutError):
                self.timeout_count += 1
            return detail, False

    def record_retry(self) -> None:
        with self.lock:
            self.retry_count += 1

    def open_circuit(self) -> bool:
        now = time.monotonic()
        with self.lock:
            return self._open_circuit_locked(now)

    def complete_timeout(self, exc: TimeoutError) -> tuple[str, bool]:
        detail = _exception_detail(exc)
        now = time.monotonic()
        with self.lock:
            self.in_flight_started_at = False
            if self.deadline_reported:
                return str(self.failure), False
            self.deadline_reported = True
            self.failure = detail
            self.consecutive_failures += 1
            self.error_count += 1
            self.timeout_count += 1
            return detail, self._open_circuit_locked(now)

    def expire_if_overdue(
        self,
        now: float,
    ) -> tuple[str, bool] | bool:
        with self.lock:
            started_at = self.in_flight_started_at
            if started_at is False:
                return False
            if self.deadline_reported:
                return False
            elapsed = max(now - float(started_at), 0.0)
            if elapsed <= self.operation_timeout_seconds:
                return False
            detail = (
                f"{self.name} operation exceeded "
                f"{self.operation_timeout_seconds:.3f}s deadline"
            )
            self.deadline_reported = True
            self.failure = detail
            self.consecutive_failures += 1
            self.error_count += 1
            self.timeout_count += 1
            opened = self._open_circuit_locked(now)
            return detail, opened

    def _open_circuit_locked(self, now: float) -> bool:
        already_open = self.circuit_state is CircuitState.OPEN
        self.circuit_state = CircuitState.OPEN
        self.circuit_retry_at = now + self.circuit_reset_seconds
        if already_open:
            return False
        self.circuit_open_count += 1
        return True

    def seconds_until_attempt(self) -> float:
        now = time.monotonic()
        with self.lock:
            if self.circuit_state is not CircuitState.OPEN:
                return 0.0
            retry_at = self.circuit_retry_at
            if retry_at is False:
                return self.circuit_reset_seconds
            remaining = float(retry_at) - now
            if remaining > 0:
                return remaining
            self.circuit_state = CircuitState.HALF_OPEN
            self.circuit_retry_at = False
            return 0.0

    def mark_fatal(self, reason: str) -> bool:
        with self.lock:
            if self.fatal_failure is not False:
                return False
            self.fatal_failure = reason
            self.failure = reason
            return True

    def mark_backpressured(self, reason: str) -> bool:
        with self.lock:
            if self.fatal_failure is not False:
                return False
            self.failure = str(reason)
            self.consecutive_failures += 1
            self.error_count += 1
            return True

    def accepts_submissions(self) -> bool:
        with self.lock:
            return self.fatal_failure is False

    def is_in_flight(self) -> bool:
        with self.lock:
            return self.in_flight_started_at is not False

    def snapshot(self, now: float) -> LaneHealth:
        queue_depth = self.queue.qsize()
        queue_usage_ratio = queue_depth / self.capacity
        queue_pressure = QueuePressure.NORMAL
        if self.pressure_enabled:
            if queue_depth >= self.capacity:
                queue_pressure = QueuePressure.FULL
            elif queue_usage_ratio >= self.queue_degraded_ratio:
                queue_pressure = QueuePressure.DEGRADED
        with self.lock:
            in_flight_started_at = self.in_flight_started_at
            in_flight_age_ms: float | bool = False
            if in_flight_started_at is not False:
                in_flight_age_ms = max(
                    (now - float(in_flight_started_at)) * 1000.0,
                    0.0,
                )
            return LaneHealth(
                name=self.name,
                queue_depth=queue_depth,
                capacity=self.capacity,
                queue_usage_ratio=queue_usage_ratio,
                queue_pressure=queue_pressure.value,
                in_flight_age_ms=in_flight_age_ms,
                last_attempt_at=self.last_attempt_at,
                last_success_at=self.last_success_at,
                last_attempt_age_ms=_monotonic_age_ms(
                    now,
                    self.last_attempt_at,
                ),
                last_success_age_ms=_monotonic_age_ms(
                    now,
                    self.last_success_at,
                ),
                failure=self.failure,
                fatal_failure=self.fatal_failure,
                circuit_state=self.circuit_state.value,
                circuit_open_count=self.circuit_open_count,
                consecutive_failures=self.consecutive_failures,
                retry_count=self.retry_count,
                timeout_count=self.timeout_count,
                error_count=self.error_count,
                success_count=self.success_count,
                retry_budget=self.retry_budget,
                operation_timeout_seconds=self.operation_timeout_seconds,
            )


class NodeControlPlaneSession:
    """Owns isolated control-plane workers behind the node session interface."""

    def __init__(
        self,
        *,
        heartbeat: Callable[[], Any] | None = None,
        command_poll: Callable[[int], Iterable[Any]] | None = None,
        command_apply: Callable[[Any], Any] | None = None,
        command_ack: Callable[[Any], None] | None = None,
        intent_replay: Callable[[], Any] | None = None,
        intent_fetch: Callable[[int], Iterable[Any]] | None = None,
        intent_deliver: Callable[[Any], None] | None = None,
        execution_event_sink: Callable[[Any], None] | None = None,
        heartbeat_interval_seconds: float = (
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS
        ),
        command_poll_interval_seconds: float = (
            DEFAULT_COMMAND_POLL_INTERVAL_SECONDS
        ),
        intent_fetch_interval_seconds: float = (
            DEFAULT_INTENT_FETCH_INTERVAL_SECONDS
        ),
        command_delivery_capacity: int = (
            DEFAULT_COMMAND_DELIVERY_CAPACITY
        ),
        command_ack_capacity: int = DEFAULT_COMMAND_ACK_CAPACITY,
        intent_delivery_capacity: int = (
            DEFAULT_INTENT_DELIVERY_CAPACITY
        ),
        execution_event_capacity: int = DEFAULT_EXECUTION_EVENT_CAPACITY,
        queue_degraded_ratio: float = DEFAULT_QUEUE_DEGRADED_RATIO,
        retry_budget: int = DEFAULT_RETRY_BUDGET,
        retry_base_delay_seconds: float = (
            DEFAULT_RETRY_BASE_DELAY_SECONDS
        ),
        retry_max_delay_seconds: float = DEFAULT_RETRY_MAX_DELAY_SECONDS,
        retry_jitter_ratio: float = DEFAULT_RETRY_JITTER_RATIO,
        circuit_reset_seconds: float = DEFAULT_CIRCUIT_RESET_SECONDS,
        operation_timeout_seconds: float = (
            DEFAULT_OPERATION_TIMEOUT_SECONDS
        ),
        retry_delay_seconds: float | None = None,
        failure_callback: Callable[[str, str], None] | None = None,
        success_callback: Callable[[str], None] | None = None,
        fatal_termination_hook: Callable[[str], None] | None = None,
        consumer_ready: Callable[[], bool] | None = None,
        consumer_progress: Callable[[], float | bool | None] | None = None,
        consumer_freeze_threshold_seconds: float = (
            DEFAULT_CONSUMER_FREEZE_THRESHOLD_SECONDS
        ),
        monotonic_clock: Callable[[], float] | None = None,
        random_source: Callable[[], float] | None = None,
        thread_name_prefix: str = "node-control-plane",
    ) -> None:
        _require_positive_interval(
            "heartbeat_interval_seconds",
            heartbeat_interval_seconds,
        )
        _require_positive_interval(
            "command_poll_interval_seconds",
            command_poll_interval_seconds,
        )
        _require_positive_interval(
            "intent_fetch_interval_seconds",
            intent_fetch_interval_seconds,
        )
        if retry_delay_seconds is not None:
            retry_base_delay_seconds = retry_delay_seconds
        _require_positive_interval(
            "retry_base_delay_seconds",
            retry_base_delay_seconds,
        )
        _require_positive_interval(
            "retry_max_delay_seconds",
            retry_max_delay_seconds,
        )
        _require_positive_interval(
            "circuit_reset_seconds",
            circuit_reset_seconds,
        )
        _require_positive_interval(
            "operation_timeout_seconds",
            operation_timeout_seconds,
        )
        _require_positive_interval(
            "consumer_freeze_threshold_seconds",
            consumer_freeze_threshold_seconds,
        )
        if retry_max_delay_seconds < retry_base_delay_seconds:
            raise ValueError(
                "retry_max_delay_seconds must be at least "
                "retry_base_delay_seconds"
            )
        if retry_budget < 1:
            raise ValueError("retry_budget must be positive")
        if queue_degraded_ratio <= 0 or queue_degraded_ratio >= 1:
            raise ValueError(
                "queue_degraded_ratio must be between zero and one"
            )
        if retry_jitter_ratio < 0 or retry_jitter_ratio > 1:
            raise ValueError(
                "retry_jitter_ratio must be between zero and one"
            )
        if (
            fatal_termination_hook is not None
            and not callable(fatal_termination_hook)
        ):
            raise TypeError("fatal termination hook must be callable")
        self._heartbeat = heartbeat
        self._command_poll = command_poll
        self._command_apply = command_apply
        self._command_ack = command_ack
        self._intent_replay = intent_replay
        self._intent_fetch = intent_fetch
        self._intent_deliver = intent_deliver
        self._execution_event_sink = execution_event_sink
        self._heartbeat_interval_seconds = float(
            heartbeat_interval_seconds
        )
        self._command_poll_interval_seconds = float(
            command_poll_interval_seconds
        )
        self._intent_fetch_interval_seconds = float(
            intent_fetch_interval_seconds
        )
        self._queue_degraded_ratio = float(queue_degraded_ratio)
        self._retry_budget = int(retry_budget)
        self._retry_base_delay_seconds = float(
            retry_base_delay_seconds
        )
        self._retry_max_delay_seconds = float(
            retry_max_delay_seconds
        )
        self._retry_jitter_ratio = float(retry_jitter_ratio)
        self._circuit_reset_seconds = float(circuit_reset_seconds)
        self._operation_timeout_seconds = float(
            operation_timeout_seconds
        )
        self._failure_callback = failure_callback
        self._success_callback = success_callback
        self._fatal_termination_hook = fatal_termination_hook
        self._consumer_ready_check = consumer_ready
        self._consumer_progress_check = consumer_progress
        self._consumer_freeze_threshold_seconds = float(
            consumer_freeze_threshold_seconds
        )
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._random_source = random_source or random.random
        self._thread_name_prefix = str(thread_name_prefix).strip()
        if not self._thread_name_prefix:
            raise ValueError("thread_name_prefix is required")

        lane_options = {
            "queue_degraded_ratio": self._queue_degraded_ratio,
            "retry_budget": self._retry_budget,
            "circuit_reset_seconds": self._circuit_reset_seconds,
            "operation_timeout_seconds": self._operation_timeout_seconds,
        }
        self._lanes = {
            "heartbeat": _Lane(
                "heartbeat",
                1,
                pressure_enabled=False,
                **lane_options,
            ),
            "command_poll": _Lane(
                "command_poll",
                1,
                pressure_enabled=False,
                **lane_options,
            ),
            "command_delivery": _Lane(
                "command_delivery",
                command_delivery_capacity,
                pressure_enabled=True,
                **lane_options,
            ),
            "command_ack": _Lane(
                "command_ack",
                command_ack_capacity,
                pressure_enabled=True,
                **lane_options,
            ),
            "intent_fetch": _Lane(
                "intent_fetch",
                1,
                pressure_enabled=False,
                **lane_options,
            ),
            "intent_delivery": _Lane(
                "intent_delivery",
                intent_delivery_capacity,
                pressure_enabled=True,
                **lane_options,
            ),
            "execution_event": _Lane(
                "execution_event",
                execution_event_capacity,
                pressure_enabled=True,
                **lane_options,
            ),
        }
        self._stop = Event()
        self._termination = Event()
        self._consumers_ready = Event()
        if self._consumer_ready_check is None:
            self._consumers_ready.set()
        self._consumer_progress_lock = Lock()
        self._consumer_progress_value: float | None = None
        self._consumer_progress_observed_at: float | None = None
        self._stop_deadline_lock = Lock()
        self._stop_deadline: float | bool = False
        self._drain_failed = Event()
        self._fatal_process = Event()
        self._fatal_termination_lock = Lock()
        self._fatal_termination_invoked = False
        self._lifecycle_lock = Lock()
        self._threads: list[Thread] = []
        self._startup_thread: Thread | None = None
        self._startup_failure: str | bool = False
        self._started = False
        self._startup_complete = False
        self._stopped = False
        self._intent_replayed = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            if self._stopped:
                raise RuntimeError(
                    "control-plane session cannot restart after stop"
                )
            watchdog = self._thread("watchdog", self._run_watchdog)
            startup_thread = self._thread(
                "startup",
                self._run_startup,
            )
            self._threads = [watchdog]
            self._startup_thread = startup_thread
            self._started = True
            watchdog.start()
            startup_thread.start()

    def mark_consumers_ready(self) -> None:
        self._consumers_ready.set()

    def wait_for_termination(self, timeout: float | None = None) -> bool:
        return self._termination.wait(timeout=timeout)

    def stop(self, deadline: float) -> bool:
        cutoff = float(deadline)
        with self._stop_deadline_lock:
            current_deadline = self._stop_deadline
            if current_deadline is False:
                self._stop_deadline = cutoff
            else:
                self._stop_deadline = min(
                    float(current_deadline),
                    cutoff,
                )
        self._stop.set()
        self._termination.set()
        startup_thread = self._startup_thread
        if startup_thread is not None:
            remaining = max(cutoff - time.monotonic(), 0.0)
            startup_thread.join(timeout=remaining)
        for thread in tuple(self._threads):
            remaining = max(cutoff - time.monotonic(), 0.0)
            thread.join(timeout=remaining)
        startup_thread = self._startup_thread
        startup_stopped = (
            startup_thread is None
            or not startup_thread.is_alive()
        )
        stopped = (
            startup_stopped
            and all(
                not thread.is_alive()
                for thread in self._threads
            )
            and not self._drain_failed.is_set()
            and self._delivery_queues_are_empty()
        )
        with self._lifecycle_lock:
            self._stopped = stopped
        return stopped

    def submit_execution_event(self, event: Any) -> SubmissionResult:
        if self._stop.is_set():
            return SubmissionResult.BACKPRESSURED
        if self._execution_event_sink is None:
            return SubmissionResult.BACKPRESSURED
        accepted = self._submit_to_lane(
            self._lanes["execution_event"],
            event,
        )
        if not accepted:
            return SubmissionResult.BACKPRESSURED
        return SubmissionResult.ACCEPTED

    def snapshot(self) -> SessionHealth:
        now = time.monotonic()
        lanes = {
            name: lane.snapshot(now)
            for name, lane in self._lanes.items()
        }
        with self._lifecycle_lock:
            started = self._started
            stopped = self._stopped
            startup_complete = self._startup_complete
            startup_thread = self._startup_thread
            threads = tuple(self._threads)
        startup_running = (
            startup_complete
            or (
                startup_thread is not None
                and startup_thread.is_alive()
            )
        )
        process_liveness = (
            started
            and startup_running
            and not stopped
            and not self._stop.is_set()
            and not self._fatal_process.is_set()
            and all(thread.is_alive() for thread in threads)
        )
        failure_reasons = _lane_failure_reasons(lanes)
        degraded = any(_lane_is_degraded(lane) for lane in lanes.values())
        consumers_ready = self._consumers_ready.is_set()
        ready = (
            process_liveness
            and startup_complete
            and consumers_ready
            and not failure_reasons
            and not degraded
        )
        configuration = MappingProxyType(
            {
                "queue_degraded_ratio": self._queue_degraded_ratio,
                "retry_budget": self._retry_budget,
                "retry_base_delay_seconds": (
                    self._retry_base_delay_seconds
                ),
                "retry_max_delay_seconds": (
                    self._retry_max_delay_seconds
                ),
                "retry_jitter_ratio": self._retry_jitter_ratio,
                "circuit_reset_seconds": self._circuit_reset_seconds,
                "operation_timeout_seconds": (
                    self._operation_timeout_seconds
                ),
                "consumer_freeze_threshold_seconds": (
                    self._consumer_freeze_threshold_seconds
                ),
            }
        )
        return SessionHealth(
            started=started,
            stopped=stopped,
            consumers_ready=consumers_ready,
            process_liveness=process_liveness,
            ready=ready,
            degraded=degraded,
            failure_reasons=failure_reasons,
            configuration=configuration,
            lanes=MappingProxyType(lanes),
        )

    def _run_startup(self) -> None:
        if not self._wait_for_consumers_ready():
            return
        try:
            self._run_startup_barriers()
        except BaseException as exc:
            with self._lifecycle_lock:
                self._startup_failure = _exception_detail(exc)
            return
        with self._lifecycle_lock:
            if self._fatal_process.is_set() or self._stop.is_set():
                return
            self._seed_periodic_tokens()
            worker_threads = self._build_worker_threads()
            self._threads.extend(worker_threads)
            for thread in worker_threads:
                thread.start()
            self._startup_complete = True

    def _wait_for_consumers_ready(self) -> bool:
        while not self._stop.is_set():
            if self._consumers_ready.is_set():
                return True
            ready_check = self._consumer_ready_check
            if ready_check is not None:
                try:
                    ready = bool(ready_check())
                except Exception:
                    ready = False
                if ready:
                    self._consumers_ready.set()
                    return True
            self._stop.wait(0.01)
        return False

    def _seed_periodic_tokens(self) -> None:
        if self._heartbeat is not None:
            self._offer_token(self._lanes["heartbeat"])
        if self._command_poll is not None:
            self._offer_token(self._lanes["command_poll"])
        if self._intent_fetch is not None:
            self._offer_token(self._lanes["intent_fetch"])

    def _run_startup_barriers(self) -> None:
        replay = self._intent_replay
        if replay is None or self._intent_replayed:
            return
        self._require_startup_action(
            self._lanes["intent_fetch"],
            replay,
        )
        self._intent_replayed = True

    def _require_startup_action(
        self,
        lane: _Lane,
        action: Callable[[], Any],
    ) -> None:
        succeeded = self._execute_with_retry(
            lane,
            action,
            drain_on_stop=False,
        )
        if succeeded:
            return
        health = lane.snapshot(time.monotonic())
        reason = health.failure
        if reason is False:
            reason = f"{lane.name} startup barrier failed"
        normalized_reason = str(reason)
        self._trigger_fatal_termination(
            lane,
            normalized_reason,
        )
        raise RuntimeError(normalized_reason)

    def _build_worker_threads(self) -> list[Thread]:
        threads: list[Thread] = []
        if self._heartbeat is not None:
            threads.append(
                self._thread(
                    "heartbeat",
                    lambda: self._run_periodic(
                        "heartbeat",
                        self._heartbeat_interval_seconds,
                        self._heartbeat,
                    ),
                )
            )
        if self._command_poll is not None:
            threads.append(
                self._thread(
                    "command-poll",
                    lambda: self._run_periodic(
                        "command_poll",
                        self._command_poll_interval_seconds,
                        self._poll_commands,
                    ),
                )
            )
        if self._command_apply is not None:
            threads.append(
                self._thread(
                    "command-delivery",
                    lambda: self._run_delivery(
                        "command_delivery",
                        self._deliver_command,
                    ),
                )
            )
        if self._command_ack is not None:
            threads.append(
                self._thread(
                    "command-ack",
                    lambda: self._run_delivery(
                        "command_ack",
                        self._command_ack,
                    ),
                )
            )
        if self._intent_fetch is not None:
            threads.append(
                self._thread(
                    "intent-fetch",
                    lambda: self._run_periodic(
                        "intent_fetch",
                        self._intent_fetch_interval_seconds,
                        self._fetch_intents,
                    ),
                )
            )
        if self._intent_deliver is not None:
            threads.append(
                self._thread(
                    "intent-delivery",
                    lambda: self._run_delivery(
                        "intent_delivery",
                        self._intent_deliver,
                    ),
                )
            )
        if self._execution_event_sink is not None:
            threads.append(
                self._thread(
                    "execution-event",
                    lambda: self._run_delivery(
                        "execution_event",
                        self._execution_event_sink,
                    ),
                )
            )
        return threads

    def _thread(
        self,
        lane_name: str,
        target: Callable[[], None],
    ) -> Thread:
        return Thread(
            target=target,
            name=f"{self._thread_name_prefix}.{lane_name}",
            daemon=True,
        )

    def _run_periodic(
        self,
        lane_name: str,
        interval_seconds: float,
        action: Callable[[], None],
    ) -> None:
        lane = self._lanes[lane_name]
        while not self._stop.is_set():
            try:
                lane.queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                self._execute_with_retry(
                    lane,
                    action,
                    drain_on_stop=False,
                )
            finally:
                lane.queue.task_done()
            if self._stop.wait(interval_seconds):
                return
            self._offer_token(lane)

    def _run_delivery(
        self,
        lane_name: str,
        action: Callable[[Any], None],
    ) -> None:
        lane = self._lanes[lane_name]
        while True:
            if self._delivery_should_stop(lane):
                return
            try:
                item = lane.queue.get(timeout=0.05)
            except Empty:
                continue
            delivered = False
            while not delivered:
                if self._stop_deadline_expired():
                    self._drain_failed.set()
                    break
                delivered = self._execute_with_retry(
                    lane,
                    lambda: action(item),
                    drain_on_stop=True,
                )
                if delivered:
                    break
                if self._fatal_process.is_set():
                    break
                if not self._wait_for_circuit(
                    lane,
                    drain_on_stop=True,
                ):
                    self._drain_failed.set()
                    break
            lane.queue.task_done()

    def _execute_with_retry(
        self,
        lane: _Lane,
        action: Callable[[], None],
        *,
        drain_on_stop: bool,
    ) -> bool:
        if not self._wait_for_circuit(
            lane,
            drain_on_stop=drain_on_stop,
        ):
            return False
        attempt = 0
        while attempt < lane.retry_budget:
            half_open = lane.begin()
            started_at = time.monotonic()
            try:
                action()
            except BaseException as exc:
                detail, deadline_reported = lane.fail(exc)
                if deadline_reported:
                    return False
                attempt += 1
                if half_open or attempt >= lane.retry_budget:
                    opened = lane.open_circuit()
                    if opened:
                        self._report_failure(lane.name, exc)
                    return False
                lane.record_retry()
                delay = self._retry_delay(attempt)
                if not self._wait(
                    delay,
                    drain_on_stop=drain_on_stop,
                ):
                    return False
                continue
            elapsed = time.monotonic() - started_at
            if elapsed > lane.operation_timeout_seconds:
                exc = TimeoutError(
                    f"{lane.name} operation exceeded "
                    f"{lane.operation_timeout_seconds:.3f}s deadline"
                )
                _detail, opened = lane.complete_timeout(exc)
                if opened:
                    self._report_failure(lane.name, exc)
                return False
            lane.succeed()
            self._report_success(lane.name)
            return True
        return False

    def _poll_commands(self) -> None:
        poll = self._command_poll
        if poll is None:
            return
        delivery = self._lanes["command_delivery"]
        available = delivery.capacity - delivery.queue.qsize()
        if available <= 0:
            self._mark_lane_capacity_failure(delivery)
            return
        commands, overflowed = _take_bounded(
            poll(available),
            available,
        )
        for command in commands:
            if not self._submit_to_lane(delivery, command):
                raise RuntimeError(
                    "command delivery queue capacity exceeded"
                )
        if overflowed:
            self._mark_lane_capacity_failure(delivery)
            raise RuntimeError(
                "command delivery queue capacity exceeded"
            )

    def _deliver_command(self, command: Any) -> None:
        apply = self._command_apply
        if apply is None:
            return
        acknowledgement = apply(command)
        if acknowledgement is False:
            return
        if acknowledgement is None:
            return
        ack_lane = self._lanes["command_ack"]
        if not self._submit_to_lane(ack_lane, acknowledgement):
            raise RuntimeError("command ACK queue capacity exceeded")

    def _fetch_intents(self) -> None:
        fetch = self._intent_fetch
        if fetch is None:
            return
        delivery = self._lanes["intent_delivery"]
        available = delivery.capacity - delivery.queue.qsize()
        if available <= 0:
            self._mark_lane_capacity_failure(delivery)
            return
        items, overflowed = _take_bounded(
            fetch(available),
            available,
        )
        for item in items:
            if not self._submit_to_lane(delivery, item):
                raise RuntimeError(
                    "intent delivery queue capacity exceeded"
                )
        if overflowed:
            self._mark_lane_capacity_failure(delivery)
            raise RuntimeError(
                "intent delivery queue capacity exceeded"
            )

    def _submit_to_lane(self, lane: _Lane, item: Any) -> bool:
        if not lane.accepts_submissions():
            return False
        try:
            lane.queue.put_nowait(item)
        except Full:
            self._mark_lane_capacity_failure(lane)
            return False
        return True

    def _mark_lane_capacity_failure(self, lane: _Lane) -> None:
        reason = f"{lane.name} queue capacity exceeded"
        lane.mark_backpressured(reason)

    def _offer_token(self, lane: _Lane) -> None:
        try:
            lane.queue.put_nowait(_POLL_TOKEN)
        except Full:
            return

    def _delivery_should_stop(self, lane: _Lane) -> bool:
        if not self._stop.is_set():
            return False
        if lane.queue.empty():
            return True
        if self._stop_deadline_expired():
            self._drain_failed.set()
            return True
        return False

    def _stop_deadline_expired(self) -> bool:
        if not self._stop.is_set():
            return False
        with self._stop_deadline_lock:
            deadline = self._stop_deadline
        if deadline is False:
            return False
        return time.monotonic() >= float(deadline)

    def _run_watchdog(self) -> None:
        interval = min(
            max(self._operation_timeout_seconds / 4.0, 0.001),
            0.05,
        )
        while not self._stop.wait(interval):
            now = time.monotonic()
            for lane in self._lanes.values():
                expired = lane.expire_if_overdue(now)
                if expired is False:
                    continue
                detail, opened = expired
                if opened:
                    self._report_failure(
                        lane.name,
                        TimeoutError(detail),
                    )
            self._check_consumer_progress()

    def _check_consumer_progress(self) -> None:
        callback = self._consumer_progress_check
        if callback is None:
            return
        if not self._consumers_ready.is_set():
            return
        if self._stop.is_set() or self._fatal_process.is_set():
            return

        now = float(self._monotonic_clock())
        try:
            progress = callback()
        except Exception:
            progress = False

        observed_progress = False
        progress_value: float | None = None
        if isinstance(progress, bool):
            observed_progress = progress
        else:
            try:
                progress_value = float(progress)
            except (TypeError, ValueError):
                progress_value = None
            if (
                progress_value is not None
                and not math.isfinite(progress_value)
            ):
                progress_value = None

        with self._consumer_progress_lock:
            if observed_progress:
                self._consumer_progress_observed_at = now
                return
            if progress_value is not None:
                previous = self._consumer_progress_value
                if previous is None or progress_value != previous:
                    self._consumer_progress_value = progress_value
                    self._consumer_progress_observed_at = now
                    return
            observed_at = self._consumer_progress_observed_at
            if observed_at is None:
                self._consumer_progress_observed_at = now
                return
            frozen_for = max(now - observed_at, 0.0)

        if frozen_for <= self._consumer_freeze_threshold_seconds:
            return
        reason = (
            "control-plane consumer progress frozen for more than "
            f"{self._consumer_freeze_threshold_seconds:.3f}s"
        )
        self._trigger_fatal_termination(
            self._lanes["command_poll"],
            reason,
        )

    def _wait_for_circuit(
        self,
        lane: _Lane,
        *,
        drain_on_stop: bool,
    ) -> bool:
        while True:
            remaining = lane.seconds_until_attempt()
            if remaining <= 0:
                return True
            if not self._wait(
                min(remaining, 0.05),
                drain_on_stop=drain_on_stop,
            ):
                return False

    def _wait(
        self,
        delay: float,
        *,
        drain_on_stop: bool,
    ) -> bool:
        bounded_delay = max(float(delay), 0.0)
        if not self._stop.is_set():
            return not self._stop.wait(bounded_delay)
        if not drain_on_stop:
            return False
        with self._stop_deadline_lock:
            deadline = self._stop_deadline
        if deadline is False:
            return False
        remaining = max(float(deadline) - time.monotonic(), 0.0)
        if remaining <= 0:
            return False
        time.sleep(min(bounded_delay, remaining))
        return not self._stop_deadline_expired()

    def _retry_delay(self, failed_attempt: int) -> float:
        exponent = max(int(failed_attempt) - 1, 0)
        base = self._retry_base_delay_seconds * (2 ** exponent)
        bounded = min(base, self._retry_max_delay_seconds)
        jitter = (
            bounded
            * self._retry_jitter_ratio
            * max(min(float(self._random_source()), 1.0), 0.0)
        )
        return min(bounded + jitter, self._retry_max_delay_seconds)

    def _delivery_queues_are_empty(self) -> bool:
        lane_names = (
            "command_delivery",
            "command_ack",
            "intent_delivery",
            "execution_event",
        )
        for lane_name in lane_names:
            if not self._lanes[lane_name].queue.empty():
                return False
        return True

    def _report_failure(
        self,
        lane_name: str,
        exc: BaseException,
    ) -> None:
        callback = self._failure_callback
        if callback is None:
            return
        detail = _exception_detail(exc)
        try:
            callback(lane_name, detail)
        except Exception:
            return

    def _report_success(self, lane_name: str) -> None:
        callback = self._success_callback
        if callback is None:
            return
        try:
            callback(lane_name)
        except Exception:
            return

    def _trigger_fatal_termination(
        self,
        lane: _Lane,
        reason: str,
    ) -> None:
        with self._fatal_termination_lock:
            if self._fatal_termination_invoked:
                return
            self._fatal_termination_invoked = True
            lane.mark_fatal(reason)
            self._fatal_process.set()
            self._stop.set()
            self._termination.set()
        self._report_failure(
            lane.name,
            TimeoutError(reason),
        )
        hook = self._fatal_termination_hook
        if hook is None:
            return
        try:
            hook(reason)
        except Exception:
            return


def _require_positive_interval(name: str, value: float) -> None:
    if float(value) <= 0:
        raise ValueError(f"{name} must be positive")


def _exception_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    return type(exc).__name__


def _monotonic_age_ms(
    now: float,
    timestamp: float | bool,
) -> float | bool:
    if timestamp is False:
        return False
    return max((now - float(timestamp)) * 1000.0, 0.0)


def _lane_failure_reasons(
    lanes: Mapping[str, LaneHealth],
) -> tuple[str, ...]:
    reasons: list[str] = []
    for name, lane in lanes.items():
        if lane.circuit_state != CircuitState.CLOSED.value:
            reasons.append(
                f"{name}.circuit_state={lane.circuit_state}"
            )
        if lane.queue_pressure == QueuePressure.FULL.value:
            reasons.append(
                f"{name}.queue_pressure={lane.queue_pressure}"
            )
        if lane.fatal_failure:
            reasons.append(f"{name}.fatal_failure={lane.fatal_failure}")
    return tuple(reasons)


def _lane_is_degraded(lane: LaneHealth) -> bool:
    if lane.fatal_failure:
        return False
    if lane.failure:
        return True
    if lane.circuit_state != CircuitState.CLOSED.value:
        return True
    return lane.queue_pressure in {
        QueuePressure.DEGRADED.value,
        QueuePressure.FULL.value,
    }


def _take_bounded(
    items: Iterable[Any],
    capacity: int,
) -> tuple[tuple[Any, ...], bool]:
    accepted: list[Any] = []
    overflowed = False
    for item in items:
        if len(accepted) >= capacity:
            overflowed = True
            break
        accepted.append(item)
    return tuple(accepted), overflowed
