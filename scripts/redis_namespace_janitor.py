#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, Self

DEFAULT_REGISTRY_KEY = "trader-bot:redis-namespaces:active"
DEFAULT_REGISTRY_MAX_AGE_SECONDS = 300
DEFAULT_BACKUP_MAX_AGE_SECONDS = 24 * 60 * 60
DEFAULT_RECONCILIATION_MAX_AGE_SECONDS = 300
DEFAULT_MIN_IDLE_SECONDS = 24 * 60 * 60
DEFAULT_MAX_NAMESPACES = 10
DEFAULT_MAX_KEYS = 1000
DEFAULT_SCAN_COUNT = 500
DEFAULT_UNLINK_BATCH_SIZE = 100
DEFAULT_MAX_UNLINK_KEYS_PER_SECOND = 200
DEFAULT_MAX_ATOMIC_UNLINK_BATCH_SIZE = 1000
DEFAULT_POST_BATCH_MAXMEMORY_RATIO = 0.85
DEFAULT_OPERATION_LOCK_PATH = Path(
    "/var/lock/trader-v3-account-stall-operation.lock"
)
OPERATION_LOCK_OWNERSHIP_ENV = (
    "ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP"
)
OPERATION_LOCK_FD_ENV = "ACCOUNT_STALL_OPERATION_LOCK_FD"
OPERATION_LOCK_TOKEN_ENV = "ACCOUNT_STALL_OPERATION_LOCK_TOKEN"
SAFETY_MANIFEST_SCHEMA_VERSION = "trader-redis-janitor-safety/v2"
LEGACY_SAFETY_MANIFEST_SCHEMA_VERSION = "trader-redis-janitor-safety/v1"
EXPECTED_STABLE_NAMESPACES = {
    "account-a": "trader-TRADER-ACCOUNT-A",
    "account-b": "trader-TRADER-ACCOUNT-B",
    "account-c": "trader-TRADER-ACCOUNT-C",
    "account-d": "trader-TRADER-ACCOUNT-D",
}
_LOWERCASE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_CANONICAL_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class JanitorSafetyError(RuntimeError):
    pass


