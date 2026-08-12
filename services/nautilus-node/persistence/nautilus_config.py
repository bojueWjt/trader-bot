from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from config.node_config import NodeConfig

DEFAULT_MESSAGE_BUS_AUTOTRIM_MINS = 24 * 60
MESSAGE_BUS_AUTOTRIM_MINS_ENV = "NAUTILUS_MESSAGE_BUS_AUTOTRIM_MINS"
DEFAULT_REDIS_STREAM_MAX_ENTRIES = 100_000
DEFAULT_REDIS_STREAM_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_REDIS_TOTAL_STREAM_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_REDIS_RUNTIME_SAFETY_SCAN_COUNT = 500
DEFAULT_REDIS_RUNTIME_SAFETY_SAMPLE_INTERVAL_SECONDS = 5.0
DEFAULT_REDIS_RUNTIME_SAFETY_CRITICAL_WINDOW_SECONDS = 30.0
DEFAULT_REDIS_RUNTIME_SAFETY_THREAD_JOIN_TIMEOUT_SECONDS = 5.0
REDIS_STREAM_MAX_ENTRIES_ENV = "NAUTILUS_REDIS_STREAM_MAX_ENTRIES"
REDIS_STREAM_MAX_BYTES_ENV = "NAUTILUS_REDIS_STREAM_MAX_BYTES"
REDIS_TOTAL_STREAM_MAX_BYTES_ENV = "NAUTILUS_REDIS_TOTAL_STREAM_MAX_BYTES"
REDIS_RUNTIME_SAFETY_SCAN_COUNT_ENV = (
    "NAUTILUS_REDIS_RUNTIME_SAFETY_SCAN_COUNT"
)
REDIS_RUNTIME_SAFETY_SAMPLE_INTERVAL_SECONDS_ENV = (
    "NAUTILUS_REDIS_RUNTIME_SAFETY_SAMPLE_INTERVAL_SECONDS"
)
REDIS_RUNTIME_SAFETY_CRITICAL_WINDOW_SECONDS_ENV = (
    "NAUTILUS_REDIS_RUNTIME_SAFETY_CRITICAL_WINDOW_SECONDS"
)
REDIS_RUNTIME_SAFETY_THREAD_JOIN_TIMEOUT_SECONDS_ENV = (
    "NAUTILUS_REDIS_RUNTIME_SAFETY_THREAD_JOIN_TIMEOUT_SECONDS"
)
REDIS_RUNTIME_RESOURCE_ENV_NAMES = (
    REDIS_STREAM_MAX_ENTRIES_ENV,
    REDIS_STREAM_MAX_BYTES_ENV,
    REDIS_TOTAL_STREAM_MAX_BYTES_ENV,
    REDIS_RUNTIME_SAFETY_SCAN_COUNT_ENV,
    REDIS_RUNTIME_SAFETY_SAMPLE_INTERVAL_SECONDS_ENV,
    REDIS_RUNTIME_SAFETY_CRITICAL_WINDOW_SECONDS_ENV,
    REDIS_RUNTIME_SAFETY_THREAD_JOIN_TIMEOUT_SECONDS_ENV,
    "NAUTILUS_REDIS_MEMORY_WARNING_RATIO",
    "NAUTILUS_REDIS_MEMORY_DEGRADED_RATIO",
    "NAUTILUS_REDIS_MEMORY_CRITICAL_RATIO",
)
_POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")
_NON_NEGATIVE_INTEGER_RE = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class RedisRuntimeSafetyConfig:
    stream_root: str
    stream_max_entries: int
    stream_max_bytes: int
    total_stream_max_bytes: int
    scan_count: int
    sample_interval_seconds: float
    critical_window_seconds: float
    thread_join_timeout_seconds: float
    memory_warning_ratio: float
    memory_degraded_ratio: float
    memory_critical_ratio: float


@dataclass(frozen=True)
class NautilusPersistenceConfig:
    cache: dict[str, Any]
    message_bus: dict[str, Any]
    redis_runtime_safety: RedisRuntimeSafetyConfig
    live_exec_engine: dict[str, Any]
    trading_node: dict[str, Any]


def derive_redis_key_prefix(config: NodeConfig, component: str) -> str:
    if not component:
        raise ValueError("component must be non-empty")
    return ":".join(
        (
            config.redis.key_prefix.rstrip(":"),
            _prefix_part(config.trader_id),
            _prefix_part(component),
        )
    )


def derive_nautilus_cache_key_root(
    config: NodeConfig,
    persistence_instance_id: str | bool = False,
) -> str:
    """Return the exact prefix Nautilus applies to Redis cache keys."""

    root = f"trader-{_prefix_part(config.trader_id)}"
    instance_id = _normalize_persistence_instance_id(
        persistence_instance_id
    )
    if instance_id is False:
        return root
    return f"{root}:{instance_id}"


