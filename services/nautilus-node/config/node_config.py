from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

DEFAULT_REDIS_MEMORY_WARNING_RATIO = 0.60
DEFAULT_REDIS_MEMORY_DEGRADED_RATIO = 0.75
DEFAULT_REDIS_MEMORY_CRITICAL_RATIO = 0.85
RUNTIME_RESOURCES_SCHEMA_VERSION = "trader-v3-runtime-resources/v1"


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
class RiskNodeConfig:
    max_notional_per_order: Mapping[str, str]
    max_order_submit_rate: str
    max_order_modify_rate: str


@dataclass(frozen=True)
class BinanceNodeConfig:
    account_type: str
    environment: str
    credentials: LoadedCredentials
    credential_source: str
    proxy_url: str | bool
    proxy_source: str | bool


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
class RedisRuntimeResourcesNodeConfig:
    stream_max_entries: int
    stream_max_bytes: int
    total_stream_max_bytes: int
    scan_count: int
    sample_interval_seconds: float
    critical_window_seconds: float
    thread_join_timeout_seconds: float
    memory_warning_ratio: float = DEFAULT_REDIS_MEMORY_WARNING_RATIO
    memory_degraded_ratio: float = DEFAULT_REDIS_MEMORY_DEGRADED_RATIO
    memory_critical_ratio: float = DEFAULT_REDIS_MEMORY_CRITICAL_RATIO


@dataclass(frozen=True)
class CommandJournalResourcesNodeConfig:
    max_bytes: int


@dataclass(frozen=True)
class StrategyDurableIoResourcesNodeConfig:
    queue_capacity: int = 128
    task_timeout_seconds: float = 1.0
    shutdown_timeout_seconds: float = 2.0


@dataclass(frozen=True)
class TerminalExchangeResourcesNodeConfig:
    queue_capacity: int = 64
    result_queue_capacity: int = 128
    degraded_ratio: float = 0.8
    total_deadline_seconds: float = 6.0
    shutdown_timeout_seconds: float = 7.0


