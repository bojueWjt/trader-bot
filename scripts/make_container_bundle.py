#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

BUNDLE_FILES = (
    (
        "entry_batch.py",
        "packages/execution-domain/execution_domain/entry_batch.py",
        "/app/execution_domain/entry_batch.py",
    ),
    (
        "intent_execution_planner.py",
        "services/nautilus-node/strategy/intent_execution_planner.py",
        "/app/strategy/intent_execution_planner.py",
    ),
    (
        "execution_domain_init.py",
        "packages/execution-domain/execution_domain/__init__.py",
        "/app/execution_domain/__init__.py",
    ),
    (
        "account_execution_ledger.py",
        (
            "packages/execution-domain/execution_domain/"
            "account_execution_ledger.py"
        ),
        "/app/execution_domain/account_execution_ledger.py",
    ),
    (
        "idempotency.py",
        "packages/execution-domain/execution_domain/idempotency.py",
        "/app/execution_domain/idempotency.py",
    ),
    (
        "identifiers.py",
        "packages/execution-domain/execution_domain/identifiers.py",
        "/app/execution_domain/identifiers.py",
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
        "portfolio_baseline.py",
        "packages/execution-domain/execution_domain/portfolio_baseline.py",
        "/app/execution_domain/portfolio_baseline.py",
    ),
    (
        "order_ownership.py",
        "packages/execution-domain/execution_domain/order_ownership.py",
        "/app/execution_domain/order_ownership.py",
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
        "owned_order_recovery.py",
        "services/nautilus-node/runtime/owned_order_recovery.py",
        "/app/runtime/owned_order_recovery.py",
    ),
    (
        "intent_execution_inbox.py",
        "services/nautilus-node/runtime/intent_execution_inbox.py",
        "/app/runtime/intent_execution_inbox.py",
    ),
    (
        "redis_safety.py",
        "services/nautilus-node/runtime/redis_safety.py",
        "/app/runtime/redis_safety.py",
    ),
    (
        "reconciliation.py",
        "services/nautilus-node/runtime/reconciliation.py",
        "/app/runtime/reconciliation.py",
    ),
    (
        "binance_adapter_config.py",
        "services/nautilus-node/runtime/binance_adapter_config.py",
        "/app/runtime/binance_adapter_config.py",
    ),
    (
        "nautilus_reconciliation_scope.py",
        "services/nautilus-node/runtime/nautilus_reconciliation_scope.py",
        "/app/runtime/nautilus_reconciliation_scope.py",
    ),
    (
        "live_canary_execution.py",
        "services/nautilus-node/runtime/live_canary_execution.py",
        "/app/runtime/live_canary_execution.py",
    ),
    (
        "routing_init.py",
        "services/nautilus-node/routing/__init__.py",
        "/app/routing/__init__.py",
    ),
    (
        "routing_multi_account.py",
        "services/nautilus-node/routing/multi_account.py",
        "/app/routing/multi_account.py",
    ),
    (
        "node_config.py",
        "services/nautilus-node/config/node_config.py",
        "/app/config/node_config.py",
    ),
    (
        "risk_config.py",
        "services/nautilus-node/risk/config.py",
        "/app/risk/config.py",
    ),
    (
        "risk_init.py",
        "services/nautilus-node/risk/__init__.py",
        "/app/risk/__init__.py",
    ),
    ("node.py", "services/nautilus-node/app/node.py", "/app/app/node.py"),
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
        "nautilus_config.py",
        "services/nautilus-node/persistence/nautilus_config.py",
        "/app/persistence/nautilus_config.py",
    ),
    (
        "persistence_init.py",
        "services/nautilus-node/persistence/__init__.py",
        "/app/persistence/__init__.py",
    ),
    (
        "redis_namespace_lease.py",
        "services/nautilus-node/persistence/redis_namespace_lease.py",
        "/app/persistence/redis_namespace_lease.py",
    ),
    (
        "redis_resp_client.py",
        "services/nautilus-node/persistence/redis_resp_client.py",
        "/app/persistence/redis_resp_client.py",
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
REQUIRED_RUNTIME_BUNDLE_TARGETS = {
    "/app/app/health_server.py",
    "/app/app/run_node.py",
    "/app/runtime/health.py",
    "/app/runtime/bounded_task_worker.py",
    "/app/runtime/control_plane_session.py",
    "/app/runtime/owned_order_recovery.py",
    "/app/runtime/intent_execution_inbox.py",
    "/app/runtime/redis_safety.py",
    "/app/runtime/reconciliation.py",
    "/app/runtime/nautilus_reconciliation_scope.py",
    "/app/runtime/live_canary_execution.py",
    "/app/routing/__init__.py",
    "/app/routing/multi_account.py",
    "/app/config/node_config.py",
    "/app/risk/config.py",
    "/app/risk/__init__.py",
    "/app/projection/spool.py",
    "/app/data_client/approved_intent_client.py",
    "/app/execution_domain/__init__.py",
    "/app/execution_domain/account_execution_ledger.py",
    "/app/execution_domain/entry_batch.py",
    "/app/execution_domain/idempotency.py",
    "/app/execution_domain/identifiers.py",
    "/app/persistence/nautilus_config.py",
    "/app/persistence/__init__.py",
    "/app/persistence/redis_namespace_lease.py",
    "/app/persistence/redis_resp_client.py",
}
BUNDLE_SCHEMA_VERSION = "trader-v3-container-bundle/v2"
SCHEMA_EPOCHS = {
    "app": "account-stall-hardening-runtime/v1",
    "db": "0018_projection_reliability",
    "redis": "fenced-generation-namespace/v2",
}
MANIFEST_NAME = "bundle-manifest.json"
_GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40,64}$")


