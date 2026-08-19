#!/srv/trader-v3/.venv-cp/bin/python
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


RUNNER_PATH = Path(
    "/srv/trader-v3/account-a-canary/tools/"
    "jp24_live_four_account_20260818.py"
)
LEASE_REGISTRY_KEY = "trader-bot:redis-namespaces:active:leases"
EXPECTED_ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "jp24_redis_janitor_manifest",
        RUNNER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load jp24 runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_leases() -> dict[str, dict]:
    completed = subprocess.run(
        [
            "docker",
            "exec",
            "trader-v3-redis",
            "redis-cli",
            "--raw",
            "HGETALL",
            LEASE_REGISTRY_KEY,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.splitlines()
    if len(lines) % 2 != 0:
        raise RuntimeError("Redis lease registry response is malformed")
    leases = {}
    for index in range(0, len(lines), 2):
        namespace = lines[index]
        payload = json.loads(lines[index + 1])
        if not isinstance(payload, dict):
            raise RuntimeError("Redis lease payload is malformed")
        leases[namespace] = payload
    return leases


def load_heartbeats(runner) -> dict[str, dict]:
    connection = runner.db_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT account_id,
                       status,
                       release_id,
                       lease_fencing_token,
                       reconciliation_completed_at,
                       extract(
                           epoch FROM clock_timestamp()
                           - reconciliation_completed_at
                       )
                FROM node_heartbeats
                WHERE account_id = ANY(%s)
                ORDER BY account_id
                """,
                (list(EXPECTED_ACCOUNTS),),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    return {
        str(row[0]): {
            "status": str(row[1]),
            "release_id": str(row[2]),
            "lease_fencing_token": int(row[3]),
            "reconciliation_completed_at": row[4],
            "reconciliation_age_seconds": float(row[5]),
        }
        for row in rows
    }


def build_nodes(
    *,
    leases: dict[str, dict],
    heartbeats: dict[str, dict],
    max_reconciliation_age_seconds: int,
) -> list[dict]:
    if set(heartbeats) != set(EXPECTED_ACCOUNTS):
        raise RuntimeError("A-D heartbeat set is incomplete")
    nodes = []
    for account_id in EXPECTED_ACCOUNTS:
        heartbeat = heartbeats[account_id]
        if heartbeat["status"] != "HALTED":
            raise RuntimeError(f"{account_id} must remain HALTED")
        if (
            heartbeat["reconciliation_age_seconds"]
            > max_reconciliation_age_seconds
        ):
            raise RuntimeError(f"{account_id} reconciliation is stale")
        suffix = account_id[-1].upper()
        lease_namespace = f"trader-TRADER-ACCOUNT-{suffix}"
        lease = leases.get(lease_namespace)
        if not isinstance(lease, dict):
            raise RuntimeError(f"{account_id} live lease is missing")
        release_id = str(lease.get("release_id") or "")
        fencing_token = int(lease.get("fencing_token") or 0)
        if release_id != heartbeat["release_id"]:
            raise RuntimeError(f"{account_id} release identity mismatch")
        if fencing_token != heartbeat["lease_fencing_token"]:
            raise RuntimeError(f"{account_id} fencing token mismatch")
        reconciliation_completed_at = heartbeat[
            "reconciliation_completed_at"
        ]
        if not isinstance(reconciliation_completed_at, datetime):
            raise RuntimeError(f"{account_id} reconciliation timestamp missing")
        completed_at = reconciliation_completed_at.astimezone(timezone.utc)
        nodes.append(
            {
                "account_id": account_id,
                "lease_namespace": lease_namespace,
                "persistence_namespace": str(
                    lease.get("persistence_namespace") or ""
                ),
                "owner": str(lease.get("owner") or ""),
                "release_id": release_id,
                "fencing_token": fencing_token,
                "reconciliation": {
                    "state": "HEALTHY",
                    "completed_at_epoch": int(completed_at.timestamp()),
                    "release_id": release_id,
                },
            }
        )
    return nodes


def write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_raw)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-source-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-reconciliation-age-seconds",
        type=int,
        default=120,
    )
    args = parser.parse_args()
    backup_source = json.loads(
        args.backup_source_manifest.read_text(encoding="utf-8")
    )
    backup = backup_source.get("backup")
    if not isinstance(backup, dict):
        raise RuntimeError("backup source manifest is invalid")
    runner = load_runner()
    manifest = {
        "schema_version": "trader-redis-janitor-safety/v2",
        "backup": backup,
        "nodes": build_nodes(
            leases=load_leases(),
            heartbeats=load_heartbeats(runner),
            max_reconciliation_age_seconds=(
                args.max_reconciliation_age_seconds
            ),
        ),
    }
    write_atomic(args.output, manifest)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "accounts": [
                    node["account_id"] for node in manifest["nodes"]
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
