from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

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
        "services/control-plane/tools/exchange_state_recorder.py",
        "packages/execution-domain/execution_domain/control_plane.py",
        "scripts/account_a_live_trade_executor.py",
        "scripts/account_a_live_trade_http_adapter.py",
    }
    host_modes = {
        item["target_relative"]: item["install_mode"]
        for item in manifest["host_files"]
    }
    assert host_modes == {
        "services/control-plane/api/read_api.py": "0644",
        (
            "services/control-plane/tools/exchange_state_recorder.py"
        ): "0644",
        (
            "packages/execution-domain/execution_domain/control_plane.py"
        ): "0644",
        "scripts/account_a_live_trade_executor.py": "0755",
        "scripts/account_a_live_trade_http_adapter.py": "0755",
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
        "tools/hk-deploy-account-b-hardening.sh",
        "tools/hk-gen-recreate-patched.py",
        "tools/verify-exchange-state-recorder.py",
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


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit_all(repo_root: Path, message: str) -> str:
    _git(repo_root, "add", ".")
    _git(
        repo_root,
        "-c",
        "user.name=Bundle Test",
        "-c",
        "user.email=bundle-test@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repo_root, "rev-parse", "HEAD")


def test_account_b_peer_contract_allows_one_reviewed_observability_delta(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    repo_root.mkdir()
    _git(repo_root, "init")
    _seed_sources(repo_root)
    baseline = _commit_all(repo_root, "baseline")

    delta_bundle_path = bundle.ACCOUNT_B_OBSERVABILITY_BUNDLE_PATH
    delta_source = next(
        source
        for bundle_path, source, _target in bundle.BUNDLE_FILES
        if bundle_path == delta_bundle_path
    )
    delta_target = bundle.ACCOUNT_B_OBSERVABILITY_MOUNT_TARGET
    (repo_root / delta_source).write_text(
        "reviewed logging delta\n",
        encoding="utf-8",
    )
    release_commit = _commit_all(repo_root, "observability")

    manifest = bundle.build_bundle(
        repo_root,
        output_dir,
        repo_commit=release_commit,
        repo_dirty=False,
        account_b_peer_baseline=baseline,
        account_b_observability_bundle_path=delta_bundle_path,
    )

    contract = manifest["account_b_peer_contract"]
    assert contract["schema_version"] == "1.0"
    assert contract["peer_container"] == "trader-v3-node-a"
    assert contract["baseline_repo_commit"] == baseline
    assert len(contract["peer_files"]) == len(bundle.BUNDLE_FILES)
    delta = contract["observability_delta"]
    assert delta["bundle_path"] == delta_bundle_path
    assert delta["mount_target"] == delta_target
    assert delta["peer_sha256"] != delta["release_sha256"]


def test_account_b_peer_contract_rejects_an_extra_runtime_change(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    repo_root.mkdir()
    _git(repo_root, "init")
    _seed_sources(repo_root)
    baseline = _commit_all(repo_root, "baseline")

    first_bundle_path = bundle.ACCOUNT_B_OBSERVABILITY_BUNDLE_PATH
    first_source = next(
        source
        for bundle_path, source, _target in bundle.BUNDLE_FILES
        if bundle_path == first_bundle_path
    )
    second_source = next(
        source
        for bundle_path, source, _target in bundle.BUNDLE_FILES
        if bundle_path != first_bundle_path
    )
    (repo_root / first_source).write_text("logging delta\n", encoding="utf-8")
    (repo_root / second_source).write_text("extra delta\n", encoding="utf-8")
    release_commit = _commit_all(repo_root, "two changes")

    with pytest.raises(
        bundle.BundleError,
        match="exactly the reviewed observability file",
    ):
        bundle.build_bundle(
            repo_root,
            output_dir,
            repo_commit=release_commit,
            repo_dirty=False,
            account_b_peer_baseline=baseline,
            account_b_observability_bundle_path=first_bundle_path,
        )


def test_account_b_peer_contract_rejects_a_baseline_without_the_mount_set(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    repo_root.mkdir()
    _git(repo_root, "init")
    _seed_sources(repo_root)
    missing_source = repo_root / bundle.BUNDLE_FILES[-1][1]
    missing_source.unlink()
    baseline = _commit_all(repo_root, "incomplete baseline")
    missing_source.parent.mkdir(parents=True, exist_ok=True)
    missing_source.write_text("current-head-only\n", encoding="utf-8")
    first_bundle_path = bundle.ACCOUNT_B_OBSERVABILITY_BUNDLE_PATH
    first_source = next(
        source
        for bundle_path, source, _target in bundle.BUNDLE_FILES
        if bundle_path == first_bundle_path
    )
    (repo_root / first_source).write_text("logging delta\n", encoding="utf-8")
    release_commit = _commit_all(repo_root, "release")

    with pytest.raises(
        bundle.BundleError,
        match="peer baseline lacks runtime source",
    ):
        bundle.build_bundle(
            repo_root,
            output_dir,
            repo_commit=release_commit,
            repo_dirty=False,
            account_b_peer_baseline=baseline,
            account_b_observability_bundle_path=first_bundle_path,
        )


def test_account_b_peer_contract_rejects_a_non_observability_delta(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    repo_root.mkdir()
    _git(repo_root, "init")
    _seed_sources(repo_root)
    baseline = _commit_all(repo_root, "baseline")

    bundle_path, source, _target = next(
        item
        for item in bundle.BUNDLE_FILES
        if item[0] != bundle.ACCOUNT_B_OBSERVABILITY_BUNDLE_PATH
    )
    (repo_root / source).write_text("strategy delta\n", encoding="utf-8")
    release_commit = _commit_all(repo_root, "wrong delta")

    with pytest.raises(
        bundle.BundleError,
        match="observability delta must be control_plane_session.py",
    ):
        bundle.build_bundle(
            repo_root,
            output_dir,
            repo_commit=release_commit,
            repo_dirty=False,
            account_b_peer_baseline=baseline,
            account_b_observability_bundle_path=bundle_path,
        )
