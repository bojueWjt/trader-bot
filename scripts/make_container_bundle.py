#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


BUNDLE_FILES = (
    (
        "intent_execution_planner.py",
        "services/nautilus-node/strategy/intent_execution_planner.py",
        "/app/strategy/intent_execution_planner.py",
    ),
    (
        "contracts.py",
        "services/nautilus-node/projection/contracts.py",
        "/app/execution_domain/contracts.py",
    ),
    (
        "control_plane.py",
        "packages/execution-domain/execution_domain/control_plane.py",
        "/app/execution_domain/control_plane.py",
    ),
    (
        "http_client.py",
        "packages/execution-domain/execution_domain/http_client.py",
        "/app/execution_domain/http_client.py",
    ),
    (
        "projection_actor.py",
        "services/nautilus-node/projection/actor.py",
        "/app/projection/actor.py",
    ),
    (
        "projection_spool.py",
        "services/nautilus-node/projection/spool.py",
        "/app/projection/spool.py",
    ),
    (
        "event_mapper.py",
        "services/nautilus-node/projection/event_mapper.py",
        "/app/projection/event_mapper.py",
    ),
    (
        "intent_execution_strategy.py",
        "services/nautilus-node/strategy/intent_execution_strategy.py",
        "/app/strategy/intent_execution_strategy.py",
    ),
    (
        "exchange_cancel_adapter.py",
        "services/nautilus-node/runtime/exchange_cancel_adapter.py",
        "/app/runtime/exchange_cancel_adapter.py",
    ),
    (
        "lifecycle.py",
        "services/nautilus-node/runtime/lifecycle.py",
        "/app/runtime/lifecycle.py",
    ),
    (
        "health.py",
        "services/nautilus-node/runtime/health.py",
        "/app/runtime/health.py",
    ),
    (
        "bounded_task_worker.py",
        "services/nautilus-node/runtime/bounded_task_worker.py",
        "/app/runtime/bounded_task_worker.py",
    ),
    (
        "control_plane_session.py",
        "services/nautilus-node/runtime/control_plane_session.py",
        "/app/runtime/control_plane_session.py",
    ),
    (
        "binance_adapter_config.py",
        "services/nautilus-node/runtime/binance_adapter_config.py",
        "/app/runtime/binance_adapter_config.py",
    ),
    (
        "node_config.py",
        "services/nautilus-node/config/node_config.py",
        "/app/config/node_config.py",
    ),
    (
        "node.py",
        "services/nautilus-node/app/node.py",
        "/app/app/node.py",
    ),
    (
        "run_node.py",
        "services/nautilus-node/app/run_node.py",
        "/app/app/run_node.py",
    ),
    (
        "health_server.py",
        "services/nautilus-node/app/health_server.py",
        "/app/app/health_server.py",
    ),
    (
        "nautilus_actors.py",
        "services/nautilus-node/app/nautilus_actors.py",
        "/app/app/nautilus_actors.py",
    ),
    (
        "approved_intent_client.py",
        "services/nautilus-node/data_client/approved_intent_client.py",
        "/app/data_client/approved_intent_client.py",
    ),
    (
        "atomic_json.py",
        "services/nautilus-node/data_client/atomic_json.py",
        "/app/data_client/atomic_json.py",
    ),
    (
        "durable_intent_inbox.py",
        "services/nautilus-node/data_client/durable_intent_inbox.py",
        "/app/data_client/durable_intent_inbox.py",
    ),
    (
        "data_client_init.py",
        "services/nautilus-node/data_client/__init__.py",
        "/app/data_client/__init__.py",
    ),
    (
        "durable_command_journal.py",
        "services/nautilus-node/commands/durable_command_journal.py",
        "/app/commands/durable_command_journal.py",
    ),
    (
        "binance_execution.py",
        "container-patches/binance_execution.py",
        (
            "/usr/local/lib/python3.12/site-packages/"
            "nautilus_trader/adapters/binance/execution.py"
        ),
    ),
    (
        "binance_futures_execution.py",
        "container-patches/binance_futures_execution.py",
        (
            "/usr/local/lib/python3.12/site-packages/"
            "nautilus_trader/adapters/binance/futures/execution.py"
        ),
    ),
)

