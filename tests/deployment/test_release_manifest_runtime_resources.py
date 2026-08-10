from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_manifest  # noqa: E402
from tests.runtime_resource_fixtures import (  # noqa: E402
    strict_invalid_cases,
    strict_runtime_resources,
)


@pytest.mark.parametrize(
    "case",
    strict_invalid_cases(),
    ids=lambda case: case.case_id,
)
def test_release_manifest_matches_the_shared_strict_matrix(case) -> None:
    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match=case.expected_path,
    ):
        release_manifest.runtime_resources_sha256(case.value())


def test_release_manifest_normalizes_with_the_shared_contract() -> None:
    raw = strict_runtime_resources()

    normalized = release_manifest._validated_runtime_resources(
        raw,
        label="runtime_resources",
    )

    assert normalized == raw
    assert release_manifest.runtime_resources_sha256(raw)


def test_manifest_api_is_dependency_closed_for_phase_b() -> None:
    assert set(release_manifest.__all__) == {
        "CONFIG_MOUNT_TARGET",
        "RUNTIME_RESOURCES_SCHEMA_VERSION",
        "ReleaseManifestError",
        "build_node_config_artifacts",
        "canonical_json_bytes",
        "node_config_sha256",
        "node_config_value_sha256",
        "normalize_node_config",
        "runtime_resources_sha256",
    }
    assert not hasattr(release_manifest, "TRANSITION_RUNTIME_FILES")
    assert not hasattr(release_manifest, "augment_transition_bundle")
    assert not hasattr(release_manifest, "build_release_manifest")
    assert not hasattr(release_manifest, "capture_release_manifest")


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["verify-live", "--manifest", "/definitely/missing.json"],
        ["capture", "--help"],
    ],
)
def test_phase_b_manifest_cli_fails_closed(args: list[str]) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "release_manifest.py"),
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "library-only" in result.stderr


def test_node_config_hash_uses_normalized_runtime_resources() -> None:
    first = {
        "account_id": "account-a",
        "node_id": "node-a",
        "runtime_resources": strict_runtime_resources(),
    }
    second = {
        "account_id": "account-b",
        "node_id": "node-b",
        "runtime_resources": strict_runtime_resources(),
    }
    first["runtime_resources"]["control_plane_session"][
        "operation_timeout_seconds"
    ] = 15
    second["runtime_resources"]["control_plane_session"][
        "operation_timeout_seconds"
    ] = 15.0

    assert release_manifest.node_config_value_sha256(first) == (
        release_manifest.node_config_value_sha256(second)
    )


def test_node_identity_normalization_preserves_hostname_tokens() -> None:
    first = {
        "account_id": "account-a",
        "node_id": "node-a",
        "control_plane_url": "https://node-api.internal",
        "runtime_resources": strict_runtime_resources(),
    }
    second = {
        "account_id": "account-b",
        "node_id": "node-b",
        "control_plane_url": "https://node-bpi.internal",
        "runtime_resources": strict_runtime_resources(),
    }

    assert release_manifest.node_config_value_sha256(first) != (
        release_manifest.node_config_value_sha256(second)
    )


def test_release_root_layout_can_import_the_shared_contract(
    tmp_path: Path,
) -> None:
    packaged_manifest = tmp_path / "release_manifest.py"
    packaged_contract = (
        tmp_path
        / "packages"
        / "runtime_resource_contract"
        / "__init__.py"
    )
    packaged_contract.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "release_manifest.py", packaged_manifest)
    shutil.copy2(
        REPO_ROOT
        / "packages"
        / "runtime_resource_contract"
        / "__init__.py",
        packaged_contract,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import release_manifest; "
                "print(release_manifest.RUNTIME_RESOURCES_SCHEMA_VERSION)"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "trader-v3-runtime-resources/v2"
