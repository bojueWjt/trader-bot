#!/usr/bin/env python3
"""Validate release-bound node runtime resources and A/B config identity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_ROOT.parent
for candidate in (SCRIPT_ROOT.parent, SCRIPT_ROOT):
    package_root = candidate / "packages" / "runtime_resource_contract"
    if package_root.is_dir():
        REPO_ROOT = candidate
        break
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packages.runtime_resource_contract import (  # noqa: E402
    ABSENT,
    SCHEMA_VERSION as RUNTIME_RESOURCES_SCHEMA_VERSION,
    RuntimeResourceContractError,
    RuntimeResourcePolicy,
    parse_runtime_resources,
)

CONFIG_MOUNT_TARGET = "/cfg.json"
REQUIRED_ACCOUNTS = {"account-a", "account-b"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTITY_PATTERNS = (
    re.compile(r"trader[-_]?account[-_]?[ab]", re.IGNORECASE),
    re.compile(r"instance[-_]?account[-_]?[ab]", re.IGNORECASE),
    re.compile(r"account[-_]?[ab]", re.IGNORECASE),
    re.compile(r"node[-_]?[ab]", re.IGNORECASE),
)

__all__ = (
    "CONFIG_MOUNT_TARGET",
    "RUNTIME_RESOURCES_SCHEMA_VERSION",
    "ReleaseManifestError",
    "build_node_config_artifacts",
    "canonical_json_bytes",
    "node_config_sha256",
    "node_config_value_sha256",
    "normalize_node_config",
    "runtime_resources_sha256",
)


class ReleaseManifestError(ValueError):
    """Release-bound node configuration is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _normalize_identity_string(value: str) -> str:
    normalized = value
    for pattern in IDENTITY_PATTERNS:
        normalized = pattern.sub("<node-identity>", normalized)
    return normalized


