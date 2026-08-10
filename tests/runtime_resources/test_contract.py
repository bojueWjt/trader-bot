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


@pytest.mark.parametrize(
    "case",
    strict_invalid_cases(),
    ids=lambda case: case.case_id,
)
def test_live_strict_rejects_the_56_case_invalid_matrix(case) -> None:
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


def test_compat_materializes_defaults_when_contract_is_absent() -> None:
    parsed = parse_runtime_resources(
        ABSENT,
        policy=RuntimeResourcePolicy.COMPAT,
    )

    assert parsed.schema_version == "trader-v3-runtime-resources/v1"
    assert parsed.redis.memory_warning_ratio == 0.60
    assert parsed.strategy_durable_io.queue_capacity == 128
    assert parsed.reporter_workers.incident_queue_capacity == 64


def test_compat_uses_matching_legacy_session() -> None:
    raw = strict_runtime_resources()
    session = raw.pop("control_plane_session")
    raw.pop("strategy_durable_io")
    raw.pop("terminal_exchange")
    raw.pop("reporter_workers")

    parsed = parse_runtime_resources(
        raw,
        policy=RuntimeResourcePolicy.COMPAT,
        legacy_session=session,
    )

    assert parsed.control_plane_session.to_dict() == session


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
        ("redis", "unsupported_redis"),
        ("command_journal", "unsupported_journal"),
        ("control_plane_session", "unsupported_session"),
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
