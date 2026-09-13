#!/usr/bin/env python3
"""Verify retained rollback bytes when a maintenance rollout's old container is gone.

The deployment caller must first validate the original cold/capacity evidence
chain and the unchanged live Redis identity. This does not authorize a Redis
rebaseline, reconstruct historical container identities, or start Redis.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError(f"rollback verification command failed: {args[0]}")
    return result.stdout


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_containers() -> list:
    ids = run("docker", "ps", "-aq").split()
    return json.loads(run("docker", "inspect", *ids)) if ids else []


def check_rdb(root: Path, image_id: str, rdb: Path) -> str:
    token = uuid4().hex
    name = f"trader-retired-redis-check-{token}"
    label = f"trader.evidence.check={token}"
    try:
        return run(
            "docker", "run", "--rm", "--name", name, "--label", label,
            "--network", "none", "--read-only", "--memory", "256m",
            "--cpus", "0.5", "--pids-limit", "32", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--entrypoint",
            "redis-check-rdb", "-v", f"{root}:/backup:ro", image_id,
            f"/backup/{rdb.name}",
        )
    finally:
        # A docker CLI timeout does not cancel work in the daemon. Only remove
        # this invocation's unique, labelled checker, then verify it is gone.
        filters = ("--filter", f"name=^/{name}$", "--filter", f"label={label}")
        ids = run("docker", "ps", "-aq", *filters).split()
        for container_id in ids:
            run("docker", "rm", "-f", container_id)
        if run("docker", "ps", "-aq", *filters).strip():
            raise RuntimeError("Redis checker container cleanup failed")


def verify_retained_files(backup: dict, volume: dict, containers: list) -> Path:
    root = Path(backup["source_data_root"])
    if root.resolve() != root or not root.is_dir():
        raise ValueError("retired Redis source path is not canonical")
    if volume.get("Name") != backup["source_volume_name"]:
        raise ValueError("retired Redis volume name differs")
    if volume.get("Mountpoint") != str(root):
        raise ValueError("retired Redis volume path differs")
    if volume.get("Driver") != "local" or volume.get("Options"):
        raise ValueError("retired Redis volume is not an ordinary local volume")
    for container in containers:
        for mount in container.get("Mounts", []):
            source = Path(mount.get("Source") or "/nonexistent")
            source = source.resolve()
            overlaps = root.is_relative_to(source) or source.is_relative_to(root)
            if mount.get("Name") == volume["Name"] or overlaps:
                raise ValueError("retired Redis volume is still attached to a container")
    expected = {}
    for item in backup["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "data":
            raise ValueError("invalid retired Redis artifact path")
        path = root.joinpath(*relative.parts[1:])
        if path in expected:
            raise ValueError("duplicate retired Redis artifact")
        expected[path] = item
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise ValueError("retired Redis volume contains an unsafe file")
        if path.is_file():
            actual.add(path)
    if not expected or actual != set(expected):
        raise ValueError("retired Redis volume file set differs from cold backup")
    for path, item in expected.items():
        if path.stat().st_size != item["size_bytes"] or digest(path) != item["sha256"]:
            raise ValueError("retired Redis volume bytes differ from cold backup")
    return root


def main() -> None:
    backup_path, capacity_path, mode = sys.argv[1:]
    if mode != "maintenance_fence":
        raise ValueError("missing legacy container recovery is limited to maintenance")
    backup = json.loads(Path(backup_path).read_text())
    capacity = json.loads(Path(capacity_path).read_text())
    if digest(Path(backup_path)) != capacity["source_backup_manifest_sha256"]:
        raise ValueError("cold backup is not bound to capacity evidence")
    if capacity["active_volume"] == backup["source_volume_name"]:
        raise ValueError("retired Redis volume is the active volume")
    containers = inspect_containers()
    for container in containers:
        if container.get("Name", "").lstrip("/") == capacity["legacy_container"]:
            raise ValueError("legacy container exists; its original identity must be checked")
        if container.get("Id") == backup["source_container_id"]:
            raise ValueError("original legacy container still exists under another name")
    volume = json.loads(run("docker", "volume", "inspect", backup["source_volume_name"]))[0]
    root = verify_retained_files(backup, volume, containers)
    image = json.loads(run("docker", "image", "inspect", backup["source_image_digest"]))[0]
    if image["Id"] != backup["source_image_digest"]:
        raise ValueError("original Redis rollback image differs")
    if backup["source_aof_enabled"] != 0 or backup["source_appendonly"] != "no":
        raise ValueError("retired Redis AOF recovery needs a separate recovery procedure")
    rdb = root / backup["source_rdb_filename"]
    if not rdb.is_file() or len(backup["files"]) != 1:
        raise ValueError("retired Redis recovery requires a single verified RDB")
    output = check_rdb(root, image["Id"], rdb)
    volume = json.loads(run("docker", "volume", "inspect", backup["source_volume_name"]))[0]
    verify_retained_files(backup, volume, inspect_containers())
    print(json.dumps({
        "schema_version": "trader-v3-retired-redis-artifacts/v1",
        "legacy_container_present": False,
        "original_container_id": backup["source_container_id"],
        "retained_volume": volume["Name"],
        "retained_image_digest": image["Id"],
        "rdb_sha256": digest(rdb),
        "validator_output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "passed": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
