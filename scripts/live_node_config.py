#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

POLICY_SCHEMA_VERSION = "trader-v3-live-risk-policy/v2"
BACKUP_SCHEMA_VERSION = "trader-v3-live-node-config-backup/v2"
LEGACY_BACKUP_SCHEMA_VERSION = "trader-v3-live-node-config-backup/v1"
NODE_CONFIG_CONTRACT_SCHEMA_VERSION = (
    "trader-v3-node-config-contract/v1"
)
CAPTURE_SCHEMA_VERSION = "trader-v3-live-risk-capture/v1"
ARTIFACT_RECORD_SCHEMA_VERSION = (
    "trader-v3-live-node-config-artifact-record/v1"
)
CONFIG_MOUNT_TARGET = "/cfg.json"
SUPPORTED_ACCOUNTS = {
    "account-a",
    "account-b",
    "account-c",
    "account-d",
}
ALLOWED_INSTRUMENTS = {
    "BNBUSDT-PERP.BINANCE",
    "BTCUSDT-PERP.BINANCE",
    "ETHUSDT-PERP.BINANCE",
    "SOLUSDT-PERP.BINANCE",
}
DEFAULT_NOTIONAL_KEY = "*"
IDENTITY_PATTERNS = (
    re.compile(r"trader[-_]?account[-_]?[abcd]", re.IGNORECASE),
    re.compile(r"instance[-_]?account[-_]?[abcd]", re.IGNORECASE),
    re.compile(r"account[-_]?[abcd]", re.IGNORECASE),
    re.compile(r"node[-_]?[abcd]", re.IGNORECASE),
)
ACCOUNT_NETWORK_CONTROL_PLANE_BASE_URLS = {
    "account-a": "http://172.30.1.1:8080",
    "account-b": "http://172.30.2.1:8080",
    "account-c": "http://172.30.3.1:8080",
    "account-d": "http://172.30.4.1:8080",
}
RISK_ENV_NAMES = (
    "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON",
    "NAUTILUS_MAX_ORDER_SUBMIT_RATE",
    "NAUTILUS_MAX_ORDER_MODIFY_RATE",
)


