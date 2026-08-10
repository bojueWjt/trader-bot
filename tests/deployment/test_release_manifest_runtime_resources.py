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


def test_transition_release_carries_the_shared_contract() -> None:
    mounts = {
        bundle_name: mount_target
        for bundle_name, _source, mount_target in (
            release_manifest.TRANSITION_RUNTIME_FILES
        )
    }

    assert mounts["runtime_resource_contract.py"] == (
        "/app/packages/runtime_resource_contract/__init__.py"
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
    assert result.stdout.strip() == "trader-v3-runtime-resources/v1"