@dataclass(frozen=True)
class ReporterWorkersResourcesNodeConfig:
    denial_queue_capacity: int = 256
    protection_event_queue_capacity: int = 1024
    live_canary_risk_queue_capacity: int = 128
    incident_queue_capacity: int = 64
    task_timeout_seconds: float = 5.0
    shutdown_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class RuntimeResourcesNodeConfig:
    schema_version: str
    redis: RedisRuntimeResourcesNodeConfig
    command_journal: CommandJournalResourcesNodeConfig
    control_plane_session: ControlPlaneSessionNodeConfig
    strategy_durable_io: StrategyDurableIoResourcesNodeConfig
    terminal_exchange: TerminalExchangeResourcesNodeConfig
    reporter_workers: ReporterWorkersResourcesNodeConfig


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
    risk: Optional[RiskNodeConfig]
    binance: BinanceNodeConfig
    control_plane: ControlPlaneNodeConfig
    runtime_resources: RuntimeResourcesNodeConfig

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
    environment = _required_str(binance_raw, "environment").lower()
    if environment not in {"sandbox", "testnet", "live"}:
        raise NodeConfigError(
            f"binance.environment must be sandbox/testnet/live, got {environment!r}"
        )

    redis = RedisNodeConfig(
        url=_required_str(redis_raw, "url"),
        key_prefix=_required_str(redis_raw, "key_prefix"),
    )
    cache = _load_cache_config(raw.get("cache"))
    message_bus = _load_message_bus_config(raw.get("message_bus"))
    reconciliation = _load_reconciliation_config(raw.get("reconciliation"))
    risk = _load_risk_config(raw.get("risk"))
    runtime_resources = _load_runtime_resources_config(
        raw.get("runtime_resources"),
        require_explicit=_requires_explicit_runtime_resources(
            environment
        ),
        control_plane_session_raw=control_plane_raw.get("session"),
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
    proxy_url, proxy_source = _resolve_optional_proxy(
        binance_raw.get("proxy_url")
    )
    binance = BinanceNodeConfig(
        account_type=account_type,
        environment=environment,
        credentials=LoadedCredentials(api_key=api_key, api_secret=api_secret),
        credential_source=f"{api_key_source},{api_secret_source}",
        proxy_url=proxy_url,
        proxy_source=proxy_source,
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
        session=runtime_resources.control_plane_session,
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
        risk=risk,
        binance=binance,
        control_plane=control_plane,
        runtime_resources=runtime_resources,
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


def _resolve_optional_proxy(raw: Any) -> tuple[str | bool, str | bool]:
    if raw is None:
        return False, False
    proxy_source = "config"
    if isinstance(raw, str):
        proxy_url = raw.strip()
    else:
        proxy_url, proxy_source = _resolve_secret(
            "binance.proxy_url",
            raw,
        )
    parsed_proxy = urlsplit(proxy_url)
    if (
        parsed_proxy.scheme not in {"http", "https"}
        or not parsed_proxy.hostname
    ):
        raise NodeConfigError(
            "binance.proxy_url must resolve to an http(s) URL"
        )
    if (
        parsed_proxy.username is not None
        or parsed_proxy.password is not None
    ):
        raise NodeConfigError(
            "binance.proxy_url must not contain credentials"
        )
    return proxy_url, proxy_source


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
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NodeConfigError(f"{key} must be a positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise NodeConfigError(f"{key} must be a positive number")
    return number


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


def _load_risk_config(raw: Any) -> Optional[RiskNodeConfig]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise NodeConfigError("risk must be an object")
    max_notional_raw = _required_mapping(raw, "max_notional_per_order")
    if not max_notional_raw:
        raise NodeConfigError(
            "risk.max_notional_per_order must not be empty"
        )
    max_notional: dict[str, str] = {}
    for instrument_id, value in max_notional_raw.items():
        instrument = str(instrument_id).strip()
        if not instrument:
            raise NodeConfigError(
                "risk.max_notional_per_order instrument must be non-empty"
            )
        max_notional[instrument] = _positive_decimal_string(
            value,
            f"risk.max_notional_per_order[{instrument}]",
        )
    submit_rate = _required_rate(raw, "max_order_submit_rate")
    modify_rate = _required_rate(raw, "max_order_modify_rate")
    return RiskNodeConfig(
        max_notional_per_order=max_notional,
        max_order_submit_rate=submit_rate,
        max_order_modify_rate=modify_rate,
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


def _load_runtime_resources_config(
    raw: Any,
    *,
    require_explicit: bool,
    control_plane_session_raw: Any,
) -> RuntimeResourcesNodeConfig:
    if raw is None:
        if require_explicit:
            raise NodeConfigError(
                "runtime_resources must be release-bound for live nodes"
            )
        raw = {
            "schema_version": RUNTIME_RESOURCES_SCHEMA_VERSION,
            "redis": {
                "stream_max_entries": 100_000,
                "stream_max_bytes": 64 * 1024 * 1024,
                "total_stream_max_bytes": 256 * 1024 * 1024,
                "scan_count": 500,
                "sample_interval_seconds": 5.0,
                "critical_window_seconds": 30.0,
                "thread_join_timeout_seconds": 5.0,
                "memory_warning_ratio": (
                    DEFAULT_REDIS_MEMORY_WARNING_RATIO
                ),
                "memory_degraded_ratio": (
                    DEFAULT_REDIS_MEMORY_DEGRADED_RATIO
                ),
                "memory_critical_ratio": (
                    DEFAULT_REDIS_MEMORY_CRITICAL_RATIO
                ),
            },
            "command_journal": {
                "max_bytes": 16 * 1024 * 1024,
            },
        }
    if not isinstance(raw, Mapping):
        raise NodeConfigError("runtime_resources must be an object")
    schema_version = _required_str(raw, "schema_version")
    if schema_version != RUNTIME_RESOURCES_SCHEMA_VERSION:
        raise NodeConfigError("runtime_resources schema version mismatch")
    allowed_fields = {
        "schema_version",
        "redis",
        "command_journal",
        "control_plane_session",
        "strategy_durable_io",
        "terminal_exchange",
        "reporter_workers",
    }
    required_fields = {
        "schema_version",
        "redis",
        "command_journal",
    }
    _require_exact_or_allowed_fields(
        raw,
        "runtime_resources",
        required_fields=required_fields,
        allowed_fields=allowed_fields,
    )
    redis_raw = _required_mapping(raw, "redis")
    command_journal_raw = _required_mapping(raw, "command_journal")
    warning_ratio = _runtime_memory_ratio(
        redis_raw,
        "memory_warning_ratio",
        default=DEFAULT_REDIS_MEMORY_WARNING_RATIO,
        require_explicit=require_explicit,
    )
    degraded_ratio = _runtime_memory_ratio(
        redis_raw,
        "memory_degraded_ratio",
        default=DEFAULT_REDIS_MEMORY_DEGRADED_RATIO,
        require_explicit=require_explicit,
    )
    critical_ratio = _runtime_memory_ratio(
        redis_raw,
        "memory_critical_ratio",
        default=DEFAULT_REDIS_MEMORY_CRITICAL_RATIO,
        require_explicit=require_explicit,
    )
    if not warning_ratio < degraded_ratio < critical_ratio:
        raise NodeConfigError(
            "runtime_resources Redis memory ratios must increase from "
            "warning to degraded to critical"
        )
    stream_max_bytes = _required_positive_int(
        redis_raw,
        "stream_max_bytes",
    )
    total_stream_max_bytes = _required_positive_int(
        redis_raw,
        "total_stream_max_bytes",
    )
    if total_stream_max_bytes < stream_max_bytes:
        raise NodeConfigError(
            "runtime_resources.redis.total_stream_max_bytes must cover "
            "stream_max_bytes"
    )
    release_session_raw = raw.get("control_plane_session")
    if release_session_raw is not None:
        if not isinstance(release_session_raw, Mapping):
            raise NodeConfigError(
                "runtime_resources.control_plane_session must be an object"
            )
        _require_exact_or_allowed_fields(
            release_session_raw,
            "runtime_resources.control_plane_session",
            required_fields=set(),
            allowed_fields={
                "command_delivery_capacity",
                "command_ack_capacity",
                "intent_delivery_capacity",
                "execution_event_capacity",
                "queue_degraded_ratio",
                "retry_budget",
                "retry_base_delay_seconds",
                "retry_max_delay_seconds",
                "retry_jitter_ratio",
                "circuit_reset_seconds",
                "operation_timeout_seconds",
                "shutdown_timeout_seconds",
            },
        )
    effective_session_raw = release_session_raw
    if effective_session_raw is None:
        effective_session_raw = control_plane_session_raw
    control_plane_session = _load_control_plane_session_config(
        effective_session_raw
    )
    if (
        release_session_raw is not None
        and control_plane_session_raw is not None
    ):
        legacy_session = _load_control_plane_session_config(
            control_plane_session_raw
        )
        if _session_overlap(legacy_session) != _session_overlap(
            control_plane_session
        ):
            raise NodeConfigError(
                "runtime_resources.control_plane_session differs from "
                "control_plane.session"
            )
    return RuntimeResourcesNodeConfig(
        schema_version=schema_version,
        redis=RedisRuntimeResourcesNodeConfig(
            stream_max_entries=_required_positive_int(
                redis_raw,
                "stream_max_entries",
            ),
            stream_max_bytes=stream_max_bytes,
            total_stream_max_bytes=total_stream_max_bytes,
            scan_count=_required_positive_int(
                redis_raw,
                "scan_count",
            ),
            sample_interval_seconds=_required_positive_number(
                redis_raw,
                "sample_interval_seconds",
            ),
            critical_window_seconds=_required_positive_number(
                redis_raw,
                "critical_window_seconds",
            ),
            thread_join_timeout_seconds=_required_positive_number(
                redis_raw,
                "thread_join_timeout_seconds",
            ),
            memory_warning_ratio=warning_ratio,
            memory_degraded_ratio=degraded_ratio,
            memory_critical_ratio=critical_ratio,
        ),
        command_journal=CommandJournalResourcesNodeConfig(
            max_bytes=_required_positive_int(
                command_journal_raw,
                "max_bytes",
            ),
        ),
        control_plane_session=control_plane_session,
        strategy_durable_io=_load_strategy_durable_io_resources(
            raw.get("strategy_durable_io"),
            require_explicit=False,
        ),
        terminal_exchange=_load_terminal_exchange_resources(
            raw.get("terminal_exchange"),
            require_explicit=False,
        ),
        reporter_workers=_load_reporter_worker_resources(
            raw.get("reporter_workers"),
            require_explicit=False,
        ),
    )


def _session_overlap(
    session: ControlPlaneSessionNodeConfig,
) -> tuple[int | float, ...]:
    return (
        session.command_delivery_capacity,
        session.command_ack_capacity,
        session.intent_delivery_capacity,
        session.execution_event_capacity,
        session.queue_degraded_ratio,
        session.retry_budget,
        session.retry_base_delay_seconds,
        session.retry_max_delay_seconds,
        session.retry_jitter_ratio,
        session.circuit_reset_seconds,
        session.operation_timeout_seconds,
        session.shutdown_timeout_seconds,
    )


def _load_strategy_durable_io_resources(
    raw: Any,
    *,
    require_explicit: bool,
) -> StrategyDurableIoResourcesNodeConfig:
    source = _optional_resource_mapping(
        raw,
        "runtime_resources.strategy_durable_io",
        {
            "queue_capacity",
            "task_timeout_seconds",
            "shutdown_timeout_seconds",
        },
        require_explicit=require_explicit,
    )
    return StrategyDurableIoResourcesNodeConfig(
        queue_capacity=_optional_positive_int(
            source,
            "queue_capacity",
            128,
        ),
        task_timeout_seconds=_optional_positive_number(
            source,
            "task_timeout_seconds",
            1.0,
        ),
        shutdown_timeout_seconds=_optional_positive_number(
            source,
            "shutdown_timeout_seconds",
            2.0,
        ),
    )


def _load_terminal_exchange_resources(
    raw: Any,
    *,
    require_explicit: bool,
) -> TerminalExchangeResourcesNodeConfig:
    source = _optional_resource_mapping(
        raw,
        "runtime_resources.terminal_exchange",
        {
            "queue_capacity",
            "result_queue_capacity",
            "degraded_ratio",
            "total_deadline_seconds",
            "shutdown_timeout_seconds",
        },
        require_explicit=require_explicit,
    )
    return TerminalExchangeResourcesNodeConfig(
        queue_capacity=_optional_positive_int(
            source,
            "queue_capacity",
            64,
        ),
        result_queue_capacity=_optional_positive_int(
            source,
            "result_queue_capacity",
            128,
        ),
        degraded_ratio=_optional_ratio(
            source,
            "degraded_ratio",
            0.8,
            allow_zero=False,
        ),
        total_deadline_seconds=_optional_positive_number(
            source,
            "total_deadline_seconds",
            6.0,
        ),
        shutdown_timeout_seconds=_optional_positive_number(
            source,
            "shutdown_timeout_seconds",
            7.0,
        ),
    )


def _load_reporter_worker_resources(
    raw: Any,
    *,
    require_explicit: bool,
) -> ReporterWorkersResourcesNodeConfig:
    source = _optional_resource_mapping(
        raw,
        "runtime_resources.reporter_workers",
        {
            "denial_queue_capacity",
            "protection_event_queue_capacity",
            "live_canary_risk_queue_capacity",
            "incident_queue_capacity",
            "task_timeout_seconds",
            "shutdown_timeout_seconds",
        },
        require_explicit=require_explicit,
    )
    return ReporterWorkersResourcesNodeConfig(
        denial_queue_capacity=_optional_positive_int(
            source,
            "denial_queue_capacity",
            256,
        ),
        protection_event_queue_capacity=_optional_positive_int(
            source,
            "protection_event_queue_capacity",
            1024,
        ),
        live_canary_risk_queue_capacity=_optional_positive_int(
            source,
            "live_canary_risk_queue_capacity",
            128,
        ),
        incident_queue_capacity=_optional_positive_int(
            source,
            "incident_queue_capacity",
            64,
        ),
        task_timeout_seconds=_optional_positive_number(
            source,
            "task_timeout_seconds",
            5.0,
        ),
        shutdown_timeout_seconds=_optional_positive_number(
            source,
            "shutdown_timeout_seconds",
            5.0,
        ),
    )


def _optional_resource_mapping(
    raw: Any,
    label: str,
    allowed_fields: set[str],
    *,
    require_explicit: bool,
) -> Mapping[str, Any]:
    if raw is None:
        if require_explicit:
            raise NodeConfigError(f"{label} must be explicit")
        return {}
    if not isinstance(raw, Mapping):
        raise NodeConfigError(f"{label} must be an object")
    required_fields: set[str] = set()
    if require_explicit:
        required_fields = set(allowed_fields)
    _require_exact_or_allowed_fields(
        raw,
        label,
        required_fields=required_fields,
        allowed_fields=allowed_fields,
    )
    return raw


def _requires_explicit_runtime_resources(environment: str) -> bool:
    return environment == "live"


def _require_exact_or_allowed_fields(
    raw: Mapping[str, Any],
    label: str,
    *,
    required_fields: set[str],
    allowed_fields: set[str],
) -> None:
    actual_fields = set(raw)
    missing = sorted(required_fields - actual_fields)
    if missing:
        raise NodeConfigError(
            f"{label} missing required fields: {', '.join(missing)}"
        )
    extra = sorted(actual_fields - allowed_fields)
    if extra:
        raise NodeConfigError(
            f"{label} has unsupported fields: {', '.join(extra)}"
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


def _required_positive_int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
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


def _required_ratio(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NodeConfigError(f"{key} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number >= 1:
        raise NodeConfigError(f"{key} must be between zero and one")
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
        raise NodeConfigError(f"{key} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise NodeConfigError(f"{key} must be between zero and one")
    minimum_valid = number > 0
    if allow_zero:
        minimum_valid = number >= 0
    if not minimum_valid or number >= 1:
        raise NodeConfigError(f"{key} must be between zero and one")
    return number


def _runtime_memory_ratio(
    raw: Mapping[str, Any],
    key: str,
    *,
    default: float,
    require_explicit: bool,
) -> float:
    if require_explicit:
        return _required_ratio(raw, key)
    return _optional_ratio(
        raw,
        key,
        default,
        allow_zero=False,
    )


def _required_rate(raw: Mapping[str, Any], key: str) -> str:
    value = _required_str(raw, key)
    if re.fullmatch(r"[1-9][0-9]*/[0-9]{2}:[0-9]{2}:[0-9]{2}", value) is None:
        raise NodeConfigError(
            f"risk.{key} must use count/HH:MM:SS"
        )
    return value


def _positive_decimal_string(value: Any, label: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise NodeConfigError(f"{label} must be a decimal") from exc
    if not number.is_finite() or number <= 0:
        raise NodeConfigError(f"{label} must be positive")
    return format(number, "f")


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
