from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import make_account_stall_release as release  # noqa: E402

SYSTEMD_ROOT = REPO_ROOT / "infra" / "systemd"
RESOURCE_FILES = {
    "account-node": SYSTEMD_ROOT / "account-stall-account-node.conf",
    "control-plane-writer": (
        SYSTEMD_ROOT / "account-stall-control-plane-writer.conf"
    ),
    "control-plane-reader": (
        SYSTEMD_ROOT / "account-stall-control-plane-reader.conf"
    ),
    "redis": SYSTEMD_ROOT / "account-stall-redis.conf",
}
REQUIRED_BUDGETS = {
    "MemoryMax",
    "MemorySwapMax",
    "CPUQuota",
    "TasksMax",
    "LimitNOFILE",
    "Restart",
    "RestartSec",
}


def _parse_drop_in(path: Path) -> dict[str, str]:
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines[0] == "[Service]"
    assert all(not line.startswith("[") for line in lines[1:])
    directives: dict[str, str] = {}
    for line in lines[1:]:
        key, separator, value = line.partition("=")
        assert separator == "=", line
        assert key not in directives, key
        directives[key] = value
    return directives


def _validate_resource_budget(directives: dict[str, str]) -> None:
    missing = sorted(REQUIRED_BUDGETS - directives.keys())
    if missing:
        raise ValueError(f"missing resource budgets: {missing}")
    assert re.fullmatch(r"[1-9][0-9]*[KMGTP]?", directives["MemoryMax"])
    assert directives["MemorySwapMax"] == "0"
    assert re.fullmatch(r"[1-9][0-9]*%", directives["CPUQuota"])
    assert re.fullmatch(r"[1-9][0-9]*", directives["TasksMax"])
    assert re.fullmatch(r"[1-9][0-9]*", directives["LimitNOFILE"])
    assert directives["Restart"] in {"on-failure", "always"}
    assert re.fullmatch(r"[1-9][0-9]*s?", directives["RestartSec"])


def _memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([KMGTP]?)", value)
    assert match is not None
    amount = int(match.group(1))
    suffix = match.group(2)
    powers = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5}
    return amount * 1024 ** powers[suffix]


def test_account_stall_drop_ins_have_complete_resource_budgets() -> None:
    parsed = {}
    for role, path in RESOURCE_FILES.items():
        assert path.is_file(), role
        directives = _parse_drop_in(path)
        _validate_resource_budget(directives)
        parsed[role] = directives

    assert _memory_bytes(parsed["account-node"]["MemoryMax"]) == 448 * 1024**2
    assert parsed["account-node"]["CPUQuota"] == "100%"
    assert _memory_bytes(parsed["redis"]["MemoryMax"]) == 640 * 1024**2
    assert _memory_bytes(
        parsed["control-plane-writer"]["MemoryMax"]
    ) > _memory_bytes(parsed["control-plane-reader"]["MemoryMax"])
    assert int(
        parsed["control-plane-writer"]["TasksMax"]
    ) >= int(parsed["control-plane-reader"]["TasksMax"])
    assert parsed["account-node"]["LimitNOFILE"] == "65536"
    assert parsed["redis"]["LimitNOFILE"] == "65536"


def test_account_stall_drop_ins_have_exact_release_consumers() -> None:
    expected_artifacts = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in RESOURCE_FILES.values()
    }
    release_artifacts = {
        destination
        for source, destination in release.RELEASE_FILES
        if source in release.SYSTEMD_RESOURCE_FILES
    }
    consumers = {
        item["artifact"]: item
        for item in release.SYSTEMD_RESOURCE_CONSUMERS
    }

    assert expected_artifacts == set(release.SYSTEMD_RESOURCE_FILES)
    assert release_artifacts == expected_artifacts
    assert set(consumers) == expected_artifacts
    for artifact, contract in consumers.items():
        assert contract["consumer_entrypoint"]
        assert contract["application"] in {
            "docker_host_config",
            "inline_service_unit",
            "systemd_service_drop_in",
        }
        assert contract["owner_units"]
        assert len(contract["owner_units"]) == len(
            contract["destinations"]
        )
        if contract["application"] == "docker_host_config":
            assert all(
                destination.startswith("docker://")
                for destination in contract["destinations"]
            ), artifact
        else:
            assert all(
                destination.startswith("/etc/systemd/system/")
                for destination in contract["destinations"]
            ), artifact


@pytest.mark.parametrize("missing_budget", sorted(REQUIRED_BUDGETS))
def test_resource_budget_validator_rejects_missing_directive(
    missing_budget: str,
) -> None:
    directives = _parse_drop_in(RESOURCE_FILES["account-node"])
    del directives[missing_budget]

    with pytest.raises(ValueError, match="missing resource budgets"):
        _validate_resource_budget(directives)
