from __future__ import annotations

import json
import logging
import math
import random
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from inspect import Parameter, signature
from itertools import islice
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import Any

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
DEFAULT_HEALTH_LOG_INTERVAL_SECONDS = 30.0
DEFAULT_RETRY_DELAY_SECONDS = DEFAULT_RETRY_BASE_DELAY_SECONDS
_TERMINAL_HEARTBEAT_MAX_WAIT_SECONDS = 1.0
_LOGGER = logging.getLogger(__name__)
if not _LOGGER.handlers:
    _LOG_HANDLER = logging.StreamHandler(sys.stdout)
    _LOG_HANDLER.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(_LOG_HANDLER)
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False
_TELEMETRY_LANES = frozenset(
    {
        "heartbeat",
        "execution_event",
    }
)
_AUTH_IDENTITY_HTTP_STATUS_CODES = frozenset({401, 403})
_TRANSIENT_CLIENT_HTTP_STATUS_CODES = frozenset({408, 425, 429})
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

    def succeed(self) -> tuple[bool, str]:
        now = time.monotonic()
        with self.lock:
            previous_circuit_state = self.circuit_state.value
            recovered = (
                self.failure is not False
                or self.circuit_state is not CircuitState.CLOSED
                or self.consecutive_failures > 0
            )
            self.last_success_at = now
            self.in_flight_started_at = False
            self.failure = False
            self.circuit_state = CircuitState.CLOSED
            self.circuit_retry_at = False
            self.consecutive_failures = 0
            self.success_count += 1
            self.deadline_reported = False
            return recovered, previous_circuit_state

    def fail(
        self,
        exc: BaseException,
        *,
        open_circuit: bool = False,
    ) -> tuple[str, bool, bool, bool]:
        detail = _exception_detail(exc)
        now = time.monotonic()
        with self.lock:
            self.in_flight_started_at = False
            if self.deadline_reported:
                return str(self.failure), True, False, False
            first_failure = self.failure is False
            self.failure = detail
            self.consecutive_failures += 1
            self.error_count += 1
            if isinstance(exc, TimeoutError):
                self.timeout_count += 1
            opened = False
            if open_circuit:
                opened = self._open_circuit_locked(now)
            return detail, False, first_failure, opened

    def record_retry(self) -> None:
        with self.lock:
            self.retry_count += 1

    def open_circuit(self) -> bool:
        now = time.monotonic()
        with self.lock:
            return self._open_circuit_locked(now)

    def complete_timeout(
        self,
        exc: TimeoutError,
    ) -> tuple[str, bool, bool, bool]:
        detail = _exception_detail(exc)
        now = time.monotonic()
        with self.lock:
            self.in_flight_started_at = False
            if self.deadline_reported:
                return str(self.failure), False, False, False
            first_failure = self.failure is False
            self.deadline_reported = True
            self.failure = detail
            self.consecutive_failures += 1
            self.error_count += 1
            self.timeout_count += 1
            opened = self._open_circuit_locked(now)
            return detail, opened, first_failure, True

    def expire_if_overdue(
        self,
        now: float,
    ) -> tuple[str, bool, bool] | bool:
        with self.lock:
            started_at = self.in_flight_started_at
            if started_at is False:
                return False
            if self.deadline_reported:
                return False
            elapsed = max(now - float(started_at), 0.0)
            if elapsed <= self.operation_timeout_seconds:
                return False
            first_failure = self.failure is False
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
            return detail, opened, first_failure

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

    def mark_backpressured(self, reason: str) -> tuple[bool, bool]:
        with self.lock:
            if self.fatal_failure is not False:
                return False, False
            first_failure = self.failure is False
            self.failure = str(reason)
            self.consecutive_failures += 1
            self.error_count += 1
            return True, first_failure

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
        intent_replay: Callable[..., Any] | None = None,
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
        health_log_interval_seconds: float = (
            DEFAULT_HEALTH_LOG_INTERVAL_SECONDS
        ),
        log_callback: Callable[[Mapping[str, Any]], None] | None = None,
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
        _require_positive_interval(
            "health_log_interval_seconds",
            health_log_interval_seconds,
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
        if log_callback is not None and not callable(log_callback):
            raise TypeError("log callback must be callable")
        self._heartbeat = heartbeat
        self._command_poll = command_poll
        self._command_apply = command_apply
        self._command_ack = command_ack
        self._intent_replay = intent_replay
        self._intent_replay_accepts_capacity = (
            _callable_accepts_capacity(intent_replay)
        )
        self._legacy_intent_replay_invoked = False
        self._legacy_intent_replay_iterator: Iterator[Any] | None = None
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
        self._health_log_interval_seconds = float(
            health_log_interval_seconds
        )
        self._log_callback = log_callback or _standard_log_callback
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
        self._command_delivery_ids_lock = Lock()
        self._command_delivery_ids: set[str] = set()
        self._stop_deadline_lock = Lock()
        self._stop_deadline: float | bool = False
        self._drain_failed = Event()
        self._fatal_process = Event()
        self._fatal_termination_lock = Lock()
        self._fatal_termination_invoked = False
        self._log_state_lock = Lock()
        self._stop_requested_logged = False
        self._stopped_logged = False
        self._last_health_log_at: float | bool = False
        self._queue_pressure_by_lane = {
            name: QueuePressure.NORMAL.value
            for name in self._lanes
        }
        self._queue_capacity_logged = {
            name: False
            for name in self._lanes
        }
        self._heartbeat_publish_lock = Lock()
        self._lifecycle_lock = Lock()
        self._threads: list[Thread] = []
        self._startup_thread: Thread | None = None
        self._startup_failure: str | bool = False
        self._started = False
        self._startup_complete = False
        self._stopped = False
        self._intent_replayed = intent_replay is None

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
            self._last_health_log_at = float(self._monotonic_clock())
            watchdog.start()
            startup_thread.start()
        self._emit_log(
            "control_plane_session.started",
            level="INFO",
        )

    def mark_consumers_ready(self) -> None:
        self._consumers_ready.set()

    def wait_for_termination(self, timeout: float | None = None) -> bool:
        return self._termination.wait(timeout=timeout)

    def stop(self, deadline: float) -> bool:
        cutoff = float(deadline)
        self._emit_stop_requested()
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
        self._emit_stopped(stopped)
        return stopped

    def submit_execution_event(self, event: Any) -> SubmissionResult:
        if self._stop.is_set() or self._fatal_process.is_set():
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
                "health_log_interval_seconds": (
                    self._health_log_interval_seconds
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
        if (
            self._intent_fetch is not None
            or (
                self._intent_replay is not None
                and not self._intent_replayed
            )
        ):
            self._offer_token(self._lanes["intent_fetch"])

    def _run_startup_barriers(self) -> None:
        replay = self._intent_replay
        if replay is None or self._intent_replayed:
            return

        self._require_startup_action(
            self._lanes["intent_fetch"],
            self._replay_into_delivery_lane,
        )

    def _replay_into_delivery_lane(self) -> None:
        try:
            self._replay_into_delivery_lane_inner()
        except BaseException as exc:
            self._trigger_fatal_termination(
                self._lanes["intent_fetch"],
                _exception_detail(exc),
            )
            raise

    def _replay_into_delivery_lane_inner(self) -> None:
        replay = self._intent_replay
        if replay is None or self._intent_replayed:
            return
        delivery = self._lanes["intent_delivery"]
        available = delivery.capacity - delivery.queue.qsize()
        if available <= 0:
            self._mark_lane_capacity_failure(delivery)
            return
        if not self._intent_replay_accepts_capacity:
            self._replay_legacy_items(delivery, available)
            return

        result = replay(available)
        if result is None or isinstance(result, bool):
            self._intent_replayed = True
            return
        items, overflowed = _take_bounded(result, available)
        accepted_count = self._submit_replay_items(
            delivery,
            items,
        )
        replay_complete = self._commit_replay_result(
            result,
            accepted_count,
        )
        if accepted_count < len(items):
            self._mark_lane_capacity_failure(delivery)
            return
        if replay_complete:
            self._intent_replayed = True
            return
        if overflowed or accepted_count >= available:
            self._mark_lane_capacity_failure(delivery)

    def _replay_legacy_items(
        self,
        delivery: _Lane,
        available: int,
    ) -> None:
        if not self._legacy_intent_replay_invoked:
            replay = self._intent_replay
            if replay is None:
                self._intent_replayed = True
                return
            result = replay()
            self._legacy_intent_replay_invoked = True
            if result is None or isinstance(result, bool):
                self._intent_replayed = True
                return
            self._legacy_intent_replay_iterator = iter(result)
        iterator = self._legacy_intent_replay_iterator
        if iterator is None:
            self._intent_replayed = True
            return
        items = tuple(islice(iterator, available))
        if not items:
            self._legacy_intent_replay_iterator = None
            self._intent_replayed = True
            return
        accepted_count = self._submit_replay_items(
            delivery,
            items,
        )
        if accepted_count < len(items):
            self._mark_lane_capacity_failure(delivery)
            return
        if len(items) < available:
            self._legacy_intent_replay_iterator = None
            self._intent_replayed = True
            return
        self._mark_lane_capacity_failure(delivery)

    def _submit_replay_items(
        self,
        delivery: _Lane,
        items: tuple[Any, ...],
    ) -> int:
        accepted_count = 0
        for item in items:
            if not self._submit_to_lane(delivery, item):
                break
            accepted_count += 1
        return accepted_count

    def _commit_replay_result(
        self,
        result: Any,
        accepted_count: int,
    ) -> bool:
        commit = getattr(result, "commit", None)
        if not callable(commit):
            return False
        return bool(commit(accepted_count))

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
                        self._publish_heartbeat_once,
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
        if (
            self._intent_fetch is not None
            or (
                self._intent_replay is not None
                and not self._intent_replayed
            )
        ):
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
        while (
            not self._stop.is_set()
            and not self._fatal_process.is_set()
        ):
            try:
                lane.queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                if self._fatal_process.is_set():
                    return
                self._execute_with_retry(
                    lane,
                    action,
                    drain_on_stop=False,
                )
            finally:
                lane.queue.task_done()
            if self._fatal_process.is_set():
                return
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
            self._emit_queue_pressure_if_changed(lane)
            if self._fatal_process.is_set():
                if lane_name == "command_delivery":
                    self._forget_command_delivery(item)
                lane.queue.task_done()
                continue
            delivered = False
            while not delivered:
                if self._stop_deadline_expired():
                    self._drain_failed.set()
                    break
                delivered = self._execute_with_retry(
                    lane,
                    lambda delivery_item=item: action(
                        delivery_item
                    ),
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
            if lane_name == "command_delivery":
                self._forget_command_delivery(item)
            lane.queue.task_done()

    def _execute_with_retry(
        self,
        lane: _Lane,
        action: Callable[[], None],
        *,
        drain_on_stop: bool,
    ) -> bool:
        if self._fatal_process.is_set():
            return False
        if not self._wait_for_circuit(
            lane,
            drain_on_stop=drain_on_stop,
        ):
            return False
        attempt = 0
        while attempt < lane.retry_budget:
            if self._fatal_process.is_set():
                return False
            half_open = lane.begin()
            started_at = time.monotonic()
            try:
                action()
            except BaseException as exc:
                fatal = _requires_fatal_termination(lane, exc)
                nonfatal_telemetry = _is_nonfatal_telemetry_rejection(
                    lane,
                    exc,
                )
                should_open = (
                    not fatal
                    and not nonfatal_telemetry
                    and (half_open or attempt + 1 >= lane.retry_budget)
                )
                if nonfatal_telemetry:
                    self._report_failure(lane.name, exc)
                (
                    detail,
                    deadline_reported,
                    first_failure,
                    opened,
                ) = lane.fail(
                    exc,
                    open_circuit=should_open,
                )
                if fatal:
                    if first_failure:
                        self._emit_lane_failure(lane, exc)
                    self._trigger_fatal_termination(lane, detail)
                    return False
                if nonfatal_telemetry:
                    if first_failure:
                        self._emit_lane_failure(lane, exc)
                    if (
                        isinstance(exc, TimeoutError)
                        and not deadline_reported
                    ):
                        self._emit_lane_timeout(lane)
                    return True
                if deadline_reported:
                    return False
                attempt += 1
                if should_open:
                    if first_failure:
                        self._emit_lane_failure(lane, exc)
                    if isinstance(exc, TimeoutError):
                        self._emit_lane_timeout(lane)
                    if opened:
                        self._emit_lane_circuit_open(lane)
                        self._report_failure(lane.name, exc)
                    return False
                lane.record_retry()
                delay = self._retry_delay(attempt)
                if first_failure:
                    self._emit_lane_failure(lane, exc)
                if isinstance(exc, TimeoutError):
                    self._emit_lane_timeout(lane)
                self._emit_lane_retry(lane, delay)
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
                (
                    _detail,
                    opened,
                    first_failure,
                    timeout_recorded,
                ) = lane.complete_timeout(exc)
                if first_failure:
                    self._emit_lane_failure(lane, exc)
                if timeout_recorded:
                    self._emit_lane_timeout(lane)
                if opened:
                    self._emit_lane_circuit_open(lane)
                    self._report_failure(lane.name, exc)
                return False
            recovered, previous_circuit_state = lane.succeed()
            if recovered:
                self._emit_lane_recovered(
                    lane,
                    previous_circuit_state,
                )
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
            if not self._remember_command_delivery(command):
                continue
            if not self._submit_to_lane(delivery, command):
                self._forget_command_delivery(command)
                break
        if overflowed:
            self._mark_lane_capacity_failure(delivery)

    def _remember_command_delivery(self, command: Any) -> bool:
        command_id = _command_delivery_id(command)
        if command_id is False:
            return True
        with self._command_delivery_ids_lock:
            if command_id in self._command_delivery_ids:
                return False
            self._command_delivery_ids.add(command_id)
            return True

    def _forget_command_delivery(self, command: Any) -> None:
        command_id = _command_delivery_id(command)
        if command_id is False:
            return
        with self._command_delivery_ids_lock:
            self._command_delivery_ids.discard(command_id)

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
        if not self._intent_replayed:
            self._replay_into_delivery_lane()
            if not self._intent_replayed:
                return
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
        if self._fatal_process.is_set():
            return False
        if not lane.accepts_submissions():
            return False
        try:
            lane.queue.put_nowait(item)
        except Full:
            self._mark_lane_capacity_failure(lane)
            return False
        self._emit_queue_pressure_if_changed(lane)
        return True

    def _mark_lane_capacity_failure(self, lane: _Lane) -> None:
        reason = f"{lane.name} queue capacity exceeded"
        marked, first_failure = lane.mark_backpressured(reason)
        if not marked:
            return
        if first_failure:
            self._emit_lane_failure_kind(
                lane,
                error_type="QueueCapacityExceeded",
            )
        self._emit_queue_capacity_exceeded(lane)

    def _offer_token(self, lane: _Lane) -> None:
        try:
            lane.queue.put_nowait(_POLL_TOKEN)
        except Full:
            return

    def _delivery_should_stop(self, lane: _Lane) -> bool:
        if self._fatal_process.is_set():
            self._discard_delivery_queue(lane)
            return True
        if not self._stop.is_set():
            return False
        if lane.queue.empty():
            return True
        if self._stop_deadline_expired():
            self._drain_failed.set()
            return True
        return False

    def _discard_delivery_queue(self, lane: _Lane) -> None:
        while True:
            try:
                item = lane.queue.get_nowait()
            except Empty:
                return
            if lane.name == "command_delivery":
                self._forget_command_delivery(item)
            lane.queue.task_done()
            self._emit_queue_pressure_if_changed(lane)

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
                detail, opened, first_failure = expired
                if first_failure:
                    self._emit_lane_failure(
                        lane,
                        TimeoutError(detail),
                    )
                self._emit_lane_timeout(lane)
                if opened:
                    self._emit_lane_circuit_open(lane)
                    self._report_failure(
                        lane.name,
                        TimeoutError(detail),
                    )
            self._check_consumer_progress()
            self._maybe_emit_health_summary()

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
            if self._fatal_process.is_set():
                return False
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
        if self._fatal_process.is_set():
            return False
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

    def _emit_stop_requested(self) -> None:
        with self._log_state_lock:
            if self._stop_requested_logged:
                return
            self._stop_requested_logged = True
        self._emit_log(
            "control_plane_session.stop_requested",
            level="INFO",
        )

    def _emit_stopped(self, stopped: bool) -> None:
        with self._log_state_lock:
            if self._stopped_logged:
                return
            self._stopped_logged = True
        level = "ERROR"
        if stopped:
            level = "INFO"
        self._emit_log(
            "control_plane_session.stopped",
            level=level,
            completed=bool(stopped),
            drain_failed=self._drain_failed.is_set(),
            delivery_queues_empty=self._delivery_queues_are_empty(),
        )

    def _emit_lane_failure(
        self,
        lane: _Lane,
        exc: BaseException,
    ) -> None:
        self._emit_lane_failure_kind(
            lane,
            **_safe_exception_fields(exc),
        )

    def _emit_lane_failure_kind(
        self,
        lane: _Lane,
        **failure_fields: Any,
    ) -> None:
        health = lane.snapshot(time.monotonic())
        self._emit_log(
            "control_plane_session.lane_failure",
            level="WARNING",
            **_lane_log_fields(health),
            **failure_fields,
        )

    def _emit_lane_retry(
        self,
        lane: _Lane,
        delay: float,
    ) -> None:
        health = lane.snapshot(time.monotonic())
        self._emit_log(
            "control_plane_session.lane_retry",
            level="WARNING",
            retry_delay_ms=round(max(delay, 0.0) * 1000.0, 3),
            **_lane_log_fields(health),
        )

    def _emit_lane_timeout(self, lane: _Lane) -> None:
        health = lane.snapshot(time.monotonic())
        self._emit_log(
            "control_plane_session.lane_timeout",
            level="ERROR",
            **_lane_log_fields(health),
        )

    def _emit_lane_circuit_open(self, lane: _Lane) -> None:
        health = lane.snapshot(time.monotonic())
        self._emit_log(
            "control_plane_session.lane_circuit_open",
            level="ERROR",
            circuit_reset_seconds=lane.circuit_reset_seconds,
            **_lane_log_fields(health),
        )

    def _emit_lane_recovered(
        self,
        lane: _Lane,
        previous_circuit_state: str,
    ) -> None:
        health = lane.snapshot(time.monotonic())
        self._emit_log(
            "control_plane_session.lane_recovered",
            level="INFO",
            previous_circuit_state=previous_circuit_state,
            **_lane_log_fields(health),
        )

    def _emit_queue_pressure_if_changed(self, lane: _Lane) -> None:
        if not lane.pressure_enabled:
            return
        with self._log_state_lock:
            health = lane.snapshot(time.monotonic())
            previous = self._queue_pressure_by_lane[lane.name]
            current = health.queue_pressure
            if previous == current:
                return
            self._queue_pressure_by_lane[lane.name] = current
            if current != QueuePressure.FULL.value:
                self._queue_capacity_logged[lane.name] = False
        level = "INFO"
        if current != QueuePressure.NORMAL.value:
            level = "WARNING"
        self._emit_log(
            "control_plane_session.queue_pressure",
            level=level,
            previous_queue_pressure=previous,
            **_lane_log_fields(health),
        )

    def _emit_queue_capacity_exceeded(self, lane: _Lane) -> None:
        with self._log_state_lock:
            if self._queue_capacity_logged[lane.name]:
                return
            self._queue_capacity_logged[lane.name] = True
        health = lane.snapshot(time.monotonic())
        self._emit_log(
            "control_plane_session.queue_capacity_exceeded",
            level="ERROR",
            **_lane_log_fields(health),
        )

    def _maybe_emit_health_summary(self) -> None:
        now = float(self._monotonic_clock())
        with self._log_state_lock:
            last_emitted_at = self._last_health_log_at
            if last_emitted_at is False:
                self._last_health_log_at = now
                return
            elapsed = max(now - float(last_emitted_at), 0.0)
            if elapsed < self._health_log_interval_seconds:
                return
            self._last_health_log_at = now

        health = self.snapshot()
        level = "INFO"
        if health.degraded or not health.process_liveness:
            level = "WARNING"
        lanes = {
            name: _lane_health_log_fields(lane)
            for name, lane in health.lanes.items()
        }
        self._emit_log(
            "control_plane_session.health",
            level=level,
            started=health.started,
            stopped=health.stopped,
            consumers_ready=health.consumers_ready,
            process_liveness=health.process_liveness,
            ready=health.ready,
            degraded=health.degraded,
            consumer_progress_age_ms=(
                self._consumer_progress_age_ms(now)
            ),
            lanes=lanes,
        )

    def _consumer_progress_age_ms(
        self,
        now: float,
    ) -> float | bool:
        with self._consumer_progress_lock:
            observed_at = self._consumer_progress_observed_at
        if observed_at is None:
            return False
        age_ms = max(now - observed_at, 0.0) * 1000.0
        return round(age_ms, 3)

    def _emit_log(
        self,
        event: str,
        *,
        level: str,
        **fields: Any,
    ) -> None:
        record = {
            "event": str(event),
            "level": str(level).upper(),
            **fields,
        }
        try:
            self._log_callback(record)
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
        health = lane.snapshot(time.monotonic())
        self._emit_log(
            "control_plane_session.fatal",
            level="CRITICAL",
            **_lane_log_fields(health),
        )
        self._publish_terminal_heartbeat(lane)
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

    def _publish_terminal_heartbeat(self, fatal_lane: _Lane) -> None:
        heartbeat = self._heartbeat
        if heartbeat is None:
            return
        if fatal_lane.name == "heartbeat":
            return

        completed = Event()
        failures: list[BaseException] = []

        def publish() -> None:
            try:
                self._publish_heartbeat_once()
            except BaseException as exc:
                failures.append(exc)
            finally:
                completed.set()

        worker = Thread(
            target=publish,
            name=f"{self._thread_name_prefix}.terminal-heartbeat",
            daemon=True,
        )
        worker.start()
        timeout = min(
            self._operation_timeout_seconds,
            _TERMINAL_HEARTBEAT_MAX_WAIT_SECONDS,
        )
        if not completed.wait(timeout=timeout):
            self._report_failure(
                "terminal_heartbeat",
                TimeoutError(
                    "terminal heartbeat timed out after "
                    f"{timeout:.3f}s"
                ),
            )
            return
        if failures:
            self._report_failure(
                "terminal_heartbeat",
                failures[0],
            )

    def _publish_heartbeat_once(self) -> None:
        heartbeat = self._heartbeat
        if heartbeat is None:
            return
        with self._heartbeat_publish_lock:
            heartbeat()


def _require_positive_interval(name: str, value: float) -> None:
    if float(value) <= 0:
        raise ValueError(f"{name} must be positive")


def _standard_log_callback(record: Mapping[str, Any]) -> None:
    level_name = str(record.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    message = json.dumps(
        dict(record),
        sort_keys=True,
        separators=(",", ":"),
    )
    _LOGGER.log(level, message)


def _lane_log_fields(lane: LaneHealth) -> dict[str, Any]:
    return {
        "lane": lane.name,
        "queue_depth": lane.queue_depth,
        "queue_capacity": lane.capacity,
        "queue_pressure": lane.queue_pressure,
        "circuit_state": lane.circuit_state,
        "circuit_open_count": lane.circuit_open_count,
        "consecutive_failures": lane.consecutive_failures,
        "retry_count": lane.retry_count,
        "timeout_count": lane.timeout_count,
        "error_count": lane.error_count,
        "success_count": lane.success_count,
    }


def _lane_health_log_fields(lane: LaneHealth) -> dict[str, Any]:
    fields = _lane_log_fields(lane)
    fields.pop("lane")
    fields["queue_usage_ratio"] = round(lane.queue_usage_ratio, 6)
    fields["last_success_age_ms"] = _rounded_age_ms(
        lane.last_success_age_ms
    )
    return fields


def _rounded_age_ms(value: float | bool) -> float | bool:
    if value is False:
        return False
    return round(float(value), 3)


def _safe_exception_fields(exc: BaseException) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "error_type": type(exc).__name__,
    }
    status_code = getattr(exc, "status_code", False)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        fields["status_code"] = status_code
    return fields


def _exception_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    return type(exc).__name__


def _is_fence_conflict(exc: BaseException) -> bool:
    for candidate in _exception_chain(exc):
        status_code = getattr(candidate, "status_code", None)
        if status_code != 409:
            continue
        if getattr(candidate, "is_fence_conflict", False) is True:
            return True
    return False


def _is_identity_conflict(exc: BaseException) -> bool:
    for candidate in _exception_chain(exc):
        if getattr(candidate, "is_identity_conflict", False) is True:
            return True
        if type(candidate).__name__ == "ControlPlaneIdentityError":
            return True
        status_code = getattr(candidate, "status_code", None)
        if status_code in _AUTH_IDENTITY_HTTP_STATUS_CODES:
            return True
    return False


def _is_permanent_publish_rejection(exc: BaseException) -> bool:
    for candidate in _exception_chain(exc):
        if getattr(candidate, "permanent", False) is True:
            return True
        status_code = getattr(candidate, "status_code", None)
        if not isinstance(status_code, int):
            continue
        if status_code in _TRANSIENT_CLIENT_HTTP_STATUS_CODES:
            continue
        if 400 <= status_code < 500:
            return True
    return False


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None:
        identity = id(current)
        if identity in seen:
            return
        seen.add(identity)
        yield current
        cause = current.__cause__
        if cause is None:
            cause = current.__context__
        current = cause


def _requires_fatal_termination(
    lane: _Lane,
    exc: BaseException,
) -> bool:
    if _is_fence_conflict(exc):
        return True
    if _is_identity_conflict(exc):
        return True
    if getattr(exc, "fatal", False) is True:
        return True
    if lane.name in _TELEMETRY_LANES:
        return False
    return _is_permanent_publish_rejection(exc)


def _is_nonfatal_telemetry_rejection(
    lane: _Lane,
    exc: BaseException,
) -> bool:
    if lane.name not in _TELEMETRY_LANES:
        return False
    if _is_fence_conflict(exc):
        return False
    if _is_identity_conflict(exc):
        return False
    if getattr(exc, "fatal", False) is True:
        return False
    return _is_permanent_publish_rejection(exc)


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


def _command_delivery_id(command: Any) -> str | bool:
    value = getattr(command, "command_id", False)
    if value is False or value is None:
        return False
    command_id = str(value).strip()
    if not command_id:
        return False
    return command_id


def _callable_accepts_capacity(
    callback: Callable[..., Any] | None,
) -> bool:
    if callback is None:
        return False
    try:
        parameters = tuple(signature(callback).parameters.values())
    except (TypeError, ValueError):
        return True
    for parameter in parameters:
        if parameter.kind is Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in {
            Parameter.POSITIONAL_ONLY,
            Parameter.POSITIONAL_OR_KEYWORD,
        }:
            return True
    return False


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