def normalize_node_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize_node_config(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [normalize_node_config(item) for item in value]
    if isinstance(value, str):
        return _normalize_identity_string(value)
    return value


def node_config_sha256(path: Path) -> str:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(
            f"cannot read node config as JSON: {path}"
        ) from exc
    if not isinstance(config, dict):
        raise ReleaseManifestError(
            f"node config root must be an object: {path}"
        )
    return node_config_value_sha256(config)


def node_config_value_sha256(config: dict[str, Any]) -> str:
    normalized = normalize_node_config(config)
    if not isinstance(normalized, dict):
        raise ReleaseManifestError("node config root must be an object")
    if "runtime_resources" in config:
        legacy_session = _legacy_control_plane_session(config)
        resources = _validated_runtime_resources(
            config["runtime_resources"],
            label="runtime_resources",
            legacy_control_plane_session=legacy_session,
        )
        normalized["runtime_resources"] = resources
        control_plane = normalized.get("control_plane")
        if isinstance(control_plane, dict) and "session" in control_plane:
            control_plane["session"] = resources[
                "control_plane_session"
            ]
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def _validated_runtime_resources(
    raw: Any,
    *,
    label: str,
    legacy_control_plane_session: Any = ABSENT,
) -> dict[str, Any]:
    try:
        parsed = parse_runtime_resources(
            raw,
            policy=RuntimeResourcePolicy.LIVE_STRICT,
            legacy_session=legacy_control_plane_session,
        )
    except RuntimeResourceContractError as exc:
        path = exc.path
        if label != "runtime_resources":
            path = path.replace("runtime_resources", label, 1)
        raise ReleaseManifestError(f"{path}: {exc.detail}") from exc
    return parsed.to_dict()


def runtime_resources_sha256(
    value: dict[str, Any],
    *,
    legacy_control_plane_session: Any = ABSENT,
) -> str:
    validated = _validated_runtime_resources(
        value,
        label="runtime_resources",
        legacy_control_plane_session=legacy_control_plane_session,
    )
    return hashlib.sha256(canonical_json_bytes(validated)).hexdigest()


def _legacy_control_plane_session(config: dict[str, Any]) -> Any:
    control_plane = config.get("control_plane")
    if not isinstance(control_plane, dict):
        return ABSENT
    if "session" not in control_plane:
        return ABSENT
    return control_plane["session"]


def _read_node_config(
    path: Path,
) -> tuple[bytes, dict[str, Any], os.stat_result]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReleaseManifestError(
            f"cannot open node config artifact: {path}"
        ) from exc
    try:
        file_stat = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read()
        config = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ReleaseManifestError(
            f"cannot read node config artifact: {path}"
        ) from exc
    finally:
        os.close(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        raise ReleaseManifestError(
            f"node config artifact must be regular: {path}"
        )
    if stat.S_IMODE(file_stat.st_mode) & 0o222:
        raise ReleaseManifestError(
            f"node config artifact must be read-only: {path}"
        )
    if path.is_symlink():
        raise ReleaseManifestError(
            f"node config artifact cannot be a symlink: {path}"
        )
    if not isinstance(config, dict):
        raise ReleaseManifestError(
            f"node config artifact root must be an object: {path}"
        )
    try:
        current_stat = path.stat()
    except OSError as exc:
        raise ReleaseManifestError(
            f"node config artifact path disappeared: {path}"
        ) from exc
    opened_inode = (file_stat.st_dev, file_stat.st_ino)
    current_inode = (current_stat.st_dev, current_stat.st_ino)
    if current_inode != opened_inode:
        raise ReleaseManifestError(
            f"node config artifact path changed while reading: {path}"
        )
    return payload, config, file_stat


def build_node_config_artifacts(
    specs: dict[str, tuple[str, Path]],
) -> list[dict[str, Any]]:
    if set(specs) != REQUIRED_ACCOUNTS:
        raise ReleaseManifestError(
            "exactly account-a and account-b config artifacts are required"
        )
    entries = []
    for account_id, raw_spec in sorted(specs.items()):
        container, raw_path = raw_spec
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
            raise ReleaseManifestError(
                f"invalid config artifact container: {container}"
            )
        if raw_path.is_symlink():
            raise ReleaseManifestError(
                f"node config artifact cannot be a symlink: {raw_path}"
            )
        path = raw_path.resolve()
        payload, config, file_stat = _read_node_config(path)
        if config.get("account_id") != account_id:
            raise ReleaseManifestError(
                f"node config artifact account mismatch: {path}"
            )
        node_id = str(config.get("node_id") or "").strip()
        if not node_id:
            raise ReleaseManifestError(
                f"node config artifact node_id is missing: {path}"
            )
        legacy_session = _legacy_control_plane_session(config)
        runtime_resources = _validated_runtime_resources(
            config.get("runtime_resources"),
            label=f"{account_id} runtime_resources",
            legacy_control_plane_session=legacy_session,
        )
        resources_sha256 = runtime_resources_sha256(runtime_resources)
        digest = hashlib.sha256(payload).hexdigest()
        if path.name != f"{digest}.json":
            raise ReleaseManifestError(
                f"node config artifact path is not content-addressed: {path}"
            )
        entries.append(
            {
                "account_id": account_id,
                "container": container,
                "node_id": node_id,
                "host_path": str(path),
                "mount_target": CONFIG_MOUNT_TARGET,
                "sha256": digest,
                "normalized_sha256": node_config_value_sha256(config),
                "runtime_resources": runtime_resources,
                "runtime_resources_sha256": resources_sha256,
                "size": file_stat.st_size,
            }
        )
    return _validated_node_configs(entries)


def _validated_node_configs(
    raw_entries: Any,
) -> list[dict[str, Any]]:
    if not isinstance(raw_entries, list) or len(raw_entries) != 2:
        raise ReleaseManifestError(
            "release manifest node_configs must contain two entries"
        )
    entries = []
    account_ids = set()
    containers = set()
    node_ids = set()
    host_paths = set()
    normalized_hashes = set()
    runtime_resource_hashes = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ReleaseManifestError(
                "release node config entry must be an object"
            )
        account_id = str(raw.get("account_id") or "")
        container = str(raw.get("container") or "")
        node_id = str(raw.get("node_id") or "").strip()
        host_path = str(raw.get("host_path") or "")
        mount_target = str(raw.get("mount_target") or "")
        digest = _require_sha256(
            str(raw.get("sha256") or ""),
            "node config sha256",
        )
        normalized_digest = _require_sha256(
            str(raw.get("normalized_sha256") or ""),
            "node config normalized_sha256",
        )
        runtime_resources = _validated_runtime_resources(
            raw.get("runtime_resources"),
            label=f"{account_id} runtime_resources",
        )
        resources_digest = _require_sha256(
            str(raw.get("runtime_resources_sha256") or ""),
            "node config runtime_resources_sha256",
        )
        if resources_digest != runtime_resources_sha256(runtime_resources):
            raise ReleaseManifestError(
                "node config runtime_resources hash mismatch"
            )
        size = raw.get("size")
        if account_id not in REQUIRED_ACCOUNTS:
            raise ReleaseManifestError(
                f"release node config account is invalid: {account_id}"
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
            raise ReleaseManifestError(
                f"release node config container is invalid: {container}"
            )
        if not node_id or len(node_id) > 255:
            raise ReleaseManifestError(
                f"release node config node_id is invalid: {node_id}"
            )
        if not Path(host_path).is_absolute():
            raise ReleaseManifestError(
                "release node config host_path must be absolute"
            )
        if Path(host_path).name != f"{digest}.json":
            raise ReleaseManifestError(
                "release node config host_path must be content-addressed"
            )
        if Path(host_path).parent.name != account_id:
            raise ReleaseManifestError(
                "release node config host_path must be account-scoped"
            )
        if mount_target != CONFIG_MOUNT_TARGET:
            raise ReleaseManifestError(
                "release node config mount_target must be /cfg.json"
            )
        if not isinstance(size, int) or size <= 0:
            raise ReleaseManifestError(
                "release node config size must be positive"
            )
        if account_id in account_ids:
            raise ReleaseManifestError(
                f"duplicate release node config account: {account_id}"
            )
        if container in containers:
            raise ReleaseManifestError(
                f"duplicate release node config container: {container}"
            )
        if node_id in node_ids:
            raise ReleaseManifestError(
                f"duplicate release node config node_id: {node_id}"
            )
        if host_path in host_paths:
            raise ReleaseManifestError(
                f"duplicate release node config host_path: {host_path}"
            )
        account_ids.add(account_id)
        containers.add(container)
        node_ids.add(node_id)
        host_paths.add(host_path)
        normalized_hashes.add(normalized_digest)
        runtime_resource_hashes.add(resources_digest)
        entries.append(
            {
                "account_id": account_id,
                "container": container,
                "node_id": node_id,
                "host_path": host_path,
                "mount_target": mount_target,
                "sha256": digest,
                "normalized_sha256": normalized_digest,
                "runtime_resources": runtime_resources,
                "runtime_resources_sha256": resources_digest,
                "size": size,
            }
        )
    if account_ids != REQUIRED_ACCOUNTS:
        raise ReleaseManifestError(
            "release node config accounts must be account-a and account-b"
        )
    if len(normalized_hashes) != 1:
        raise ReleaseManifestError(
            "A/B normalized config artifact hashes differ"
        )
    if len(runtime_resource_hashes) != 1:
        raise ReleaseManifestError(
            "A/B runtime resource config hashes differ"
        )
    return sorted(entries, key=lambda item: item["account_id"])


def _require_sha256(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ReleaseManifestError(
            f"{field} must be a lowercase sha256"
        )
    return normalized