HOST_FILES = (
    (
        "host/read_api.py",
        "services/control-plane/api/read_api.py",
        "services/control-plane/api/read_api.py",
        "0644",
    ),
    (
        "host/exchange_state_recorder.py",
        "services/control-plane/tools/exchange_state_recorder.py",
        "services/control-plane/tools/exchange_state_recorder.py",
        "0644",
    ),
    (
        "host/control_plane.py",
        "packages/execution-domain/execution_domain/control_plane.py",
        "packages/execution-domain/execution_domain/control_plane.py",
        "0644",
    ),
    (
        "host/account_a_live_trade_executor.py",
        "scripts/account_a_live_trade_executor.py",
        "scripts/account_a_live_trade_executor.py",
        "0755",
    ),
    (
        "host/account_a_live_trade_http_adapter.py",
        "scripts/account_a_live_trade_http_adapter.py",
        "scripts/account_a_live_trade_http_adapter.py",
        "0755",
    ),
)

MIGRATION_FILES = (
    (
        "migration/migrate.py",
        "services/control-plane/db/migrate.py",
        "services/control-plane/db/migrate.py",
    ),
    (
        "migration/0005_order_management.up.sql",
        "db/migrations/0005_order_management.up.sql",
        "db/migrations/0005_order_management.up.sql",
    ),
    (
        "migration/0005_order_management.down.sql",
        "db/migrations/0005_order_management.down.sql",
        "db/migrations/0005_order_management.down.sql",
    ),
    (
        "migration/0010_evidence_and_poll_indexes.up.sql",
        "db/migrations/0010_evidence_and_poll_indexes.up.sql",
        "db/migrations/0010_evidence_and_poll_indexes.up.sql",
    ),
    (
        "migration/0010_evidence_and_poll_indexes.down.sql",
        "db/migrations/0010_evidence_and_poll_indexes.down.sql",
        "db/migrations/0010_evidence_and_poll_indexes.down.sql",
    ),
)

DEPLOYMENT_FILES = (
    (
        "tools/hk-gen-recreate-patched.py",
        "scripts/hk-gen-recreate-patched.py",
    ),
    (
        "tools/hk-deploy-account-b-hardening.sh",
        "scripts/hk-deploy-account-b-hardening.sh",
    ),
    (
        "tools/verify-exchange-state-recorder.py",
        "scripts/verify_exchange_state_recorder.py",
    ),
    (
        "deploy.sh",
        "scripts/hk-deploy-account-stall-availability.sh",
    ),
)

MANIFEST_NAME = "bundle-manifest.json"
CHECKSUMS_NAME = "SHA256SUMS"
ACCOUNT_B_OBSERVABILITY_BUNDLE_PATH = "control_plane_session.py"
ACCOUNT_B_OBSERVABILITY_MOUNT_TARGET = (
    "/app/runtime/control_plane_session.py"
)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class BundleError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_artifact(
    repo_root: Path,
    output_dir: Path,
    *,
    bundle_path: str,
    source_relative: str,
) -> dict[str, object]:
    source = (repo_root / source_relative).resolve()
    try:
        source.relative_to(repo_root)
    except ValueError as exc:
        raise BundleError(
            f"source escapes repository: {source_relative}"
        ) from exc
    if not source.is_file():
        raise BundleError(f"bundle source is missing: {source_relative}")

    destination = (output_dir / bundle_path).resolve()
    try:
        destination.relative_to(output_dir)
    except ValueError as exc:
        raise BundleError(
            f"bundle path escapes output directory: {bundle_path}"
        ) from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    source_hash = _sha256(source)
    destination_hash = _sha256(destination)
    if source_hash != destination_hash:
        raise BundleError(f"copied bundle hash mismatch: {bundle_path}")
    return {
        "bundle_path": bundle_path,
        "source_path": source_relative,
        "sha256": destination_hash,
        "size": destination.stat().st_size,
    }


