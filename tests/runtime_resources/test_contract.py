from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.runtime_resource_contract import (
    ABSENT,
    RuntimeResourceContractError,
    RuntimeResourcePolicy,
    parse_runtime_resources,
)
from tests.runtime_resource_fixtures import (
    strict_invalid_cases,
    strict_runtime_resources,
)


def _v1_runtime_resources() -> dict[str, object]:
    raw = strict_runtime_resources()
    raw["schema_version"] = "trader-v3-runtime-resources/v1"
    raw["redis"] = {
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
    }
    raw["terminal_exchange"] = {
        "queue_capacity": 64,
        "result_queue_capacity": 128,
        "degraded_ratio": 0.8,
        "total_deadline_seconds": 6.0,
        "shutdown_timeout_seconds": 7.0,
    }
    raw["reporter_workers"] = {
        "denial_queue_capacity": 256,
        "protection_event_queue_capacity": 1024,
        "live_canary_risk_queue_capacity": 128,
        "incident_queue_capacity": 64,
        "task_timeout_seconds": 5.0,
        "shutdown_timeout_seconds": 5.0,
    }
    return raw


@pytest.mark.parametrize(
    "case",
    strict_invalid_cases(),
    ids=lambda case: case.case_id,
)
def test_live_strict_rejects_the_invalid_matrix(case) -> None:
    with pytest.raises(
        RuntimeResourceContractError,
        match=case.expected_path,
    ):
        parse_runtime_resources(
            case.value(),
            policy=RuntimeResourcePolicy.LIVE_STRICT,
        )


def test_live_strict_normalizes_the_complete_contract() -> None:
    raw = strict_runtime_resources()

    parsed = parse_runtime_resources(
        raw,
        policy=RuntimeResourcePolicy.LIVE_STRICT,
    )

    assert parsed.to_dict() == raw


@pytest.mark.parametrize(
    "policy",
    [None, "live_strict", "compat", object()],
)
def test_policy_must_be_a_runtime_resource_policy(policy: object) -> None:
    with pytest.raises(TypeError, match="RuntimeResourcePolicy"):
        parse_runtime_resources(
            strict_runtime_resources(),
            policy=policy,  # type: ignore[arg-type]
        )


def test_compat_materializes_defaults_when_contract_is_absent() -> None:
    parsed = parse_runtime_resources(
        ABSENT,
        policy=RuntimeResourcePolicy.COMPAT,
    )

    assert parsed.schema_version == "trader-v3-runtime-resources/v2"
    assert parsed.command_journal.max_bytes == 16 * 1024 * 1024
    assert parsed.strategy_durable_io.queue_capacity == 128


def test_compat_uses_matching_legacy_session() -> None:
    raw = strict_runtime_resources()
    session = raw.pop("control_plane_session")
    raw.pop("strategy_durable_io")

    parsed = parse_runtime_resources(
        raw,
        policy=RuntimeResourcePolicy.COMPAT,
        legacy_session=session,
    )

    assert parsed.control_plane_session.to_dict() == session


def test_compat_normalizes_v1_resources_to_the_v2_contract() -> None:
    raw = _v1_runtime_resources()

    parsed = parse_runtime_resources(
        raw,
        policy=RuntimeResourcePolicy.COMPAT,
    )

    expected = strict_runtime_resources()
    assert parsed.to_dict() == expected


def test_live_strict_rejects_v1_resources() -> None:
    with pytest.raises(RuntimeResourceContractError):
        parse_runtime_resources(
            _v1_runtime_resources(),
            policy=RuntimeResourcePolicy.LIVE_STRICT,
        )


def test_duplicate_session_rejects_drift() -> None:
    raw = strict_runtime_resources()
    legacy = dict(raw["control_plane_session"])
    legacy["command_delivery_capacity"] = 64

    with pytest.raises(
        RuntimeResourceContractError,
        match="differs from control_plane.session",
    ):
        parse_runtime_resources(
            raw,
            policy=RuntimeResourcePolicy.LIVE_STRICT,
            legacy_session=legacy,
        )


@pytest.mark.parametrize(
    ("group", "field"),
    [
        (False, "unsupported_root"),
        ("command_journal", "unsupported_journal"),
        ("control_plane_session", "unsupported_session"),
        ("strategy_durable_io", "unsupported_strategy"),
    ],
)
def test_allowed_fields_are_owned_by_the_shared_contract(
    group: object,
    field: str,
) -> None:
    raw = strict_runtime_resources()
    target = raw
    if isinstance(group, str):
        nested = raw[group]
        assert isinstance(nested, dict)
        target = nested
    target[field] = 1

    with pytest.raises(
        RuntimeResourceContractError,
        match=field,
    ):
        parse_runtime_resources(
            raw,
            policy=RuntimeResourcePolicy.LIVE_STRICT,
        )


@pytest.mark.parametrize(
    "group",
    ["redis", "terminal_exchange", "reporter_workers"],
)
def test_application_contract_rejects_resources_without_runtime_consumers(
    group: str,
) -> None:
    raw = strict_runtime_resources()
    raw[group] = {}

    with pytest.raises(
        RuntimeResourceContractError,
        match=group,
    ):
        parse_runtime_resources(
            raw,
            policy=RuntimeResourcePolicy.LIVE_STRICT,
        )


def test_live_risk_policy_materializes_the_complete_strict_contract() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    policy_path = (
        repo_root
        / "services"
        / "nautilus-node"
        / "config"
        / "live-risk-policy.json"
    )
    policy = json.loads(policy_path.read_text(encoding="utf-8"))

    parsed = parse_runtime_resources(
        policy["runtime_resource_contract"],
        policy=RuntimeResourcePolicy.LIVE_STRICT,
    )

    assert parsed.to_dict() == policy["runtime_resource_contract"]
