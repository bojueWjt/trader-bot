from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


def strict_runtime_resources() -> dict[str, Any]:
    return {
        "schema_version": "trader-v3-runtime-resources/v1",
        "redis": {
            "stream_max_entries": 100_000,
            "stream_max_bytes": 64 * 1024 * 1024,
            "total_stream_max_bytes": 256 * 1024 * 1024,
            "scan_count": 500,
            "sample_interval_seconds": 5.0,
            "critical_window_seconds": 30.0,
            "thread_join_timeout_seconds": 5.0,
            "memory_warning_ratio": 0.60,
            "memory_degraded_ratio": 0.75,
            "memory_critical_ratio": 0.85,
        },
        "command_journal": {
            "max_bytes": 16 * 1024 * 1024,
        },
        "control_plane_session": {
            "command_delivery_capacity": 128,
            "command_ack_capacity": 256,
            "intent_delivery_capacity": 256,
            "execution_event_capacity": 1024,
            "queue_degraded_ratio": 0.8,
            "retry_budget": 3,
            "retry_base_delay_seconds": 0.05,
            "retry_max_delay_seconds": 1.0,
            "retry_jitter_ratio": 0.2,
            "circuit_reset_seconds": 5.0,
            "operation_timeout_seconds": 15.0,
            "shutdown_timeout_seconds": 1.0,
        },
        "strategy_durable_io": {
            "queue_capacity": 128,
            "task_timeout_seconds": 1.0,
            "shutdown_timeout_seconds": 2.0,
        },
        "terminal_exchange": {
            "queue_capacity": 64,
            "result_queue_capacity": 128,
            "degraded_ratio": 0.8,
            "total_deadline_seconds": 6.0,
            "shutdown_timeout_seconds": 7.0,
        },
        "reporter_workers": {
            "denial_queue_capacity": 256,
            "protection_event_queue_capacity": 1024,
            "live_canary_risk_queue_capacity": 128,
            "incident_queue_capacity": 64,
            "task_timeout_seconds": 5.0,
            "shutdown_timeout_seconds": 5.0,
        },
    }


@dataclass(frozen=True)
class InvalidRuntimeResourceCase:
    case_id: str
    expected_path: str
    mutate: Callable[[dict[str, Any]], None]

    def value(self) -> dict[str, Any]:
        value = strict_runtime_resources()
        self.mutate(value)
        return value


def strict_invalid_cases() -> list[InvalidRuntimeResourceCase]:
    leaves = [
        ("schema_version",),
        *[
            ("redis", field)
            for field in strict_runtime_resources()["redis"]
        ],
        ("command_journal", "max_bytes"),
        *[
            ("control_plane_session", field)
            for field in strict_runtime_resources()["control_plane_session"]
        ],
        *[
            ("strategy_durable_io", field)
            for field in strict_runtime_resources()["strategy_durable_io"]
        ],
        *[
            ("terminal_exchange", field)
            for field in strict_runtime_resources()["terminal_exchange"]
        ],
        *[
            ("reporter_workers", field)
            for field in strict_runtime_resources()["reporter_workers"]
        ],
    ]
    cases = [
        InvalidRuntimeResourceCase(
            case_id=f"missing-{'-'.join(path)}",
            expected_path="runtime_resources." + ".".join(path),
            mutate=_delete_path(path),
        )
        for path in leaves
    ]
    wrong_type_paths = leaves[1:19]
    cases.extend(
        InvalidRuntimeResourceCase(
            case_id=f"wrong-type-{'-'.join(path)}",
            expected_path="runtime_resources." + ".".join(path),
            mutate=_replace_path(path, "invalid"),
        )
        for path in wrong_type_paths
    )
    assert len(cases) == 56
    return cases


def _delete_path(path: tuple[str, ...]) -> Callable[[dict[str, Any]], None]:
    def mutate(value: dict[str, Any]) -> None:
        parent = value
        for key in path[:-1]:
            child = parent[key]
            assert isinstance(child, dict)
            parent = child
        del parent[path[-1]]

    return mutate


def _replace_path(
    path: tuple[str, ...],
    replacement: Any,
) -> Callable[[dict[str, Any]], None]:
    def mutate(value: dict[str, Any]) -> None:
        parent = value
        for key in path[:-1]:
            child = parent[key]
            assert isinstance(child, dict)
            parent = child
        parent[path[-1]] = replacement

    return mutate