class AccountStallOperationLock:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._fd: int | None = None
        self._inherited = False

    def __enter__(self) -> Self:
        if not self._enabled:
            return self
        path = _operation_lock_path()
        ownership = os.environ.get(
            OPERATION_LOCK_OWNERSHIP_ENV,
            "standalone",
        ).strip()
        if ownership == "inherited":
            self._acquire_inherited(path)
            return self
        if ownership != "standalone":
            raise JanitorSafetyError(
                "invalid account-stall operation lock ownership"
            )
        self._acquire_standalone(path)
        return self

    def __exit__(self, *_args: object) -> None:
        fd = self._fd
        self._fd = None
        if fd is None or self._inherited:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _acquire_inherited(self, path: Path) -> None:
        fd_raw = os.environ.get(OPERATION_LOCK_FD_ENV, "").strip()
        token = os.environ.get(OPERATION_LOCK_TOKEN_ENV, "").strip()
        if fd_raw != "9":
            raise JanitorSafetyError(
                "account-stall inherited lock fd must be 9"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise JanitorSafetyError(
                "account-stall inherited lock token is invalid"
            )
        fd = int(fd_raw)
        try:
            fd_metadata = os.fstat(fd)
            path_metadata = path.stat()
        except OSError as exc:
            raise JanitorSafetyError(
                "account-stall inherited lock is unavailable"
            ) from exc
        if not stat.S_ISREG(fd_metadata.st_mode):
            raise JanitorSafetyError(
                "account-stall inherited lock fd is not a regular file"
            )
        if (
            fd_metadata.st_dev != path_metadata.st_dev
            or fd_metadata.st_ino != path_metadata.st_ino
        ):
            raise JanitorSafetyError(
                "account-stall inherited lock fd does not match lock path"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JanitorSafetyError(
                f"another account-stall operation holds {path}"
            ) from exc
        try:
            recorded_token = os.pread(fd, 4096, 0).decode(
                "ascii"
            ).strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise JanitorSafetyError(
                "account-stall inherited lock token is unreadable"
            ) from exc
        if recorded_token != token:
            raise JanitorSafetyError(
                "account-stall inherited lock token mismatch"
            )
        self._fd = fd
        self._inherited = True

    def _acquire_standalone(self, path: Path) -> None:
        flags = os.O_RDWR | os.O_CREAT
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags | no_follow, 0o600)
        except OSError as exc:
            raise JanitorSafetyError(
                f"cannot open account-stall operation lock: {path}"
            ) from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise JanitorSafetyError(
                    "account-stall operation lock is not a regular file"
                )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(fd, 0o600)
        except BlockingIOError as exc:
            os.close(fd)
            raise JanitorSafetyError(
                f"another account-stall operation holds {path}"
            ) from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd


def _operation_lock_path() -> Path:
    raw = os.environ.get(
        "ACCOUNT_STALL_OPERATION_LOCK",
        str(DEFAULT_OPERATION_LOCK_PATH),
    ).strip()
    path = Path(raw)
    if not path.is_absolute():
        raise JanitorSafetyError(
            "account-stall operation lock path must be absolute"
        )
    if path.is_symlink():
        raise JanitorSafetyError(
            "account-stall operation lock cannot be a symlink"
        )
    return path


class RedisJanitorClient(Protocol):
    def scan_iter(self, match: str, count: int) -> Iterable[bytes | str]:
        ...

    def object(self, subcommand: str, key: bytes | str) -> int | None:
        ...

    def zrangebyscore(
        self,
        key: str,
        minimum: int,
        maximum: str,
    ) -> Iterable[bytes | str]:
        ...

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        ...

    def time(self) -> Sequence[int]:
        ...

    def info(self, section: str) -> Mapping[str, object]:
        ...

    def hget(self, key: str, field: str) -> bytes | str | None:
        ...

    def zscore(self, key: str, member: str) -> float | None:
        ...

    def dbsize(self) -> int:
        ...


_ATOMIC_UNLINK_LUA = r"""
-- redis-namespace-janitor:atomic-unlink-v2
local registry_key = KEYS[1]
local metadata_key = KEYS[2]
local namespace = ARGV[1]
local fresh_after_epoch = tonumber(ARGV[2])
local min_idle_seconds = tonumber(ARGV[3])
local lease_namespace_count = tonumber(ARGV[4])
local namespace_prefix = namespace .. ":"

local function is_canonical_uuid4(value)
    if type(value) ~= "string" or string.len(value) ~= 36 then
        return false
    end
    if string.sub(value, 9, 9) ~= "-"
        or string.sub(value, 14, 14) ~= "-"
        or string.sub(value, 19, 19) ~= "-"
        or string.sub(value, 24, 24) ~= "-" then
        return false
    end
    if string.sub(value, 15, 15) ~= "4" then
        return false
    end
    local variant = string.sub(value, 20, 20)
    if variant ~= "8"
        and variant ~= "9"
        and variant ~= "a"
        and variant ~= "b" then
        return false
    end
    local compact = string.gsub(value, "-", "")
    return string.match(compact, "^[0-9a-f]+$") ~= nil
end

local function persistence_namespace_status()
    local score = redis.call("ZSCORE", registry_key, namespace)
    if score and tonumber(score) >= fresh_after_epoch then
        return "ACTIVE"
    end

    for index = 1, lease_namespace_count do
        local lease_namespace = ARGV[4 + index]
        local lease_score = redis.call(
            "ZSCORE",
            registry_key,
            lease_namespace
        )
        if lease_score and tonumber(lease_score) >= fresh_after_epoch then
            local current_json = redis.call(
                "HGET",
                metadata_key,
                lease_namespace
            )
            if not current_json then
                return "LEASE_METADATA_MISSING"
            end
            local decoded, current = pcall(cjson.decode, current_json)
            if not decoded then
                return "LEASE_METADATA_INVALID"
            end
            local record_lease_namespace = current["lease_namespace"]
            if not record_lease_namespace then
                record_lease_namespace = current["namespace"]
            end
            local fencing_token = tonumber(current["fencing_token"])
            local refreshed_at_epoch = tonumber(
                current["refreshed_at_epoch"]
            )
            local persistence_instance_id = current[
                "persistence_instance_id"
            ]
            local persistence_namespace = current[
                "persistence_namespace"
            ]
            if record_lease_namespace ~= lease_namespace
                or not fencing_token
                or fencing_token < 1
                or fencing_token ~= math.floor(fencing_token)
                or not refreshed_at_epoch
                or refreshed_at_epoch ~= tonumber(lease_score)
                or not is_canonical_uuid4(persistence_instance_id)
                or type(persistence_namespace) ~= "string"
                or persistence_namespace
                    ~= lease_namespace .. ":" .. persistence_instance_id then
                return "LEASE_METADATA_INVALID"
            end
            if persistence_namespace == namespace then
                return "ACTIVE"
            end
        end
    end
    return "INACTIVE"
end

local namespace_status = persistence_namespace_status()
if namespace_status ~= "INACTIVE" then
    return {0, namespace_status}
end

for index = 3, #KEYS do
    local key = KEYS[index]
    if string.sub(key, 1, string.len(namespace_prefix)) ~= namespace_prefix then
        return {0, "WRONG_NAMESPACE"}
    end
    local idle_seconds = redis.call("OBJECT", "IDLETIME", key)
    if not idle_seconds then
        return {0, "KEY_MISSING"}
    end
    if tonumber(idle_seconds) < min_idle_seconds then
        return {0, "KEY_ACTIVE"}
    end
end

namespace_status = persistence_namespace_status()
if namespace_status ~= "INACTIVE" then
    return {0, namespace_status}
end

local deleted = redis.call("UNLINK", unpack(KEYS, 3, #KEYS))
return {deleted, "DELETED"}
"""


@dataclass(frozen=True)
class JanitorReport:
    apply: bool
    safety_manifest_verified: bool
    scanned_keys: int
    candidate_namespaces: tuple[str, ...]
    protected_namespaces: tuple[str, ...]
    skipped_namespaces: tuple[str, ...]
    selected_namespaces: tuple[str, ...]
    partial_namespaces: tuple[str, ...]
    selected_keys: int
    deleted_keys: int
    post_batch_verifications: int


@dataclass(frozen=True)
class _SafetyEvidence:
    fresh_after_epoch: int
    lease_namespaces: tuple[str, ...]
    active_persistence_namespaces: tuple[str, ...]


def run_janitor(
    redis_client: RedisJanitorClient,
    *,
    legacy_prefixes: Iterable[str],
    active_namespaces: set[str],
    registry_key: str,
    registry_fresh_after_epoch: int,
    min_idle_seconds: int,
    max_namespaces: int,
    max_keys: int,
    apply: bool,
    safety_manifest: Mapping[str, object] | None = None,
    registry_max_age_seconds: int = DEFAULT_REGISTRY_MAX_AGE_SECONDS,
    backup_max_age_seconds: int = DEFAULT_BACKUP_MAX_AGE_SECONDS,
    reconciliation_max_age_seconds: int = (
        DEFAULT_RECONCILIATION_MAX_AGE_SECONDS
    ),
    scan_count: int = DEFAULT_SCAN_COUNT,
    unlink_batch_size: int = DEFAULT_UNLINK_BATCH_SIZE,
    max_unlink_keys_per_second: int = DEFAULT_MAX_UNLINK_KEYS_PER_SECOND,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> JanitorReport:
    prefixes = _validated_prefixes(legacy_prefixes)
    _require_positive("min_idle_seconds", min_idle_seconds)
    _require_positive("max_namespaces", max_namespaces)
    _require_positive("max_keys", max_keys)
    _require_positive("registry_max_age_seconds", registry_max_age_seconds)
    _require_positive("backup_max_age_seconds", backup_max_age_seconds)
    _require_positive(
        "reconciliation_max_age_seconds",
        reconciliation_max_age_seconds,
    )
    _require_positive("scan_count", scan_count)
    _require_positive("unlink_batch_size", unlink_batch_size)
    _require_positive(
        "max_unlink_keys_per_second",
        max_unlink_keys_per_second,
    )
    manifest_namespaces: set[str] = set()
    manifest_lease_namespaces: set[str] = set()
    manifest_nodes: object = ()
    safety_manifest_verified = False
    if apply:
        if safety_manifest is None:
            raise JanitorSafetyError(
                "apply requires a verified safety manifest"
            )
        safety_evidence = _validate_safety_manifest(
            redis_client,
            safety_manifest,
            registry_key=registry_key,
            registry_max_age_seconds=registry_max_age_seconds,
            backup_max_age_seconds=backup_max_age_seconds,
            reconciliation_max_age_seconds=reconciliation_max_age_seconds,
        )
        registry_fresh_after_epoch = safety_evidence.fresh_after_epoch
        manifest_namespaces.update(
            safety_evidence.active_persistence_namespaces
        )
        manifest_lease_namespaces.update(safety_evidence.lease_namespaces)
        manifest_nodes = safety_manifest.get("nodes")
        safety_manifest_verified = True

    registered = _load_active_registry(
        redis_client,
        registry_key=registry_key,
        fresh_after_epoch=registry_fresh_after_epoch,
        require_generation=apply,
    )
    allowlisted = {_normalize_namespace(item) for item in active_namespaces}
    allowlisted.update(manifest_namespaces)
    protected = set(allowlisted)
    protected.update(registered)

    scanned_keys = 0
    protected_seen: set[str] = set()
    namespace_keys: dict[str, list[bytes | str]] = {}
    for prefix in prefixes:
        for raw_key in redis_client.scan_iter(match=f"{prefix}*", count=scan_count):
            scanned_keys += 1
            key = _decode(raw_key)
            namespace = extract_legacy_uuid_namespace(key, prefix)
            if namespace is False:
                continue
            if namespace in protected:
                protected_seen.add(namespace)
                continue
            namespace_keys.setdefault(namespace, []).append(raw_key)

    eligible: dict[str, list[bytes | str]] = {}
    skipped: set[str] = set()
    for namespace, keys in sorted(namespace_keys.items()):
        if _namespace_is_idle(redis_client, keys, min_idle_seconds):
            eligible[namespace] = keys
            continue
        skipped.add(namespace)

    selected: dict[str, list[bytes | str]] = {}
    partial: set[str] = set()
    selected_key_count = 0
    for namespace, keys in eligible.items():
        if len(selected) >= max_namespaces:
            skipped.add(namespace)
            continue
        remaining_key_budget = max_keys - selected_key_count
        if remaining_key_budget <= 0:
            skipped.add(namespace)
            continue
        ordered_keys = sorted(keys, key=_decode)
        selected_keys = ordered_keys[:remaining_key_budget]
        selected[namespace] = selected_keys
        selected_key_count += len(selected_keys)
        if len(selected_keys) < len(ordered_keys):
            partial.add(namespace)

    deleted_keys = 0
    post_batch_verifications = 0
    if apply:
        for namespace, keys in selected.items():
            unlink_result = _unlink_rate_limited(
                redis_client,
                namespace,
                keys,
                batch_size=unlink_batch_size,
                max_keys_per_second=max_unlink_keys_per_second,
                allowlisted_namespaces=allowlisted,
                lease_namespaces=manifest_lease_namespaces,
                registry_key=registry_key,
                registry_fresh_after_epoch=registry_fresh_after_epoch,
                min_idle_seconds=min_idle_seconds,
                manifest_nodes=manifest_nodes,
                expected_lease_namespaces=tuple(
                    sorted(manifest_lease_namespaces)
                ),
                expected_persistence_namespaces=tuple(
                    sorted(manifest_namespaces)
                ),
                registry_max_age_seconds=registry_max_age_seconds,
                reconciliation_max_age_seconds=(
                    reconciliation_max_age_seconds
                ),
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            )
            deleted_keys += unlink_result.deleted_keys
            post_batch_verifications += (
                unlink_result.post_batch_verifications
            )

    return JanitorReport(
        apply=apply,
        safety_manifest_verified=safety_manifest_verified,
        scanned_keys=scanned_keys,
        candidate_namespaces=tuple(eligible),
        protected_namespaces=tuple(sorted(protected_seen)),
        skipped_namespaces=tuple(sorted(skipped)),
        selected_namespaces=tuple(selected),
        partial_namespaces=tuple(sorted(partial)),
        selected_keys=selected_key_count,
        deleted_keys=deleted_keys,
        post_batch_verifications=post_batch_verifications,
    )


def extract_legacy_uuid_namespace(key: str, legacy_prefix: str) -> str | bool:
    if not key.startswith(legacy_prefix):
        return False
    remainder = key[len(legacy_prefix) :]
    runtime_id, separator, _suffix = remainder.partition(":")
    if not separator:
        return False
    if not _CANONICAL_UUID_RE.fullmatch(runtime_id):
        return False
    return f"{legacy_prefix}{runtime_id.lower()}"


def _namespace_is_idle(
    redis_client: RedisJanitorClient,
    keys: Iterable[bytes | str],
    min_idle_seconds: int,
) -> bool:
    found = False
    for key in keys:
        found = True
        idle_seconds = redis_client.object("idletime", key)
        if idle_seconds is None:
            return False
        if int(idle_seconds) < min_idle_seconds:
            return False
    return found


def _load_active_registry(
    redis_client: RedisJanitorClient,
    *,
    registry_key: str,
    fresh_after_epoch: int,
    require_generation: bool,
) -> set[str]:
    if not registry_key:
        raise JanitorSafetyError("registry_key must be non-empty")
    members = redis_client.zrangebyscore(
        registry_key,
        fresh_after_epoch,
        "+inf",
    )
    active_namespaces: set[str] = set()
    metadata_key = f"{registry_key}:leases"
    for raw_member in members:
        lease_namespace = _normalize_namespace(_decode(raw_member))
        raw_record = redis_client.hget(metadata_key, lease_namespace)
        if raw_record is None:
            active_namespaces.add(lease_namespace)
            continue
        active_namespaces.add(
            _active_persistence_namespace_from_lease_record(
                redis_client,
                raw_record,
                registry_key=registry_key,
                lease_namespace=lease_namespace,
                require_generation=require_generation,
            )
        )
    return active_namespaces


def _active_persistence_namespace_from_lease_record(
    redis_client: RedisJanitorClient,
    raw_record: bytes | str,
    *,
    registry_key: str,
    lease_namespace: str,
    require_generation: bool,
) -> str:
    try:
        decoded = json.loads(_decode(raw_record))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JanitorSafetyError(
            f"{lease_namespace} live lease metadata is invalid"
        ) from exc
    record = _required_mapping(decoded, f"{lease_namespace} live lease")
    _fencing_token, persistence_namespace = _lease_record_generation(
        record,
        lease_namespace=lease_namespace,
        require_generation=require_generation,
    )
    refreshed_at_epoch = _positive_int(
        record.get("refreshed_at_epoch"),
        f"{lease_namespace} live lease refreshed_at_epoch",
    )
    score = redis_client.zscore(registry_key, lease_namespace)
    if score is None or float(score) != float(refreshed_at_epoch):
        raise JanitorSafetyError(
            f"{lease_namespace} live lease freshness mismatch"
        )
    return persistence_namespace


def _lease_record_generation(
    record: Mapping[str, object],
    *,
    lease_namespace: str,
    require_generation: bool = True,
) -> tuple[int, str]:
    record_lease_namespace = record.get("lease_namespace")
    if record_lease_namespace is None:
        record_lease_namespace = record.get("namespace")
    normalized_record_lease_namespace = _normalize_namespace(
        _required_string(
            record_lease_namespace,
            f"{lease_namespace} live lease namespace",
        )
    )
    if normalized_record_lease_namespace != lease_namespace:
        raise JanitorSafetyError(
            f"{lease_namespace} live lease identity mismatch"
        )
    fencing_token = _positive_fencing_token(
        record.get("fencing_token"),
        f"{lease_namespace} live lease fencing_token",
    )
    raw_instance_id = record.get("persistence_instance_id")
    raw_persistence_namespace = record.get("persistence_namespace")
    if raw_instance_id is None or raw_persistence_namespace is None:
        if require_generation:
            raise JanitorSafetyError(
                f"{lease_namespace} live lease generation metadata is incomplete"
            )
        return fencing_token, lease_namespace
    persistence_instance_id = _required_string(
        raw_instance_id,
        f"{lease_namespace} persistence_instance_id",
    )
    if _CANONICAL_UUID4_RE.fullmatch(persistence_instance_id) is None:
        raise JanitorSafetyError(
            f"{lease_namespace} persistence_instance_id must be a UUID4"
        )
    persistence_namespace = _normalize_namespace(
        _required_string(
            raw_persistence_namespace,
            f"{lease_namespace} persistence_namespace",
        )
    )
    expected_persistence_namespace = (
        f"{lease_namespace}:{persistence_instance_id}"
    )
    if persistence_namespace != expected_persistence_namespace:
        raise JanitorSafetyError(
            f"{lease_namespace} live lease generation mismatch"
        )
    return fencing_token, persistence_namespace


def _unlink_rate_limited(
    redis_client: RedisJanitorClient,
    namespace: str,
    keys: list[bytes | str],
    *,
    batch_size: int,
    max_keys_per_second: int,
    allowlisted_namespaces: set[str],
    lease_namespaces: set[str],
    registry_key: str,
    registry_fresh_after_epoch: int,
    min_idle_seconds: int,
    manifest_nodes: object,
    expected_lease_namespaces: tuple[str, ...],
    expected_persistence_namespaces: tuple[str, ...],
    registry_max_age_seconds: int,
    reconciliation_max_age_seconds: int,
    sleep_fn,
    monotonic_fn,
) -> _UnlinkResult:
    deleted = 0
    post_batch_verifications = 0
    expected_dbsize = _non_negative_int(
        redis_client.dbsize(),
        "Redis DBSIZE",
    )
    effective_batch_size = min(
        batch_size,
        max_keys_per_second,
        DEFAULT_MAX_ATOMIC_UNLINK_BATCH_SIZE,
    )
    limiter = _MonotonicTokenBucket(
        max_keys_per_second=max_keys_per_second,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )
    for offset in range(0, len(keys), effective_batch_size):
        batch = keys[offset : offset + effective_batch_size]
        if namespace in allowlisted_namespaces:
            raise JanitorSafetyError(
                "namespace became active before key deletion"
            )
        limiter.wait_for_capacity(len(batch))
        deleted_in_batch = _atomic_unlink_batch(
            redis_client,
            namespace=namespace,
            keys=batch,
            lease_namespaces=lease_namespaces,
            registry_key=registry_key,
            registry_fresh_after_epoch=registry_fresh_after_epoch,
            min_idle_seconds=min_idle_seconds,
        )
        deleted += deleted_in_batch
        expected_dbsize -= deleted_in_batch
        _verify_post_unlink_batch(
            redis_client,
            expected_dbsize=expected_dbsize,
            manifest_nodes=manifest_nodes,
            expected_lease_namespaces=expected_lease_namespaces,
            expected_persistence_namespaces=(
                expected_persistence_namespaces
            ),
            registry_key=registry_key,
            registry_max_age_seconds=registry_max_age_seconds,
            reconciliation_max_age_seconds=(
                reconciliation_max_age_seconds
            ),
        )
        post_batch_verifications += 1
    return _UnlinkResult(
        deleted_keys=deleted,
        post_batch_verifications=post_batch_verifications,
    )


@dataclass(frozen=True)
class _UnlinkResult:
    deleted_keys: int
    post_batch_verifications: int


def _verify_post_unlink_batch(
    redis_client: RedisJanitorClient,
    *,
    expected_dbsize: int,
    manifest_nodes: object,
    expected_lease_namespaces: tuple[str, ...],
    expected_persistence_namespaces: tuple[str, ...],
    registry_key: str,
    registry_max_age_seconds: int,
    reconciliation_max_age_seconds: int,
) -> None:
    actual_dbsize = _non_negative_int(
        redis_client.dbsize(),
        "Redis DBSIZE",
    )
    if actual_dbsize != expected_dbsize:
        raise JanitorSafetyError(
            "Redis key count drifted after unlink batch"
        )

    memory = _required_mapping(
        redis_client.info("memory"),
        "Redis INFO memory",
    )
    used_memory = _positive_int(
        memory.get("used_memory"),
        "Redis used_memory",
    )
    maxmemory = _positive_int(
        memory.get("maxmemory"),
        "Redis maxmemory",
    )
    if used_memory >= int(maxmemory * DEFAULT_POST_BATCH_MAXMEMORY_RATIO):
        raise JanitorSafetyError(
            "Redis memory usage reached the 85% post-batch safety threshold"
        )

    server_time = _redis_server_time(redis_client)
    server_info = _required_mapping(
        redis_client.info("server"),
        "Redis INFO server",
    )
    uptime_in_seconds = _non_negative_int(
        server_info.get("uptime_in_seconds"),
        "Redis uptime_in_seconds",
    )
    startup_epoch = server_time - uptime_in_seconds
    if startup_epoch <= 0:
        raise JanitorSafetyError("Redis startup time is invalid")
    lease_namespaces, persistence_namespaces = _validate_node_evidence(
        redis_client,
        manifest_nodes,
        registry_key=registry_key,
        server_time=server_time,
        startup_epoch=startup_epoch,
        registry_max_age_seconds=registry_max_age_seconds,
        reconciliation_max_age_seconds=reconciliation_max_age_seconds,
    )
    if lease_namespaces != expected_lease_namespaces:
        raise JanitorSafetyError(
            "stable lease namespaces drifted after unlink batch"
        )
    if persistence_namespaces != expected_persistence_namespaces:
        raise JanitorSafetyError(
            "active persistence namespaces drifted after unlink batch"
        )


class _MonotonicTokenBucket:
    def __init__(
        self,
        *,
        max_keys_per_second: int,
        sleep_fn,
        monotonic_fn,
    ) -> None:
        self._max_keys_per_second = max_keys_per_second
        self._sleep_fn = sleep_fn
        self._monotonic_fn = monotonic_fn
        self._capacity = float(max_keys_per_second)
        self._tokens = self._capacity
        self._last_refill_at = float(monotonic_fn())

    def wait_for_capacity(self, key_count: int) -> None:
        while True:
            now = float(self._monotonic_fn())
            elapsed = max(0.0, now - self._last_refill_at)
            refill = elapsed * self._max_keys_per_second
            self._tokens = min(self._capacity, self._tokens + refill)
            self._last_refill_at = now
            if self._tokens >= key_count:
                self._tokens -= key_count
                return
            missing_tokens = key_count - self._tokens
            self._sleep_fn(missing_tokens / self._max_keys_per_second)


def _atomic_unlink_batch(
    redis_client: RedisJanitorClient,
    *,
    namespace: str,
    keys: list[bytes | str],
    lease_namespaces: set[str],
    registry_key: str,
    registry_fresh_after_epoch: int,
    min_idle_seconds: int,
) -> int:
    if not keys:
        return 0
    ordered_lease_namespaces = tuple(sorted(lease_namespaces))
    if not ordered_lease_namespaces:
        raise JanitorSafetyError(
            "atomic unlink requires stable lease namespaces"
        )
    raw_result = redis_client.eval(
        _ATOMIC_UNLINK_LUA,
        len(keys) + 2,
        registry_key,
        f"{registry_key}:leases",
        *keys,
        namespace,
        registry_fresh_after_epoch,
        min_idle_seconds,
        len(ordered_lease_namespaces),
        *ordered_lease_namespaces,
    )
    if not isinstance(raw_result, (list, tuple)) or len(raw_result) != 2:
        raise JanitorSafetyError("atomic unlink returned an invalid result")
    deleted = int(raw_result[0])
    status = _decode(raw_result[1])
    if status == "DELETED":
        return deleted
    if status == "ACTIVE":
        raise JanitorSafetyError(
            "namespace became active before key deletion"
        )
    if status in {"LEASE_METADATA_MISSING", "LEASE_METADATA_INVALID"}:
        raise JanitorSafetyError(
            "active lease metadata changed before key deletion"
        )
    if status == "WRONG_NAMESPACE":
        raise JanitorSafetyError(
            "key namespace ownership changed before deletion"
        )
    if status == "KEY_MISSING":
        raise JanitorSafetyError(
            "key disappeared before deletion"
        )
    if status == "KEY_ACTIVE":
        raise JanitorSafetyError(
            "key activity changed before deletion"
        )
    raise JanitorSafetyError(f"atomic unlink failed closed with status {status!r}")


def _validate_safety_manifest(
    redis_client: RedisJanitorClient,
    manifest: Mapping[str, object],
    *,
    registry_key: str,
    registry_max_age_seconds: int,
    backup_max_age_seconds: int,
    reconciliation_max_age_seconds: int,
) -> _SafetyEvidence:
    if not isinstance(manifest, Mapping):
        raise JanitorSafetyError("safety manifest must be a JSON object")
    schema_version = _required_string(
        manifest.get("schema_version"),
        "safety manifest schema_version",
    )
    if schema_version == LEGACY_SAFETY_MANIFEST_SCHEMA_VERSION:
        raise JanitorSafetyError(
            "apply requires safety manifest schema v2"
        )
    if schema_version != SAFETY_MANIFEST_SCHEMA_VERSION:
        raise JanitorSafetyError("safety manifest schema_version is unsupported")

    server_time = _redis_server_time(redis_client)
    server_info = _required_mapping(
        redis_client.info("server"),
        "Redis INFO server",
    )
    current_run_id = _required_string(
        server_info.get("run_id"),
        "Redis run_id",
    )
    uptime_in_seconds = _non_negative_int(
        server_info.get("uptime_in_seconds"),
        "Redis uptime_in_seconds",
    )
    startup_epoch = server_time - uptime_in_seconds
    if startup_epoch <= 0:
        raise JanitorSafetyError("Redis startup time is invalid")

    backup = _required_mapping(manifest.get("backup"), "backup")
    _validate_cold_backup(
        backup,
        server_time=server_time,
        startup_epoch=startup_epoch,
        current_run_id=current_run_id,
        max_age_seconds=backup_max_age_seconds,
    )
    lease_namespaces, active_persistence_namespaces = _validate_node_evidence(
        redis_client,
        manifest.get("nodes"),
        registry_key=registry_key,
        server_time=server_time,
        startup_epoch=startup_epoch,
        registry_max_age_seconds=registry_max_age_seconds,
        reconciliation_max_age_seconds=reconciliation_max_age_seconds,
    )
    return _SafetyEvidence(
        fresh_after_epoch=server_time - registry_max_age_seconds,
        lease_namespaces=lease_namespaces,
        active_persistence_namespaces=active_persistence_namespaces,
    )


def _validate_cold_backup(
    backup: Mapping[str, object],
    *,
    server_time: int,
    startup_epoch: int,
    current_run_id: str,
    max_age_seconds: int,
) -> None:
    mode = _required_string(backup.get("mode"), "backup mode")
    if mode.lower() != "cold":
        raise JanitorSafetyError("backup mode must be cold")
    source_run_id = _required_string(
        backup.get("source_run_id"),
        "backup source_run_id",
    )
    if source_run_id == current_run_id:
        raise JanitorSafetyError(
            "cold backup source run_id must differ from the current Redis run_id"
        )
    completed_at_epoch = _positive_int(
        backup.get("completed_at_epoch"),
        "backup completed_at_epoch",
    )
    if completed_at_epoch > server_time:
        raise JanitorSafetyError("backup completion time is in the future")
    if server_time - completed_at_epoch > max_age_seconds:
        raise JanitorSafetyError("cold backup is stale")
    if completed_at_epoch > startup_epoch:
        raise JanitorSafetyError(
            "cold backup must complete before the current Redis process starts"
        )

    artifacts = _required_sequence(backup.get("artifacts"), "backup artifacts")
    if not artifacts:
        raise JanitorSafetyError("cold backup requires an RDB or AOF artifact")
    for index, raw_artifact in enumerate(artifacts):
        artifact = _required_mapping(
            raw_artifact,
            f"backup artifact {index}",
        )
        kind = _required_string(
            artifact.get("kind"),
            f"backup artifact {index} kind",
        ).lower()
        if kind not in {"rdb", "aof"}:
            raise JanitorSafetyError(
                "cold backup artifacts must be RDB or AOF files"
            )
        raw_path = _required_string(
            artifact.get("path"),
            f"backup artifact {index} path",
        )
        path = Path(raw_path)
        if not path.is_absolute():
            raise JanitorSafetyError("backup artifact paths must be absolute")
        if not path.is_file():
            raise JanitorSafetyError("backup artifact file is missing")
        expected_sha256 = _required_string(
            artifact.get("sha256"),
            f"backup artifact {index} sha256",
        )
        if _LOWERCASE_SHA256_RE.fullmatch(expected_sha256) is None:
            raise JanitorSafetyError(
                "backup artifact sha256 must be lowercase hexadecimal"
            )
        if _sha256_file(path) != expected_sha256:
            raise JanitorSafetyError("backup artifact sha256 does not match")


def _validate_node_evidence(
    redis_client: RedisJanitorClient,
    raw_nodes: object,
    *,
    registry_key: str,
    server_time: int,
    startup_epoch: int,
    registry_max_age_seconds: int,
    reconciliation_max_age_seconds: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    nodes = _required_sequence(raw_nodes, "nodes")
    if len(nodes) != len(EXPECTED_STABLE_NAMESPACES):
        raise JanitorSafetyError(
            "safety manifest requires exactly account-a, account-b, "
            "account-c, and account-d"
        )

    seen_accounts: set[str] = set()
    seen_lease_namespaces: set[str] = set()
    seen_persistence_namespaces: set[str] = set()
    seen_owners: set[str] = set()
    for index, raw_node in enumerate(nodes):
        node = _required_mapping(raw_node, f"node {index}")
        account_id = _required_string(
            node.get("account_id"),
            f"node {index} account_id",
        )
        expected_namespace = EXPECTED_STABLE_NAMESPACES.get(account_id)
        if expected_namespace is None or account_id in seen_accounts:
            raise JanitorSafetyError(
                "safety manifest requires exactly account-a, account-b, "
                "account-c, and account-d"
            )
        lease_namespace = _normalize_namespace(
            _required_string(
                node.get("lease_namespace"),
                f"{account_id} lease_namespace",
            )
        )
        if lease_namespace != expected_namespace:
            raise JanitorSafetyError(
                f"{account_id} must use stable lease namespace "
                f"{expected_namespace}"
            )
        persistence_namespace = _validate_persistence_namespace(
            node.get("persistence_namespace"),
            account_id=account_id,
            lease_namespace=lease_namespace,
        )
        owner = _required_string(node.get("owner"), f"{account_id} owner")
        release_id = _required_string(
            node.get("release_id"),
            f"{account_id} release_id",
        )
        fencing_token = _positive_int(
            node.get("fencing_token"),
            f"{account_id} fencing_token",
        )
        _validate_reconciliation_evidence(
            node.get("reconciliation"),
            account_id=account_id,
            release_id=release_id,
            server_time=server_time,
            startup_epoch=startup_epoch,
            max_age_seconds=reconciliation_max_age_seconds,
        )
        _validate_live_lease(
            redis_client,
            registry_key=registry_key,
            lease_namespace=lease_namespace,
            persistence_namespace=persistence_namespace,
            owner=owner,
            release_id=release_id,
            fencing_token=fencing_token,
            server_time=server_time,
            startup_epoch=startup_epoch,
            max_age_seconds=registry_max_age_seconds,
        )
        seen_accounts.add(account_id)
        seen_lease_namespaces.add(lease_namespace)
        seen_persistence_namespaces.add(persistence_namespace)
        seen_owners.add(owner)

    if seen_accounts != set(EXPECTED_STABLE_NAMESPACES):
        raise JanitorSafetyError(
            "safety manifest requires exactly account-a, account-b, "
            "account-c, and account-d"
        )
    if len(seen_lease_namespaces) != len(EXPECTED_STABLE_NAMESPACES):
        raise JanitorSafetyError("A-D stable lease namespaces must be distinct")
    if len(seen_persistence_namespaces) != len(EXPECTED_STABLE_NAMESPACES):
        raise JanitorSafetyError(
            "A-D persistence namespaces must be distinct"
        )
    if len(seen_owners) != len(EXPECTED_STABLE_NAMESPACES):
        raise JanitorSafetyError("A-D namespace lease owners must be distinct")
    return (
        tuple(sorted(seen_lease_namespaces)),
        tuple(sorted(seen_persistence_namespaces)),
    )


def _validate_persistence_namespace(
    raw_namespace: object,
    *,
    account_id: str,
    lease_namespace: str,
) -> str:
    persistence_namespace = _normalize_namespace(
        _required_string(
            raw_namespace,
            f"{account_id} persistence_namespace",
        )
    )
    expected_prefix = f"{lease_namespace}:"
    if not persistence_namespace.startswith(expected_prefix):
        raise JanitorSafetyError(
            f"{account_id} persistence namespace must belong to "
            f"{lease_namespace}"
        )
    runtime_id = persistence_namespace[len(expected_prefix) :]
    if _CANONICAL_UUID4_RE.fullmatch(runtime_id) is None:
        raise JanitorSafetyError(
            f"{account_id} persistence namespace must end with a UUID4"
        )
    return persistence_namespace


def _validate_reconciliation_evidence(
    raw_reconciliation: object,
    *,
    account_id: str,
    release_id: str,
    server_time: int,
    startup_epoch: int,
    max_age_seconds: int,
) -> None:
    reconciliation = _required_mapping(
        raw_reconciliation,
        f"{account_id} reconciliation",
    )
    state = _required_string(
        reconciliation.get("state"),
        f"{account_id} reconciliation state",
    )
    if state.lower() != "healthy":
        raise JanitorSafetyError(
            f"{account_id} reconciliation evidence is not healthy"
        )
    proof_release_id = _required_string(
        reconciliation.get("release_id"),
        f"{account_id} reconciliation release_id",
    )
    if proof_release_id != release_id:
        raise JanitorSafetyError(
            f"{account_id} reconciliation release identity does not match lease"
        )
    completed_at_epoch = _positive_int(
        reconciliation.get("completed_at_epoch"),
        f"{account_id} reconciliation completed_at_epoch",
    )
    if completed_at_epoch > server_time:
        raise JanitorSafetyError(
            f"{account_id} reconciliation evidence is from the future"
        )
    if completed_at_epoch < startup_epoch:
        raise JanitorSafetyError(
            f"{account_id} reconciliation evidence predates Redis startup"
        )
    if server_time - completed_at_epoch > max_age_seconds:
        raise JanitorSafetyError(
            f"{account_id} reconciliation evidence is stale"
        )


def _validate_live_lease(
    redis_client: RedisJanitorClient,
    *,
    registry_key: str,
    lease_namespace: str,
    persistence_namespace: str,
    owner: str,
    release_id: str,
    fencing_token: int,
    server_time: int,
    startup_epoch: int,
    max_age_seconds: int,
) -> None:
    metadata_key = f"{registry_key}:leases"
    raw_record = redis_client.hget(metadata_key, lease_namespace)
    if raw_record is None:
        raise JanitorSafetyError(
            f"{lease_namespace} live lease metadata is missing"
        )
    try:
        decoded = json.loads(_decode(raw_record))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JanitorSafetyError(
            f"{lease_namespace} live lease metadata is invalid"
        ) from exc
    record = _required_mapping(decoded, f"{lease_namespace} live lease")
    refreshed_at_epoch = _positive_int(
        record.get("refreshed_at_epoch"),
        f"{lease_namespace} live lease refreshed_at_epoch",
    )
    record_fencing_token, record_persistence_namespace = (
        _lease_record_generation(
            record,
            lease_namespace=lease_namespace,
            require_generation=True,
        )
    )
    expected_identity = (owner, release_id, fencing_token)
    actual_identity = (
        record.get("owner"),
        record.get("release_id"),
        record_fencing_token,
    )
    if (
        actual_identity != expected_identity
        or record_persistence_namespace != persistence_namespace
    ):
        raise JanitorSafetyError(
            f"{lease_namespace} live lease identity mismatch"
        )
    score = redis_client.zscore(registry_key, lease_namespace)
    if score is None or float(score) != float(refreshed_at_epoch):
        raise JanitorSafetyError(
            f"{lease_namespace} live lease freshness mismatch"
        )
    if refreshed_at_epoch > server_time:
        raise JanitorSafetyError(
            f"{lease_namespace} live lease is from the future"
        )
    if refreshed_at_epoch < startup_epoch:
        raise JanitorSafetyError(
            f"{lease_namespace} live lease predates Redis startup"
        )
    if server_time - refreshed_at_epoch > max_age_seconds:
        raise JanitorSafetyError(f"{lease_namespace} live lease is stale")


def _redis_server_time(redis_client: RedisJanitorClient) -> int:
    raw_time = redis_client.time()
    if not isinstance(raw_time, (list, tuple)) or len(raw_time) < 1:
        raise JanitorSafetyError("Redis TIME returned an invalid result")
    return _positive_int(raw_time[0], "Redis server time")


def _required_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise JanitorSafetyError(f"{label} must be an object")
    return value


def _required_sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise JanitorSafetyError(f"{label} must be an array")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise JanitorSafetyError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise JanitorSafetyError(f"{label} must be non-empty")
    return normalized


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise JanitorSafetyError(f"{label} must be a positive integer")
    return value


def _positive_fencing_token(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise JanitorSafetyError(
            f"{label} must be a positive integer or decimal string"
        )
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        normalized = int(value)
    else:
        raise JanitorSafetyError(
            f"{label} must be a positive integer or decimal string"
        )
    if normalized <= 0:
        raise JanitorSafetyError(
            f"{label} must be a positive integer or decimal string"
        )
    return normalized


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JanitorSafetyError(f"{label} must be a non-negative integer")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_prefixes(values: Iterable[str]) -> tuple[str, ...]:
    prefixes = tuple(sorted(set(values)))
    if not prefixes:
        raise JanitorSafetyError("at least one legacy prefix is required")
    for prefix in prefixes:
        if not prefix.endswith(":"):
            raise JanitorSafetyError("legacy prefixes must end with ':'")
        if not prefix.startswith("trader-"):
            raise JanitorSafetyError("legacy prefixes must be scoped to a trader")
    return prefixes


def _normalize_namespace(value: str) -> str:
    normalized = value.strip().rstrip(":")
    if not normalized:
        raise JanitorSafetyError("active namespace values must be non-empty")
    return normalized


def _decode(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _require_positive(label: str, value: int) -> None:
    if value <= 0:
        raise JanitorSafetyError(f"{label} must be positive")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and remove inactive legacy UUID Redis namespaces.",
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL"),
        help="Redis URL; defaults to REDIS_URL and is never included in output.",
    )
    parser.add_argument(
        "--legacy-prefix",
        action="append",
        required=True,
        help="Scoped prefix immediately before the runtime UUID; repeatable.",
    )
    parser.add_argument(
        "--active-namespace",
        action="append",
        default=[],
        help="Complete active namespace allowlist entry; repeatable.",
    )
    parser.add_argument("--registry-key", default=DEFAULT_REGISTRY_KEY)
    parser.add_argument(
        "--registry-max-age-seconds",
        type=int,
        default=DEFAULT_REGISTRY_MAX_AGE_SECONDS,
    )
    parser.add_argument(
        "--backup-max-age-seconds",
        type=int,
        default=DEFAULT_BACKUP_MAX_AGE_SECONDS,
    )
    parser.add_argument(
        "--reconciliation-max-age-seconds",
        type=int,
        default=DEFAULT_RECONCILIATION_MAX_AGE_SECONDS,
    )
    parser.add_argument(
        "--min-idle-seconds",
        type=int,
        default=DEFAULT_MIN_IDLE_SECONDS,
    )
    parser.add_argument(
        "--max-namespaces",
        type=int,
        default=DEFAULT_MAX_NAMESPACES,
    )
    parser.add_argument("--max-keys", type=int, default=DEFAULT_MAX_KEYS)
    parser.add_argument("--scan-count", type=int, default=DEFAULT_SCAN_COUNT)
    parser.add_argument(
        "--unlink-batch-size",
        type=int,
        default=DEFAULT_UNLINK_BATCH_SIZE,
    )
    parser.add_argument(
        "--max-unlink-keys-per-second",
        type=int,
        default=DEFAULT_MAX_UNLINK_KEYS_PER_SECOND,
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--safety-manifest",
        type=Path,
        help="JSON safety manifest required for apply.",
    )
    return parser


def _execute(args: argparse.Namespace) -> int:
    safety_manifest = None
    if args.safety_manifest is not None:
        try:
            raw_manifest = json.loads(
                args.safety_manifest.read_text(encoding="utf-8")
            )
            safety_manifest = _required_mapping(
                raw_manifest,
                "safety manifest",
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(
                f"safety gate: safety manifest is unreadable: {exc}",
                file=sys.stderr,
            )
            return 2
        except JanitorSafetyError as exc:
            print(f"safety gate: {exc}", file=sys.stderr)
            return 2

    try:
        import redis
    except ImportError:
        print("redis-py is required", file=sys.stderr)
        return 2

    fresh_after = int(time.time()) - args.registry_max_age_seconds
    try:
        client = redis.Redis.from_url(args.redis_url, decode_responses=False)
        report = run_janitor(
            client,
            legacy_prefixes=args.legacy_prefix,
            active_namespaces=set(args.active_namespace),
            registry_key=args.registry_key,
            registry_fresh_after_epoch=fresh_after,
            min_idle_seconds=args.min_idle_seconds,
            max_namespaces=args.max_namespaces,
            max_keys=args.max_keys,
            apply=args.apply,
            safety_manifest=safety_manifest,
            registry_max_age_seconds=args.registry_max_age_seconds,
            backup_max_age_seconds=args.backup_max_age_seconds,
            reconciliation_max_age_seconds=(
                args.reconciliation_max_age_seconds
            ),
            scan_count=args.scan_count,
            unlink_batch_size=args.unlink_batch_size,
            max_unlink_keys_per_second=args.max_unlink_keys_per_second,
        )
    except JanitorSafetyError as exc:
        print(f"safety gate: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001
        print("Redis janitor operation failed", file=sys.stderr)
        return 1

    print(json.dumps(asdict(report), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.redis_url:
        print(
            "redis URL missing; set REDIS_URL or pass --redis-url",
            file=sys.stderr,
        )
        return 2
    if args.apply and args.safety_manifest is None:
        print(
            "safety gate: apply requires --safety-manifest",
            file=sys.stderr,
        )
        return 2
    try:
        with AccountStallOperationLock(enabled=args.apply):
            return _execute(args)
    except JanitorSafetyError as exc:
        print(f"safety gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
