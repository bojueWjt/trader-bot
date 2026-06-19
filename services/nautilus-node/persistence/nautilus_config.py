from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from config.node_config import NodeConfig


@dataclass(frozen=True)
class NautilusPersistenceConfig:
    cache: dict[str, Any]
    message_bus: dict[str, Any]
    live_exec_engine: dict[str, Any]
    trading_node: dict[str, Any]


def derive_redis_key_prefix(config: NodeConfig, component: str) -> str:
    if not component:
        raise ValueError("component must be non-empty")
    return ":".join(
        (
            config.redis.key_prefix.rstrip(":"),
            _prefix_part(config.trader_id),
            _prefix_part(config.instance_id),
            _prefix_part(component),
        )
    )


def derive_cache_config_payload(config: NodeConfig) -> dict[str, Any]:
    return {
        "database": _redis_database_payload(config),
        "encoding": config.cache.encoding,
        "use_trader_prefix": True,
        "use_instance_id": True,
        "flush_on_start": False,
        "account_key_prefix": derive_redis_key_prefix(config, "cache"),
    }


def derive_message_bus_config_payload(config: NodeConfig) -> dict[str, Any]:
    return {
        "database": _redis_database_payload(config),
        "encoding": config.message_bus.encoding,
        "streams_prefix": derive_redis_key_prefix(config, "message-bus"),
        "use_trader_id": True,
        "use_instance_id": True,
    }


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


def build_trading_node_kwargs(config: NodeConfig) -> dict[str, Any]:
    """Return TradingNodeConfig kwargs for cache/message bus and continuous reconcile.

    TODO(host-verify): confirm whether continuous reconciliation is passed directly
    on TradingNodeConfig or nested below LiveExecEngineConfig in Nautilus 1.227.0.
    """

    return {
        "cache": derive_cache_config_payload(config),
        "message_bus": derive_message_bus_config_payload(config),
        "exec_engine": build_live_exec_engine_kwargs(config),
    }


def build_nautilus_persistence_config(config: NodeConfig) -> NautilusPersistenceConfig:
    return NautilusPersistenceConfig(
        cache=derive_cache_config_payload(config),
        message_bus=derive_message_bus_config_payload(config),
        live_exec_engine=build_live_exec_engine_kwargs(config),
        trading_node=build_trading_node_kwargs(config),
    )


def build_database_config(config: NodeConfig) -> Any:
    """Build Nautilus ``DatabaseConfig`` for Redis.

    TODO(host-verify): execute in hk with nautilus_trader==1.227.0 to confirm
    import location and constructor kwargs against the pinned wheel.
    """

    from nautilus_trader.config import DatabaseConfig  # type: ignore[import-not-found]

    return DatabaseConfig(**_redis_database_payload(config))


def build_cache_config(config: NodeConfig) -> Any:
    """Build Nautilus ``CacheConfig`` with Redis database persistence.

    TODO(host-verify): execute in hk with nautilus_trader==1.227.0 to confirm
    constructor behavior, especially Redis auth URL mapping.
    """

    from nautilus_trader.config import CacheConfig  # type: ignore[import-not-found]

    payload = derive_cache_config_payload(config)
    return CacheConfig(
        database=build_database_config(config),
        encoding=payload["encoding"],
        use_trader_prefix=payload["use_trader_prefix"],
        use_instance_id=payload["use_instance_id"],
        flush_on_start=payload["flush_on_start"],
    )


def build_message_bus_config(config: NodeConfig) -> Any:
    """Build Nautilus ``MessageBusConfig`` with Redis-backed streams."""

    from nautilus_trader.config import MessageBusConfig  # type: ignore[import-not-found]

    payload = derive_message_bus_config_payload(config)
    return MessageBusConfig(
        database=build_database_config(config),
        encoding=payload["encoding"],
        streams_prefix=payload["streams_prefix"],
        use_trader_id=payload["use_trader_id"],
        use_instance_id=payload["use_instance_id"],
    )


def build_live_exec_engine_config(config: NodeConfig) -> Any:
    """Build Nautilus ``LiveExecEngineConfig`` with startup/continuous reconcile."""

    from nautilus_trader.config import LiveExecEngineConfig  # type: ignore[import-not-found]

    return LiveExecEngineConfig(**build_live_exec_engine_kwargs(config))


def _redis_database_payload(config: NodeConfig) -> dict[str, Any]:
    parsed = urlparse(config.redis.url)
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("redis.url must use redis:// or rediss://")
    if parsed.hostname is None:
        raise ValueError("redis.url must include a host")

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


def _prefix_part(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("prefix identity parts must be non-empty")
    return stripped.replace(":", "_")
