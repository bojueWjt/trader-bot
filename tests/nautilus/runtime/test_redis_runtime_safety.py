from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from persistence.nautilus_config import RedisRuntimeSafetyConfig
from runtime.redis_safety import (
    RedisRuntimeSafetyGuard,
    build_redis_runtime_safety_config,
)

STREAM_ROOT = (
    "trader-TRADER-ACCOUNT-A:"
    "00000000-0000-4000-8000-000000000001:"
    "nautilus:account-a:TRADER-ACCOUNT-A:message-bus"
)


class _FakeRedis:
    def __init__(
        self,
        *,
        pages: dict[int, tuple[int, list[bytes | str]]],
        stream_bytes: dict[bytes | str, int | None],
        memory_samples: list[tuple[int, int]],
        trimmed_entries: dict[bytes | str, int] | None = None,
    ) -> None:
        self._pages = pages
        self._stream_bytes = stream_bytes
        self._memory_samples = list(memory_samples)
        self._trimmed_entries = trimmed_entries
        if self._trimmed_entries is None:
            self._trimmed_entries = {}
        self.scan_calls: list[tuple[int, str, int]] = []
        self.xtrim_calls: list[tuple[bytes | str, int]] = []
        self.memory_usage_calls: list[bytes | str] = []
        self.sample_completed = Event()
        self.sample_count = 0

    def scan_streams(
        self,
        cursor: int,
        *,
        match: str,
        count: int,
    ) -> tuple[int, list[bytes | str]]:
        self.scan_calls.append((cursor, match, count))
        return self._pages[cursor]

    def xtrim_maxlen(
        self,
        key: bytes | str,
        max_entries: int,
    ) -> int:
        self.xtrim_calls.append((key, max_entries))
        trimmed_entries = self._trimmed_entries
        assert trimmed_entries is not None
        return trimmed_entries.get(key, 0)

    def memory_usage(self, key: bytes | str) -> int | None:
        self.memory_usage_calls.append(key)
        return self._stream_bytes[key]

    def memory_info(self) -> tuple[int, int]:
        index = self.sample_count
        if index >= len(self._memory_samples):
            index = len(self._memory_samples) - 1
        sample = self._memory_samples[index]
        self.sample_count += 1
        self.sample_completed.set()
        return sample


class _ManualClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FailingRedis:
    def memory_info(self) -> tuple[int, int]:
        return 1_000, 10_000

    def scan_streams(
        self,
        cursor: int,
        *,
        match: str,
        count: int,
    ) -> tuple[int, list[bytes | str]]:
        del cursor, match, count
        raise TimeoutError("Redis SCAN timed out")


def _config(
    *,
    stream_max_entries: int = 100_000,
    stream_max_bytes: int = 64 * 1024 * 1024,
    total_stream_max_bytes: int = 256 * 1024 * 1024,
    sample_interval_seconds: float = 60.0,
    critical_window_seconds: float = 30.0,
    memory_warning_ratio: float = 0.60,
    memory_degraded_ratio: float = 0.75,
    memory_critical_ratio: float = 0.85,
) -> RedisRuntimeSafetyConfig:
    return build_redis_runtime_safety_config(
        stream_root=STREAM_ROOT,
        stream_max_entries=stream_max_entries,
        stream_max_bytes=stream_max_bytes,
        total_stream_max_bytes=total_stream_max_bytes,
        scan_count=500,
        sample_interval_seconds=sample_interval_seconds,
        critical_window_seconds=critical_window_seconds,
        thread_join_timeout_seconds=1.0,
        memory_warning_ratio=memory_warning_ratio,
        memory_degraded_ratio=memory_degraded_ratio,
        memory_critical_ratio=memory_critical_ratio,
    )


def test_compatible_config_builder_defaults_memory_thresholds() -> None:
    config = build_redis_runtime_safety_config(
        stream_root=STREAM_ROOT,
        stream_max_entries=100_000,
        stream_max_bytes=64 * 1024 * 1024,
        total_stream_max_bytes=256 * 1024 * 1024,
        scan_count=500,
        sample_interval_seconds=5.0,
        critical_window_seconds=30.0,
        thread_join_timeout_seconds=5.0,
    )

    assert config.memory_warning_ratio == 0.60
    assert config.memory_degraded_ratio == 0.75
    assert config.memory_critical_ratio == 0.85


