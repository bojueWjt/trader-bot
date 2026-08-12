#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

DEFAULT_HEADROOM_PERCENT = 20
DEFAULT_CONTAINER_HEADROOM_PERCENT = 20
GIB = 1024**3
DEFAULT_SYSTEM_RESERVE_BYTES = 3 * GIB
MIN_SYSTEM_RESERVE_BYTES = 1 * GIB
MAXMEMORY_POLICY = "noeviction"
_CGROUP_LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
_MEMORY_SIZE_RE = re.compile(
    r"^(?P<amount>[0-9]+(?:\.[0-9]+)?)"
    r"(?P<unit>b|kb|kib|mb|mib|gb|gib|tb|tib)?$",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RDB_HEADER_RE = re.compile(rb"^REDIS[0-9]{4}$")
_AOF_MANIFEST_LINE_RE = re.compile(
    r"^file (?P<filename>[^/\s]+) seq [0-9]+ type [bih]$"
)
_UNIT_MULTIPLIERS = {
    "b": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024**2,
    "mib": 1024**2,
    "gb": 1024**3,
    "gib": 1024**3,
    "tb": 1024**4,
    "tib": 1024**4,
}


class CapacitySafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RedisCapacityPlan:
    maxmemory_bytes: int
    used_memory_bytes: int
    dataset_bytes: int
    headroom_percent: int
    required_maxmemory_bytes: int
    remaining_headroom_bytes: int
    redis_cgroup_limit_bytes: int
    container_headroom_percent: int
    required_container_bytes: int
    host_total_memory_bytes: int
    host_available_memory_bytes: int
    other_services_reserve_bytes: int
    system_reserve_bytes: int
    required_host_total_bytes: int
    growth_to_maxmemory_bytes: int
    required_host_available_bytes: int
    policy: str = MAXMEMORY_POLICY


def parse_memory_size(value: str) -> int:
    normalized = value.strip().lower()
    match = _MEMORY_SIZE_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(f"invalid memory size {value!r}")
    amount_raw = match.group("amount")
    unit = match.group("unit") or "b"
    try:
        amount = Decimal(amount_raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid memory size {value!r}") from exc
    result = amount * _UNIT_MULTIPLIERS[unit]
    return int(result.to_integral_value(rounding=ROUND_CEILING))


def build_capacity_plan(
    *,
    maxmemory_bytes: int,
    used_memory_bytes: int,
    dataset_bytes: int,
    redis_cgroup_limit_bytes: int,
    host_total_memory_bytes: int,
    host_available_memory_bytes: int,
    other_services_reserve_bytes: int,
    system_reserve_bytes: int = DEFAULT_SYSTEM_RESERVE_BYTES,
    headroom_percent: int = DEFAULT_HEADROOM_PERCENT,
    container_headroom_percent: int = DEFAULT_CONTAINER_HEADROOM_PERCENT,
) -> RedisCapacityPlan:
    _require_positive("maxmemory_bytes", maxmemory_bytes)
    _require_positive("used_memory_bytes", used_memory_bytes)
    _require_nonnegative("dataset_bytes", dataset_bytes)
    _require_positive("redis_cgroup_limit_bytes", redis_cgroup_limit_bytes)
    _require_positive("host_total_memory_bytes", host_total_memory_bytes)
    _require_positive("host_available_memory_bytes", host_available_memory_bytes)
    _require_positive("other_services_reserve_bytes", other_services_reserve_bytes)
    _require_positive("system_reserve_bytes", system_reserve_bytes)
    _require_positive("headroom_percent", headroom_percent)
    _require_positive("container_headroom_percent", container_headroom_percent)
    if dataset_bytes > used_memory_bytes:
        raise CapacitySafetyError("dataset memory cannot exceed used memory")
    if host_available_memory_bytes > host_total_memory_bytes:
        raise CapacitySafetyError("host available memory cannot exceed host total memory")
    if redis_cgroup_limit_bytes > host_total_memory_bytes:
        raise CapacitySafetyError(
            "Redis cgroup limit exceeds host total memory and is not a finite host budget"
        )
    if system_reserve_bytes < MIN_SYSTEM_RESERVE_BYTES:
        raise CapacitySafetyError(
            f"system reserve must be at least {MIN_SYSTEM_RESERVE_BYTES} bytes"
        )

    multiplier = Decimal(100 + headroom_percent) / Decimal(100)
    required = int(
        (Decimal(used_memory_bytes) * multiplier).to_integral_value(
            rounding=ROUND_CEILING,
        )
    )
    if maxmemory_bytes <= used_memory_bytes:
        raise CapacitySafetyError(
            "maxmemory is at or below current Redis used_memory; "
            "noeviction will reject writes after the limit is applied"
        )
    if maxmemory_bytes < required:
        raise CapacitySafetyError(
            "maxmemory lacks the required operating headroom; "
            "noeviction will reject writes as memory grows"
        )

    container_multiplier = (
        Decimal(100 + container_headroom_percent) / Decimal(100)
    )
    required_container = int(
        (Decimal(maxmemory_bytes) * container_multiplier).to_integral_value(
            rounding=ROUND_CEILING,
        )
    )
    if required_container > redis_cgroup_limit_bytes:
        raise CapacitySafetyError(
            "Redis cgroup limit cannot contain maxmemory plus container headroom"
        )

    required_host_total = (
        required_container
        + other_services_reserve_bytes
        + system_reserve_bytes
    )
    if required_host_total > host_total_memory_bytes:
        raise CapacitySafetyError(
            "host total memory cannot contain Redis, other services, and system reserve"
        )

    growth_to_maxmemory = maxmemory_bytes - used_memory_bytes
    container_headroom_bytes = required_container - maxmemory_bytes
    required_host_available = (
        growth_to_maxmemory
        + container_headroom_bytes
        + other_services_reserve_bytes
        + system_reserve_bytes
    )
    if required_host_available > host_available_memory_bytes:
        raise CapacitySafetyError(
            "host available memory cannot preserve required reserves while Redis grows"
        )

    return RedisCapacityPlan(
        maxmemory_bytes=maxmemory_bytes,
        used_memory_bytes=used_memory_bytes,
        dataset_bytes=dataset_bytes,
        headroom_percent=headroom_percent,
        required_maxmemory_bytes=required,
        remaining_headroom_bytes=maxmemory_bytes - used_memory_bytes,
        redis_cgroup_limit_bytes=redis_cgroup_limit_bytes,
        container_headroom_percent=container_headroom_percent,
        required_container_bytes=required_container,
        host_total_memory_bytes=host_total_memory_bytes,
        host_available_memory_bytes=host_available_memory_bytes,
        other_services_reserve_bytes=other_services_reserve_bytes,
        system_reserve_bytes=system_reserve_bytes,
        required_host_total_bytes=required_host_total,
        growth_to_maxmemory_bytes=growth_to_maxmemory,
        required_host_available_bytes=required_host_available,
    )


def render_docker_args(plan: RedisCapacityPlan) -> tuple[str, ...]:
    return (
        "--maxmemory",
        str(plan.maxmemory_bytes),
        "--maxmemory-policy",
        plan.policy,
    )


def render_docker_memory_args(plan: RedisCapacityPlan) -> tuple[str, ...]:
    limit = str(plan.redis_cgroup_limit_bytes)
    return (
        "--memory",
        limit,
        "--memory-swap",
        limit,
    )


def render_redis_config(plan: RedisCapacityPlan) -> str:
    return (
        f"maxmemory {plan.maxmemory_bytes}\n"
        f"maxmemory-policy {plan.policy}\n"
    )


def inspect_running_redis(
    redis_client,
    *,
    expected_maxmemory_bytes: int,
    redis_cgroup_limit_bytes: int,
    host_total_memory_bytes: int,
    host_available_memory_bytes: int,
    other_services_reserve_bytes: int,
    system_reserve_bytes: int,
    headroom_percent: int,
    container_headroom_percent: int,
) -> RedisCapacityPlan:
    memory = redis_client.info("memory")
    config = redis_client.config_get("maxmemory")
    policy_config = redis_client.config_get("maxmemory-policy")
    actual_maxmemory = int(config.get("maxmemory", 0))
    actual_policy = str(policy_config.get("maxmemory-policy", ""))
    if actual_maxmemory <= 0:
        raise CapacitySafetyError(
            "running Redis maxmemory is unbounded"
        )
    if actual_maxmemory != expected_maxmemory_bytes:
        raise CapacitySafetyError("running Redis maxmemory differs from expected value")
    if actual_policy != MAXMEMORY_POLICY:
        raise CapacitySafetyError("running Redis maxmemory-policy must be noeviction")

    return build_capacity_plan(
        maxmemory_bytes=actual_maxmemory,
        used_memory_bytes=int(memory["used_memory"]),
        dataset_bytes=int(memory["used_memory_dataset"]),
        redis_cgroup_limit_bytes=redis_cgroup_limit_bytes,
        host_total_memory_bytes=host_total_memory_bytes,
        host_available_memory_bytes=host_available_memory_bytes,
        other_services_reserve_bytes=other_services_reserve_bytes,
        system_reserve_bytes=system_reserve_bytes,
        headroom_percent=headroom_percent,
        container_headroom_percent=container_headroom_percent,
    )


def _plan_payload(plan: RedisCapacityPlan) -> dict[str, object]:
    payload = asdict(plan)
    payload["maxmemory_policy"] = payload.pop("policy")
    payload["docker_args"] = render_docker_args(plan)
    payload["redis_argument_fragment"] = payload["docker_args"]
    payload["docker_memory_args"] = render_docker_memory_args(plan)
    payload["redis_config"] = render_redis_config(plan)
    return payload


def read_host_memory(
    meminfo_path: Path = Path("/proc/meminfo"),
) -> tuple[int, int]:
    values: dict[str, int] = {}
    try:
        lines = meminfo_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CapacitySafetyError("host memory probe failed") from exc
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        key = parts[0].rstrip(":")
        if key not in {"MemTotal", "MemAvailable"}:
            continue
        if len(parts) < 3 or parts[2].lower() != "kb":
            raise CapacitySafetyError("host memory probe returned an unknown unit")
        try:
            values[key] = int(parts[1]) * 1024
        except ValueError as exc:
            raise CapacitySafetyError("host memory probe returned invalid data") from exc
    if "MemTotal" not in values or "MemAvailable" not in values:
        raise CapacitySafetyError(
            "host memory probe requires MemTotal and MemAvailable"
        )
    return values["MemTotal"], values["MemAvailable"]


def read_cgroup_memory_limit(
    paths: tuple[Path, ...] = _CGROUP_LIMIT_PATHS,
) -> int:
    for path in paths:
        if not path.exists():
            continue
        try:
            raw_value = path.read_text(encoding="utf-8").strip().lower()
        except OSError as exc:
            raise CapacitySafetyError("Redis cgroup memory probe failed") from exc
        if raw_value == "max":
            raise CapacitySafetyError(
                "Redis cgroup memory is unlimited; provide a finite container limit"
            )
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise CapacitySafetyError(
                "Redis cgroup memory probe returned invalid data"
            ) from exc
        _require_positive("redis_cgroup_limit_bytes", value)
        return value
    raise CapacitySafetyError("Redis cgroup memory limit is unavailable")


def verify_cold_backup_manifest(
    manifest_path: Path,
    *,
    expected_backup_root: Path,
    expected_source_run_id: str,
    expected_source_volume: str,
    expected_source_container_id: str,
) -> dict[str, object]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapacitySafetyError("cold backup manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise CapacitySafetyError("cold backup manifest must be a JSON object")
    if payload.get("schema_version") != "trader-v3-redis-cold-backup/v2":
        raise CapacitySafetyError("cold backup manifest schema is unsupported")
    if payload.get("mode") != "cold":
        raise CapacitySafetyError("cold backup mode must be cold")
    if payload.get("nodes_stopped") is not True:
        raise CapacitySafetyError("cold backup requires stopped execution nodes")
    if payload.get("rdb_changes_since_last_save") != 0:
        raise CapacitySafetyError("cold backup was not captured after a clean SAVE")
    if payload.get("source_mount_type") != "volume":
        raise CapacitySafetyError("cold backup source must be a Docker volume")
    if payload.get("source_redis_dir") != "/data":
        raise CapacitySafetyError("cold backup source Redis dir must be /data")

    backup_root = expected_backup_root.resolve()
    manifest_root_raw = payload.get("backup_root")
    if not isinstance(manifest_root_raw, str):
        raise CapacitySafetyError("cold backup root is missing")
    if Path(manifest_root_raw).resolve() != backup_root:
        raise CapacitySafetyError("cold backup root differs from expected path")
    if payload.get("source_redis_run_id") != expected_source_run_id:
        raise CapacitySafetyError("cold backup source Redis run_id mismatch")
    if payload.get("source_run_id") != expected_source_run_id:
        raise CapacitySafetyError("cold backup source run_id mismatch")
    if payload.get("source_volume_name") != expected_source_volume:
        raise CapacitySafetyError("cold backup source volume mismatch")
    if payload.get("source_container_id") != expected_source_container_id:
        raise CapacitySafetyError("cold backup source container identity mismatch")

    rdb_filename = payload.get("source_rdb_filename")
    if not isinstance(rdb_filename, str) or not rdb_filename:
        raise CapacitySafetyError("cold backup RDB filename is missing")
    if Path(rdb_filename).name != rdb_filename:
        raise CapacitySafetyError("cold backup RDB filename is unsafe")
    save_policy = payload.get("source_save_policy")
    if not isinstance(save_policy, str) or not save_policy.strip():
        raise CapacitySafetyError("cold backup source save policy is missing")
    aof_enabled = payload.get("source_aof_enabled")
    if aof_enabled not in {0, 1}:
        raise CapacitySafetyError("cold backup AOF mode is invalid")
    appendonly = payload.get("source_appendonly")
    expected_appendonly = "no"
    if aof_enabled == 1:
        expected_appendonly = "yes"
    if appendonly != expected_appendonly:
        raise CapacitySafetyError("cold backup AOF config and runtime state differ")

    files_raw = payload.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise CapacitySafetyError("cold backup file inventory is empty")
    file_inventory: dict[str, dict[str, object]] = {}
    for entry in files_raw:
        if not isinstance(entry, dict):
            raise CapacitySafetyError("cold backup file inventory is invalid")
        relative_raw = entry.get("path")
        digest = entry.get("sha256")
        size_bytes = entry.get("size_bytes")
        if not isinstance(relative_raw, str):
            raise CapacitySafetyError("cold backup file path is invalid")
        if relative_raw in file_inventory:
            raise CapacitySafetyError("cold backup file inventory has duplicates")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise CapacitySafetyError("cold backup file hash is invalid")
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise CapacitySafetyError("cold backup file size is invalid")
        path = _resolve_backup_path(backup_root, relative_raw)
        if not path.is_file() or path.is_symlink():
            raise CapacitySafetyError("cold backup inventory path is not a regular file")
        if path.stat().st_size != size_bytes:
            raise CapacitySafetyError("cold backup file size mismatch")
        if _sha256_file(path) != digest:
            raise CapacitySafetyError("cold backup file hash mismatch")
        file_inventory[relative_raw] = entry

    data_root = backup_root / "data"
    if not data_root.is_dir():
        raise CapacitySafetyError("cold backup data directory is missing")
    actual_files = {
        str(path.relative_to(backup_root))
        for path in data_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_files != set(file_inventory):
        raise CapacitySafetyError("cold backup inventory does not match copied data")

    artifacts_raw = payload.get("artifacts")
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise CapacitySafetyError("cold backup artifact inventory is empty")
    artifact_paths: set[str] = set()
    artifact_kinds: set[str] = set()
    evidence_root = manifest_path.parent.resolve()
    for artifact in artifacts_raw:
        if not isinstance(artifact, dict):
            raise CapacitySafetyError("cold backup artifact entry is invalid")
        kind = artifact.get("kind")
        relative_raw = artifact.get("path")
        digest = artifact.get("sha256")
        validator = artifact.get("validator")
        validator_output_raw = artifact.get("validator_output_path")
        validator_output_sha256 = artifact.get("validator_output_sha256")
        if kind not in {"rdb", "aof", "aof-manifest"}:
            raise CapacitySafetyError("cold backup artifact kind is invalid")
        if not isinstance(relative_raw, str):
            raise CapacitySafetyError("cold backup artifact path is invalid")
        file_entry = file_inventory.get(relative_raw)
        if file_entry is None or file_entry.get("sha256") != digest:
            raise CapacitySafetyError("cold backup artifact is absent from file inventory")
        if artifact.get("validation_passed") is not True:
            raise CapacitySafetyError("cold backup artifact validation did not pass")
        expected_validator = {
            "rdb": "redis-check-rdb",
            "aof": "redis-check-aof",
            "aof-manifest": "redis-aof-manifest-parser",
        }[kind]
        if validator != expected_validator:
            raise CapacitySafetyError("cold backup artifact validator mismatch")
        if not isinstance(validator_output_raw, str):
            raise CapacitySafetyError("cold backup validator output path is invalid")
        if (
            not isinstance(validator_output_sha256, str)
            or _SHA256_RE.fullmatch(validator_output_sha256) is None
        ):
            raise CapacitySafetyError("cold backup validator output hash is invalid")
        validator_output = _resolve_backup_path(
            evidence_root,
            validator_output_raw,
        )
        if not validator_output.is_file() or validator_output.is_symlink():
            raise CapacitySafetyError("cold backup validator output is missing")
        if _sha256_file(validator_output) != validator_output_sha256:
            raise CapacitySafetyError("cold backup validator output hash mismatch")
        path = _resolve_backup_path(backup_root, relative_raw)
        if kind == "rdb":
            _verify_rdb_header(path)
        elif kind == "aof":
            _verify_aof_header(path)
        else:
            _verify_aof_manifest(path)
        artifact_paths.add(relative_raw)
        artifact_kinds.add(kind)

    expected_rdb_path = f"data/{rdb_filename}"
    if expected_rdb_path not in artifact_paths or "rdb" not in artifact_kinds:
        raise CapacitySafetyError("cold backup lacks the configured RDB artifact")
    if aof_enabled == 1 and "aof" not in artifact_kinds:
        raise CapacitySafetyError("cold backup lacks enabled AOF artifacts")
    return payload


def _resolve_backup_path(backup_root: Path, relative_raw: str) -> Path:
    relative = Path(relative_raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise CapacitySafetyError("cold backup file path escapes backup root")
    path = (backup_root / relative).resolve()
    if backup_root != path and backup_root not in path.parents:
        raise CapacitySafetyError("cold backup file path escapes backup root")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CapacitySafetyError("cold backup file cannot be hashed") from exc
    return digest.hexdigest()


def _verify_rdb_header(path: Path) -> None:
    try:
        header = path.read_bytes()[:9]
    except OSError as exc:
        raise CapacitySafetyError("cold backup RDB cannot be read") from exc
    if _RDB_HEADER_RE.fullmatch(header) is None:
        raise CapacitySafetyError("cold backup RDB header is invalid")


def _verify_aof_header(path: Path) -> None:
    try:
        header = path.read_bytes()[:9]
    except OSError as exc:
        raise CapacitySafetyError("cold backup AOF cannot be read") from exc
    if header.startswith((b"REDIS", b"*")):
        return
    raise CapacitySafetyError("cold backup AOF header is invalid")


def _verify_aof_manifest(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CapacitySafetyError("cold backup AOF manifest cannot be read") from exc
    entries = [line.strip() for line in lines if line.strip()]
    if not entries:
        raise CapacitySafetyError("cold backup AOF manifest is empty")
    for entry in entries:
        if _AOF_MANIFEST_LINE_RE.fullmatch(entry) is None:
            raise CapacitySafetyError("cold backup AOF manifest is invalid")


def _add_budget_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--redis-cgroup-limit")
    parser.add_argument(
        "--probe-local-cgroup",
        action="store_true",
        help="Read the finite memory limit from the local process cgroup.",
    )
    parser.add_argument("--host-total-memory")
    parser.add_argument("--host-available-memory")
    parser.add_argument(
        "--probe-host-memory",
        action="store_true",
        help="Read MemTotal and MemAvailable from /proc/meminfo.",
    )
    parser.add_argument("--other-services-reserve", required=True)
    parser.add_argument(
        "--system-reserve",
        default=str(DEFAULT_SYSTEM_RESERVE_BYTES),
    )
    parser.add_argument(
        "--container-headroom-percent",
        type=int,
        default=DEFAULT_CONTAINER_HEADROOM_PERCENT,
    )


def _resolve_budget_arguments(args) -> dict[str, int]:
    if args.redis_cgroup_limit:
        if args.probe_local_cgroup:
            raise CapacitySafetyError(
                "choose either --redis-cgroup-limit or --probe-local-cgroup"
            )
        redis_cgroup_limit = parse_memory_size(args.redis_cgroup_limit)
    elif args.probe_local_cgroup:
        redis_cgroup_limit = read_cgroup_memory_limit()
    else:
        raise CapacitySafetyError(
            "Redis cgroup budget missing; pass --redis-cgroup-limit "
            "or --probe-local-cgroup"
        )

    explicit_host_budget = bool(
        args.host_total_memory or args.host_available_memory
    )
    if explicit_host_budget and args.probe_host_memory:
        raise CapacitySafetyError(
            "choose explicit host memory values or --probe-host-memory"
        )
    if explicit_host_budget:
        if not args.host_total_memory or not args.host_available_memory:
            raise CapacitySafetyError(
                "host total and available memory must be provided together"
            )
        host_total_memory = parse_memory_size(args.host_total_memory)
        host_available_memory = parse_memory_size(args.host_available_memory)
    elif args.probe_host_memory:
        host_total_memory, host_available_memory = read_host_memory()
    else:
        raise CapacitySafetyError(
            "host memory budget missing; pass total and available memory "
            "or --probe-host-memory"
        )

    return {
        "redis_cgroup_limit_bytes": redis_cgroup_limit,
        "host_total_memory_bytes": host_total_memory,
        "host_available_memory_bytes": host_available_memory,
        "other_services_reserve_bytes": parse_memory_size(
            args.other_services_reserve
        ),
        "system_reserve_bytes": parse_memory_size(args.system_reserve),
        "container_headroom_percent": args.container_headroom_percent,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and validate persistent Redis maxmemory governance.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate")
    generate.add_argument("--maxmemory", required=True)
    generate.add_argument("--current-used-memory", required=True)
    generate.add_argument("--current-dataset-size", required=True)
    generate.add_argument(
        "--headroom-percent",
        type=int,
        default=DEFAULT_HEADROOM_PERCENT,
    )
    generate.add_argument(
        "--format",
        choices=("json", "docker-args", "redis-conf"),
        default="json",
    )
    _add_budget_arguments(generate)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL"),
        help="Redis URL; defaults to REDIS_URL and is never included in output.",
    )
    inspect.add_argument("--expected-maxmemory", required=True)
    inspect.add_argument(
        "--headroom-percent",
        type=int,
        default=DEFAULT_HEADROOM_PERCENT,
    )
    _add_budget_arguments(inspect)

    verify_backup = subparsers.add_parser("verify-backup")
    verify_backup.add_argument("--manifest", required=True)
    verify_backup.add_argument("--backup-root", required=True)
    verify_backup.add_argument("--source-run-id", required=True)
    verify_backup.add_argument("--source-volume", required=True)
    verify_backup.add_argument("--source-container-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-backup":
            payload = verify_cold_backup_manifest(
                Path(args.manifest),
                expected_backup_root=Path(args.backup_root),
                expected_source_run_id=args.source_run_id,
                expected_source_volume=args.source_volume,
                expected_source_container_id=args.source_container_id,
            )
            print(json.dumps(payload, sort_keys=True))
            return 0

        budget = _resolve_budget_arguments(args)
        if args.command == "generate":
            plan = build_capacity_plan(
                maxmemory_bytes=parse_memory_size(args.maxmemory),
                used_memory_bytes=parse_memory_size(args.current_used_memory),
                dataset_bytes=parse_memory_size(args.current_dataset_size),
                headroom_percent=args.headroom_percent,
                **budget,
            )
            if args.format == "docker-args":
                print(json.dumps(render_docker_args(plan)))
                return 0
            if args.format == "redis-conf":
                print(render_redis_config(plan), end="")
                return 0
            print(json.dumps(_plan_payload(plan), sort_keys=True))
            return 0

        if not args.redis_url:
            print("redis URL missing; set REDIS_URL or pass --redis-url", file=sys.stderr)
            return 2
        try:
            import redis
        except ImportError:
            print("redis-py is required", file=sys.stderr)
            return 2
        client = redis.Redis.from_url(args.redis_url, decode_responses=True)
        plan = inspect_running_redis(
            client,
            expected_maxmemory_bytes=parse_memory_size(args.expected_maxmemory),
            headroom_percent=args.headroom_percent,
            **budget,
        )
        print(json.dumps(_plan_payload(plan), sort_keys=True))
        return 0
    except (CapacitySafetyError, ValueError) as exc:
        print(f"capacity gate: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - CLI inspection must fail closed.
        print("Redis capacity inspection failed", file=sys.stderr)
        return 1


def _require_positive(label: str, value: int) -> None:
    if value <= 0:
        raise CapacitySafetyError(f"{label} must be positive")


def _require_nonnegative(label: str, value: int) -> None:
    if value < 0:
        raise CapacitySafetyError(f"{label} must be nonnegative")


if __name__ == "__main__":
    raise SystemExit(main())