class LiveNodeConfigError(ValueError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalize_identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_identity(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_normalize_identity(item) for item in value]
    if isinstance(value, str):
        normalized = value
        for pattern in IDENTITY_PATTERNS:
            normalized = pattern.sub("<node-identity>", normalized)
        return normalized
    return value


def normalized_config_sha256(config: dict[str, Any]) -> str:
    normalized = _normalize_identity(config)
    account_id = config.get("account_id")
    expected_base_url = ACCOUNT_NETWORK_CONTROL_PLANE_BASE_URLS.get(
        account_id
    )
    control_plane = config.get("control_plane")
    normalized_control_plane = normalized.get("control_plane")
    if (
        isinstance(control_plane, dict)
        and isinstance(normalized_control_plane, dict)
        and control_plane.get("base_url") == expected_base_url
    ):
        normalized_control_plane["base_url"] = (
            "<account-network-control-plane>"
        )
    return _sha256_bytes(_canonical_json_bytes(normalized))


def normalized_config_contract_sha256(
    normalized_hashes: dict[str, str],
) -> str:
    if (
        not normalized_hashes
        or not set(normalized_hashes).issubset(SUPPORTED_ACCOUNTS)
    ):
        raise LiveNodeConfigError(
            "normalized config hashes must be a non-empty supported "
            "account collection"
        )
    accounts = []
    for account_id, digest in sorted(normalized_hashes.items()):
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise LiveNodeConfigError(
                f"normalized config hash is invalid: {account_id}"
            )
        accounts.append(
            {
                "account_id": account_id,
                "normalized_sha256": digest,
            }
        )
    contract = {
        "schema_version": NODE_CONFIG_CONTRACT_SCHEMA_VERSION,
        "accounts": accounts,
    }
    return _sha256_bytes(_canonical_json_bytes(contract))


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveNodeConfigError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise LiveNodeConfigError(f"{label} root must be an object: {path}")
    return value


def _positive_cap(
    value: Any,
    label: str,
    *,
    ceiling: Decimal,
) -> str:
    try:
        cap = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveNodeConfigError(f"{label} must be a decimal") from exc
    if not cap.is_finite() or cap <= 0 or cap > ceiling:
        raise LiveNodeConfigError(
            f"{label} must be positive and at most {ceiling}"
        )
    return format(cap, "f")


def load_risk_policy(path: Path) -> dict[str, Any]:
    policy = _load_object(path, "risk policy")
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise LiveNodeConfigError("risk policy schema version mismatch")
    migration = policy.get("legacy_migration_contract")
    if not isinstance(migration, dict):
        raise LiveNodeConfigError(
            "risk policy legacy_migration_contract is invalid"
        )
    instruments = migration.get("allowed_instruments")
    if not isinstance(instruments, list):
        raise LiveNodeConfigError("risk policy instrument allowlist is invalid")
    if set(instruments) != ALLOWED_INSTRUMENTS:
        raise LiveNodeConfigError("risk policy instrument allowlist mismatch")
    try:
        ceiling = Decimal(str(migration.get("max_notional_ceiling_usdt")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveNodeConfigError(
            "risk policy max_notional ceiling is invalid"
        ) from exc
    if not ceiling.is_finite() or ceiling <= 0:
        raise LiveNodeConfigError(
            "risk policy max_notional ceiling must be positive"
        )
    target_cap = _positive_cap(
        migration.get("global_safety_max_notional_usdt"),
        "risk policy global_safety_max_notional_usdt",
        ceiling=ceiling,
    )
    default_cap = _positive_cap(
        migration.get("default_max_notional_usdt"),
        "risk policy default_max_notional_usdt",
        ceiling=ceiling,
    )
    default_rates = {
        "max_order_submit_rate": _required_rate(
            migration.get("default_order_submit_rate"),
            "risk policy default_order_submit_rate",
        ),
        "max_order_modify_rate": _required_rate(
            migration.get("default_order_modify_rate"),
            "risk policy default_order_modify_rate",
        ),
    }
    entry = policy.get("entry_contract")
    if not isinstance(entry, dict):
        raise LiveNodeConfigError("risk policy entry_contract is invalid")
    if entry.get("type") != "limit" or entry.get("time_in_force") != "IOC":
        raise LiveNodeConfigError("live canary entry contract must be limit+IOC")
    _positive_cap(
        entry.get("max_notional_usdt"),
        "entry contract max_notional_usdt",
        ceiling=Decimal(12),
    )
    return {
        "max_notional_ceiling_usdt": ceiling,
        "global_safety_max_notional_usdt": target_cap,
        "default_max_notional_usdt": default_cap,
        "default_rates": default_rates,
        "entry_contract": dict(entry),
    }


def _required_rate(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[1-9][0-9]*/[0-9]{2}:[0-9]{2}:[0-9]{2}",
            value,
        )
        is None
    ):
        raise LiveNodeConfigError(f"{label} is invalid")
    return value


def _validate_migration_risk(
    raw: Any,
    *,
    ceiling: Decimal,
    allow_default: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LiveNodeConfigError("legacy risk must be an object")
    raw_caps = raw.get("max_notional_per_order")
    if not isinstance(raw_caps, dict):
        raise LiveNodeConfigError(
            "legacy risk max_notional_per_order must be an object"
        )
    allowed_keys = set(ALLOWED_INSTRUMENTS)
    if allow_default:
        allowed_keys.add(DEFAULT_NOTIONAL_KEY)
    raw_keys = set(raw_caps)
    if not ALLOWED_INSTRUMENTS.issubset(raw_keys):
        raise LiveNodeConfigError("legacy risk instrument allowlist mismatch")
    if not raw_keys.issubset(allowed_keys):
        raise LiveNodeConfigError("legacy risk instrument allowlist mismatch")
    caps = {
        instrument: _positive_cap(
            raw_caps[instrument],
            f"legacy risk cap {instrument}",
            ceiling=ceiling,
        )
        for instrument in sorted(raw_caps)
    }
    return {
        "max_notional_per_order": caps,
        "max_order_submit_rate": _required_rate(
            raw.get("max_order_submit_rate"),
            "legacy max_order_submit_rate",
        ),
        "max_order_modify_rate": _required_rate(
            raw.get("max_order_modify_rate"),
            "legacy max_order_modify_rate",
        ),
    }


def _risk_from_environment(
    values: list[str],
    container: str,
    *,
    default_rates: dict[str, str],
) -> Any:
    selected = {}
    for entry in values:
        name, separator, value = entry.partition("=")
        if separator and name in RISK_ENV_NAMES:
            selected[name] = value
    if not selected:
        return False
    if "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON" not in selected:
        raise LiveNodeConfigError(
            f"{container} legacy risk environment lacks max_notional"
        )
    try:
        max_notional = json.loads(
            selected["NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON"]
        )
    except json.JSONDecodeError as exc:
        raise LiveNodeConfigError(
            f"{container} legacy max_notional JSON is invalid"
        ) from exc
    return {
        "max_notional_per_order": max_notional,
        "max_order_submit_rate": selected.get(
            "NAUTILUS_MAX_ORDER_SUBMIT_RATE",
            default_rates["max_order_submit_rate"],
        ),
        "max_order_modify_rate": selected.get(
            "NAUTILUS_MAX_ORDER_MODIFY_RATE",
            default_rates["max_order_modify_rate"],
        ),
    }


def _docker_inspect_environment(container: str) -> list[str]:
    try:
        output = subprocess.check_output(
            ["docker", "inspect", container],
            text=True,
            stderr=subprocess.PIPE,
        )
        inspected = json.loads(output)
    except (
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        raise LiveNodeConfigError(
            f"cannot inspect legacy risk environment: {container}"
        ) from exc
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise LiveNodeConfigError(f"docker inspect is invalid: {container}")
    config = inspected[0].get("Config")
    if not isinstance(config, dict):
        raise LiveNodeConfigError(f"docker Config is invalid: {container}")
    values = config.get("Env")
    if not isinstance(values, list):
        raise LiveNodeConfigError(f"docker Env is invalid: {container}")
    return [str(value) for value in values]


def capture_legacy_risk(
    *,
    policy_path: Path,
    configs: dict[str, Path],
    containers: dict[str, str],
    output_path: Path,
    expected_owner_uid: int,
) -> dict[str, Any]:
    if not configs or not set(configs).issubset(SUPPORTED_ACCOUNTS):
        raise LiveNodeConfigError(
            "configs must be a non-empty supported account collection"
        )
    if set(containers) != set(configs):
        raise LiveNodeConfigError("config and container accounts differ")
    policy = load_risk_policy(policy_path)
    ceiling = policy["max_notional_ceiling_usdt"]
    candidates = []
    sources = []
    for account_id, path in sorted(configs.items()):
        _, _, config = _read_secure_config(
            path,
            expected_owner_uid=expected_owner_uid,
        )
        if config.get("account_id") != account_id:
            raise LiveNodeConfigError(
                f"node config account identity mismatch: {path}"
            )
        configured_risk = config.get("risk")
        if configured_risk is not None:
            candidates.append(
                _validate_migration_risk(
                    configured_risk,
                    ceiling=ceiling,
                )
            )
            sources.append(f"{account_id}:json")
        container = containers[account_id]
        environment_risk = _risk_from_environment(
            _docker_inspect_environment(container),
            container,
            default_rates=policy["default_rates"],
        )
        if environment_risk is not False:
            candidates.append(
                _validate_migration_risk(
                    environment_risk,
                    ceiling=ceiling,
                )
            )
            sources.append(f"{account_id}:container")
    if not candidates:
        raise LiveNodeConfigError("no trusted legacy risk source is available")
    canonical = {
        _canonical_json_bytes(candidate)
        for candidate in candidates
    }
    if len(canonical) != 1:
        raise LiveNodeConfigError("legacy risk sources differ")
    captured = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "risk": candidates[0],
        "source_count": len(sources),
        "sources": sources,
    }
    _write_private(
        output_path,
        (json.dumps(captured, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        expected_owner_uid,
    )
    return captured


def load_captured_risk(
    path: Path,
    *,
    policy_path: Path,
) -> dict[str, Any]:
    captured = _load_object(path, "legacy risk capture")
    if captured.get("schema_version") != CAPTURE_SCHEMA_VERSION:
        raise LiveNodeConfigError("legacy risk capture schema mismatch")
    policy = load_risk_policy(policy_path)
    return _validate_migration_risk(
        captured.get("risk"),
        ceiling=policy["max_notional_ceiling_usdt"],
    )


def _policy_target_risk(
    captured_risk: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    target_cap = policy["global_safety_max_notional_usdt"]
    caps = {
        instrument: target_cap
        for instrument in sorted(ALLOWED_INSTRUMENTS)
    }
    caps[DEFAULT_NOTIONAL_KEY] = policy["default_max_notional_usdt"]
    return {
        "max_notional_per_order": caps,
        "max_order_submit_rate": captured_risk["max_order_submit_rate"],
        "max_order_modify_rate": captured_risk["max_order_modify_rate"],
    }


def _read_secure_config(
    path: Path,
    *,
    expected_owner_uid: int,
) -> tuple[bytes, os.stat_result, dict[str, Any]]:
    if path.is_symlink():
        raise LiveNodeConfigError(f"node config cannot be a symlink: {path}")
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise LiveNodeConfigError(f"node config is missing: {path}") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise LiveNodeConfigError(f"node config must be regular: {path}")
    if file_stat.st_uid != expected_owner_uid:
        raise LiveNodeConfigError(f"node config owner mismatch: {path}")
    payload = path.read_bytes()
    try:
        config = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise LiveNodeConfigError(f"node config is invalid JSON: {path}") from exc
    if not isinstance(config, dict):
        raise LiveNodeConfigError(f"node config root must be an object: {path}")
    return payload, file_stat, config


def _atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(mode))
        if os.geteuid() == 0:
            os.chown(temporary, uid, gid)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_private(path: Path, payload: bytes, owner_uid: int) -> None:
    _atomic_write(
        path,
        payload,
        mode=0o600,
        uid=owner_uid,
        gid=owner_uid,
    )


def _ensure_artifact_directory(path: Path, expected_owner_uid: int) -> None:
    if path.is_symlink():
        raise LiveNodeConfigError(
            f"config artifact directory cannot be a symlink: {path}"
        )
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    file_stat = path.stat()
    if not stat.S_ISDIR(file_stat.st_mode):
        raise LiveNodeConfigError(
            f"config artifact directory must be a directory: {path}"
        )
    if file_stat.st_uid != expected_owner_uid:
        raise LiveNodeConfigError(
            f"config artifact directory owner mismatch: {path}"
        )


def _read_immutable_artifact(
    path: Path,
    *,
    expected_owner_uid: int,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    flags |= no_follow
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LiveNodeConfigError(
            f"cannot open config artifact: {path}"
        ) from exc
    try:
        file_stat = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read()
    finally:
        os.close(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        raise LiveNodeConfigError(
            f"config artifact must be regular: {path}"
        )
    if file_stat.st_uid != expected_owner_uid:
        raise LiveNodeConfigError(
            f"config artifact owner mismatch: {path}"
        )
    if stat.S_IMODE(file_stat.st_mode) & 0o222:
        raise LiveNodeConfigError(
            f"config artifact must be read-only: {path}"
        )
    if path.is_symlink():
        raise LiveNodeConfigError(
            f"config artifact cannot be a symlink: {path}"
        )
    try:
        current_stat = path.stat()
    except OSError as exc:
        raise LiveNodeConfigError(
            f"config artifact path disappeared: {path}"
        ) from exc
    opened_inode = (file_stat.st_dev, file_stat.st_ino)
    current_inode = (current_stat.st_dev, current_stat.st_ino)
    if current_inode != opened_inode:
        raise LiveNodeConfigError(
            f"config artifact path changed while reading: {path}"
        )
    return payload, file_stat


def _publish_immutable_artifact(
    path: Path,
    payload: bytes,
    *,
    expected_owner_uid: int,
    gid: int,
) -> os.stat_result:
    _ensure_artifact_directory(path.parent, expected_owner_uid)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    published = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o440)
        if os.geteuid() == 0:
            os.chown(temporary, expected_owner_uid, gid)
        try:
            os.link(temporary, path)
            published = True
        except FileExistsError:
            published = False
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()

    existing_payload, file_stat = _read_immutable_artifact(
        path,
        expected_owner_uid=expected_owner_uid,
    )
    if existing_payload != payload:
        raise LiveNodeConfigError(
            f"config artifact content-address collision: {path}"
        )
    if published and file_stat.st_nlink < 1:
        raise LiveNodeConfigError(
            f"config artifact publication is invalid: {path}"
        )
    return file_stat


def _config_with_captured_risk(
    *,
    account_id: str,
    source_config_path: Path,
    policy_path: Path,
    legacy_risk_path: Path,
    expected_owner_uid: int,
) -> tuple[bytes, os.stat_result, dict[str, Any]]:
    if account_id not in SUPPORTED_ACCOUNTS:
        raise LiveNodeConfigError(f"unsupported account_id: {account_id}")
    _, file_stat, config = _read_secure_config(
        source_config_path,
        expected_owner_uid=expected_owner_uid,
    )
    if config.get("account_id") != account_id:
        raise LiveNodeConfigError(
            f"node config account identity mismatch: {source_config_path}"
        )
    policy = load_risk_policy(policy_path)
    legacy_risk = load_captured_risk(
        legacy_risk_path,
        policy_path=policy_path,
    )
    target_risk = _policy_target_risk(legacy_risk, policy)
    current_risk = config.get("risk")
    if current_risk is not None:
        validated_current = _validate_migration_risk(
            current_risk,
            ceiling=policy["max_notional_ceiling_usdt"],
            allow_default=True,
        )
        if validated_current not in (legacy_risk, target_risk):
            raise LiveNodeConfigError(
                "node config risk differs from captured or target risk: "
                f"{source_config_path}"
            )
    updated = dict(config)
    updated["risk"] = target_risk
    payload = (
        json.dumps(updated, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return payload, file_stat, updated


def verify_target_config_artifact(
    record: dict[str, Any],
    *,
    expected_owner_uid: int,
) -> dict[str, Any]:
    if record.get("schema_version") != ARTIFACT_RECORD_SCHEMA_VERSION:
        raise LiveNodeConfigError("config artifact record schema mismatch")
    account_id = str(record.get("account_id") or "")
    if account_id not in SUPPORTED_ACCOUNTS:
        raise LiveNodeConfigError("config artifact record account is invalid")
    if record.get("mount_target") != CONFIG_MOUNT_TARGET:
        raise LiveNodeConfigError(
            "config artifact record mount target mismatch"
        )
    raw_path = str(record.get("host_path") or "")
    path = Path(raw_path)
    if not path.is_absolute():
        raise LiveNodeConfigError(
            "config artifact record host path must be absolute"
        )
    payload, file_stat = _read_immutable_artifact(
        path,
        expected_owner_uid=expected_owner_uid,
    )
    digest = _sha256_bytes(payload)
    if digest != record.get("sha256"):
        raise LiveNodeConfigError(
            f"config artifact hash mismatch: {path}"
        )
    if path.name != f"{digest}.json":
        raise LiveNodeConfigError(
            f"config artifact path is not content-addressed: {path}"
        )
    try:
        config = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise LiveNodeConfigError(
            f"config artifact is invalid JSON: {path}"
        ) from exc
    if not isinstance(config, dict):
        raise LiveNodeConfigError(
            f"config artifact root must be an object: {path}"
        )
    if config.get("account_id") != account_id:
        raise LiveNodeConfigError(
            f"config artifact account identity mismatch: {path}"
        )
    normalized_digest = normalized_config_sha256(config)
    if normalized_digest != record.get("normalized_sha256"):
        raise LiveNodeConfigError(
            f"config artifact normalized hash mismatch: {path}"
        )
    if file_stat.st_size != record.get("size"):
        raise LiveNodeConfigError(
            f"config artifact size mismatch: {path}"
        )
    return {
        "schema_version": ARTIFACT_RECORD_SCHEMA_VERSION,
        "account_id": account_id,
        "host_path": str(path),
        "mount_target": CONFIG_MOUNT_TARGET,
        "sha256": digest,
        "normalized_sha256": normalized_digest,
        "size": file_stat.st_size,
        "mode": stat.S_IMODE(file_stat.st_mode),
        "uid": file_stat.st_uid,
        "gid": file_stat.st_gid,
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
    }


def _write_artifact_record(
    path: Path,
    record: dict[str, Any],
    *,
    expected_owner_uid: int,
) -> None:
    payload = (
        json.dumps(record, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if path.is_symlink():
        raise LiveNodeConfigError(
            f"config artifact record cannot be a symlink: {path}"
        )
    if path.exists():
        existing = path.read_bytes()
        if existing != payload:
            raise LiveNodeConfigError(
                f"config artifact record already differs: {path}"
            )
        return
    _write_private(path, payload, expected_owner_uid)


def prepare_target_config_artifact(
    *,
    account_id: str,
    source_config_path: Path,
    policy_path: Path,
    legacy_risk_path: Path,
    artifact_root: Path,
    record_output_path: Path,
    expected_owner_uid: int,
) -> dict[str, Any]:
    payload, source_stat, updated = _config_with_captured_risk(
        account_id=account_id,
        source_config_path=source_config_path,
        policy_path=policy_path,
        legacy_risk_path=legacy_risk_path,
        expected_owner_uid=expected_owner_uid,
    )
    digest = _sha256_bytes(payload)
    account_root = artifact_root.resolve() / account_id
    artifact_path = account_root / f"{digest}.json"
    _publish_immutable_artifact(
        artifact_path,
        payload,
        expected_owner_uid=expected_owner_uid,
        gid=source_stat.st_gid,
    )
    artifact_payload, artifact_stat = _read_immutable_artifact(
        artifact_path,
        expected_owner_uid=expected_owner_uid,
    )
    record = {
        "schema_version": ARTIFACT_RECORD_SCHEMA_VERSION,
        "account_id": account_id,
        "host_path": str(artifact_path),
        "mount_target": CONFIG_MOUNT_TARGET,
        "sha256": _sha256_bytes(artifact_payload),
        "normalized_sha256": normalized_config_sha256(updated),
        "size": artifact_stat.st_size,
        "mode": stat.S_IMODE(artifact_stat.st_mode),
        "uid": artifact_stat.st_uid,
        "gid": artifact_stat.st_gid,
    }
    verified = verify_target_config_artifact(
        record,
        expected_owner_uid=expected_owner_uid,
    )
    stable_record = {
        key: value
        for key, value in verified.items()
        if key not in {"device", "inode"}
    }
    _write_artifact_record(
        record_output_path,
        stable_record,
        expected_owner_uid=expected_owner_uid,
    )
    return verified


def apply_policy(
    *,
    policy_path: Path,
    legacy_risk_path: Path,
    configs: dict[str, Path],
    backup_dir: Path,
    expected_owner_uid: int,
) -> str:
    if not configs or not set(configs).issubset(SUPPORTED_ACCOUNTS):
        raise LiveNodeConfigError(
            "configs must be a non-empty supported account collection"
        )
    if backup_dir.exists():
        raise LiveNodeConfigError(f"config backup path exists: {backup_dir}")
    policy = load_risk_policy(policy_path)
    legacy_risk = load_captured_risk(
        legacy_risk_path,
        policy_path=policy_path,
    )
    target_risk = _policy_target_risk(legacy_risk, policy)
    originals: dict[str, tuple[bytes, os.stat_result, dict[str, Any]]] = {}
    updated_payloads: dict[str, bytes] = {}
    normalized_hashes: dict[str, str] = {}
    for account_id, path in sorted(configs.items()):
        payload, file_stat, config = _read_secure_config(
            path,
            expected_owner_uid=expected_owner_uid,
        )
        if config.get("account_id") != account_id:
            raise LiveNodeConfigError(
                f"node config account identity mismatch: {path}"
            )
        current_risk = config.get("risk")
        if current_risk is not None:
            validated_current = _validate_migration_risk(
                current_risk,
                ceiling=policy["max_notional_ceiling_usdt"],
                allow_default=True,
            )
            if validated_current not in (legacy_risk, target_risk):
                raise LiveNodeConfigError(
                    "node config risk differs from captured or target risk: "
                    f"{path}"
                )
        originals[account_id] = (payload, file_stat, config)
        updated = dict(config)
        updated["risk"] = target_risk
        updated_payloads[account_id] = (
            json.dumps(updated, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        normalized_hashes[account_id] = normalized_config_sha256(updated)
    normalized_contract_sha256 = normalized_config_contract_sha256(
        normalized_hashes
    )

    backup_dir.mkdir(parents=True, mode=0o700)
    if os.geteuid() == 0:
        os.chown(backup_dir, expected_owner_uid, expected_owner_uid)
    entries = []
    for account_id, path in sorted(configs.items()):
        payload, file_stat, _ = originals[account_id]
        backup_name = f"{account_id}.json"
        _write_private(backup_dir / backup_name, payload, expected_owner_uid)
        entries.append(
            {
                "account_id": account_id,
                "source_path": str(path.resolve()),
                "backup_file": backup_name,
                "sha256": _sha256_bytes(payload),
                "mode": stat.S_IMODE(file_stat.st_mode),
                "uid": file_stat.st_uid,
                "gid": file_stat.st_gid,
            }
        )
    manifest_path = backup_dir / "manifest.json"
    manifest = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "status": "prepared",
        "normalized_config_sha256": normalized_contract_sha256,
        "normalized_config_sha256_by_account": normalized_hashes,
        "entries": entries,
    }
    _write_private(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        expected_owner_uid,
    )

    try:
        for account_id, path in sorted(configs.items()):
            _, file_stat, _ = originals[account_id]
            _atomic_write(
                path,
                updated_payloads[account_id],
                mode=file_stat.st_mode,
                uid=file_stat.st_uid,
                gid=file_stat.st_gid,
            )
        for account_id, path in sorted(configs.items()):
            current = _load_object(path, "updated node config")
            if normalized_config_sha256(current) != normalized_hashes[account_id]:
                raise LiveNodeConfigError(
                    f"updated node config hash mismatch: {path}"
                )
        manifest["status"] = "applied"
        _write_private(
            manifest_path,
            (
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8"),
            expected_owner_uid,
        )
    except Exception:
        rollback(backup_dir=backup_dir, expected_owner_uid=expected_owner_uid)
        raise

    return str(manifest["normalized_config_sha256"])


def rollback(*, backup_dir: Path, expected_owner_uid: int) -> None:
    manifest_path = backup_dir / "manifest.json"
    manifest = _load_object(manifest_path, "config backup manifest")
    if manifest.get("schema_version") not in {
        LEGACY_BACKUP_SCHEMA_VERSION,
        BACKUP_SCHEMA_VERSION,
    }:
        raise LiveNodeConfigError("config backup manifest schema mismatch")
    entries = manifest.get("entries")
    if (
        not isinstance(entries, list)
        or not entries
        or len(entries) > len(SUPPORTED_ACCOUNTS)
    ):
        raise LiveNodeConfigError("config backup manifest entries are invalid")
    account_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise LiveNodeConfigError("config backup entry is invalid")
        account_id = str(entry.get("account_id") or "")
        if (
            account_id not in SUPPORTED_ACCOUNTS
            or account_id in account_ids
        ):
            raise LiveNodeConfigError("config backup account is invalid")
        account_ids.add(account_id)
        backup_file = str(entry.get("backup_file") or "")
        if backup_file != f"{account_id}.json":
            raise LiveNodeConfigError("config backup file name is invalid")
        source = Path(str(entry.get("source_path") or ""))
        backup = backup_dir / backup_file
        payload = backup.read_bytes()
        if _sha256_bytes(payload) != entry.get("sha256"):
            raise LiveNodeConfigError(f"config backup hash mismatch: {backup}")
        _atomic_write(
            source,
            payload,
            mode=int(entry["mode"]),
            uid=int(entry["uid"]),
            gid=int(entry["gid"]),
        )
    manifest["status"] = "rolled_back"
    _write_private(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        expected_owner_uid,
    )


def _parse_configs(values: list[str]) -> dict[str, Path]:
    configs: dict[str, Path] = {}
    for raw in values:
        account_id, separator, path = raw.partition("=")
        if not separator or account_id in configs or not path:
            raise LiveNodeConfigError(f"invalid --config value: {raw}")
        configs[account_id] = Path(path)
    return configs


def _parse_containers(values: list[str]) -> dict[str, str]:
    containers = {}
    for raw in values:
        account_id, separator, container = raw.partition("=")
        if not separator or account_id in containers or not container:
            raise LiveNodeConfigError(f"invalid --container value: {raw}")
        containers[account_id] = container
    return containers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--policy", type=Path, required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--config", action="append", required=True)
    capture_parser.add_argument("--container", action="append", required=True)
    capture_parser.add_argument("--expected-owner-uid", type=int, default=0)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--policy", type=Path, required=True)
    apply_parser.add_argument("--legacy-risk", type=Path, required=True)
    apply_parser.add_argument("--backup-dir", type=Path, required=True)
    apply_parser.add_argument("--config", action="append", required=True)
    apply_parser.add_argument("--expected-owner-uid", type=int, default=0)
    prepare_parser = subparsers.add_parser("prepare-target")
    prepare_parser.add_argument(
        "--account-id",
        choices=sorted(SUPPORTED_ACCOUNTS),
        required=True,
    )
    prepare_parser.add_argument(
        "--source-config",
        type=Path,
        required=True,
    )
    prepare_parser.add_argument("--policy", type=Path, required=True)
    prepare_parser.add_argument("--legacy-risk", type=Path, required=True)
    prepare_parser.add_argument("--artifact-root", type=Path, required=True)
    prepare_parser.add_argument("--record-output", type=Path, required=True)
    prepare_parser.add_argument(
        "--expected-owner-uid",
        type=int,
        default=0,
    )
    verify_parser = subparsers.add_parser("verify-target")
    verify_parser.add_argument("--record", type=Path, required=True)
    verify_parser.add_argument(
        "--expected-owner-uid",
        type=int,
        default=0,
    )
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--backup-dir", type=Path, required=True)
    rollback_parser.add_argument("--expected-owner-uid", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            captured = capture_legacy_risk(
                policy_path=args.policy,
                configs=_parse_configs(args.config),
                containers=_parse_containers(args.container),
                output_path=args.output,
                expected_owner_uid=args.expected_owner_uid,
            )
            print(f"legacy_risk_source_count={captured['source_count']}")
        elif args.command == "apply":
            digest = apply_policy(
                policy_path=args.policy,
                legacy_risk_path=args.legacy_risk,
                configs=_parse_configs(args.config),
                backup_dir=args.backup_dir,
                expected_owner_uid=args.expected_owner_uid,
            )
            print(f"normalized_config_sha256={digest}")
        elif args.command == "prepare-target":
            record = prepare_target_config_artifact(
                account_id=args.account_id,
                source_config_path=args.source_config,
                policy_path=args.policy,
                legacy_risk_path=args.legacy_risk,
                artifact_root=args.artifact_root,
                record_output_path=args.record_output,
                expected_owner_uid=args.expected_owner_uid,
            )
            print(
                f"account_id={record['account_id']} "
                f"config_artifact={record['host_path']} "
                f"config_artifact_sha256={record['sha256']} "
                f"normalized_config_sha256="
                f"{record['normalized_sha256']}"
            )
        elif args.command == "verify-target":
            record = _load_object(
                args.record,
                "config artifact record",
            )
            verified = verify_target_config_artifact(
                record,
                expected_owner_uid=args.expected_owner_uid,
            )
            print(
                f"account_id={verified['account_id']} "
                f"config_artifact_sha256={verified['sha256']} "
                f"config_artifact_inode={verified['inode']}"
            )
        else:
            rollback(
                backup_dir=args.backup_dir,
                expected_owner_uid=args.expected_owner_uid,
            )
            print("live_node_config_rollback=complete")
    except (OSError, LiveNodeConfigError) as exc:
        print(f"FATAL: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