def _validate_file_contracts() -> None:
    bundle_paths = [item[0] for item in BUNDLE_FILES]
    mount_targets = [item[2] for item in BUNDLE_FILES]
    artifact_paths = bundle_paths.copy()
    artifact_paths.extend(item[0] for item in HOST_FILES)
    artifact_paths.extend(item[0] for item in MIGRATION_FILES)
    artifact_paths.extend(item[0] for item in DEPLOYMENT_FILES)
    if len(bundle_paths) != len(set(bundle_paths)):
        raise BundleError("container bundle paths must be unique")
    if len(mount_targets) != len(set(mount_targets)):
        raise BundleError("container mount targets must be unique")
    if len(artifact_paths) != len(set(artifact_paths)):
        raise BundleError("bundle artifact paths must be unique")


def _write_checksums(output_dir: Path, artifact_paths: list[str]) -> None:
    lines = []
    for relative in sorted(artifact_paths):
        path = output_dir / relative
        lines.append(f"{_sha256(path)}  {relative}")
    checksums_path = output_dir / CHECKSUMS_NAME
    checksums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _git_blob(
    repo_root: Path,
    commit: str,
    source_relative: str,
) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{commit}:{source_relative}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise BundleError(
            "peer baseline lacks runtime source: "
            f"{source_relative} at {commit}"
        )
    return result.stdout


def _account_b_peer_contract(
    repo_root: Path,
    files: list[dict[str, object]],
    *,
    baseline_commit: str,
    observability_bundle_path: str,
) -> dict[str, object]:
    if _COMMIT_RE.fullmatch(baseline_commit) is None:
        raise BundleError(
            "account_b_peer_baseline must be a full lowercase git SHA"
        )
    if (
        observability_bundle_path
        != ACCOUNT_B_OBSERVABILITY_BUNDLE_PATH
    ):
        raise BundleError(
            "account-b observability delta must be "
            f"{ACCOUNT_B_OBSERVABILITY_BUNDLE_PATH}"
        )
    source_by_bundle = {
        bundle_path: source_relative
        for bundle_path, source_relative, _ in BUNDLE_FILES
    }
    if observability_bundle_path not in source_by_bundle:
        raise BundleError(
            "account_b_observability_bundle_path is not a runtime file"
        )
    mount_by_bundle = {
        bundle_path: mount_target
        for bundle_path, _, mount_target in BUNDLE_FILES
    }
    if (
        mount_by_bundle[observability_bundle_path]
        != ACCOUNT_B_OBSERVABILITY_MOUNT_TARGET
    ):
        raise BundleError(
            "account-b observability mount target is invalid"
        )

    peer_files = []
    changed = []
    for item in files:
        bundle_path = str(item["bundle_path"])
        source_relative = source_by_bundle[bundle_path]
        peer_payload = _git_blob(
            repo_root,
            baseline_commit,
            source_relative,
        )
        peer_sha256 = hashlib.sha256(peer_payload).hexdigest()
        release_sha256 = str(item["sha256"])
        peer_files.append(
            {
                "bundle_path": bundle_path,
                "mount_target": str(item["mount_target"]),
                "sha256": peer_sha256,
            }
        )
        if peer_sha256 != release_sha256:
            changed.append(
                {
                    "bundle_path": bundle_path,
                    "mount_target": str(item["mount_target"]),
                    "peer_sha256": peer_sha256,
                    "release_sha256": release_sha256,
                }
            )

    changed_paths = {
        str(item["bundle_path"])
        for item in changed
    }
    if changed_paths != {observability_bundle_path}:
        raise BundleError(
            "account-b runtime delta must contain exactly the reviewed "
            f"observability file: expected={observability_bundle_path!r} "
            f"actual={sorted(changed_paths)!r}"
        )

    return {
        "schema_version": "1.0",
        "peer_container": "trader-v3-node-a",
        "baseline_repo_commit": baseline_commit,
        "peer_files": peer_files,
        "observability_delta": changed[0],
    }


