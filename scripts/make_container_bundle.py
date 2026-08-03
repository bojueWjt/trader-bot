#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
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
        "binance_adapter_config.py",
        "services/nautilus-node/runtime/binance_adapter_config.py",
        "/app/runtime/binance_adapter_config.py",
    ),
    ("node.py", "services/nautilus-node/app/node.py", "/app/app/node.py"),
    (
        "nautilus_actors.py",
        "services/nautilus-node/app/nautilus_actors.py",
        "/app/app/nautilus_actors.py",
    ),
    (
        "binance_execution.py",
        "container-patches/binance_execution.py",
        "/usr/local/lib/python3.12/site-packages/"
        "nautilus_trader/adapters/binance/execution.py",
    ),
    (
        "binance_futures_execution.py",
        "container-patches/binance_futures_execution.py",
        "/usr/local/lib/python3.12/site-packages/"
        "nautilus_trader/adapters/binance/futures/execution.py",
    ),
)
MANIFEST_NAME = "bundle-manifest.json"


class BundleError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_bundle(
    repo_root: Path,
    output_dir: Path,
    *,
    repo_commit: str,
    repo_dirty: bool,
) -> dict:
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BundleError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for bundle_name, source_relative, mount_target in BUNDLE_FILES:
        source = (repo_root / source_relative).resolve()
        try:
            source.relative_to(repo_root)
        except ValueError as exc:
            raise BundleError(f"source escapes repository: {source_relative}") from exc
        if not source.is_file():
            raise BundleError(f"bundle source is missing: {source_relative}")
        destination = output_dir / bundle_name
        shutil.copyfile(source, destination)
        source_hash = _sha256(source)
        destination_hash = _sha256(destination)
        if source_hash != destination_hash:
            raise BundleError(f"copied bundle hash mismatch: {bundle_name}")
        files.append(
            {
                "bundle_path": bundle_name,
                "source_path": source_relative,
                "mount_target": mount_target,
                "sha256": destination_hash,
                "size": destination.stat().st_size,
            }
        )

    manifest = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": repo_commit,
        "repo_dirty": repo_dirty,
        "files": files,
    }
    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
        description="Build container-patches from pinned repository source paths."
    )
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--allow-dirty", action="store_true")
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
