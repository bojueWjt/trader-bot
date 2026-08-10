from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from config.node_config import NodeConfigError, load_node_config  # noqa: E402
from tests.runtime_resource_fixtures import (  # noqa: E402
    strict_invalid_cases,
    strict_runtime_resources,
)


def test_live_node_requires_release_bound_runtime_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(tmp_path, environment="live", resources=False)
    _set_credentials(monkeypatch)

    with pytest.raises(
        NodeConfigError,
        match="runtime_resources must be release-bound",
    ):
        load_node_config(path)


@pytest.mark.parametrize("environment", ["sandbox", "testnet"])
def test_non_live_node_materializes_compatibility_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    path = _write_config(
        tmp_path,
        environment=environment,
        resources=False,
    )
    _set_credentials(monkeypatch)

    config = load_node_config(path)

    assert config.runtime_resources.command_journal.max_bytes == (
        16 * 1024 * 1024
    )
    assert config.runtime_resources.strategy_durable_io.queue_capacity == 128


@pytest.mark.parametrize(
    "case",
    strict_invalid_cases(),
    ids=lambda case: case.case_id,
)
def test_live_node_matches_the_shared_strict_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case,
) -> None:
    path = _write_config(
        tmp_path,
        environment="live",
        resources=case.value(),
    )
    _set_credentials(monkeypatch)

    with pytest.raises(NodeConfigError, match=case.expected_path):
        load_node_config(path)


def test_live_node_ignores_release_environment_compatibility_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(tmp_path, environment="live", resources=False)
    _set_credentials(monkeypatch)
    monkeypatch.delenv("TRADER_RELEASE_MANIFEST_SCHEMA_VERSION", raising=False)
    monkeypatch.delenv("TRADER_RELEASE_PURPOSE", raising=False)

    with pytest.raises(NodeConfigError, match="release-bound"):
        load_node_config(path)


def _write_config(
    tmp_path: Path,
    *,
    environment: str,
    resources: object,
) -> Path:
    raw = json.loads(
        (
            SERVICE_ROOT
            / "config"
            / "examples"
            / "account-a.sandbox.json"
        ).read_text(encoding="utf-8")
    )
    raw["binance"]["environment"] = environment
    if resources is False:
        raw.pop("runtime_resources", None)
    else:
        raw["runtime_resources"] = resources
    path = tmp_path / "node.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_KEY", "key")
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_SECRET", "secret")
    monkeypatch.setenv("CONTROL_PLANE_ACCOUNT_A_TOKEN", "token")
