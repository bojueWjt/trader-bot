from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "make_container_bundle.py"
SPEC = importlib.util.spec_from_file_location("make_container_bundle", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


REQUIRED_DEPENDENCY_CLOSURE = {
    "run_node.py": "/app/app/run_node.py",
    "health_server.py": "/app/app/health_server.py",
    "health.py": "/app/runtime/health.py",
    "bounded_task_worker.py": "/app/runtime/bounded_task_worker.py",
    "control_plane_session.py": "/app/runtime/control_plane_session.py",
    "node_config.py": "/app/config/node_config.py",
    "approved_intent_client.py": "/app/data_client/approved_intent_client.py",
    "atomic_json.py": "/app/data_client/atomic_json.py",
    "durable_intent_inbox.py": "/app/data_client/durable_intent_inbox.py",
    "durable_command_journal.py": (
        "/app/commands/durable_command_journal.py"
    ),
    "projection_spool.py": "/app/projection/spool.py",
}


def _seed_sources(root: Path) -> None:
    source_paths = {item[1] for item in bundle.BUNDLE_FILES}
    source_paths.update(item[1] for item in bundle.HOST_FILES)
    source_paths.update(item[1] for item in bundle.MIGRATION_FILES)
    source_paths.update(item[1] for item in bundle.DEPLOYMENT_FILES)
    for source_relative in source_paths:
        source = root / source_relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            f"source={source_relative}\n",
            encoding="utf-8",
        )


def test_bundle_writes_dependency_closed_release_identity_and_checksums(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    _seed_sources(repo_root)

    manifest = bundle.build_bundle(
        repo_root,
        output_dir,
        repo_commit="a" * 40,
        repo_dirty=False,
    )

    assert manifest["schema_version"] == "2.0"
    assert manifest["release_id"] == "a" * 40
    assert manifest["repo_commit"] == "a" * 40
    assert manifest["repo_dirty"] is False
    actual_mounts = {
        item["bundle_path"]: item["mount_target"]
        for item in manifest["files"]
    }
    assert REQUIRED_DEPENDENCY_CLOSURE.items() <= actual_mounts.items()
    assert "commands_init.py" not in actual_mounts
    assert "/app/commands/__init__.py" not in actual_mounts.values()
    assert len(actual_mounts) == len(bundle.BUNDLE_FILES)

    host_targets = {
        item["target_relative"]
        for item in manifest["host_files"]
    }
    assert host_targets == {
        "services/control-plane/api/read_api.py",
        "packages/execution-domain/execution_domain/control_plane.py",
    }
    migration_targets = {
        item["target_relative"]
        for item in manifest["migration_files"]
    }
    assert migration_targets == {
        "services/control-plane/db/migrate.py",
        "db/migrations/0005_order_management.up.sql",
        "db/migrations/0005_order_management.down.sql",
        "db/migrations/0010_evidence_and_poll_indexes.up.sql",
        "db/migrations/0010_evidence_and_poll_indexes.down.sql",
    }
    deployment_paths = {
        item["bundle_path"]
        for item in manifest["deployment_files"]
    }
    assert deployment_paths == {
        "deploy.sh",
        "tools/hk-gen-recreate-patched.py",
    }
    for relative in deployment_paths:
        assert (output_dir / relative).stat().st_mode & 0o111

    persisted = json.loads(
        (output_dir / bundle.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert persisted == manifest
    checksum_lines = (
        output_dir / bundle.CHECKSUMS_NAME
    ).read_text(encoding="utf-8").splitlines()
    checksums = {
        line.split("  ", 1)[1]: line.split("  ", 1)[0]
        for line in checksum_lines
    }
    expected_paths = {
        item["bundle_path"]
        for group in (
            manifest["files"],
            manifest["host_files"],
            manifest["migration_files"],
            manifest["deployment_files"],
        )
        for item in group
    }
    expected_paths.add(bundle.MANIFEST_NAME)
    assert set(checksums) == expected_paths
    for relative, digest in checksums.items():
        assert digest == hashlib.sha256(
            (output_dir / relative).read_bytes()
        ).hexdigest()


def test_bundle_fails_closed_when_a_pinned_source_is_missing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    _seed_sources(repo_root)
    missing = repo_root / bundle.BUNDLE_FILES[0][1]
    missing.unlink()

    with pytest.raises(bundle.BundleError, match="bundle source is missing"):
        bundle.build_bundle(
            repo_root,
            output_dir,
            repo_commit="b" * 40,
            repo_dirty=False,
        )


def test_bundle_rejects_abbreviated_release_identity(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    _seed_sources(repo_root)

    with pytest.raises(bundle.BundleError, match="full lowercase git SHA"):
        bundle.build_bundle(
            repo_root,
            output_dir,
            repo_commit="abc123",
            repo_dirty=False,
        )