class BundleError(ValueError):
    pass


SourceLoader = Callable[[str], tuple[bytes, str | bool, str]]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_source_path(source_relative: str) -> str:
    path = PurePosixPath(source_relative)
    if path.is_absolute() or ".." in path.parts or str(path) != source_relative:
        raise BundleError(
            f"source path must be repository-relative: {source_relative}"
        )
    return source_relative


def _git_run(
    repo_root: Path,
    args: list[str],
    *,
    text: bool,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=text,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            message = stderr.decode("utf-8", errors="replace").strip()
        else:
            message = stderr.strip()
        raise BundleError(message or "git command failed")
    return result


def _git_output(repo_root: Path, *args: str) -> str:
    result = _git_run(repo_root, list(args), text=True)
    return result.stdout.strip()


def resolve_git_commit(repo_root: Path, source_ref: str) -> str:
    commit = _git_output(
        repo_root,
        "rev-parse",
        "--verify",
        f"{source_ref}^{{commit}}",
    ).lower()
    if _GIT_OBJECT_RE.fullmatch(commit) is None:
        raise BundleError(f"resolved Git commit is invalid: {source_ref}")
    return commit


def resolve_git_tree(repo_root: Path, commit: str) -> str:
    tree = _git_output(
        repo_root,
        "rev-parse",
        "--verify",
        f"{commit}^{{tree}}",
    ).lower()
    if _GIT_OBJECT_RE.fullmatch(tree) is None:
        raise BundleError(f"resolved Git tree is invalid: {commit}")
    return tree


def git_status_porcelain(repo_root: Path) -> str:
    return _git_output(
        repo_root,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )


def read_git_file(
    repo_root: Path,
    commit: str,
    source_relative: str,
) -> tuple[bytes, str, str]:
    source_relative = _validated_source_path(source_relative)
    result = _git_run(
        repo_root,
        ["ls-tree", "-z", commit, "--", source_relative],
        text=False,
    )
    raw = result.stdout
    if not raw:
        raise BundleError(
            f"Git source is missing at {commit}: {source_relative}"
        )
    records = [item for item in raw.split(b"\0") if item]
    if len(records) != 1 or b"\t" not in records[0]:
        raise BundleError(
            f"Git source lookup is ambiguous: {source_relative}"
        )
    metadata, raw_path = records[0].split(b"\t", 1)
    parts = metadata.decode("ascii").split()
    if len(parts) != 3 or parts[1] != "blob":
        raise BundleError(f"Git source is not a file: {source_relative}")
    mode, _object_type, blob_id = parts
    if mode not in {"100644", "100755"}:
        raise BundleError(
            f"Git source mode is unsupported: {source_relative}"
        )
    if _GIT_OBJECT_RE.fullmatch(blob_id) is None:
        raise BundleError(f"Git blob id is invalid: {source_relative}")
    actual_path = raw_path.decode("utf-8")
    if actual_path != source_relative:
        raise BundleError(
            f"Git source path mismatch: {source_relative}"
        )
    blob = _git_run(
        repo_root,
        ["cat-file", "blob", blob_id],
        text=False,
    ).stdout
    return blob, blob_id, mode


def _validate_bundle_contract() -> None:
    configured_targets = {
        mount_target
        for _, _, mount_target in BUNDLE_FILES
    }
    missing_runtime_targets = (
        REQUIRED_RUNTIME_BUNDLE_TARGETS - configured_targets
    )
    if missing_runtime_targets:
        raise BundleError(
            "runtime bundle contract is incomplete: "
            f"{sorted(missing_runtime_targets)}"
        )
    bundle_names = [item[0] for item in BUNDLE_FILES]
    mount_targets = [item[2] for item in BUNDLE_FILES]
    if len(bundle_names) != len(set(bundle_names)):
        raise BundleError("bundle paths must be unique")
    if len(mount_targets) != len(set(mount_targets)):
        raise BundleError("bundle mount targets must be unique")


def _write_bundle(
    output_dir: Path,
    *,
    repo_commit: str,
    repo_tree: str | bool,
    repo_dirty: bool,
    source_mode: str,
    source_loader: SourceLoader,
) -> dict:
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BundleError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    _validate_bundle_contract()

    files = []
    for bundle_name, source_relative, mount_target in BUNDLE_FILES:
        payload, source_blob, git_mode = source_loader(source_relative)
        destination = output_dir / bundle_name
        destination.write_bytes(payload)
        if git_mode == "100755":
            destination.chmod(0o755)
        destination_hash = _sha256(destination)
        if destination_hash != _sha256_bytes(payload):
            raise BundleError(f"copied bundle hash mismatch: {bundle_name}")
        files.append(
            {
                "bundle_path": bundle_name,
                "source_path": source_relative,
                "source_git_blob": source_blob,
                "source_git_mode": git_mode,
                "mount_target": mount_target,
                "sha256": destination_hash,
                "size": destination.stat().st_size,
            }
        )

    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "schema_epochs": dict(SCHEMA_EPOCHS),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": repo_commit,
        "repo_tree": repo_tree,
        "repo_dirty": repo_dirty,
        "source_mode": source_mode,
        "runtime_bundle_contract": sorted(
            REQUIRED_RUNTIME_BUNDLE_TARGETS
        ),
        "files": files,
    }
    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_bundle(
    repo_root: Path,
    output_dir: Path,
    *,
    repo_commit: str,
    repo_dirty: bool,
) -> dict:
    repo_root = repo_root.resolve()

    def load_worktree_file(
        source_relative: str,
    ) -> tuple[bytes, str | bool, str]:
        source = (repo_root / source_relative).resolve()
        try:
            source.relative_to(repo_root)
        except ValueError as exc:
            raise BundleError(
                f"source escapes repository: {source_relative}"
            ) from exc
        if not source.is_file():
            raise BundleError(
                f"bundle source is missing: {source_relative}"
            )
        mode = "100644"
        if source.stat().st_mode & 0o111:
            mode = "100755"
        return source.read_bytes(), False, mode

    return _write_bundle(
        output_dir,
        repo_commit=repo_commit,
        repo_tree=False,
        repo_dirty=repo_dirty,
        source_mode="worktree",
        source_loader=load_worktree_file,
    )


