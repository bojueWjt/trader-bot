#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


REGISTRY_KEY = "trader-bot:redis-namespaces:active"
LEASES_KEY = f"{REGISTRY_KEY}:leases"
DEFAULT_STATE_FILE = Path(
    "/var/lib/trader-v3/redis-dead-instance-janitor.json"
)
DEFAULT_LOCK_FILE = Path(
    "/var/lock/trader-v3-redis-dead-instance-janitor.lock"
)
DEFAULT_DAILY_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_LEASE_MAX_AGE_SECONDS = 120
DEFAULT_MEMORY_THRESHOLD_RATIO = 0.70
DEFAULT_SCAN_COUNT = 1000
DEFAULT_BGSAVE_TIMEOUT_SECONDS = 300.0
_BGSAVE_IMMEDIATE_CONFIRMATION_SECONDS = 1.0
_UUID4_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_UUID4_RE = re.compile(rf"^{_UUID4_PATTERN}$")
_DEAD_INSTANCE_KEY_RE = re.compile(
    rf"^(?P<namespace>trader-TRADER-ACCOUNT-[A-Z0-9-]+):"
    rf"(?P<instance_id>{_UUID4_PATTERN}):.+$"
)

_ATOMIC_DELETE_LUA = r"""
-- jp24-redis-dead-instance-janitor:delete-v1
local registry_key = KEYS[1]
local leases_key = KEYS[2]
local candidate_key = KEYS[3]
local instance_id = ARGV[1]
local fresh_after_epoch = tonumber(ARGV[2])
local lease_rows = redis.call("HGETALL", leases_key)

for index = 1, #lease_rows, 2 do
    local lease_namespace = lease_rows[index]
    local lease_score = redis.call(
        "ZSCORE",
        registry_key,
        lease_namespace
    )
    if lease_score and tonumber(lease_score) >= fresh_after_epoch then
        local decoded, lease = pcall(cjson.decode, lease_rows[index + 1])
        if not decoded or type(lease) ~= "table" then
            return {0, "LEASE_METADATA_INVALID"}
        end
        if lease["persistence_instance_id"] == instance_id then
            return {0, "PROTECTED"}
        end
    end
end

local deleted = redis.call("UNLINK", candidate_key)
return {deleted, "DELETED"}
"""


class JanitorError(RuntimeError):
    pass


class RedisJanitorClient(Protocol):
    def info(self, section: str) -> Mapping[str, object]:
        ...

    def bgsave(self) -> object:
        ...

    def hgetall(
        self,
        key: str,
    ) -> Mapping[bytes | str, bytes | str]:
        ...

    def zscore(self, key: str, member: str) -> float | None:
        ...

    def time(self) -> Sequence[int]:
        ...

    def scan_iter(
        self,
        match: str,
        count: int,
    ) -> Iterable[bytes | str]:
        ...

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        ...


@dataclass(frozen=True)
class MemorySnapshot:
    used_memory: int
    maxmemory: int
    used_ratio: float


@dataclass(frozen=True)
class BackupReceipt:
    started_at_epoch: int
    completed_at_epoch: int
    status: str