def derive_nautilus_message_bus_stream_root(
    config: NodeConfig,
    persistence_instance_id: str | bool = False,
) -> str:
    """Return the exact prefix Nautilus applies to external message streams."""

    root_parts = [
        f"trader-{_prefix_part(config.trader_id)}",
    ]
    instance_id = _normalize_persistence_instance_id(
        persistence_instance_id
    )
    if instance_id is not False:
        root_parts.append(instance_id)
    root_parts.append(derive_redis_key_prefix(config, "message-bus"))
    return ":".join(root_parts)


def derive_redis_runtime_safety_config(
    config: NodeConfig,
    persistence_instance_id: str | bool = False,
) -> RedisRuntimeSafetyConfig:
    configured_overrides = [
        name
        for name in REDIS_RUNTIME_RESOURCE_ENV_NAMES
        if name in os.environ
    ]
    if configured_overrides:
        raise ValueError(
            "Redis runtime resource env overrides are forbidden: "
            + ", ".join(sorted(configured_overrides))
        )
    resources = config.runtime_resources.redis
    return RedisRuntimeSafetyConfig(
        stream_root=derive_nautilus_message_bus_stream_root(
            config,
            persistence_instance_id,
        ),
        stream_max_entries=resources.stream_max_entries,
        stream_max_bytes=resources.stream_max_bytes,
        total_stream_max_bytes=resources.total_stream_max_bytes,
        scan_count=resources.scan_count,
        sample_interval_seconds=resources.sample_interval_seconds,
        critical_window_seconds=resources.critical_window_seconds,
        thread_join_timeout_seconds=resources.thread_join_timeout_seconds,
        memory_warning_ratio=resources.memory_warning_ratio,
        memory_degraded_ratio=resources.memory_degraded_ratio,
        memory_critical_ratio=resources.memory_critical_ratio,
    )


def derive_cache_config_payload(
    config: NodeConfig,
    persistence_instance_id: str | bool = False,
) -> dict[str, Any]:
    instance_id = _normalize_persistence_instance_id(
        persistence_instance_id
    )
    return {
        "database": _redis_database_payload(config),
        "encoding": config.cache.encoding,
        "use_trader_prefix": True,
        "use_instance_id": instance_id is not False,
        "flush_on_start": False,
        "account_key_prefix": derive_redis_key_prefix(config, "cache"),
        "key_root": derive_nautilus_cache_key_root(config, instance_id),
    }


def derive_message_bus_config_payload(
    config: NodeConfig,
    persistence_instance_id: str | bool = False,
) -> dict[str, Any]:
    instance_id = _normalize_persistence_instance_id(
        persistence_instance_id
    )
    return {
        "database": _redis_database_payload(config),
        "encoding": config.message_bus.encoding,
        "autotrim_mins": resolve_message_bus_autotrim_mins(),
        "streams_prefix": derive_redis_key_prefix(config, "message-bus"),
        "use_trader_prefix": True,
        "use_trader_id": True,
        "use_instance_id": instance_id is not False,
        "stream_root": derive_nautilus_message_bus_stream_root(
            config,
            instance_id,
        ),
    }


def resolve_message_bus_autotrim_mins() -> int:
    raw = os.environ.get(MESSAGE_BUS_AUTOTRIM_MINS_ENV)
    if raw is None:
        return DEFAULT_MESSAGE_BUS_AUTOTRIM_MINS
    if not _POSITIVE_INTEGER_RE.fullmatch(raw):
        raise ValueError(
            f"{MESSAGE_BUS_AUTOTRIM_MINS_ENV} must be a positive integer"
        )
    return int(raw)


def build_live_exec_engine_kwargs(config: NodeConfig) -> dict[str, Any]:
    """Return engine-level reconciliation kwargs for Nautilus live config.

    B-00 established reconciliation belongs on the engine layer, not the Binance
    adapter. The field names below are from Nautilus docs requested for 1.227.0;
    TODO(host-verify): confirm exact LiveExecEngineConfig constructor behavior in
    the hk container because pudu-mini cannot import nautilus_trader.
    """

    return {
        "reconciliation": config.reconciliation.startup,
        "reconciliation_lookback_mins": config.reconciliation.lookback_mins,
        "open_check_interval_secs": config.reconciliation.interval_mins * 60,
        "open_check_lookback_mins": config.reconciliation.lookback_mins,
        "position_check_interval_secs": config.reconciliation.interval_mins * 60,
        "position_check_lookback_mins": config.reconciliation.lookback_mins,
    }


def build_trading_node_kwargs(
    config: NodeConfig,
    persistence_instance_id: str | bool = False,
) -> dict[str, Any]:
    """Return TradingNodeConfig kwargs for cache/message bus and continuous reconcile.

    TODO(host-verify): confirm whether continuous reconciliation is passed directly
    on TradingNodeConfig or nested below LiveExecEngineConfig in Nautilus 1.227.0.
    """

    return {
        "cache": derive_cache_config_payload(
            config,
            persistence_instance_id,
        ),
        "message_bus": derive_message_bus_config_payload(
            config,
            persistence_instance_id,
        ),
        "exec_engine": build_live_exec_engine_kwargs(config),
    }


