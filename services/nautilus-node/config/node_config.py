from __future__ import annotations

import json
import math
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
class CacheNodeConfig:
    enabled: bool
    backend: str
    encoding: str


@dataclass(frozen=True)
class MessageBusNodeConfig:
    enabled: bool
    backend: str
    encoding: str


@dataclass(frozen=True)
class ReconciliationNodeConfig:
    startup: bool
    continuous: bool
    lookback_mins: int
    interval_mins: int


@dataclass(frozen=True)
class BinanceNodeConfig:
    account_type: str
    environment: str
    credentials: LoadedCredentials
    credential_source: str


@dataclass(frozen=True)
class ControlPlaneSessionNodeConfig:
    command_delivery_capacity: int = 128
    command_ack_capacity: int = 256
    intent_delivery_capacity: int = 256
    execution_event_capacity: int = 1024
    queue_degraded_ratio: float = 0.8
    retry_budget: int = 3
    retry_base_delay_seconds: float = 0.05
    retry_max_delay_seconds: float = 1.0
    retry_jitter_ratio: float = 0.2
    circuit_reset_seconds: float = 5.0
    operation_timeout_seconds: float = 15.0
    shutdown_timeout_seconds: float = 1.0


@dataclass(frozen=True)
class ControlPlaneNodeConfig:
    base_url: str
    token: str
    auth_source: str
    heartbeat_timeout: timedelta
    snapshot_stale_after: timedelta
    session: ControlPlaneSessionNodeConfig


