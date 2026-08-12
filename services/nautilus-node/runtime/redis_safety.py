from __future__ import annotations

import math
import time
from collections.abc import Callable
from threading import Event, RLock, Thread, current_thread
from typing import Protocol

from persistence.nautilus_config import RedisRuntimeSafetyConfig

DEFAULT_MEMORY_WARNING_RATIO = 0.60
DEFAULT_MEMORY_DEGRADED_RATIO = 0.75
DEFAULT_MEMORY_CRITICAL_RATIO = 0.85


class RedisRuntimeSafetyClient(Protocol):
    def scan_streams(
        self,
        cursor: int,
        *,
        match: str,
        count: int,
    ) -> tuple[int, list[bytes | str]]:
        ...

    def xtrim_maxlen(
        self,
        key: bytes | str,
        max_entries: int,
    ) -> int:
        ...

    def memory_usage(self, key: bytes | str) -> int | None:
        ...

    def memory_info(self) -> tuple[int, int]:
        ...


def build_redis_runtime_safety_config(
    *,
    stream_root: str,
    stream_max_entries: int,
    stream_max_bytes: int,
    total_stream_max_bytes: int,
    scan_count: int,
    sample_interval_seconds: float,
    critical_window_seconds: float,
    thread_join_timeout_seconds: float,
    memory_warning_ratio: float = DEFAULT_MEMORY_WARNING_RATIO,
    memory_degraded_ratio: float = DEFAULT_MEMORY_DEGRADED_RATIO,
    memory_critical_ratio: float = DEFAULT_MEMORY_CRITICAL_RATIO,
) -> RedisRuntimeSafetyConfig:
    """Build a validated config while preserving pre-threshold call sites."""

    config = RedisRuntimeSafetyConfig(
        stream_root=stream_root,
        stream_max_entries=stream_max_entries,
        stream_max_bytes=stream_max_bytes,
        total_stream_max_bytes=total_stream_max_bytes,
        scan_count=scan_count,
        sample_interval_seconds=sample_interval_seconds,
        critical_window_seconds=critical_window_seconds,
        thread_join_timeout_seconds=thread_join_timeout_seconds,
        memory_warning_ratio=memory_warning_ratio,
        memory_degraded_ratio=memory_degraded_ratio,
        memory_critical_ratio=memory_critical_ratio,
    )
    _validate_config(config)
    return config