def build_nautilus_persistence_config(
    config: NodeConfig,
    persistence_instance_id: str | bool = False,
) -> NautilusPersistenceConfig:
    return NautilusPersistenceConfig(
        cache=derive_cache_config_payload(
            config,
            persistence_instance_id,
        ),
        message_bus=derive_message_bus_config_payload(
            config,
            persistence_instance_id,
        ),
        redis_runtime_safety=derive_redis_runtime_safety_config(
            config,
            persistence_instance_id,
        ),
        live_exec_engine=build_live_exec_engine_kwargs(config),
        trading_node=build_trading_node_kwargs(
            config,
            persistence_instance_id,
        ),
    )


def build_database_config(config: NodeConfig) -> Any:
    """Build Nautilus ``DatabaseConfig`` for Redis.

    TODO(host-verify): execute in hk with nautilus_trader==1.227.0 to confirm
    import location and constructor kwargs against the pinned wheel.
    """

    from nautilus_trader.config import DatabaseConfig  # type: ignore[import-not-found]

    return DatabaseConfig(**_redis_database_payload(config))


def build_cache_config(
    config: NodeConfig,
    persistence_instance_id: str | bool = False,
) -> Any:
    """Build Nautilus ``CacheConfig`` with Redis database persistence.

    TODO(host-verify): execute in hk with nautilus_trader==1.227.0 to confirm
    constructor behavior, especially Redis auth URL mapping.
    """

    from nautilus_trader.config import CacheConfig  # type: ignore[import-not-found]

    payload = derive_cache_config_payload(
        config,
        persistence_instance_id,
    )
    return CacheConfig(
        database=build_database_config(config),
        encoding=payload["encoding"],
        use_trader_prefix=payload["use_trader_prefix"],
        use_instance_id=payload["use_instance_id"],
        flush_on_start=payload["flush_on_start"],
    )


def build_message_bus_config(
    config: NodeConfig,
    persistence_instance_id: str | bool = False,
) -> Any:
    """Build Nautilus ``MessageBusConfig`` with Redis-backed streams."""

    from nautilus_trader.config import (
        MessageBusConfig,  # type: ignore[import-not-found]
    )

    payload = derive_message_bus_config_payload(
        config,
        persistence_instance_id,
    )
    return MessageBusConfig(
        database=build_database_config(config),
        encoding=payload["encoding"],
        autotrim_mins=payload["autotrim_mins"],
        streams_prefix=payload["streams_prefix"],
        use_trader_prefix=payload["use_trader_prefix"],
        use_trader_id=payload["use_trader_id"],
        use_instance_id=payload["use_instance_id"],
    )


def build_live_exec_engine_config(config: NodeConfig) -> Any:
    """Build Nautilus ``LiveExecEngineConfig`` with startup/continuous reconcile."""

    from nautilus_trader.config import (
        LiveExecEngineConfig,  # type: ignore[import-not-found]
    )

    return LiveExecEngineConfig(**build_live_exec_engine_kwargs(config))


def _redis_database_payload(config: NodeConfig) -> dict[str, Any]:
    parsed = urlparse(config.redis.url)
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("redis.url must use redis:// or rediss://")
    if parsed.hostname is None:
        raise ValueError("redis.url must include a host")
    _validate_redis_database_path(parsed.path)

    payload: dict[str, Any] = {
        "type": config.cache.backend,
        "host": parsed.hostname,
        "port": parsed.port or 6379,
        "ssl": parsed.scheme == "rediss",
    }
    if parsed.username:
        payload["username"] = parsed.username
    if parsed.password:
        payload["password"] = parsed.password
    return payload


def _validate_redis_database_path(path: str) -> None:
    database = path.strip("/")
    if not database:
        return
    if _NON_NEGATIVE_INTEGER_RE.fullmatch(database) is None:
        raise ValueError(
            "redis.url database path must be a non-negative integer"
        )
    if int(database) != 0:
        raise ValueError(
            "non-zero Redis database is unsupported by the Nautilus mapping"
        )


def _prefix_part(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("prefix identity parts must be non-empty")
    return stripped.replace(":", "_")


def _normalize_persistence_instance_id(
    value: str | bool,
) -> str | bool:
    if value is False:
        return False
    if not isinstance(value, str):
        raise TypeError("persistence_instance_id must be a UUID4 string")
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("persistence_instance_id must be non-empty")
    try:
        parsed = UUID(normalized)
    except ValueError as exc:
        raise ValueError(
            "persistence_instance_id must be a UUID4 string"
        ) from exc
    if parsed.version != 4 or str(parsed) != normalized:
        raise ValueError(
            "persistence_instance_id must be a canonical UUID4 string"
        )
    return normalized