def verify_git_backed_bundle(
    repo_root: Path,
    output_dir: Path,
    manifest: dict,
) -> None:
    repo_commit = str(manifest.get("repo_commit", ""))
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BundleError("bundle manifest files must be a list")
    for item in files:
        if not isinstance(item, dict):
            raise BundleError("bundle manifest file entry is invalid")
        source_relative = str(item.get("source_path", ""))
        payload, blob_id, git_mode = read_git_file(
            repo_root,
            repo_commit,
            source_relative,
        )
        if item.get("source_git_blob") != blob_id:
            raise BundleError(
                f"bundle Git blob changed: {source_relative}"
            )
        if item.get("source_git_mode") != git_mode:
            raise BundleError(
                f"bundle Git mode changed: {source_relative}"
            )
        destination = output_dir / str(item.get("bundle_path", ""))
        expected_hash = _sha256_bytes(payload)
        if item.get("sha256") != expected_hash:
            raise BundleError(
                f"bundle source hash changed: {source_relative}"
            )
        if not destination.is_file() or _sha256(destination) != expected_hash:
            raise BundleError(
                f"bundle payload hash mismatch: {destination.name}"
            )


def build_bundle_from_commit(
    repo_root: Path,
    output_dir: Path,
    *,
    source_ref: str = "HEAD",
    repo_dirty: bool = False,
    expected_head: str | bool = False,
) -> dict:
    repo_root = repo_root.resolve()
    source_commit = resolve_git_commit(repo_root, source_ref)
    source_tree = resolve_git_tree(repo_root, source_commit)

    def load_git_source(
        source_relative: str,
    ) -> tuple[bytes, str | bool, str]:
        return read_git_file(
            repo_root,
            source_commit,
            source_relative,
        )

    manifest = _write_bundle(
        output_dir,
        repo_commit=source_commit,
        repo_tree=source_tree,
        repo_dirty=repo_dirty,
        source_mode="git_object",
        source_loader=load_git_source,
    )
    verify_git_backed_bundle(repo_root, output_dir, manifest)

    final_source_commit = resolve_git_commit(repo_root, source_ref)
    if final_source_commit != source_commit:
        raise BundleError("source Git ref changed during bundle build")
    if resolve_git_tree(repo_root, source_commit) != source_tree:
        raise BundleError("source Git tree changed during bundle build")
    if expected_head:
        final_head = resolve_git_commit(repo_root, "HEAD")
        if final_head != expected_head:
            raise BundleError("HEAD changed during bundle build")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build container patches from a fixed Git commit."
    )
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--source-commit", default="HEAD")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)

    try:
        repo_root = args.repo_root.resolve()
        initial_head = resolve_git_commit(repo_root, "HEAD")
        initial_status = git_status_porcelain(repo_root)
        repo_dirty = bool(initial_status)
        if repo_dirty and not args.allow_dirty:
            raise BundleError(
                "repository has uncommitted changes; commit them or pass --allow-dirty"
            )
        manifest = build_bundle_from_commit(
            repo_root,
            args.output_dir,
            source_ref=args.source_commit,
            repo_dirty=repo_dirty,
            expected_head=initial_head,
        )
        final_status = git_status_porcelain(repo_root)
        if final_status != initial_status:
            raise BundleError(
                "worktree changed during bundle build"
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