class RedisRuntimeSafetyGuard:
    """Continuously bounds active-generation Redis stream capacity."""

    process_owned = True

    def __init__(
        self,
        redis_client: RedisRuntimeSafetyClient,
        *,
        config: RedisRuntimeSafetyConfig,
        halt_callback: Callable[[str], None],
        monotonic_fn: Callable[[], float] = time.monotonic,
        wall_clock_fn: Callable[[], float] = time.time,
    ) -> None:
        if not callable(halt_callback):
            raise TypeError("halt_callback must be callable")
        _validate_config(config)
        self._redis = redis_client
        self._config = config
        self._halt_callback = halt_callback
        self._monotonic_fn = monotonic_fn
        self._wall_clock_fn = wall_clock_fn
        self._lock = RLock()
        self._stop_event = Event()
        self._thread: Thread | bool = False
        self._closed = False
        self._halt_reason = ""
        self._last_error = ""
        self._last_sample_epoch: float | bool = False
        self._memory_pressure = "unknown"
        self._used_memory_bytes = 0
        self._maxmemory_bytes = 0
        self._memory_ratio: float | bool = False
        self._stream_count = 0
        self._stream_bytes = 0
        self._largest_stream_bytes = 0
        self._largest_stream_key = ""
        self._trimmed_entries = 0
        self._critical_since: float | bool = False
        self._critical_duration_seconds = 0.0

    def start(self) -> bool:
        with self._lock:
            if self._halt_reason:
                raise RuntimeError(
                    "Redis runtime safety guard is already halted: "
                    f"{self._halt_reason}"
                )
            if self._closed:
                raise RuntimeError("Redis runtime safety guard is closed")
            thread = self._thread
            if isinstance(thread, Thread) and thread.is_alive():
                return False
            self._stop_event.clear()
            thread = Thread(
                target=self._run,
                name="redis-runtime-safety",
                daemon=True,
            )
            self._thread = thread
        try:
            thread.start()
        except Exception as exc:
            self._record_halt(
                "Redis runtime safety thread failed to start",
                exc,
            )
            raise RuntimeError(
                "Redis runtime safety thread failed to start"
            ) from exc
        return True

    def sample_now(self) -> dict[str, object]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Redis runtime safety guard is closed")
            if self._halt_reason:
                raise RuntimeError(
                    "Redis runtime safety guard is already halted: "
                    f"{self._halt_reason}"
                )
        try:
            self._sample()
        except Exception as exc:
            self._record_halt(
                "Redis runtime safety sample failed",
                exc,
            )
            raise RuntimeError(
                "Redis runtime safety sample failed"
            ) from exc
        with self._lock:
            halt_reason = self._halt_reason
        if halt_reason:
            raise RuntimeError(halt_reason)
        return self.snapshot()

    def stop(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self._stop_event.set()
            thread = self._thread
        thread_alive = False
        if isinstance(thread, Thread) and thread is not current_thread():
            thread.join(
                timeout=self._config.thread_join_timeout_seconds,
            )
            thread_alive = thread.is_alive()
        if thread_alive:
            self._record_halt(
                "Redis runtime safety thread exceeded shutdown deadline"
            )
            raise RuntimeError(
                "Redis runtime safety thread exceeded shutdown deadline"
            )
        return True

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            thread = self._thread
            running = isinstance(thread, Thread) and thread.is_alive()
            return {
                "running": running,
                "halted": bool(self._halt_reason),
                "halt_reason": self._halt_reason,
                "last_error": self._last_error,
                "last_sample_epoch": self._last_sample_epoch,
                "memory_pressure": self._memory_pressure,
                "used_memory_bytes": self._used_memory_bytes,
                "maxmemory_bytes": self._maxmemory_bytes,
                "memory_ratio": self._memory_ratio,
                "memory_warning_ratio": (
                    self._config.memory_warning_ratio
                ),
                "memory_degraded_ratio": (
                    self._config.memory_degraded_ratio
                ),
                "memory_critical_ratio": (
                    self._config.memory_critical_ratio
                ),
                "critical_duration_seconds": (
                    self._critical_duration_seconds
                ),
                "stream_root": self._config.stream_root,
                "stream_count": self._stream_count,
                "stream_bytes": self._stream_bytes,
                "largest_stream_bytes": self._largest_stream_bytes,
                "largest_stream_key": self._largest_stream_key,
                "trimmed_entries": self._trimmed_entries,
                "stream_max_entries": self._config.stream_max_entries,
                "stream_max_bytes": self._config.stream_max_bytes,
                "total_stream_max_bytes": (
                    self._config.total_stream_max_bytes
                ),
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._sample()
            except Exception as exc:  # noqa: BLE001
                self._record_halt(
                    "Redis runtime safety sample failed",
                    exc,
                )
                return
            with self._lock:
                if self._halt_reason:
                    return
            if self._stop_event.wait(
                self._config.sample_interval_seconds
            ):
                return

    def _sample(self) -> None:
        used_memory, maxmemory = self._redis.memory_info()
        _validate_memory_sample(used_memory, maxmemory)
        memory_pressure = _memory_pressure(
            used_memory,
            maxmemory,
            warning_ratio=self._config.memory_warning_ratio,
            degraded_ratio=self._config.memory_degraded_ratio,
            critical_ratio=self._config.memory_critical_ratio,
        )
        memory_ratio: float | bool = False
        if maxmemory > 0:
            memory_ratio = used_memory / maxmemory
        sampled_monotonic = float(self._monotonic_fn())
        if memory_pressure == "unbounded":
            with self._lock:
                self._critical_since = False
                self._last_sample_epoch = float(self._wall_clock_fn())
                self._memory_pressure = memory_pressure
                self._used_memory_bytes = used_memory
                self._maxmemory_bytes = maxmemory
                self._memory_ratio = False
                self._critical_duration_seconds = 0.0
            self._record_halt(
                "Redis maxmemory is unbounded: maxmemory=0"
            )
            return

        stream_keys = self._scan_stream_keys()
        stream_bytes = 0
        largest_stream_bytes = 0
        largest_stream_key: bytes | str | bool = False
        oversized_stream: tuple[bytes | str, int] | bool = False
        trimmed_entries = 0
        for stream_key in stream_keys:
            trimmed_entries += self._redis.xtrim_maxlen(
                stream_key,
                self._config.stream_max_entries,
            )
            usage = self._redis.memory_usage(stream_key)
            if usage is None:
                continue
            stream_bytes += usage
            if usage > largest_stream_bytes:
                largest_stream_bytes = usage
                largest_stream_key = stream_key
            if (
                oversized_stream is False
                and usage > self._config.stream_max_bytes
            ):
                oversized_stream = (stream_key, usage)

        critical_duration_seconds = 0.0

        with self._lock:
            if memory_pressure == "critical":
                if self._critical_since is False:
                    self._critical_since = sampled_monotonic
                critical_since = self._critical_since
                if critical_since is not False:
                    critical_duration_seconds = max(
                        0.0,
                        sampled_monotonic - critical_since,
                    )
            else:
                self._critical_since = False
            self._last_sample_epoch = float(self._wall_clock_fn())
            self._memory_pressure = memory_pressure
            self._used_memory_bytes = used_memory
            self._maxmemory_bytes = maxmemory
            self._memory_ratio = memory_ratio
            self._critical_duration_seconds = (
                critical_duration_seconds
            )
            self._stream_count = len(stream_keys)
            self._stream_bytes = stream_bytes
            self._largest_stream_bytes = largest_stream_bytes
            self._largest_stream_key = _display_key(largest_stream_key)
            self._trimmed_entries = trimmed_entries

        if oversized_stream is not False:
            stream_key, usage = oversized_stream
            self._record_halt(
                "Redis stream byte limit exceeded: "
                f"{_display_key(stream_key)} uses {usage} bytes, limit "
                f"{self._config.stream_max_bytes}"
            )
        if stream_bytes > self._config.total_stream_max_bytes:
            self._record_halt(
                "Redis total stream byte limit exceeded: "
                f"{stream_bytes} bytes, limit "
                f"{self._config.total_stream_max_bytes}"
            )
        if (
            memory_pressure == "critical"
            and critical_duration_seconds
            >= self._config.critical_window_seconds
        ):
            self._record_halt(
                "Redis maxmemory "
                f"{self._config.memory_critical_ratio:.0%} "
                "critical window exceeded: "
                f"threshold {self._config.memory_critical_ratio:.3f}, "
                f"ratio {memory_ratio}, duration "
                f"{critical_duration_seconds:.3f}s, limit "
                f"{self._config.critical_window_seconds:.3f}s"
            )

    def _scan_stream_keys(self) -> list[bytes | str]:
        pattern = f"{self._config.stream_root}:*"
        cursor = 0
        seen_cursors: set[int] = set()
        seen_keys: set[bytes | str] = set()
        stream_keys: list[bytes | str] = []
        while True:
            if cursor in seen_cursors:
                raise RuntimeError("Redis stream SCAN cursor repeated")
            seen_cursors.add(cursor)
            cursor, page = self._redis.scan_streams(
                cursor,
                match=pattern,
                count=self._config.scan_count,
            )
            for stream_key in page:
                if stream_key in seen_keys:
                    continue
                seen_keys.add(stream_key)
                stream_keys.append(stream_key)
            if cursor == 0:
                return stream_keys

    def _record_halt(
        self,
        reason: str,
        exc: Exception | bool = False,
    ) -> None:
        detail = ""
        if exc is not False:
            detail = str(exc).strip()
            if not detail:
                detail = type(exc).__name__
        halt_reason = reason
        if detail:
            halt_reason = f"{reason}: {detail}"
        with self._lock:
            if self._halt_reason:
                return
            self._halt_reason = halt_reason
            self._last_error = detail
        try:
            self._halt_callback(halt_reason)
        except Exception as callback_exc:  # noqa: BLE001
            with self._lock:
                self._last_error = (
                    "halt callback failed: "
                    f"{type(callback_exc).__name__}: {callback_exc}"
                )


def _memory_pressure(
    used_memory: int,
    maxmemory: int,
    *,
    warning_ratio: float,
    degraded_ratio: float,
    critical_ratio: float,
) -> str:
    if maxmemory <= 0:
        return "unbounded"
    ratio = used_memory / maxmemory
    if ratio >= critical_ratio:
        return "critical"
    if ratio >= degraded_ratio:
        return "degraded"
    if ratio >= warning_ratio:
        return "warning"
    return "normal"


def _display_key(value: bytes | str | bool) -> str:
    if value is False:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _validate_config(config: RedisRuntimeSafetyConfig) -> None:
    if not isinstance(config.stream_root, str) or not config.stream_root.strip():
        raise ValueError("Redis runtime safety stream_root must be non-empty")
    _validate_positive_integer(
        config.stream_max_entries,
        "Redis stream_max_entries",
    )
    _validate_positive_integer(
        config.stream_max_bytes,
        "Redis stream_max_bytes",
    )
    _validate_positive_integer(
        config.total_stream_max_bytes,
        "Redis total_stream_max_bytes",
    )
    if config.total_stream_max_bytes < config.stream_max_bytes:
        raise ValueError(
            "Redis total_stream_max_bytes must cover stream_max_bytes"
        )
    _validate_positive_integer(
        config.scan_count,
        "Redis runtime safety scan_count",
    )
    _validate_positive_number(
        config.sample_interval_seconds,
        "Redis runtime safety sample_interval_seconds",
    )
    _validate_positive_number(
        config.critical_window_seconds,
        "Redis runtime safety critical_window_seconds",
    )
    _validate_positive_number(
        config.thread_join_timeout_seconds,
        "Redis runtime safety thread_join_timeout_seconds",
    )
    _validate_ratio(
        config.memory_warning_ratio,
        "Redis runtime safety memory_warning_ratio",
    )
    _validate_ratio(
        config.memory_degraded_ratio,
        "Redis runtime safety memory_degraded_ratio",
    )
    _validate_ratio(
        config.memory_critical_ratio,
        "Redis runtime safety memory_critical_ratio",
    )
    warning_ratio = float(config.memory_warning_ratio)
    degraded_ratio = float(config.memory_degraded_ratio)
    critical_ratio = float(config.memory_critical_ratio)
    if not warning_ratio < degraded_ratio < critical_ratio:
        raise ValueError(
            "Redis runtime safety memory ratios must increase from warning "
            "to degraded to critical"
        )


def _validate_positive_integer(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def _validate_positive_number(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a positive finite number")


def _validate_ratio(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number >= 1:
        raise ValueError(f"{label} must be between zero and one")


def _validate_memory_sample(used_memory: object, maxmemory: object) -> None:
    values = (
        ("used_memory", used_memory),
        ("maxmemory", maxmemory),
    )
    for label, value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(
                f"Redis memory sample {label} must be a nonnegative integer"
            )
