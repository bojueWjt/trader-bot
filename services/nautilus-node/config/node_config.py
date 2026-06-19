from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Optional


class NodeConfigError(ValueError):
    """Invalid per-account node configuration."""


class CredentialResolutionError(NodeConfigError):
    """A required secret was not present in its configured env var or file."""

    def __init__(self, label: str, sources: list[str]) -> None:
        self.label = label
        self.sources = sources
        self.resolved_value_hint = "missing required secret sources: " + ", ".join(sources)
        super().__init__(f"{label} missing; checked {', '.join(sources)}")


@dataclass(frozen=True)
class LoadedCredentials:
    api_key: str
    api_secret: str


@dataclass(frozen=True)
class RedisNodeConfig:
    url: str
    key_prefix: str


@dataclass(frozen=True)
class BinanceNodeConfig:
    account_type: str
    environment: str
    credentials: LoadedCredentials
    credential_source: str


@dataclass(frozen=True)
class ControlPlaneNodeConfig:
    base_url: str
    token: str
    auth_source: str
    heartbeat_timeout: timedelta
    snapshot_stale_after: timedelta


@dataclass(frozen=True)
class NodeConfig:
    account_id: str
    node_id: str
    trader_id: str
    instance_id: str
    redis: RedisNodeConfig
    binance: BinanceNodeConfig
    control_plane: ControlPlaneNodeConfig

    @property
    def route_prefix(self) -> str:
        return f"{self.redis.key_prefix}:routes:{self.account_id}:{self.node_id}"


def load_node_config(path: str | Path) -> NodeConfig:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise NodeConfigError("node config root must be an object")

    account_id = _required_str(raw, "account_id")
    node_id = _required_str(raw, "node_id")
    trader_id = _required_str(raw, "trader_id")
    instance_id = _required_str(raw, "instance_id")

    redis_raw = _required_mapping(raw, "redis")
    binance_raw = _required_mapping(raw, "binance")
    control_plane_raw = _required_mapping(raw, "control_plane")

    redis = RedisNodeConfig(
        url=_required_str(redis_raw, "url"),
        key_prefix=_required_str(redis_raw, "key_prefix"),
    )

    environment = _required_str(binance_raw, "environment").lower()
    if environment not in {"sandbox", "testnet"}:
        raise NodeConfigError(
            f"binance.environment must be sandbox/testnet for B-01, got {environment!r}"
        )
    account_type = _required_str(binance_raw, "account_type")
    if account_type not in {"USDT-M", "USDT_FUTURES", "USDT-FUTURES"}:
        raise NodeConfigError(
            "binance.account_type must identify Binance USDT-M futures"
        )

    api_key, api_key_source = _resolve_secret("binance.api_key", binance_raw["api_key"])
    api_secret, api_secret_source = _resolve_secret(
        "binance.api_secret", binance_raw["api_secret"]
    )
    binance = BinanceNodeConfig(
        account_type=account_type,
        environment=environment,
        credentials=LoadedCredentials(api_key=api_key, api_secret=api_secret),
        credential_source=f"{api_key_source},{api_secret_source}",
    )

    token, token_source = _resolve_secret(
        "control_plane.token", control_plane_raw["token"]
    )
    control_plane = ControlPlaneNodeConfig(
        base_url=_required_str(control_plane_raw, "base_url").rstrip("/"),
        token=token,
        auth_source=token_source,
        heartbeat_timeout=timedelta(
            seconds=_required_positive_number(
                control_plane_raw, "heartbeat_timeout_seconds"
            )
        ),
        snapshot_stale_after=timedelta(
            seconds=_required_positive_number(
                control_plane_raw, "snapshot_stale_after_seconds"
            )
        ),
    )

    _reject_overlapping_identity(account_id, node_id, trader_id, instance_id, redis)

    return NodeConfig(
        account_id=account_id,
        node_id=node_id,
        trader_id=trader_id,
        instance_id=instance_id,
        redis=redis,
        binance=binance,
        control_plane=control_plane,
    )


def _resolve_secret(label: str, raw: Any) -> tuple[str, str]:
    if not isinstance(raw, Mapping):
        raise NodeConfigError(f"{label} must be an object with env or file")

    sources: list[str] = []
    env_name = raw.get("env")
    if env_name is not None:
        if not isinstance(env_name, str) or not env_name:
            raise NodeConfigError(f"{label}.env must be a non-empty string")
        sources.append(f"env:{env_name}")
        value = os.environ.get(env_name)
        if value:
            return value, f"env:{env_name}"

    file_name = raw.get("file")
    if file_name is not None:
        if not isinstance(file_name, str) or not file_name:
            raise NodeConfigError(f"{label}.file must be a non-empty string")
        sources.append(f"file:{file_name}")
        secret_path = Path(file_name)
        if secret_path.exists():
            value = secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value, f"file:{secret_path}"

    if not sources:
        raise NodeConfigError(f"{label} must configure at least env or file")
    raise CredentialResolutionError(label, sources)


def _required_mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise NodeConfigError(f"{key} must be an object")
    return value


def _required_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise NodeConfigError(f"{key} must be a non-empty string")
    return value


def _required_positive_number(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or value <= 0:
        raise NodeConfigError(f"{key} must be a positive number")
    return float(value)


def _reject_overlapping_identity(
    account_id: str,
    node_id: str,
    trader_id: str,
    instance_id: str,
    redis: RedisNodeConfig,
) -> None:
    fields = {
        "account_id": account_id,
        "node_id": node_id,
        "trader_id": trader_id,
        "instance_id": instance_id,
        "redis.key_prefix": redis.key_prefix,
    }
    if len(set(fields.values())) != len(fields):
        raise NodeConfigError(
            "account_id, node_id, trader_id, instance_id, and redis.key_prefix must be distinct"
        )
    expected_prefix = f"nautilus:{account_id}:"
    if not redis.key_prefix.startswith(expected_prefix):
        raise NodeConfigError(
            f"redis.key_prefix must start with {expected_prefix!r} for account isolation"
        )