@pytest.mark.parametrize(
    (
        "warning_ratio",
        "degraded_ratio",
        "critical_ratio",
        "message",
    ),
    [
        (True, 0.75, 0.85, "memory_warning_ratio"),
        (0.60, 0.60, 0.85, "must increase"),
        (0.60, 0.75, float("nan"), "memory_critical_ratio"),
        (0.60, 0.75, 1.0, "memory_critical_ratio"),
    ],
)
def test_compatible_config_builder_rejects_invalid_memory_thresholds(
    warning_ratio: float,
    degraded_ratio: float,
    critical_ratio: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _config(
            memory_warning_ratio=warning_ratio,
            memory_degraded_ratio=degraded_ratio,
            memory_critical_ratio=critical_ratio,
        )


def test_guard_trims_active_generation_streams_and_reports_snapshot() -> None:
    orders = f"{STREAM_ROOT}:orders".encode()
    positions = f"{STREAM_ROOT}:positions".encode()
    redis = _FakeRedis(
        pages={
            0: (17, [orders, positions]),
            17: (0, [orders]),
        },
        stream_bytes={
            orders: 4_096,
            positions: 8_192,
        },
        memory_samples=[(1_000, 10_000)],
        trimmed_entries={
            orders: 23,
            positions: 0,
        },
    )
    halt_reasons: list[str] = []
    guard = RedisRuntimeSafetyGuard(
        redis,
        config=_config(),
        halt_callback=halt_reasons.append,
    )

    assert guard.start() is True
    assert redis.sample_completed.wait(timeout=0.5)
    snapshot = guard.snapshot()
    assert guard.stop() is True

    assert redis.scan_calls == [
        (0, f"{STREAM_ROOT}:*", 500),
        (17, f"{STREAM_ROOT}:*", 500),
    ]
    assert redis.xtrim_calls == [
        (orders, 100_000),
        (positions, 100_000),
    ]
    assert redis.memory_usage_calls == [orders, positions]
    assert snapshot["running"] is True
    assert snapshot["halted"] is False
    assert snapshot["halt_reason"] == ""
    assert snapshot["memory_pressure"] == "normal"
    assert snapshot["used_memory_bytes"] == 1_000
    assert snapshot["maxmemory_bytes"] == 10_000
    assert snapshot["memory_ratio"] == 0.1
    assert snapshot["memory_warning_ratio"] == 0.60
    assert snapshot["memory_degraded_ratio"] == 0.75
    assert snapshot["memory_critical_ratio"] == 0.85
    assert snapshot["stream_count"] == 2
    assert snapshot["stream_bytes"] == 12_288
    assert snapshot["largest_stream_bytes"] == 8_192
    assert snapshot["trimmed_entries"] == 23
    assert halt_reasons == []
    assert guard.snapshot()["running"] is False


def test_single_stream_byte_limit_triggers_sticky_halt_once() -> None:
    orders = f"{STREAM_ROOT}:orders"
    redis = _FakeRedis(
        pages={
            0: (0, [orders]),
        },
        stream_bytes={
            orders: 1_025,
        },
        memory_samples=[(1_000, 10_000)],
    )
    halt_reasons: list[str] = []
    guard = RedisRuntimeSafetyGuard(
        redis,
        config=_config(
            stream_max_bytes=1_024,
            total_stream_max_bytes=4_096,
            sample_interval_seconds=0.01,
        ),
        halt_callback=halt_reasons.append,
    )

    guard.start()
    assert _wait_until(lambda: bool(halt_reasons))
    snapshot = guard.snapshot()
    time.sleep(0.03)
    guard.stop()

    assert len(halt_reasons) == 1
    assert "stream byte limit exceeded" in halt_reasons[0]
    assert orders in halt_reasons[0]
    assert snapshot["halted"] is True
    assert snapshot["halt_reason"] == halt_reasons[0]
    assert snapshot["largest_stream_bytes"] == 1_025
    assert snapshot["stream_bytes"] == 1_025


def test_total_active_generation_stream_bytes_trigger_sticky_halt() -> None:
    orders = f"{STREAM_ROOT}:orders"
    positions = f"{STREAM_ROOT}:positions"
    redis = _FakeRedis(
        pages={
            0: (0, [orders, positions]),
        },
        stream_bytes={
            orders: 800,
            positions: 800,
        },
        memory_samples=[(1_000, 10_000)],
    )
    halt_reasons: list[str] = []
    guard = RedisRuntimeSafetyGuard(
        redis,
        config=_config(
            stream_max_bytes=1_000,
            total_stream_max_bytes=1_500,
        ),
        halt_callback=halt_reasons.append,
    )

    guard.start()
    assert _wait_until(lambda: bool(halt_reasons))
    snapshot = guard.snapshot()
    guard.stop()

    assert len(halt_reasons) == 1
    assert "total stream byte limit exceeded" in halt_reasons[0]
    assert snapshot["stream_count"] == 2
    assert snapshot["stream_bytes"] == 1_600
    assert snapshot["halted"] is True


def test_memory_pressure_reports_60_75_85_percent_status_bands() -> None:
    cases = (
        (599, "normal"),
        (600, "warning"),
        (750, "degraded"),
        (850, "critical"),
    )
    for used_memory, expected_pressure in cases:
        redis = _FakeRedis(
            pages={
                0: (0, []),
            },
            stream_bytes={},
            memory_samples=[(used_memory, 1_000)],
        )
        halt_reasons: list[str] = []
        guard = RedisRuntimeSafetyGuard(
            redis,
            config=_config(),
            halt_callback=halt_reasons.append,
        )

        guard.start()
        assert redis.sample_completed.wait(timeout=0.5)
        assert _wait_until(
            lambda current_guard=guard: (
                current_guard.snapshot()["memory_pressure"] != "unknown"
            )
        )
        snapshot = guard.snapshot()
        guard.stop()

        assert snapshot["memory_pressure"] == expected_pressure
        assert snapshot["memory_ratio"] == used_memory / 1_000
        assert halt_reasons == []


def test_critical_memory_pressure_halts_after_sustained_window_once() -> None:
    clock = _ManualClock(100.0)
    redis = _FakeRedis(
        pages={
            0: (0, []),
        },
        stream_bytes={},
        memory_samples=[(850, 1_000)],
    )
    halt_reasons: list[str] = []
    guard = RedisRuntimeSafetyGuard(
        redis,
        config=_config(
            sample_interval_seconds=0.01,
            critical_window_seconds=30.0,
        ),
        halt_callback=halt_reasons.append,
        monotonic_fn=clock,
    )

    guard.start()
    assert _wait_until(lambda: redis.sample_count >= 1)
    first = guard.snapshot()
    clock.advance(31.0)
    assert _wait_until(lambda: bool(halt_reasons))
    second = guard.snapshot()
    time.sleep(0.03)
    guard.stop()

    assert first["memory_pressure"] == "critical"
    assert first["critical_duration_seconds"] == 0.0
    assert len(halt_reasons) == 1
    assert "85% critical window exceeded" in halt_reasons[0]
    assert second["halted"] is True
    assert second["critical_duration_seconds"] >= 30.0


def test_memory_recovery_resets_critical_window() -> None:
    clock = _ManualClock(100.0)
    redis = _FakeRedis(
        pages={
            0: (0, []),
        },
        stream_bytes={},
        memory_samples=[
            (850, 1_000),
            (740, 1_000),
            (850, 1_000),
        ],
    )
    halt_reasons: list[str] = []
    guard = RedisRuntimeSafetyGuard(
        redis,
        config=_config(
            sample_interval_seconds=0.01,
            critical_window_seconds=30.0,
        ),
        halt_callback=halt_reasons.append,
        monotonic_fn=clock,
    )

    guard.start()
    assert _wait_until(lambda: redis.sample_count >= 1)
    clock.advance(31.0)
    assert _wait_until(lambda: redis.sample_count >= 3)
    assert halt_reasons == []
    assert guard.snapshot()["critical_duration_seconds"] == 0.0

    clock.advance(31.0)
    assert _wait_until(lambda: bool(halt_reasons))
    guard.stop()

    assert len(halt_reasons) == 1
    assert "85% critical window exceeded" in halt_reasons[0]


def test_unbounded_redis_maxmemory_halts_immediately() -> None:
    redis = _FakeRedis(
        pages={
            0: (0, []),
        },
        stream_bytes={},
        memory_samples=[(1_000, 0)],
    )
    halt_reasons: list[str] = []
    guard = RedisRuntimeSafetyGuard(
        redis,
        config=_config(),
        halt_callback=halt_reasons.append,
    )

    guard.start()
    assert _wait_until(lambda: bool(halt_reasons))
    snapshot = guard.snapshot()
    guard.stop()

    assert len(halt_reasons) == 1
    assert "maxmemory is unbounded" in halt_reasons[0]
    assert snapshot["memory_pressure"] == "unbounded"
    assert snapshot["memory_ratio"] is False
    assert snapshot["halted"] is True
    assert redis.scan_calls == []

    with pytest.raises(RuntimeError, match="already halted"):
        guard.start()


def test_redis_command_failure_halts_sticky_and_blocks_restart() -> None:
    halt_reasons: list[str] = []
    guard = RedisRuntimeSafetyGuard(
        _FailingRedis(),
        config=_config(),
        halt_callback=halt_reasons.append,
    )

    guard.start()
    assert _wait_until(lambda: bool(halt_reasons))
    snapshot = guard.snapshot()

    with pytest.raises(RuntimeError, match="already halted"):
        guard.start()

    guard.stop()
    assert len(halt_reasons) == 1
    assert "sample failed" in halt_reasons[0]
    assert "Redis SCAN timed out" in halt_reasons[0]
    assert snapshot["halted"] is True
    assert snapshot["last_error"] == "Redis SCAN timed out"


def _wait_until(
    predicate: Callable[[], bool],
    timeout: float = 0.5,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False