@dataclass(frozen=True)
class NodeConfig:
    account_id: str
    node_id: str
    trader_id: str
    instance_id: str
    redis: RedisNodeConfig
    cache: CacheNodeConfig
    message_bus: MessageBusNodeConfig
    reconciliation: ReconciliationNodeConfig
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
    cache = _load_cache_config(raw.get("cache"))
    message_bus = _load_message_bus_config(raw.get("message_bus"))
    reconciliation = _load_reconciliation_config(raw.get("reconciliation"))

    environment = _required_str(binance_raw, "environment").lower()
    if environment not in {"sandbox", "testnet", "live"}:
        raise NodeConfigError(
            f"binance.environment must be sandbox/testnet/live, got {environment!r}"
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
        session=_load_control_plane_session_config(
            control_plane_raw.get("session")
        ),
    )

    _reject_overlapping_identity(account_id, node_id, trader_id, instance_id, redis)

    return NodeConfig(
        account_id=account_id,
        node_id=node_id,
        trader_id=trader_id,
        instance_id=instance_id,
        redis=redis,
        cache=cache,
        message_bus=message_bus,
        reconciliation=reconciliation,
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


def _load_cache_config(raw: Any) -> CacheNodeConfig:
    if raw is None:
        return CacheNodeConfig(enabled=True, backend="redis", encoding="msgpack")
    if not isinstance(raw, Mapping):
        raise NodeConfigError("cache must be an object")
    enabled = _optional_bool(raw, "enabled", True)
    backend = _optional_str(raw, "backend", "redis")
    encoding = _optional_str(raw, "encoding", "msgpack")
    if not enabled:
        raise NodeConfigError("cache.enabled must stay true for B-06")
    if backend != "redis":
        raise NodeConfigError("cache.backend must be redis for B-06")
    if encoding not in {"msgpack", "json"}:
        raise NodeConfigError("cache.encoding must be msgpack or json")
    return CacheNodeConfig(enabled=enabled, backend=backend, encoding=encoding)


def _load_message_bus_config(raw: Any) -> MessageBusNodeConfig:
    if raw is None:
        return MessageBusNodeConfig(enabled=True, backend="redis", encoding="msgpack")
    if not isinstance(raw, Mapping):
        raise NodeConfigError("message_bus must be an object")
    enabled = _optional_bool(raw, "enabled", True)
    backend = _optional_str(raw, "backend", "redis")
    encoding = _optional_str(raw, "encoding", "msgpack")
    if not enabled:
        raise NodeConfigError("message_bus.enabled must stay true for B-06")
    if backend != "redis":
        raise NodeConfigError("message_bus.backend must be redis for B-06")
    if encoding not in {"msgpack", "json"}:
        raise NodeConfigError("message_bus.encoding must be msgpack or json")
    return MessageBusNodeConfig(enabled=enabled, backend=backend, encoding=encoding)


def _load_reconciliation_config(raw: Any) -> ReconciliationNodeConfig:
    if raw is None:
        return ReconciliationNodeConfig(
            startup=True,
            continuous=True,
            lookback_mins=60,
            interval_mins=5,
        )
    if not isinstance(raw, Mapping):
        raise NodeConfigError("reconciliation must be an object")
    startup = _optional_bool(raw, "startup", True)
    continuous = _optional_bool(raw, "continuous", True)
    lookback_mins = _optional_positive_int(raw, "lookback_mins", 60)
    interval_mins = _optional_positive_int(raw, "interval_mins", 5)
    if not startup:
        raise NodeConfigError("reconciliation.startup must stay enabled for B-06")
    if not continuous:
        raise NodeConfigError("reconciliation.continuous must stay enabled for B-06")
    if lookback_mins < 60:
        raise NodeConfigError("reconciliation.lookback_mins must be at least 60")
    return ReconciliationNodeConfig(
        startup=startup,
        continuous=continuous,
        lookback_mins=lookback_mins,
        interval_mins=interval_mins,
    )


def _load_control_plane_session_config(
    raw: Any,
) -> ControlPlaneSessionNodeConfig:
    if raw is None:
        return ControlPlaneSessionNodeConfig()
    if not isinstance(raw, Mapping):
        raise NodeConfigError("control_plane.session must be an object")

    retry_base_delay_seconds = _optional_positive_number(
        raw,
        "retry_base_delay_seconds",
        0.05,
    )
    retry_max_delay_seconds = _optional_positive_number(
        raw,
        "retry_max_delay_seconds",
        1.0,
    )
    if retry_max_delay_seconds < retry_base_delay_seconds:
        raise NodeConfigError(
            "control_plane.session.retry_max_delay_seconds must be at "
            "least retry_base_delay_seconds"
        )

    return ControlPlaneSessionNodeConfig(
        command_delivery_capacity=_optional_positive_int(
            raw,
            "command_delivery_capacity",
            128,
        ),
        command_ack_capacity=_optional_positive_int(
            raw,
            "command_ack_capacity",
            256,
        ),
        intent_delivery_capacity=_optional_positive_int(
            raw,
            "intent_delivery_capacity",
            256,
        ),
        execution_event_capacity=_optional_positive_int(
            raw,
            "execution_event_capacity",
            1024,
        ),
        queue_degraded_ratio=_optional_ratio(
            raw,
            "queue_degraded_ratio",
            0.8,
            allow_zero=False,
        ),
        retry_budget=_optional_positive_int(
            raw,
            "retry_budget",
            3,
        ),
        retry_base_delay_seconds=retry_base_delay_seconds,
        retry_max_delay_seconds=retry_max_delay_seconds,
        retry_jitter_ratio=_optional_ratio(
            raw,
            "retry_jitter_ratio",
            0.2,
            allow_zero=True,
        ),
        circuit_reset_seconds=_optional_positive_number(
            raw,
            "circuit_reset_seconds",
            5.0,
        ),
        operation_timeout_seconds=_optional_positive_number(
            raw,
            "operation_timeout_seconds",
            15.0,
        ),
        shutdown_timeout_seconds=_optional_positive_number(
            raw,
            "shutdown_timeout_seconds",
            1.0,
        ),
    )


def _optional_bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise NodeConfigError(f"{key} must be a boolean")
    return value


def _optional_str(raw: Mapping[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise NodeConfigError(f"{key} must be a non-empty string")
    return value


def _optional_positive_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise NodeConfigError(f"{key} must be a positive integer")
    return value


def _optional_positive_number(
    raw: Mapping[str, Any],
    key: str,
    default: float,
) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NodeConfigError(f"{key} must be a positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise NodeConfigError(f"{key} must be a positive number")
    return number


def _optional_ratio(
    raw: Mapping[str, Any],
    key: str,
    default: float,
    *,
    allow_zero: bool,
) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NodeConfigError(f"{key} must be a ratio below 1")
    ratio = float(value)
    lower_bound_valid = ratio >= 0
    if not allow_zero:
        lower_bound_valid = ratio > 0
    if not math.isfinite(ratio) or not lower_bound_valid or ratio >= 1:
        raise NodeConfigError(f"{key} must be a ratio below 1")
    return ratio


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
