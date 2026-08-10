from __future__ import annotations

import json
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


def test_manifest_api_exposes_release_and_runtime_contracts() -> None:
    required = {
        "TRANSITION_RUNTIME_FILES",
        "augment_transition_bundle",
        "build_release_manifest",
        "capture_release_manifest",
        "validate_build_attestation",
        "validate_bundle_payload",
        "validate_migration_manifest",
        "validate_release_payload_envelope",
        "validate_release_source_manifest",
        "verify_live_release",
    }

    assert all(hasattr(release_manifest, name) for name in required)


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["capture", "--help"],
        ["validate-bundle", "--help"],
        ["verify-live", "--help"],
    ],
)
def test_release_manifest_cli_help_is_available(args: list[str]) -> None:
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

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_release_manifest_cli_rejects_missing_manifest() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "release_manifest.py"),
            "verify-live",
            "--manifest",
            "/definitely/missing.json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "FATAL:" in result.stderr


def test_validate_bundle_cli_checks_payload(
    tmp_path: Path,
    capsys,
) -> None:
    payload = tmp_path / "node.py"
    payload.write_text("release payload\n", encoding="utf-8")
    bundle = tmp_path / "bundle-manifest.json"
    bundle.write_text(
        json.dumps(
            {
                "repo_commit": "1" * 40,
                "repo_dirty": False,
                "schema_epochs": dict(release_manifest.SCHEMA_EPOCHS),
                "files": [
                    {
                        "bundle_path": payload.name,
                        "mount_target": "/app/app/node.py",
                        "sha256": release_manifest.sha256_file(payload),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = release_manifest.main(
        [
            "validate-bundle",
            "--bundle-manifest",
            str(bundle),
            "--patch-root",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert "PASS: bundle files=1" in capsys.readouterr().out


def test_capture_cli_dispatches_reviewed_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured = {}
    manifest = {
        "release_commit": "1" * 40,
        "release_id": "2" * 64,
        "delivery_mode": release_manifest.DELIVERY_TRANSITION,
        "release_purpose": (
            release_manifest.RELEASE_PURPOSE_EMERGENCY_ROLLBACK
        ),
        "image_digest": "sha256:" + ("3" * 64),
        "config_sha256": "4" * 64,
    }

    def fake_capture_release_manifest(**kwargs):
        captured.update(kwargs)
        return manifest

    monkeypatch.setattr(
        release_manifest,
        "capture_release_manifest",
        fake_capture_release_manifest,
    )
    result = release_manifest.main(
        [
            "capture",
            "--bundle-manifest",
            str(tmp_path / "bundle-manifest.json"),
            "--dependency-lock",
            str(tmp_path / release_manifest.RELEASE_DEPENDENCY_LOCK_NAME),
            "--output",
            str(tmp_path / "release-manifest.json"),
            "--delivery-mode",
            release_manifest.DELIVERY_TRANSITION,
            "--emergency-rollback",
            "--node-config",
            f"account-a=trader-v3-node-a={tmp_path / 'account-a.json'}",
            "--node-config",
            f"account-b=trader-v3-node-b={tmp_path / 'account-b.json'}",
        ]
    )

    assert result == 0
    assert captured["delivery_mode"] == release_manifest.DELIVERY_TRANSITION
    assert captured["emergency_rollback"] is True
    assert set(captured["node_config_specs"]) == {
        "account-a",
        "account-b",
    }


def test_verify_live_cli_dispatches_selected_nodes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = {
        "release_commit": "1" * 40,
        "release_id": "2" * 64,
        "delivery_mode": release_manifest.DELIVERY_TRANSITION,
        "release_purpose": (
            release_manifest.RELEASE_PURPOSE_EMERGENCY_ROLLBACK
        ),
        "image_digest": "sha256:" + ("3" * 64),
        "config_sha256": "4" * 64,
    }
    captured = {}

    def fake_verify_live_release(
        value,
        containers,
        *,
        payload_root,
        reviewer_trust_sha256,
    ):
        captured["manifest"] = value
        captured["containers"] = containers
        captured["payload_root"] = payload_root
        captured["reviewer_trust_sha256"] = reviewer_trust_sha256
        return [{"source_hashes": {"/app/app/node.py": "5" * 64}}]

    monkeypatch.setattr(release_manifest, "_load_json", lambda _path: manifest)
    monkeypatch.setattr(
        release_manifest,
        "verify_live_release",
        fake_verify_live_release,
    )
    manifest_path = tmp_path / "release-manifest.json"
    result = release_manifest.main(
        [
            "verify-live",
            "--manifest",
            str(manifest_path),
            "--container",
            "trader-v3-node-a",
        ]
    )

    assert result == 0
    assert captured["containers"] == ["trader-v3-node-a"]
    assert captured["payload_root"] == tmp_path


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
