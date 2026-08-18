#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


GIB = 1024**3
DEFAULT_VALIDITY_SECONDS = 24 * 60 * 60
DEFAULT_MINIMUM_DISK_FREE_BYTES = 8 * GIB
SCHEMA_VERSION = "trader-v3-redis-capacity-refresh/v1"
BASE_SCHEMA_VERSION = "trader-v3-redis-capacity-evidence/v3"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{40}$")


class CapacityRefreshError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise CapacityRefreshError(f"{label} cannot be a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapacityRefreshError(f"{label} is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise CapacityRefreshError(f"{label} root must be an object")
    return payload


def _required_int(
    document: dict[str, Any],
    key: str,
    *,
    minimum: int = 0,
) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapacityRefreshError(f"base evidence integer is invalid: {key}")
    if value < minimum:
        raise CapacityRefreshError(f"base evidence integer is too small: {key}")
    return value


def _required_text(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CapacityRefreshError(f"base evidence text is invalid: {key}")
    return value.strip()


def _single_data_mount(inspect: dict[str, Any]) -> dict[str, Any]:
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list):
        raise CapacityRefreshError("Redis container mounts are invalid")
    data_mounts = [
        item
        for item in mounts
        if isinstance(item, dict) and item.get("Destination") == "/data"
    ]
    if len(data_mounts) != 1:
        raise CapacityRefreshError(
            "Redis container requires one /data volume mount"
        )
    mount = data_mounts[0]
    if mount.get("Type") != "volume" or mount.get("RW") is not True:
        raise CapacityRefreshError("Redis /data mount must be a writable volume")
    return mount


def _validate_runtime_identity(
    *,
    base: dict[str, Any],
    inspect: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    if inspect.get("Id") != _required_text(base, "active_container_id"):
        raise CapacityRefreshError(
            "running Redis container ID differs from base evidence"
        )
    run_id = str(runtime.get("run_id") or "").strip()
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise CapacityRefreshError("running Redis run_id is invalid")
    if run_id != _required_text(base, "active_redis_run_id"):
        raise CapacityRefreshError(
            "running Redis run_id differs from base evidence"
        )
    mount = _single_data_mount(inspect)
    if mount.get("Name") != _required_text(base, "active_volume"):
        raise CapacityRefreshError(
            "running Redis volume differs from base evidence"
        )
    actual_source = Path(str(mount.get("Source") or "")).resolve()
    expected_source = Path(
        _required_text(base, "active_volume_source")
    ).resolve()
    if actual_source != expected_source:
        raise CapacityRefreshError(
            "running Redis volume source differs from base evidence"
        )
    fencing_epoch = str(runtime.get("fencing_epoch") or "").strip()
    if fencing_epoch != _required_text(base, "redis_fencing_epoch"):
        raise CapacityRefreshError(
            "running Redis fencing epoch differs from base evidence"
        )
    fencing_hash = hashlib.sha256(fencing_epoch.encode("utf-8")).hexdigest()
    if fencing_hash != _required_text(base, "redis_fencing_epoch_sha256"):
        raise CapacityRefreshError("running Redis fencing epoch hash differs")


def _validate_runtime_resources(
    *,
    base: dict[str, Any],
    inspect: dict[str, Any],
    runtime: dict[str, Any],
    host_total_bytes: int,
    host_available_bytes: int,
    disk_free_bytes: int,
    minimum_disk_free_bytes: int,
) -> int:
    host_config = inspect.get("HostConfig")
    if not isinstance(host_config, dict):
        raise CapacityRefreshError("Redis HostConfig is invalid")
    memory_limit = int(host_config.get("Memory") or 0)
    memory_swap = int(host_config.get("MemorySwap") or 0)
    expected_memory = _required_int(base, "memory_limit_bytes", minimum=1)
    expected_swap = _required_int(
        base,
        "memory_swap_limit_bytes",
        minimum=1,
    )
    if memory_limit != expected_memory:
        raise CapacityRefreshError(
            "running Redis memory limit differs from base evidence"
        )
    if memory_swap != expected_swap or memory_swap != memory_limit:
        raise CapacityRefreshError(
            "running Redis memory swap differs from base evidence"
        )
    maxmemory = int(runtime.get("maxmemory_bytes") or 0)
    if maxmemory != _required_int(base, "maxmemory_bytes", minimum=1):
        raise CapacityRefreshError(
            "running Redis maxmemory differs from base evidence"
        )
    policy = str(runtime.get("maxmemory_policy") or "").strip()
    if policy != "noeviction" or policy != base.get("maxmemory_policy"):
        raise CapacityRefreshError(
            "running Redis maxmemory policy differs from base evidence"
        )
    if str(runtime.get("appendonly") or "") != base.get("active_appendonly"):
        raise CapacityRefreshError(
            "running Redis appendonly differs from base evidence"
        )
    if int(runtime.get("aof_enabled") or 0) != int(
        base.get("active_aof_enabled", -1)
    ):
        raise CapacityRefreshError(
            "running Redis AOF state differs from base evidence"
        )
    if str(runtime.get("save_policy") or "") != base.get("active_save_policy"):
        raise CapacityRefreshError(
            "running Redis save policy differs from base evidence"
        )
    if str(runtime.get("rdb_last_bgsave_status") or "") != base.get(
        "active_rdb_last_bgsave_status"
    ):
        raise CapacityRefreshError(
            "running Redis persistence status differs from base evidence"
        )
    if int(runtime.get("dbsize") or 0) < 1:
        raise CapacityRefreshError("running Redis fencing key is unavailable")
    used_memory = int(runtime.get("used_memory_bytes") or 0)
    dataset_memory = int(runtime.get("dataset_memory_bytes") or 0)
    if used_memory <= 0 or dataset_memory < 0 or dataset_memory > used_memory:
        raise CapacityRefreshError("running Redis memory metrics are invalid")
    if maxmemory <= used_memory:
        raise CapacityRefreshError("running Redis has no maxmemory headroom")
    headroom_percent = _required_int(base, "headroom_percent", minimum=1)
    required_maxmemory = (
        used_memory * (100 + headroom_percent) + 99
    ) // 100
    if maxmemory < required_maxmemory:
        raise CapacityRefreshError(
            "running Redis lacks required operating headroom"
        )
    container_headroom_percent = _required_int(
        base,
        "container_headroom_percent",
        minimum=1,
    )
    required_container = (
        maxmemory * (100 + container_headroom_percent) + 99
    ) // 100
    if memory_limit < required_container:
        raise CapacityRefreshError(
            "running Redis cgroup lacks container headroom"
        )
    other_reserve = _required_int(
        base,
        "other_services_reserve_bytes",
        minimum=1,
    )
    system_reserve = _required_int(
        base,
        "system_reserve_bytes",
        minimum=1,
    )
    required_total = required_container + other_reserve + system_reserve
    if host_total_bytes < required_total:
        raise CapacityRefreshError("host total memory is insufficient")
    required_available = (
        maxmemory
        - used_memory
        + required_container
        - maxmemory
        + other_reserve
        + system_reserve
    )
    if host_available_bytes < required_available:
        raise CapacityRefreshError(
            "host available memory cannot preserve required reserves"
        )
    if disk_free_bytes < minimum_disk_free_bytes:
        raise CapacityRefreshError(
            "disk free space is below the deployment reserve"
        )
    return required_available


def build_refresh_receipt(
    *,
    base_path: Path,
    base: dict[str, Any],
    inspect: dict[str, Any],
    runtime: dict[str, Any],
    host_total_bytes: int,
    host_available_bytes: int,
    disk_free_bytes: int,
    minimum_disk_free_bytes: int,
    now: datetime,
) -> dict[str, Any]:
    if base.get("schema_version") != BASE_SCHEMA_VERSION:
        raise CapacityRefreshError("base capacity evidence schema mismatch")
    if now.tzinfo is None or now.utcoffset() is None:
        raise CapacityRefreshError("refresh timestamp requires a timezone")
    _validate_runtime_identity(base=base, inspect=inspect, runtime=runtime)
    required_available = _validate_runtime_resources(
        base=base,
        inspect=inspect,
        runtime=runtime,
        host_total_bytes=host_total_bytes,
        host_available_bytes=host_available_bytes,
        disk_free_bytes=disk_free_bytes,
        minimum_disk_free_bytes=minimum_disk_free_bytes,
    )
    created_at = now.astimezone(timezone.utc).replace(microsecond=0)
    expires_at = created_at + timedelta(seconds=DEFAULT_VALIDITY_SECONDS)
    mount = _single_data_mount(inspect)
    host_config = inspect["HostConfig"]
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "validity_seconds": DEFAULT_VALIDITY_SECONDS,
        "passed": True,
        "base_capacity_evidence_path": str(base_path.resolve()),
        "base_capacity_evidence_sha256": sha256_file(base_path),
        "active_container_id": str(inspect["Id"]),
        "active_image_digest": str(inspect.get("Image") or ""),
        "active_redis_run_id": str(runtime["run_id"]),
        "active_volume": str(mount["Name"]),
        "active_volume_source": str(Path(str(mount["Source"])).resolve()),
        "redis_fencing_epoch": str(runtime["fencing_epoch"]),
        "redis_fencing_epoch_sha256": hashlib.sha256(
            str(runtime["fencing_epoch"]).encode("utf-8")
        ).hexdigest(),
        "maxmemory_bytes": int(runtime["maxmemory_bytes"]),
        "maxmemory_policy": str(runtime["maxmemory_policy"]),
        "memory_limit_bytes": int(host_config["Memory"]),
        "memory_swap_limit_bytes": int(host_config["MemorySwap"]),
        "used_memory_bytes": int(runtime["used_memory_bytes"]),
        "dataset_memory_bytes": int(runtime["dataset_memory_bytes"]),
        "host_total_memory_bytes": host_total_bytes,
        "host_available_memory_bytes": host_available_bytes,
        "required_host_available_bytes": required_available,
        "disk_free_bytes": disk_free_bytes,
        "minimum_disk_free_bytes": minimum_disk_free_bytes,
        "runtime_checks": {
            "base_hash_bound": True,
            "container_identity_bound": True,
            "redis_run_id_bound": True,
            "volume_identity_bound": True,
            "fencing_epoch_bound": True,
            "memory_contract_passed": True,
            "persistence_contract_passed": True,
            "host_reserve_passed": True,
            "disk_reserve_passed": True,
        },
    }


def write_receipt_atomic(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_raw)
    try:
        os.fchmod(descriptor, 0o400)
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def verify_refresh_receipt(
    *,
    receipt_path: Path,
    base_path: Path,
    now: datetime,
    max_age_seconds: int,
) -> dict[str, Any]:
    receipt = load_json_object(receipt_path, "capacity refresh receipt")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise CapacityRefreshError("capacity refresh receipt schema mismatch")
    if receipt.get("passed") is not True:
        raise CapacityRefreshError("capacity refresh receipt is not PASS")
    actual_base_hash = sha256_file(base_path)
    if receipt.get("base_capacity_evidence_sha256") != actual_base_hash:
        raise CapacityRefreshError(
            "capacity refresh base evidence hash differs"
        )
    if Path(
        str(receipt.get("base_capacity_evidence_path") or "")
    ).resolve() != base_path.resolve():
        raise CapacityRefreshError("capacity refresh base evidence path differs")
    try:
        created_at = datetime.fromisoformat(
            str(receipt.get("created_at") or "").replace("Z", "+00:00")
        )
        expires_at = datetime.fromisoformat(
            str(receipt.get("expires_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise CapacityRefreshError(
            "capacity refresh timestamp is invalid"
        ) from exc
    age_seconds = (now.astimezone(timezone.utc) - created_at).total_seconds()
    if age_seconds < 0 or age_seconds > max_age_seconds:
        raise CapacityRefreshError("capacity refresh receipt is stale")
    if expires_at <= now.astimezone(timezone.utc):
        raise CapacityRefreshError("capacity refresh receipt has expired")
    checks = receipt.get("runtime_checks")
    if not isinstance(checks, dict) or not checks:
        raise CapacityRefreshError("capacity refresh checks are missing")
    if any(value is not True for value in checks.values()):
        raise CapacityRefreshError("capacity refresh checks did not pass")
    return receipt


def _run(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise CapacityRefreshError(
            f"command failed: {command[0]}: {stderr}"
        )
    return completed.stdout


def _parse_info(payload: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in payload.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key] = value
    return values


def _redis_scalar(
    docker_bin: str,
    container: str,
    *arguments: str,
) -> str:
    output = _run(
        [docker_bin, "exec", container, "redis-cli", "--raw", *arguments]
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise CapacityRefreshError(
            f"Redis command returned no value: {' '.join(arguments)}"
        )
    return lines[-1]


def inspect_live_runtime(
    *,
    docker_bin: str,
    container: str,
    fencing_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    inspect_payload = json.loads(_run([docker_bin, "inspect", container]))
    if not isinstance(inspect_payload, list) or len(inspect_payload) != 1:
        raise CapacityRefreshError("Docker inspect returned an invalid payload")
    inspect = inspect_payload[0]
    if not isinstance(inspect, dict):
        raise CapacityRefreshError("Docker inspect item is invalid")
    server = _parse_info(
        _run(
            [
                docker_bin,
                "exec",
                container,
                "redis-cli",
                "--raw",
                "INFO",
                "server",
            ]
        )
    )
    memory = _parse_info(
        _run(
            [
                docker_bin,
                "exec",
                container,
                "redis-cli",
                "--raw",
                "INFO",
                "memory",
            ]
        )
    )
    persistence = _parse_info(
        _run(
            [
                docker_bin,
                "exec",
                container,
                "redis-cli",
                "--raw",
                "INFO",
                "persistence",
            ]
        )
    )
    runtime = {
        "run_id": server.get("run_id", ""),
        "dbsize": int(_redis_scalar(docker_bin, container, "DBSIZE")),
        "fencing_epoch": _redis_scalar(
            docker_bin,
            container,
            "GET",
            fencing_key,
        ),
        "maxmemory_bytes": int(
            _redis_scalar(
                docker_bin,
                container,
                "CONFIG",
                "GET",
                "maxmemory",
            )
        ),
        "maxmemory_policy": _redis_scalar(
            docker_bin,
            container,
            "CONFIG",
            "GET",
            "maxmemory-policy",
        ),
        "used_memory_bytes": int(memory.get("used_memory", "0")),
        "dataset_memory_bytes": int(
            memory.get("used_memory_dataset", "0")
        ),
        "appendonly": _redis_scalar(
            docker_bin,
            container,
            "CONFIG",
            "GET",
            "appendonly",
        ),
        "aof_enabled": int(persistence.get("aof_enabled", "0")),
        "save_policy": _redis_scalar(
            docker_bin,
            container,
            "CONFIG",
            "GET",
            "save",
        ),
        "rdb_last_bgsave_status": persistence.get(
            "rdb_last_bgsave_status",
            "",
        ),
    }
    return inspect, runtime


def read_meminfo(path: Path) -> tuple[int, int]:
    values: dict[str, int] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([^:]+):\s+([0-9]+)\s+kB", raw.strip())
        if match is None:
            continue
        values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    if total <= 0 or available <= 0 or available > total:
        raise CapacityRefreshError("host memory information is invalid")
    return total, available


def disk_free_bytes(path: Path) -> int:
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh and verify live Redis capacity evidence.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("--base-evidence", type=Path, required=True)
    refresh_parser.add_argument("--output", type=Path, required=True)
    refresh_parser.add_argument(
        "--redis-container",
        default="trader-v3-redis",
    )
    refresh_parser.add_argument("--docker-bin", default="/usr/bin/docker")
    refresh_parser.add_argument(
        "--meminfo",
        type=Path,
        default=Path("/proc/meminfo"),
    )
    refresh_parser.add_argument(
        "--disk-path",
        type=Path,
        default=Path("/srv"),
    )
    refresh_parser.add_argument(
        "--minimum-disk-free-bytes",
        type=int,
        default=DEFAULT_MINIMUM_DISK_FREE_BYTES,
    )
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--base-evidence", type=Path, required=True)
    verify_parser.add_argument("--receipt", type=Path, required=True)
    verify_parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_VALIDITY_SECONDS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.command == "verify":
        verify_refresh_receipt(
            receipt_path=args.receipt,
            base_path=args.base_evidence,
            now=now,
            max_age_seconds=args.max_age_seconds,
        )
        print(f"PASS: capacity refresh receipt {args.receipt}")
        return 0
    base = load_json_object(args.base_evidence, "base capacity evidence")
    fencing_key = _required_text(base, "redis_fencing_epoch_key")
    inspect, runtime = inspect_live_runtime(
        docker_bin=args.docker_bin,
        container=args.redis_container,
        fencing_key=fencing_key,
    )
    host_total, host_available = read_meminfo(args.meminfo)
    receipt = build_refresh_receipt(
        base_path=args.base_evidence,
        base=base,
        inspect=inspect,
        runtime=runtime,
        host_total_bytes=host_total,
        host_available_bytes=host_available,
        disk_free_bytes=disk_free_bytes(args.disk_path),
        minimum_disk_free_bytes=args.minimum_disk_free_bytes,
        now=now,
    )
    write_receipt_atomic(args.output, receipt)
    verify_refresh_receipt(
        receipt_path=args.output,
        base_path=args.base_evidence,
        now=now,
        max_age_seconds=DEFAULT_VALIDITY_SECONDS,
    )
    print(f"PASS: refreshed Redis capacity evidence {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