def build_bundle(
    repo_root: Path,
    output_dir: Path,
    *,
    repo_commit: str,
    repo_dirty: bool,
    account_b_peer_baseline: str | None = None,
    account_b_observability_bundle_path: str | None = None,
) -> dict[str, object]:
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    if _COMMIT_RE.fullmatch(repo_commit) is None:
        raise BundleError("repo_commit must be a full lowercase git SHA")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BundleError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    _validate_file_contracts()

    files = []
    for bundle_path, source_relative, mount_target in BUNDLE_FILES:
        artifact = _copy_artifact(
            repo_root,
            output_dir,
            bundle_path=bundle_path,
            source_relative=source_relative,
        )
        artifact["mount_target"] = mount_target
        files.append(artifact)

    host_files = []
    for (
        bundle_path,
        source_relative,
        target_relative,
        install_mode,
    ) in HOST_FILES:
        artifact = _copy_artifact(
            repo_root,
            output_dir,
            bundle_path=bundle_path,
            source_relative=source_relative,
        )
        artifact["target_relative"] = target_relative
        artifact["install_mode"] = install_mode
        host_files.append(artifact)

    migration_files = []
    for bundle_path, source_relative, target_relative in MIGRATION_FILES:
        artifact = _copy_artifact(
            repo_root,
            output_dir,
            bundle_path=bundle_path,
            source_relative=source_relative,
        )
        artifact["target_relative"] = target_relative
        migration_files.append(artifact)

    deployment_files = []
    for bundle_path, source_relative in DEPLOYMENT_FILES:
        artifact = _copy_artifact(
            repo_root,
            output_dir,
            bundle_path=bundle_path,
            source_relative=source_relative,
        )
        (output_dir / bundle_path).chmod(0o755)
        deployment_files.append(artifact)

    manifest: dict[str, object] = {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_id": repo_commit,
        "repo_commit": repo_commit,
        "repo_dirty": repo_dirty,
        "files": files,
        "host_files": host_files,
        "migration_files": migration_files,
        "deployment_files": deployment_files,
    }
    peer_arguments = (
        account_b_peer_baseline,
        account_b_observability_bundle_path,
    )
    if any(peer_arguments) and not all(peer_arguments):
        raise BundleError(
            "account-b peer baseline and observability bundle path "
            "must be provided together"
        )
    if account_b_peer_baseline and account_b_observability_bundle_path:
        manifest["account_b_peer_contract"] = _account_b_peer_contract(
            repo_root,
            files,
            baseline_commit=account_b_peer_baseline,
            observability_bundle_path=(
                account_b_observability_bundle_path
            ),
        )
    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact_paths = [
        str(item["bundle_path"])
        for group in (
            files,
            host_files,
            migration_files,
            deployment_files,
        )
        for item in group
    ]
    artifact_paths.append(MANIFEST_NAME)
    _write_checksums(output_dir, artifact_paths)
    return manifest


def _git_output(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BundleError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an account-stall availability hotfix bundle."
    )
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--account-b-peer-baseline",
        help="full git SHA for the deployed account-a runtime baseline",
    )
    parser.add_argument(
        "--account-b-observability-bundle-path",
        help=(
            "single reviewed runtime bundle path allowed to differ from "
            "the account-a baseline"
        ),
    )
    args = parser.parse_args(argv)

    try:
        repo_root = args.repo_root.resolve()
        repo_commit = _git_output(repo_root, "rev-parse", "HEAD")
        repo_dirty = bool(_git_output(repo_root, "status", "--porcelain"))
        if repo_dirty and not args.allow_dirty:
            raise BundleError(
                "repository has uncommitted changes; commit them or pass --allow-dirty"
            )
        manifest = build_bundle(
            repo_root,
            args.output_dir,
            repo_commit=repo_commit,
            repo_dirty=repo_dirty,
            account_b_peer_baseline=args.account_b_peer_baseline,
            account_b_observability_bundle_path=(
                args.account_b_observability_bundle_path
            ),
        )
    except (BundleError, OSError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    print(
        f"WROTE {args.output_dir.resolve()} "
        f"files={len(manifest['files'])} commit={manifest['repo_commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