@dataclass(frozen=True)
class JanitorReport:
    dry_run: bool
    trigger_reasons: tuple[str, ...]
    backup: BackupReceipt
    whitelist_instance_ids: tuple[str, ...]
    delete_keys: tuple[str, ...]
    skipped_live_keys: tuple[str, ...]
    deleted_count: int
    memory_before: MemorySnapshot
    memory_after: MemorySnapshot


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | bool = False

    def __enter__(self) -> "ProcessLock":
        if not self._path.is_absolute():
            raise JanitorError("lock file path must be absolute")
        if self._path.is_symlink():
            raise JanitorError("lock file cannot be a symlink")
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise JanitorError(
                f"cannot open janitor lock file: {self._path}"
            ) from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise JanitorError("janitor lock file is not regular")
            if metadata.st_nlink != 1:
                raise JanitorError("janitor lock file has unsafe links")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(fd, 0o600)
        except BlockingIOError as exc:
            os.close(fd)
            raise JanitorError("another Redis janitor run is active") from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *_args: object) -> None:
        fd = self._fd
        self._fd = False
        if fd is False:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def memory_snapshot(redis_client: RedisJanitorClient) -> MemorySnapshot:
    raw_info = redis_client.info("memory")
    if not isinstance(raw_info, Mapping):
        raise JanitorError("Redis INFO memory returned invalid data")
    used_memory = _non_negative_int(
        raw_info.get("used_memory"),
        "Redis used_memory",
    )
    maxmemory = _positive_int(
        raw_info.get("maxmemory"),
        "Redis maxmemory",
    )
    used_ratio = used_memory / maxmemory
    if not math.isfinite(used_ratio):
        raise JanitorError("Redis memory ratio is invalid")
    return MemorySnapshot(
        used_memory=used_memory,
        maxmemory=maxmemory,
        used_ratio=used_ratio,
    )


def trigger_reasons(
    *,
    now_epoch: int,
    last_success_epoch: int | bool,
    memory: MemorySnapshot,
    daily_interval_seconds: int,
    memory_threshold_ratio: float,
    force: bool,
) -> tuple[str, ...]:
    _require_positive(
        "daily_interval_seconds",
        daily_interval_seconds,
    )
    if (
        not math.isfinite(memory_threshold_ratio)
        or memory_threshold_ratio <= 0
        or memory_threshold_ratio >= 1
    ):
        raise JanitorError("memory threshold ratio must be between 0 and 1")
    reasons = []
    if force:
        reasons.append("forced")
    daily_due = last_success_epoch is False
    if last_success_epoch is not False:
        daily_due = (
            now_epoch - int(last_success_epoch)
            >= daily_interval_seconds
        )
    if daily_due:
        reasons.append("daily")
    if memory.used_ratio > memory_threshold_ratio:
        reasons.append("memory")
    return tuple(reasons)


def run_janitor(
    redis_client: RedisJanitorClient,
    *,
    dry_run: bool,
    reasons: Sequence[str],
    lease_max_age_seconds: int = DEFAULT_LEASE_MAX_AGE_SECONDS,
    scan_count: int = DEFAULT_SCAN_COUNT,
    bgsave_timeout_seconds: float = DEFAULT_BGSAVE_TIMEOUT_SECONDS,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> JanitorReport:
    _require_positive("lease_max_age_seconds", lease_max_age_seconds)
    _require_positive("scan_count", scan_count)
    if (
        not math.isfinite(bgsave_timeout_seconds)
        or bgsave_timeout_seconds <= 0
    ):
        raise JanitorError("BGSAVE timeout must be positive")
    normalized_reasons = tuple(
        sorted({str(reason).strip() for reason in reasons if str(reason).strip()})
    )
    if not normalized_reasons:
        raise JanitorError("janitor run requires a trigger reason")

    before = memory_snapshot(redis_client)
    backup = _complete_bgsave(
        redis_client,
        timeout_seconds=bgsave_timeout_seconds,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )
    server_epoch = _redis_server_epoch(redis_client)
    fresh_after_epoch = server_epoch - lease_max_age_seconds
    whitelist = _load_live_instance_whitelist(
        redis_client,
        server_epoch=server_epoch,
        fresh_after_epoch=fresh_after_epoch,
    )
    candidates = _scan_dead_instance_keys(
        redis_client,
        whitelist=whitelist,
        scan_count=scan_count,
    )

    deleted_count = 0
    skipped_live_keys = []
    if not dry_run:
        for key in candidates:
            instance_id = _candidate_instance_id(key)
            result = redis_client.eval(
                _ATOMIC_DELETE_LUA,
                3,
                REGISTRY_KEY,
                LEASES_KEY,
                key,
                instance_id,
                fresh_after_epoch,
            )
            deleted, status = _atomic_delete_result(result)
            if status == "PROTECTED":
                skipped_live_keys.append(key)
                continue
            if status != "DELETED":
                raise JanitorError(
                    f"atomic Redis delete rejected {key}: {status}"
                )
            deleted_count += deleted

    after = memory_snapshot(redis_client)
    return JanitorReport(
        dry_run=dry_run,
        trigger_reasons=normalized_reasons,
        backup=backup,
        whitelist_instance_ids=tuple(sorted(whitelist)),
        delete_keys=tuple(candidates),
        skipped_live_keys=tuple(skipped_live_keys),
        deleted_count=deleted_count,
        memory_before=before,
        memory_after=after,
    )


def _complete_bgsave(
    redis_client: RedisJanitorClient,
    *,
    timeout_seconds: float,
    sleep_fn,
    monotonic_fn,
) -> BackupReceipt:
    _wait_for_bgsave_idle(
        redis_client,
        timeout_seconds=timeout_seconds,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )
    before = _persistence_info(redis_client)
    started_at_epoch = _redis_server_epoch(redis_client)
    baseline_save_epoch = _non_negative_int(
        before.get("rdb_last_save_time"),
        "Redis rdb_last_save_time",
    )
    result = redis_client.bgsave()
    if result is False:
        raise JanitorError("Redis BGSAVE was rejected")

    requested_at_monotonic = monotonic_fn()
    deadline = requested_at_monotonic + timeout_seconds
    observed_running = False
    while True:
        current = _persistence_info(redis_client)
        in_progress = _redis_bool(
            current.get("rdb_bgsave_in_progress"),
            "Redis rdb_bgsave_in_progress",
        )
        if in_progress:
            observed_running = True
        if not in_progress:
            status = str(
                current.get("rdb_last_bgsave_status") or ""
            ).strip().lower()
            completed_at_epoch = _non_negative_int(
                current.get("rdb_last_save_time"),
                "Redis rdb_last_save_time",
            )
            if status != "ok":
                raise JanitorError("Redis BGSAVE did not complete successfully")
            if completed_at_epoch < baseline_save_epoch:
                raise JanitorError("Redis BGSAVE timestamp regressed")
            confirmation_elapsed = (
                monotonic_fn() - requested_at_monotonic
                >= _BGSAVE_IMMEDIATE_CONFIRMATION_SECONDS
            )
            if (
                observed_running
                or completed_at_epoch > baseline_save_epoch
                or confirmation_elapsed
            ):
                return BackupReceipt(
                    started_at_epoch=started_at_epoch,
                    completed_at_epoch=completed_at_epoch,
                    status=status,
                )
        if monotonic_fn() >= deadline:
            raise JanitorError("Redis BGSAVE timed out")
        sleep_fn(0.25)


def _wait_for_bgsave_idle(
    redis_client: RedisJanitorClient,
    *,
    timeout_seconds: float,
    sleep_fn,
    monotonic_fn,
) -> None:
    deadline = monotonic_fn() + timeout_seconds
    while True:
        info = _persistence_info(redis_client)
        in_progress = _redis_bool(
            info.get("rdb_bgsave_in_progress"),
            "Redis rdb_bgsave_in_progress",
        )
        if not in_progress:
            return
        if monotonic_fn() >= deadline:
            raise JanitorError("existing Redis BGSAVE did not finish")
        sleep_fn(0.25)


def _persistence_info(
    redis_client: RedisJanitorClient,
) -> Mapping[str, object]:
    info = redis_client.info("persistence")
    if not isinstance(info, Mapping):
        raise JanitorError("Redis INFO persistence returned invalid data")
    return info


def _load_live_instance_whitelist(
    redis_client: RedisJanitorClient,
    *,
    server_epoch: int,
    fresh_after_epoch: int,
) -> set[str]:
    raw_records = redis_client.hgetall(LEASES_KEY)
    if not isinstance(raw_records, Mapping):
        raise JanitorError("Redis lease registry returned invalid data")
    whitelist: set[str] = set()
    for raw_namespace, raw_record in raw_records.items():
        lease_namespace = _decode(raw_namespace).strip()
        if not lease_namespace:
            raise JanitorError("Redis lease namespace is empty")
        score = redis_client.zscore(REGISTRY_KEY, lease_namespace)
        if score is None or float(score) < fresh_after_epoch:
            continue
        if float(score) > server_epoch:
            raise JanitorError(
                f"{lease_namespace} lease score is from the future"
            )
        record = _decode_lease_record(raw_record, lease_namespace)
        record_namespace = str(
            record.get("lease_namespace")
            or record.get("namespace")
            or ""
        ).strip()
        if record_namespace != lease_namespace:
            raise JanitorError(
                f"{lease_namespace} lease namespace mismatch"
            )
        refreshed_at_epoch = _positive_int(
            record.get("refreshed_at_epoch"),
            f"{lease_namespace} refreshed_at_epoch",
        )
        if float(refreshed_at_epoch) != float(score):
            raise JanitorError(
                f"{lease_namespace} lease freshness mismatch"
            )
        instance_id = str(
            record.get("persistence_instance_id") or ""
        ).strip()
        if _UUID4_RE.fullmatch(instance_id) is None:
            raise JanitorError(
                f"{lease_namespace} persistence_instance_id is invalid"
            )
        persistence_namespace = str(
            record.get("persistence_namespace") or ""
        ).strip()
        expected_namespace = f"{lease_namespace}:{instance_id}"
        if persistence_namespace != expected_namespace:
            raise JanitorError(
                f"{lease_namespace} persistence namespace mismatch"
            )
        whitelist.add(instance_id)
    return whitelist


def _decode_lease_record(
    raw_record: bytes | str,
    lease_namespace: str,
) -> Mapping[str, object]:
    try:
        decoded = json.loads(_decode(raw_record))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JanitorError(
            f"{lease_namespace} lease metadata is invalid"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise JanitorError(
            f"{lease_namespace} lease metadata is invalid"
        )
    return decoded


def _scan_dead_instance_keys(
    redis_client: RedisJanitorClient,
    *,
    whitelist: set[str],
    scan_count: int,
) -> list[str]:
    candidates = set()
    for raw_key in redis_client.scan_iter(
        match="trader-TRADER-ACCOUNT-*:*:*",
        count=scan_count,
    ):
        key = _decode(raw_key)
        if key in {REGISTRY_KEY, LEASES_KEY}:
            continue
        match = _DEAD_INSTANCE_KEY_RE.fullmatch(key)
        if match is None:
            continue
        instance_id = match.group("instance_id")
        if instance_id in whitelist:
            continue
        candidates.add(key)
    return sorted(candidates)


def _candidate_instance_id(key: str) -> str:
    match = _DEAD_INSTANCE_KEY_RE.fullmatch(key)
    if match is None:
        raise JanitorError(f"unsafe Redis delete candidate: {key}")
    return match.group("instance_id")


def _atomic_delete_result(result: object) -> tuple[int, str]:
    if (
        not isinstance(result, Sequence)
        or isinstance(result, (str, bytes))
        or len(result) != 2
    ):
        raise JanitorError("atomic Redis delete returned invalid data")
    deleted = _non_negative_int(result[0], "Redis deleted count")
    status = _decode(result[1]).strip().upper()
    if not status:
        raise JanitorError("atomic Redis delete returned empty status")
    return deleted, status


def _redis_server_epoch(redis_client: RedisJanitorClient) -> int:
    raw_time = redis_client.time()
    if (
        not isinstance(raw_time, Sequence)
        or isinstance(raw_time, (str, bytes))
        or len(raw_time) < 1
    ):
        raise JanitorError("Redis TIME returned invalid data")
    return _positive_int(raw_time[0], "Redis server time")


def _redis_bool(value: object, label: str) -> bool:
    if value in (0, False, "0"):
        return False
    if value in (1, True, "1"):
        return True
    raise JanitorError(f"{label} is invalid")


def _decode(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise JanitorError(f"{label} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise JanitorError(
            f"{label} must be a positive integer"
        ) from exc
    if normalized <= 0:
        raise JanitorError(f"{label} must be a positive integer")
    return normalized


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise JanitorError(f"{label} must be a non-negative integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise JanitorError(
            f"{label} must be a non-negative integer"
        ) from exc
    if normalized < 0:
        raise JanitorError(f"{label} must be a non-negative integer")
    return normalized


def _require_positive(label: str, value: int) -> None:
    if value <= 0:
        raise JanitorError(f"{label} must be positive")


def _load_last_success_epoch(path: Path) -> int | bool:
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise JanitorError("janitor state file is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JanitorError("janitor state file is unreadable") from exc
    if not isinstance(payload, Mapping):
        raise JanitorError("janitor state file is invalid")
    return _positive_int(
        payload.get("last_success_epoch"),
        "janitor last_success_epoch",
    )


def _write_state(path: Path, *, completed_at_epoch: int) -> None:
    if not path.is_absolute():
        raise JanitorError("janitor state file path must be absolute")
    if path.is_symlink():
        raise JanitorError("janitor state file cannot be a symlink")
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "schema_version": "jp24-redis-dead-instance-janitor/v1",
        "last_success_epoch": completed_at_epoch,
    }
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def report_payload(report: JanitorReport) -> dict[str, object]:
    return asdict(report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Back up Redis and remove dead trader persistence instances."
        )
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", ""),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=DEFAULT_LOCK_FILE,
    )
    parser.add_argument(
        "--daily-interval-seconds",
        type=int,
        default=DEFAULT_DAILY_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--lease-max-age-seconds",
        type=int,
        default=DEFAULT_LEASE_MAX_AGE_SECONDS,
    )
    parser.add_argument(
        "--memory-threshold-ratio",
        type=float,
        default=DEFAULT_MEMORY_THRESHOLD_RATIO,
    )
    parser.add_argument(
        "--scan-count",
        type=int,
        default=DEFAULT_SCAN_COUNT,
    )
    parser.add_argument(
        "--bgsave-timeout-seconds",
        type=float,
        default=DEFAULT_BGSAVE_TIMEOUT_SECONDS,
    )
    return parser


def _execute(args: argparse.Namespace) -> int:
    try:
        import redis
    except ImportError:
        print("redis-py is required", file=sys.stderr)
        return 2

    try:
        client = redis.Redis.from_url(
            args.redis_url,
            decode_responses=False,
        )
        now_epoch = _redis_server_epoch(client)
        before = memory_snapshot(client)
        last_success_epoch = _load_last_success_epoch(args.state_file)
        force = bool(args.force or args.dry_run)
        reasons = trigger_reasons(
            now_epoch=now_epoch,
            last_success_epoch=last_success_epoch,
            memory=before,
            daily_interval_seconds=args.daily_interval_seconds,
            memory_threshold_ratio=args.memory_threshold_ratio,
            force=force,
        )
        if not reasons:
            print(
                json.dumps(
                    {
                        "skipped": True,
                        "memory": asdict(before),
                        "last_success_epoch": last_success_epoch,
                    },
                    sort_keys=True,
                )
            )
            return 0
        report = run_janitor(
            client,
            dry_run=not args.apply,
            reasons=reasons,
            lease_max_age_seconds=args.lease_max_age_seconds,
            scan_count=args.scan_count,
            bgsave_timeout_seconds=args.bgsave_timeout_seconds,
        )
        if args.apply:
            completed_at_epoch = _redis_server_epoch(client)
            _write_state(
                args.state_file,
                completed_at_epoch=completed_at_epoch,
            )
    except JanitorError as exc:
        print(f"safety gate: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("Redis dead-instance janitor failed", file=sys.stderr)
        return 1

    print(json.dumps(report_payload(report), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.redis_url:
        print(
            "redis URL missing; set REDIS_URL or pass --redis-url",
            file=sys.stderr,
        )
        return 2
    try:
        with ProcessLock(args.lock_file):
            return _execute(args)
    except JanitorError as exc:
        print(f"safety gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
