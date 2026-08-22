from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from uuid import UUID

from config.node_config import NodeConfig, load_node_config
from execution_domain.contracts import ReconciliationState
from persistence.nautilus_config import (
    build_nautilus_persistence_config,
    build_trading_node_kwargs,
    derive_nautilus_cache_key_root,
)
from projection.actor import LifecycleProjectionHealth, ProjectionActor
from projection.event_mapper import ProjectionConfig
from projection.spool import JsonExecutionSpool
from risk.config import (
    DEFAULT_LIVE_ENTRY_NOTIONAL_KEY,
    RiskLimitConfig,
    build_live_entry_notional_inventory,
    build_live_risk_engine_kwargs,
)
from routing.multi_account import AccountRoute
from runtime.live_canary_execution import is_live_canary_account
try:
    from runtime.health import HealthService
except ModuleNotFoundError:
    HealthService = None  # type: ignore[assignment]
try:
    from runtime.reconciliation import ReconciliationCompletionCallback
except ModuleNotFoundError:
    ReconciliationCompletionCallback = None  # type: ignore[assignment,misc]


NAUTILUS_API_TODOS: tuple[str, ...] = (
    "TODO(host-verify): confirm TradingNodeConfig import path and constructor kwargs "
    "for trader_id, instance_id, cache, message_bus, risk_engine, and exec_engine on "
    "nautilus_trader==1.227.0.",
    "TODO(host-verify): confirm BinanceLiveDataClientFactory and "
    "BinanceLiveExecClientFactory import paths and add_*_client_factory signatures.",
    "TODO(host-verify): confirm Actor.clock.set_timer signature for the "
    "IntentPublisherActor polling loop.",
    "TODO(host-verify): confirm Actor.publish_data(DataType, Data) delivery path "
    "for CustomData consumed by Strategy.subscribe_data.",
    "TODO(host-verify): confirm MessageBus execution event topic names and "
    "subscribe signature used by ExecutionProjectionActor.",
    "TODO(host-verify): confirm Trader.add_strategy/add_actor direct object "
    "registration behavior for live TradingNode assembly.",
)


@dataclass(frozen=True)
class NodeComponent:
    name: str
    value: Any = None


@dataclass
class AccountRuntime:
    config: NodeConfig
    control_plane: Any
    lifecycle: Any
    health: HealthService
    reconciliation_callback: Any
    route: AccountRoute
    persistence: Any
    risk_config: RiskLimitConfig
    risk_engine_kwargs: dict[str, Any]
    intent_data_client: Any
    projection_actor: ProjectionActor
    exchange_state_mirror: Any
    exchange_cancel_adapter: Any
    exchange_evidence_provider: Any
    live_canary_portfolio_baseline_provider: Any
    live_entry_mark_snapshot_provider: Any
    strategy_config: Any
    trading_node_config_kwargs: dict[str, Any]
    trading_node: Any = None
    namespace_lease_guard: Any = None
    redis_runtime_safety_guard: Any = None
    redis_runtime_safety_client: Any = None
    control_plane_session: Any = None
    projection_egress_progress_callback: Callable[[], None] | None = None
    incident_reporter: Callable[[str, str], bool] | None = None
    incident_resolver: Callable[[str, str], bool] | None = None
    incident_state_lock: Any = field(default_factory=Lock, repr=False)
    active_runtime_incident_reasons: set[str] = field(default_factory=set)
    pending_incident_resolutions: set[str] = field(default_factory=set)
    resolved_runtime_incident_reasons: set[str] = field(default_factory=set)
    background_workers: list[Any] = field(default_factory=list)
    components: tuple[NodeComponent, ...] = ()
    nautilus_api_todos: tuple[str, ...] = NAUTILUS_API_TODOS


TradingNodeBuilder = Callable[[AccountRuntime], Any]
RestartRequiredCallback = Callable[[float], None]
RuntimeFatalCallback = Callable[[str], None]
ReconciliationCallbackRegistrar = Callable[[Any, Any], None]
ACTOR_WATCHDOG_EXIT_CODE = 75
DEFAULT_NAMESPACE_LEASE_FRESHNESS_SECONDS = 120.0


def build_account_runtime(
    config_path: str | Path,
    *,
    spool_root: str | Path | None = None,
    trading_node_builder: TradingNodeBuilder | None = None,
    build_trading_node: bool = False,
) -> AccountRuntime:
    """Build one account-scoped runtime without connecting to the exchange.

    Credentials are resolved by ``load_node_config`` before any component is
    constructed, so missing Binance/control-plane secrets fail closed.
    """

    config = load_node_config(config_path)
    control_plane = _build_control_plane_client(config)
    lifecycle = _build_lifecycle(config, control_plane)
    health = _build_health_service(lifecycle)
    reconciliation_callback = _build_reconciliation_callback(lifecycle)
    route = _build_route(config, spool_root)
    persistence = build_nautilus_persistence_config(config)
    risk_config = _build_risk_limit_config(config)
    risk_engine_kwargs = build_live_risk_engine_kwargs(risk_config)
    intent_execution_inbox_path = _intent_execution_inbox_path(route)
    (
        exchange_state_mirror,
        exchange_cancel_adapter,
        exchange_evidence_provider,
    ) = _build_exchange_cancel_dependencies(config)
    live_canary_portfolio_baseline_provider = (
        _build_live_canary_portfolio_baseline_provider(
            config,
            exchange_evidence_provider,
        )
    )
    live_entry_mark_snapshot_provider = (
        _build_live_entry_mark_snapshot_provider(config)
    )
    intent_data_client = _build_intent_data_client(
        config,
        control_plane,
        lifecycle,
        route,
        intent_execution_inbox_path,
        live_canary_portfolio_baseline_provider,
    )
    projection_actor = _build_projection_actor(
        config,
        control_plane,
        lifecycle,
        route,
    )
    strategy_config = _build_strategy_config(
        config,
        lifecycle,
        route,
        risk_config,
        intent_execution_inbox_path,
    )
    trading_node_config_kwargs = build_trading_node_kwargs(config)

    runtime = AccountRuntime(
        config=config,
        control_plane=control_plane,
        lifecycle=lifecycle,
        health=health,
        reconciliation_callback=reconciliation_callback,
        route=route,
        persistence=persistence,
        risk_config=risk_config,
        risk_engine_kwargs=risk_engine_kwargs,
        intent_data_client=intent_data_client,
        projection_actor=projection_actor,
        exchange_state_mirror=exchange_state_mirror,
        exchange_cancel_adapter=exchange_cancel_adapter,
        exchange_evidence_provider=exchange_evidence_provider,
        live_canary_portfolio_baseline_provider=(
            live_canary_portfolio_baseline_provider
        ),
        live_entry_mark_snapshot_provider=(
            live_entry_mark_snapshot_provider
        ),
        strategy_config=strategy_config,
        trading_node_config_kwargs=trading_node_config_kwargs,
    )
    _configure_runtime_incident_reporter(runtime)
    builder = trading_node_builder
    if builder is None and build_trading_node:
        builder = build_nautilus_trading_node
    if builder is not None:
        runtime.trading_node = builder(runtime)
    runtime.components = _component_list(runtime)
    return runtime


def build_nautilus_trading_node(
    runtime: AccountRuntime,
    *,
    restart_required_callback: RestartRequiredCallback | None = None,
    reconciliation_callback_registrar: ReconciliationCallbackRegistrar | None = None,
    runtime_fatal_callback: RuntimeFatalCallback | None = None,
) -> Any:
    """Build a Nautilus ``TradingNode`` for a fully assembled account runtime.

    This is intentionally host-only. The local development host does not have
    Nautilus installed; hk must execute this path against the pinned wheel.
    """

    from nautilus_trader.adapters.binance.factories import (  # type: ignore[import-not-found]
        BinanceLiveDataClientFactory,
        BinanceLiveExecClientFactory,
    )
    from nautilus_trader.config import TradingNodeConfig  # type: ignore[import-not-found]
    from nautilus_trader.core.uuid import UUID4  # type: ignore[import-not-found]
    from nautilus_trader.live.node import TradingNode  # type: ignore[import-not-found]

    from app.nautilus_actors import (
        CommandPollerActor,
        ExecutionProjectionActor,
        IntentPublisherActor,
    )
    from persistence.nautilus_config import (  # lazy: imports Nautilus config classes
        build_cache_config,
        build_live_exec_engine_config,
        build_message_bus_config,
    )
    from risk.config import build_live_risk_engine_config
    from runtime.binance_adapter_config import build_binance_client_configs

    fatal_callback = runtime_fatal_callback
    if fatal_callback is None:
        fatal_callback = _build_runtime_fatal_callback()
    namespace_lease_guard = _acquire_namespace_lease_before_node(
        runtime.config,
        lease_lost_callback=_build_namespace_lease_lost_callback(
            runtime.lifecycle,
            fatal_callback,
        ),
    )
    runtime.namespace_lease_guard = namespace_lease_guard
    try:
        if (
            namespace_lease_guard is not None
            and runtime.config.binance.environment == "live"
        ):
            _configure_lifecycle_namespace_lease(
                runtime.lifecycle,
                namespace_lease_guard,
            )
            _configure_control_plane_writer_fence(
                runtime,
                namespace_lease_guard,
                fatal_callback,
            )
        if namespace_lease_guard is not None:
            _register_health_provider(
                runtime,
                "redis_namespace_lease",
                lambda: _namespace_lease_health_snapshot(
                    namespace_lease_guard
                ),
            )
    except Exception:
        runtime.namespace_lease_guard = None
        _release_startup_namespace_lease(namespace_lease_guard)
        raise
    node_assembly_complete = False
    try:
        persistence_instance_id = _active_persistence_instance_id(
            runtime.config,
            namespace_lease_guard,
        )
        data_client_config, exec_client_config = build_binance_client_configs(
            runtime.config
        )
        node_config_kwargs: dict[str, Any] = {
            "trader_id": runtime.config.trader_id,
            "cache": build_cache_config(
                runtime.config,
                persistence_instance_id,
            ),
            "message_bus": build_message_bus_config(
                runtime.config,
                persistence_instance_id,
            ),
            "risk_engine": build_live_risk_engine_config(runtime.risk_config),
            "exec_engine": build_live_exec_engine_config(runtime.config),
            "data_clients": {"BINANCE": data_client_config},
            "exec_clients": {"BINANCE": exec_client_config},
        }
        if persistence_instance_id is not False:
            node_config_kwargs["instance_id"] = UUID4.from_str(
                persistence_instance_id
            )
        runtime.persistence = build_nautilus_persistence_config(
            runtime.config,
            persistence_instance_id,
        )
        runtime.trading_node_config_kwargs = build_trading_node_kwargs(
            runtime.config,
            persistence_instance_id,
        )
        _start_redis_runtime_safety(
            runtime,
            persistence_instance_id,
        )
        runtime.components = _component_list(runtime)
        if runtime.config.binance.environment == "live":
            from runtime.nautilus_reconciliation_scope import (
                install_scoped_reconciliation,
            )

            install_scoped_reconciliation()
        node_config = TradingNodeConfig(**node_config_kwargs)
        node = TradingNode(config=node_config)
        node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
        node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)
        strategy = _build_strategy(
            runtime,
            fatal_callback=fatal_callback,
        )
        node.trader.add_strategy(strategy)
        actor_refs: dict[str, Any] = {}
        control_plane_session = _build_node_control_plane_session(
            runtime,
            actor_refs,
            fatal_callback,
        )
        runtime.control_plane_session = control_plane_session
        intent_actor = IntentPublisherActor(
            runtime.intent_data_client,
            lifecycle=runtime.lifecycle,
            control_plane_session=control_plane_session,
            manage_control_plane_session=False,
        )
        projection_wrapper = ExecutionProjectionActor(
            runtime.projection_actor,
            fatal_callback=fatal_callback,
            progress_stalled_callback=lambda age: (
                _report_runtime_incident(
                    runtime,
                    "projection_progress_stall",
                    "execution projection made no egress progress for "
                    f"{age:.3f} seconds",
                )
            ),
            progress_recovered_callback=lambda: (
                _resolve_runtime_incident(
                    runtime,
                    "projection_progress_stall",
                    "execution projection egress progress recovered",
                )
            ),
            control_plane_session=control_plane_session,
        )
        runtime.projection_egress_progress_callback = (
            projection_wrapper.record_egress_progress
        )
        callback = restart_required_callback
        if callback is None:
            callback = _build_actor_restart_required_callback()
        command_actor = CommandPollerActor(
            runtime.control_plane,
            runtime.lifecycle,
            runtime.config.node_id,
            account_id=runtime.config.account_id,
            actor_tick_stale_callback=lambda age: (
                _report_runtime_incident(
                    runtime,
                    "actor_tick_stall",
                    "actor event loop tick stale for "
                    f"{age:.3f} seconds",
                )
            ),
            restart_required_callback=callback,
            namespace_lease=namespace_lease_guard,
            exchange_evidence_provider=runtime.exchange_evidence_provider,
            reconciliation_refresh=lambda: (
                _refresh_nautilus_reconciliation_proof(
                    node,
                    runtime,
                )
            ),
            namespace_lease_lost_callback=fatal_callback,
            command_journal_path=runtime.route.spool_path.with_suffix(
                ".commands.json"
            ),
            command_journal_max_bytes=(
                runtime.config.runtime_resources.command_journal.max_bytes
            ),
            fatal_callback=fatal_callback,
            shutdown_callback=lambda: _cancel_reconciliation_proof_refresh_task(
                node
            ),
            control_plane_session=control_plane_session,
            manage_control_plane_session=False,
        )
        actor_refs.update(
            {
                "intent": intent_actor,
                "projection": projection_wrapper,
                "command": command_actor,
            }
        )
        node.trader.add_actor(intent_actor)
        node.trader.add_actor(projection_wrapper)
        node.trader.add_actor(command_actor)
        runtime.components = _component_list(runtime)
        if reconciliation_callback_registrar is None:
            _register_nautilus_reconciliation_callback(node, runtime)
        else:
            reconciliation_callback_registrar(
                node,
                runtime.reconciliation_callback,
            )
        node_assembly_complete = True
        return node
    finally:
        if not node_assembly_complete:
            runtime.projection_egress_progress_callback = None
            _stop_control_plane_session(runtime)
            _stop_redis_runtime_safety(runtime)
            _stop_background_workers(runtime)
            runtime.namespace_lease_guard = None
            _release_startup_namespace_lease(namespace_lease_guard)


def _build_actor_restart_required_callback(
    *,
    exit_process: Callable[[int], None] = os._exit,
) -> RestartRequiredCallback:
    def restart_required(age_seconds: float) -> None:
        print(
            "[NodeRuntime] ACTOR WATCHDOG RESTART REQUIRED: "
            f"tick stale for {age_seconds:.3f}s; "
            f"terminating process with exit code {ACTOR_WATCHDOG_EXIT_CODE}",
            flush=True,
        )
        exit_process(ACTOR_WATCHDOG_EXIT_CODE)

    return restart_required


def _build_runtime_fatal_callback(
    *,
    exit_process: Callable[[int], None] = os._exit,
) -> RuntimeFatalCallback:
    def fatal(reason: str) -> None:
        print(
            "[NodeRuntime] FATAL RUNTIME FENCE: "
            f"{reason}; terminating with exit code {ACTOR_WATCHDOG_EXIT_CODE}",
            flush=True,
        )
        exit_process(ACTOR_WATCHDOG_EXIT_CODE)

    return fatal


def _build_redis_namespace_lease(config: NodeConfig) -> Any:
    if config.binance.environment != "live":
        return None
    release_id = os.environ.get("TRADER_RELEASE_ID", "").strip()
    if not release_id:
        raise RuntimeError(
            "TRADER_RELEASE_ID is required for a live Redis namespace lease"
        )
    from persistence.redis_namespace_lease import RedisNamespaceLease
    from persistence.redis_resp_client import RedisRespClient

    return RedisNamespaceLease(
        RedisRespClient(config.redis.url),
        namespace=derive_nautilus_cache_key_root(config),
        owner=config.node_id,
        release_id=release_id,
        max_age_seconds=int(
            DEFAULT_NAMESPACE_LEASE_FRESHNESS_SECONDS
        ),
    )


def _acquire_namespace_lease_before_node(
    config: NodeConfig,
    *,
    lease_lost_callback: RuntimeFatalCallback | None = None,
) -> Any:
    lease = _build_redis_namespace_lease(config)
    if lease is None:
        return None
    from persistence.redis_namespace_lease import NamespaceLeaseGuard

    guard = NamespaceLeaseGuard(
        lease,
        lease_lost_callback=lease_lost_callback,
    )
    try:
        guard.acquire()
    except Exception as exc:
        raise RuntimeError(
            "Redis namespace lease acquisition failed before "
            "TradingNode initialization"
        ) from exc
    return guard


def _release_startup_namespace_lease(guard: Any) -> None:
    if guard is None:
        return
    try:
        guard.close()
    except Exception as exc:
        raise RuntimeError(
            "Redis namespace lease cleanup failed after "
            "TradingNode initialization error"
        ) from exc


def _active_persistence_instance_id(
    config: NodeConfig,
    guard: Any,
) -> str | bool:
    if config.binance.environment != "live":
        return False
    if guard is None:
        raise RuntimeError(
            "live Redis persistence requires a namespace lease guard"
        )
    record = guard.record
    if record is False:
        raise RuntimeError(
            "live Redis persistence requires an acquired namespace lease"
        )
    instance_id = getattr(record, "persistence_instance_id", False)
    persistence_namespace = getattr(record, "persistence_namespace", False)
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise RuntimeError(
            "namespace lease is missing persistence_instance_id"
        )
    expected_namespace = derive_nautilus_cache_key_root(config)
    if getattr(record, "namespace", False) != expected_namespace:
        raise RuntimeError(
            "namespace lease stable identity does not match node config"
        )
    expected_persistence_namespace = derive_nautilus_cache_key_root(
        config,
        instance_id,
    )
    if persistence_namespace != expected_persistence_namespace:
        raise RuntimeError(
            "namespace lease persistence identity does not match node config"
        )
    return instance_id


def _configure_lifecycle_namespace_lease(
    lifecycle: Any,
    guard: Any,
    *,
    freshness_seconds: float = DEFAULT_NAMESPACE_LEASE_FRESHNESS_SECONDS,
) -> None:
    record = getattr(guard, "record", False)
    if record is False:
        raise RuntimeError(
            "live lifecycle requires an acquired namespace lease"
        )
    redis_fencing_epoch = str(
        getattr(record, "redis_fencing_epoch", "") or ""
    ).strip()
    if not redis_fencing_epoch:
        raise RuntimeError(
            "namespace lease is missing redis_fencing_epoch"
        )
    fencing_token = getattr(record, "fencing_token", False)
    if isinstance(fencing_token, bool):
        raise RuntimeError(
            "namespace lease is missing fencing_token"
        )
    try:
        generation = int(fencing_token)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "namespace lease fencing_token is invalid"
        ) from exc
    if generation < 1:
        raise RuntimeError(
            "namespace lease fencing_token is invalid"
        )
    configure = getattr(lifecycle, "configure_lease", None)
    if not callable(configure):
        raise RuntimeError(
            "live lifecycle does not support namespace lease fencing"
        )
    configure(
        redis_fencing_epoch=redis_fencing_epoch,
        generation=generation,
        freshness_seconds=freshness_seconds,
    )


def _configure_control_plane_writer_fence(
    runtime: AccountRuntime,
    guard: Any,
    fatal_callback: RuntimeFatalCallback,
) -> None:
    record = getattr(guard, "record", False)
    if record is False:
        raise RuntimeError(
            "live control-plane client requires an acquired namespace lease"
        )
    redis_fencing_epoch = str(
        getattr(record, "redis_fencing_epoch", "") or ""
    ).strip()
    runtime_generation = str(
        getattr(runtime.lifecycle, "runtime_generation", "") or ""
    ).strip()
    lease_fencing_token = getattr(record, "fencing_token", False)
    bind_identity = getattr(
        runtime.control_plane,
        "bind_writer_identity",
        None,
    )
    bind_fatal_hook = getattr(
        runtime.control_plane,
        "bind_fatal_fence_hook",
        None,
    )
    bind_transport_hook = getattr(
        runtime.control_plane,
        "bind_fatal_transport_hook",
        None,
    )
    mirror_bind_identity = getattr(
        runtime.exchange_state_mirror,
        "bind_writer_identity",
        None,
    )
    mirror_bind_fatal_hook = getattr(
        runtime.exchange_state_mirror,
        "bind_fatal_fence_hook",
        None,
    )
    if (
        not callable(bind_identity)
        or not callable(bind_fatal_hook)
        or not callable(bind_transport_hook)
        or not callable(mirror_bind_identity)
        or not callable(mirror_bind_fatal_hook)
    ):
        raise RuntimeError(
            "live control-plane client does not support writer fencing"
        )
    bind_fatal_hook(fatal_callback)
    bind_transport_hook(fatal_callback)
    writer_identity = {
        "redis_fencing_epoch": redis_fencing_epoch,
        "runtime_generation": runtime_generation,
        "lease_fencing_token": lease_fencing_token,
    }
    bind_identity(**writer_identity)
    mirror_bind_fatal_hook(fatal_callback)
    mirror_bind_identity(
        redis_fencing_epoch=redis_fencing_epoch,
        runtime_generation=runtime_generation,
        lease_fencing_token=lease_fencing_token,
    )


def _build_namespace_lease_lost_callback(
    lifecycle: Any,
    fatal_callback: RuntimeFatalCallback,
) -> RuntimeFatalCallback:
    def lease_lost(reason: str) -> None:
        try:
            dependency = _dependency_by_value("redis")
            marker = getattr(lifecycle, "mark_dependency_failed", None)
            if dependency is not None and callable(marker):
                marker(dependency, reason)
        except Exception as exc:
            print(
                "[NodeRuntime] Redis lease dependency failure marker failed: "
                f"{exc!r}",
                flush=True,
            )
        try:
            invalidator = getattr(lifecycle, "invalidate_lease", None)
            if callable(invalidator):
                invalidator(reason)
        finally:
            fatal_callback(reason)

    return lease_lost


def _start_redis_runtime_safety(
    runtime: AccountRuntime,
    persistence_instance_id: str | bool,
) -> None:
    if runtime.config.binance.environment != "live":
        return
    from persistence.redis_resp_client import RedisRespClient
    from runtime.redis_safety import RedisRuntimeSafetyGuard

    expected_stream_root = (
        runtime.persistence.redis_runtime_safety.stream_root
    )
    if (
        persistence_instance_id is not False
        and str(persistence_instance_id) not in expected_stream_root
    ):
        raise RuntimeError(
            "Redis runtime safety stream root lacks active persistence "
            "generation"
        )
    client = RedisRespClient(runtime.config.redis.url)
    guard = RedisRuntimeSafetyGuard(
        client,
        config=runtime.persistence.redis_runtime_safety,
        halt_callback=lambda reason: _mark_runtime_dependency_failed(
            runtime,
            "redis",
            reason,
        ),
    )
    runtime.redis_runtime_safety_client = client
    runtime.redis_runtime_safety_guard = guard
    try:
        guard.sample_now()
        guard.start()
    except Exception:
        runtime.redis_runtime_safety_guard = None
        runtime.redis_runtime_safety_client = None
        client.close()
        raise
    _register_health_provider(
        runtime,
        "redis_runtime_safety",
        guard.snapshot,
    )


def _namespace_lease_health_snapshot(
    guard: Any,
) -> dict[str, object]:
    record = getattr(guard, "record", False)
    payload: dict[str, object] = {
        "healthy": bool(getattr(guard, "is_healthy", False)),
        "running": bool(getattr(guard, "is_running", False)),
        "failure_reason": str(
            getattr(guard, "failure_reason", "") or ""
        ),
        "acquired": record is not False,
    }
    if record is False:
        return payload
    payload.update(
        {
            "namespace": str(
                getattr(record, "namespace", "") or ""
            ),
            "persistence_instance_id": str(
                getattr(
                    record,
                    "persistence_instance_id",
                    "",
                )
                or ""
            ),
            "persistence_namespace": str(
                getattr(record, "persistence_namespace", "") or ""
            ),
            "fencing_token": getattr(
                record,
                "fencing_token",
                False,
            ),
            "refreshed_at_epoch": getattr(
                record,
                "refreshed_at_epoch",
                False,
            ),
        }
    )
    return payload


def _stop_redis_runtime_safety(runtime: Any) -> None:
    guard = getattr(runtime, "redis_runtime_safety_guard", None)
    client = getattr(runtime, "redis_runtime_safety_client", None)
    errors: list[Exception] = []
    if guard is not None:
        try:
            guard.stop()
        except Exception as exc:
            errors.append(exc)
        runtime.redis_runtime_safety_guard = None
    if client is not None:
        try:
            client.close()
        except Exception as exc:
            errors.append(exc)
        runtime.redis_runtime_safety_client = None
    if errors:
        raise RuntimeError(
            "Redis runtime safety cleanup failed"
        ) from errors[0]


def _build_node_control_plane_session(
    runtime: AccountRuntime,
    actor_refs: dict[str, Any],
    fatal_callback: RuntimeFatalCallback,
) -> Any:
    from runtime.control_plane_session import NodeControlPlaneSession

    session_config = runtime.config.control_plane.session
    session = NodeControlPlaneSession(
        writer_bootstrap=lambda: actor_refs[
            "command"
        ].session_bootstrap_writer(),
        heartbeat=lambda: actor_refs["command"].session_send_heartbeat(),
        command_poll=lambda capacity: actor_refs[
            "command"
        ].session_poll_commands(capacity),
        command_apply=lambda command: actor_refs[
            "command"
        ].session_apply_command(command),
        command_ack=lambda acknowledgement: actor_refs[
            "command"
        ].session_ack_command(acknowledgement),
        intent_fetch=lambda capacity: runtime.intent_data_client.fetch_once(
            limit=capacity,
            wait_ms=0,
        ),
        intent_replay=runtime.intent_data_client.replay_pending,
        intent_deliver=runtime.intent_data_client.deliver,
        execution_event_sink=lambda event: _flush_projection_session_event(
            runtime,
            event,
        ),
        command_delivery_capacity=(
            session_config.command_delivery_capacity
        ),
        command_ack_capacity=session_config.command_ack_capacity,
        intent_delivery_capacity=(
            session_config.intent_delivery_capacity
        ),
        execution_event_capacity=(
            session_config.execution_event_capacity
        ),
        queue_degraded_ratio=session_config.queue_degraded_ratio,
        retry_budget=session_config.retry_budget,
        retry_base_delay_seconds=(
            session_config.retry_base_delay_seconds
        ),
        retry_max_delay_seconds=(
            session_config.retry_max_delay_seconds
        ),
        retry_jitter_ratio=session_config.retry_jitter_ratio,
        circuit_reset_seconds=session_config.circuit_reset_seconds,
        operation_timeout_seconds=(
            session_config.operation_timeout_seconds
        ),
        failure_callback=lambda lane, reason: (
            _mark_session_lane_failed(runtime, lane, reason)
        ),
        success_callback=lambda lane: (
            _mark_session_lane_ready(runtime, lane)
        ),
        fatal_termination_hook=fatal_callback,
        thread_name_prefix=f"{runtime.config.node_id}.control-plane",
    )
    _register_health_provider(
        runtime,
        "control_plane_session",
        session.snapshot,
    )
    return session


def _flush_projection_session_event(
    runtime: AccountRuntime,
    event: Any,
) -> None:
    del event
    progress_callback = getattr(
        runtime,
        "projection_egress_progress_callback",
        None,
    )
    while True:
        before = int(runtime.projection_actor.spool.pending_count)
        if before <= 0:
            if callable(progress_callback):
                progress_callback()
            return
        runtime.projection_actor.flush()
        after = int(runtime.projection_actor.spool.pending_count)
        if after >= before:
            raise RuntimeError(
                "execution projection durable spool made no egress progress"
            )
        if callable(progress_callback):
            progress_callback()


def _mark_session_lane_failed(
    runtime: AccountRuntime,
    lane: str,
    reason: str,
) -> None:
    dependency_by_lane = {
        "heartbeat": "control_plane",
        "command_poll": "command_stream",
        "command_delivery": "command_stream",
        "command_ack": "command_stream",
        "intent_fetch": "intent_stream",
        "intent_delivery": "intent_stream",
        "execution_event": "projection",
    }
    dependency = dependency_by_lane.get(lane)
    if dependency is None:
        return
    _mark_runtime_dependency_failed(
        runtime,
        dependency,
        f"{lane}: {reason}",
    )


def _mark_session_lane_ready(
    runtime: AccountRuntime,
    lane: str,
) -> None:
    dependency_by_lane = {
        "heartbeat": "control_plane",
        "command_poll": "command_stream",
        "command_delivery": "command_stream",
        "command_ack": "command_stream",
        "intent_fetch": "intent_stream",
        "intent_delivery": "intent_stream",
    }
    dependency_name = dependency_by_lane.get(lane)
    if dependency_name is None:
        return
    dependency = _dependency_by_value(dependency_name)
    marker = getattr(runtime.lifecycle, "mark_dependency_ready", None)
    if dependency is not None and callable(marker):
        marker(dependency)
    _resolve_runtime_incident(
        runtime,
        f"{dependency_name}_runtime_failure",
        f"{lane} recovered with accepted control-plane evidence",
    )


def _register_health_provider(
    runtime: AccountRuntime,
    name: str,
    provider: Callable[[], Any],
) -> None:
    register = getattr(runtime.health, "register_provider", None)
    if callable(register):
        register(name, provider)


def _stop_control_plane_session(runtime: Any) -> None:
    session = getattr(runtime, "control_plane_session", None)
    if session is None:
        return
    try:
        stopped = bool(session.stop(time.monotonic() + 1.0))
        if not stopped:
            raise RuntimeError(
                "control-plane session failed to drain before deadline"
            )
    finally:
        runtime.control_plane_session = None


def _register_nautilus_reconciliation_callback(
    node: Any,
    runtime: AccountRuntime,
    *,
    schedule_refresh: bool = True,
) -> None:
    kernel = getattr(node, "kernel", None)
    engine = getattr(kernel, "exec_engine", None)
    original = getattr(engine, "reconcile_execution_state", None)
    if engine is None or not callable(original):
        raise RuntimeError(
            "Nautilus execution engine reconciliation API is unavailable"
        )
    if getattr(engine, "_trader_reconciliation_proof_registered", False):
        raise RuntimeError("Nautilus reconciliation proof hook is already registered")

    async def reconcile_with_proof(*args: Any, **kwargs: Any) -> bool:
        loop = asyncio.get_running_loop()
        previous_loop = getattr(
            engine,
            "_trader_reconciliation_event_loop",
            None,
        )
        if previous_loop is not loop:
            task = getattr(
                engine,
                "_trader_reconciliation_proof_task",
                None,
            )
            if task is not None and not task.done():
                raise RuntimeError(
                    "Nautilus reconciliation event loop changed while "
                    "the proof refresh task is running"
                )
            engine._trader_reconciliation_lock = asyncio.Lock()
        engine._trader_reconciliation_event_loop = loop
        engine._trader_reconciliation_args = tuple(args)
        engine._trader_reconciliation_kwargs = dict(kwargs)
        healthy = await _execute_nautilus_reconciliation(
            node,
            runtime,
            original,
            args,
            kwargs,
            _nautilus_reconciliation_lock(engine),
        )
        if healthy and schedule_refresh:
            _ensure_reconciliation_proof_refresh_task(
                node,
                runtime,
                original,
                args,
                kwargs,
            )
        return healthy

    engine.reconcile_execution_state = reconcile_with_proof
    engine._trader_reconciliation_proof_registered = True


def _refresh_nautilus_reconciliation_proof(
    node: Any,
    runtime: AccountRuntime,
) -> None:
    del runtime
    kernel = getattr(node, "kernel", None)
    engine = getattr(kernel, "exec_engine", None)
    if engine is None:
        raise RuntimeError(
            "Nautilus execution engine reconciliation API is unavailable"
        )
    loop = getattr(
        engine,
        "_trader_reconciliation_event_loop",
        None,
    )
    if loop is None or loop.is_closed() or not loop.is_running():
        raise RuntimeError(
            "Nautilus reconciliation event loop is unavailable"
        )
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is loop:
        raise RuntimeError(
            "immediate reconciliation must run from a non-event-loop thread"
        )
    args = tuple(
        getattr(
            engine,
            "_trader_reconciliation_args",
            (),
        )
    )
    kwargs = dict(
        getattr(
            engine,
            "_trader_reconciliation_kwargs",
            {},
        )
    )
    reconcile = getattr(engine, "reconcile_execution_state", None)
    if not callable(reconcile):
        raise RuntimeError(
            "Nautilus execution engine reconciliation API is unavailable"
        )
    timeout_seconds = _reconciliation_timeout_seconds(args, kwargs)
    future = asyncio.run_coroutine_threadsafe(
        reconcile(*args, **kwargs),
        loop,
    )
    try:
        healthy = bool(
            future.result(
                timeout=timeout_seconds + 1.0,
            )
        )
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(
            "immediate Nautilus reconciliation exceeded "
            f"{timeout_seconds + 1.0:.3f}s command deadline"
        ) from exc
    if not healthy:
        raise RuntimeError(
            "immediate Nautilus reconciliation reported unhealthy state"
        )


def _ensure_reconciliation_proof_refresh_task(
    node: Any,
    runtime: AccountRuntime,
    original_reconcile: Callable[..., Any],
    reconcile_args: tuple[Any, ...],
    reconcile_kwargs: dict[str, Any],
) -> None:
    engine = node.kernel.exec_engine
    task = getattr(engine, "_trader_reconciliation_proof_task", None)
    if task is not None and not task.done():
        return
    loop = asyncio.get_running_loop()
    task = loop.create_task(
        _run_reconciliation_proof_refresh_loop(
            node,
            runtime,
            original_reconcile,
            reconcile_args,
            dict(reconcile_kwargs),
        ),
        name="trader-reconciliation-proof-refresh",
    )
    engine._trader_reconciliation_proof_task = task


async def _run_reconciliation_proof_refresh_loop(
    node: Any,
    runtime: AccountRuntime,
    original_reconcile: Callable[..., Any],
    reconcile_args: tuple[Any, ...],
    reconcile_kwargs: dict[str, Any],
) -> None:
    interval_seconds = max(
        runtime.config.reconciliation.interval_mins * 60,
        60,
    )
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            engine = node.kernel.exec_engine
            is_running = getattr(engine, "is_running", None)
            running = True
            if callable(is_running):
                running = bool(is_running())
            elif is_running is not None:
                running = bool(is_running)
            if not running:
                return
            try:
                await _execute_nautilus_reconciliation(
                    node,
                    runtime,
                    original_reconcile,
                    reconcile_args,
                    reconcile_kwargs,
                    _nautilus_reconciliation_lock(engine),
                )
            except Exception:
                continue
    except asyncio.CancelledError:
        return


def _nautilus_reconciliation_lock(engine: Any) -> asyncio.Lock:
    lock = getattr(
        engine,
        "_trader_reconciliation_lock",
        None,
    )
    if lock is None:
        lock = asyncio.Lock()
        engine._trader_reconciliation_lock = lock
    return lock


async def _execute_nautilus_reconciliation(
    node: Any,
    runtime: AccountRuntime,
    reconcile: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    lock: asyncio.Lock,
) -> bool:
    async with lock:
        generation = _begin_reconciliation(runtime)
        try:
            healthy = await _reconcile_with_total_deadline(
                reconcile,
                args,
                kwargs,
            )
        except Exception:
            _record_nautilus_reconciliation_proof(
                node,
                runtime,
                healthy=False,
                generation=generation,
            )
            raise
        _record_nautilus_reconciliation_proof(
            node,
            runtime,
            healthy=healthy,
            generation=generation,
        )
        return healthy


async def _reconcile_with_total_deadline(
    reconcile: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> bool:
    timeout_seconds = _reconciliation_timeout_seconds(args, kwargs)
    result = reconcile(*args, **kwargs)
    return bool(
        await asyncio.wait_for(
            result,
            timeout=timeout_seconds,
        )
    )


def _reconciliation_timeout_seconds(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> float:
    raw_timeout = kwargs.get("timeout_secs")
    if raw_timeout is None and args:
        raw_timeout = args[0]
    if raw_timeout is None:
        raw_timeout = 10.0
    timeout_seconds = float(raw_timeout)
    if timeout_seconds <= 0:
        raise ValueError("reconciliation timeout_secs must be positive")
    return timeout_seconds


def _cancel_reconciliation_proof_refresh_task(node: Any) -> None:
    kernel = getattr(node, "kernel", None)
    engine = getattr(kernel, "exec_engine", None)
    if engine is None:
        return
    task = getattr(engine, "_trader_reconciliation_proof_task", None)
    if task is not None and not task.done():
        task.cancel()
    engine._trader_reconciliation_proof_task = None


def _begin_reconciliation(runtime: AccountRuntime) -> int:
    begin = getattr(runtime.lifecycle, "begin_reconciliation", None)
    if not callable(begin):
        return int(
            getattr(runtime.lifecycle, "reconciliation_generation", 0) or 0
        )
    generation = begin()
    if generation is None:
        return int(
            getattr(runtime.lifecycle, "reconciliation_generation", 0) or 0
        )
    return int(generation)


def _record_nautilus_reconciliation_proof(
    node: Any,
    runtime: AccountRuntime,
    *,
    healthy: bool,
    generation: int,
) -> None:
    release_id = os.environ.get("TRADER_RELEASE_ID", "").strip()
    if not release_id:
        raise RuntimeError(
            "TRADER_RELEASE_ID is required for reconciliation proof"
        )
    orders = tuple(node.cache.orders())
    positions = tuple(node.cache.positions())
    fills = _order_fill_events(orders)
    state = ReconciliationState.FAILED
    if healthy:
        state = ReconciliationState.HEALTHY
    runtime.reconciliation_callback(
        account_id=runtime.config.account_id,
        node_id=runtime.config.node_id,
        release_id=release_id,
        state=state,
        orders=orders,
        positions=positions,
        fills=fills,
        completed_at=datetime.now(timezone.utc),
        generation=generation,
    )


def _order_fill_events(orders: Iterable[Any]) -> tuple[Any, ...]:
    fills = []
    for order in orders:
        events = getattr(order, "events", ())
        if callable(events):
            events = events()
        for event in events or ():
            if type(event).__name__ == "OrderFilled":
                fills.append(event)
    return tuple(fills)


def run_startup_readiness_checks(runtime: AccountRuntime) -> None:
    begin_reconciliation = getattr(runtime.lifecycle, "begin_reconciliation", None)
    if callable(begin_reconciliation):
        live_environment = runtime.config.binance.environment == "live"
        begin_reconciliation(halt_active=live_environment)
    _mark_dependency(runtime, "redis", _check_redis(runtime.config))
    _mark_dependency(runtime, "control_plane", _check_control_plane(runtime))
    _mark_dependency(runtime, "instruments", _check_adapter(runtime))
    _mark_dependency(runtime, "projection", runtime.projection_actor.spool.pending_count == 0)


def _component_list(runtime: AccountRuntime) -> tuple[NodeComponent, ...]:
    return (
        NodeComponent("config", runtime.config),
        NodeComponent("control_plane", runtime.control_plane),
        NodeComponent("lifecycle", runtime.lifecycle),
        NodeComponent("route", runtime.route),
        NodeComponent("persistence", runtime.persistence),
        NodeComponent("risk", runtime.risk_config),
        NodeComponent("binance_adapter"),
        NodeComponent("intent_data_client", runtime.intent_data_client),
        NodeComponent("projection_actor", runtime.projection_actor),
        NodeComponent("exchange_state_mirror", runtime.exchange_state_mirror),
        NodeComponent("exchange_cancel_adapter", runtime.exchange_cancel_adapter),
        NodeComponent(
            "exchange_evidence_provider",
            runtime.exchange_evidence_provider,
        ),
        NodeComponent("intent_execution_strategy", runtime.strategy_config),
        NodeComponent("trading_node_config", runtime.trading_node_config_kwargs),
        NodeComponent(
            "control_plane_session",
            runtime.control_plane_session,
        ),
        NodeComponent(
            "redis_runtime_safety_guard",
            runtime.redis_runtime_safety_guard,
        ),
        NodeComponent("trading_node", runtime.trading_node),
    )


def _build_control_plane_client(config: NodeConfig) -> Any:
    try:
        from execution_domain.http_client import HttpControlPlaneClient

        return HttpControlPlaneClient(
            base_url=config.control_plane.base_url,
            token=config.control_plane.token,
            node_id=config.node_id,
            account_id=config.account_id,
        )
    except ModuleNotFoundError:
        return _UnavailableControlPlaneClient()


def _build_lifecycle(config: NodeConfig, control_plane: Any) -> Any:
    try:
        from runtime.lifecycle import NodeLifecycle

        return NodeLifecycle(config=config, control_plane=control_plane)
    except ModuleNotFoundError:
        return _LocalLifecycle(config)


def _build_health_service(lifecycle: Any) -> Any:
    if HealthService is not None:
        return HealthService(lifecycle)
    return _LocalHealthService(lifecycle)


def _build_reconciliation_callback(lifecycle: Any) -> Any:
    if ReconciliationCompletionCallback is not None:
        return ReconciliationCompletionCallback(lifecycle)
    return _UnavailableReconciliationCallback()


def _build_route(config: NodeConfig, spool_root: str | Path | None) -> AccountRoute:
    initial_trading_state = os.environ.get(
        "NAUTILUS_INITIAL_TRADING_STATE",
        "HALTED",
    )
    if config.binance.environment == "live":
        initial_trading_state = "HALTED"
    return AccountRoute(
        account_id=config.account_id,
        node_id=config.node_id,
        redis_key_prefix=config.redis.key_prefix,
        control_plane_base_url=config.control_plane.base_url,
        spool_root=spool_root or os.environ.get(
            "NAUTILUS_SPOOL_ROOT", "/var/lib/nautilus-node/spool"
        ),
        environment=config.binance.environment,
        initial_trading_state=initial_trading_state,
    )


def _build_risk_limit_config(config: NodeConfig) -> RiskLimitConfig:
    release_bound_live_account = (
        is_live_canary_account(config.account_id)
        and config.binance.environment == "live"
    )
    risk_env_names = (
        "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON",
        "NAUTILUS_MAX_ORDER_SUBMIT_RATE",
        "NAUTILUS_MAX_ORDER_MODIFY_RATE",
    )
    configured_env_names = [
        name for name in risk_env_names
        if os.environ.get(name) is not None
    ]
    if release_bound_live_account and configured_env_names:
        names = ",".join(configured_env_names)
        raise ValueError(
            f"live account risk env overrides are forbidden: {names}"
        )
    if release_bound_live_account and config.risk is None:
        raise ValueError(
            "live account requires risk configuration in node JSON"
        )

    if config.risk is not None:
        max_notional = {
            str(key): str(value)
            for key, value in config.risk.max_notional_per_order.items()
        }
        submit_rate = config.risk.max_order_submit_rate
        modify_rate = config.risk.max_order_modify_rate
    else:
        raw_limits = os.environ.get("NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON")
        if raw_limits:
            limits = json.loads(raw_limits)
            if not isinstance(limits, dict):
                raise ValueError(
                    "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON must be an object"
                )
            max_notional = {
                str(key): str(value)
                for key, value in limits.items()
            }
        else:
            max_notional = {"BTCUSDT-PERP.BINANCE": "1"}
        submit_rate = os.environ.get(
            "NAUTILUS_MAX_ORDER_SUBMIT_RATE",
            "50/00:00:01",
        )
        modify_rate = os.environ.get(
            "NAUTILUS_MAX_ORDER_MODIFY_RATE",
            "1/00:00:01",
        )

    return RiskLimitConfig(
        max_notional_per_order=max_notional,
        max_order_submit_rate=submit_rate,
        max_order_modify_rate=modify_rate,
    )

def _build_intent_data_client(
    config: NodeConfig,
    control_plane: Any,
    lifecycle: Any,
    route: AccountRoute,
    intent_execution_inbox_path: Path,
    live_canary_portfolio_baseline_provider: Any,
) -> Any:
    try:
        from data_client.approved_intent_client import (
            ApprovedIntentDataClient,
            JsonIntentOffsetStore,
        )

        return ApprovedIntentDataClient(
            account_id=config.account_id,
            node_id=config.node_id,
            source=control_plane,
            publisher=_NautilusIntentPublisher(),
            offset_store=JsonIntentOffsetStore(route.spool_path.with_suffix(".offset.json")),
            trading_state=lambda: lifecycle.trading_state,
            live_canary_release_id=_live_canary_release_id(config),
            live_canary_execution_path=_live_canary_execution_path(route),
            intent_execution_inbox_path=intent_execution_inbox_path,
            live_canary_portfolio_baseline=(
                live_canary_portfolio_baseline_provider
            ),
            live_rollout_phase=lambda: lifecycle.rollout_phase,
            live_open_gate=lambda: lifecycle.live_open_gate,
        )
    except ModuleNotFoundError:
        return _LocalApprovedIntentDataClient(
            account_id=config.account_id,
            node_id=config.node_id,
            offset_path=route.spool_path.with_suffix(".offset.json"),
        )


def _build_projection_actor(
    config: NodeConfig,
    control_plane: Any,
    lifecycle: Any,
    route: AccountRoute,
) -> ProjectionActor:
    allowed_instrument_ids: frozenset[str] = frozenset()
    require_robot_order_ownership = False
    if config.binance.environment == "live":
        if config.risk is None:
            raise RuntimeError(
                "live execution projection requires owned instruments"
            )
        allowed_instrument_ids = frozenset(
            str(instrument_id)
            for instrument_id in config.risk.max_notional_per_order
            if instrument_id != DEFAULT_LIVE_ENTRY_NOTIONAL_KEY
        )
        if (
            DEFAULT_LIVE_ENTRY_NOTIONAL_KEY
            in config.risk.max_notional_per_order
        ):
            allowed_instrument_ids = frozenset()
        require_robot_order_ownership = True
    return ProjectionActor(
        config=ProjectionConfig(
            node_id=config.node_id,
            account_id=config.account_id,
            allowed_instrument_ids=allowed_instrument_ids,
            require_robot_order_ownership=require_robot_order_ownership,
        ),
        sink=control_plane,
        spool=JsonExecutionSpool(route.spool_path),
        health=LifecycleProjectionHealth(lifecycle),
    )


def _build_exchange_cancel_dependencies(
    config: NodeConfig,
) -> tuple[Any, Any, Any]:
    from runtime.exchange_cancel_adapter import (
        BinanceExchangeEvidenceProvider,
        BinanceExchangeCancelAdapter,
        ControlPlaneExchangeStateMirror,
        SignedBinanceTransport,
    )

    mirror = ControlPlaneExchangeStateMirror(
        account_id=config.account_id,
        node_id=config.node_id,
        base_url=config.control_plane.base_url,
        token=config.control_plane.token,
    )
    base_url = _binance_http_base_url(config)
    transport = SignedBinanceTransport(
        base_url=base_url,
        api_key=config.binance.credentials.api_key,
        api_secret=config.binance.credentials.api_secret,
        proxy_url=config.binance.proxy_url,
    )
    adapter = BinanceExchangeCancelAdapter(
        account_id=config.account_id,
        transport=transport,
    )
    evidence_provider = None
    if config.binance.environment == "live":
        evidence_provider = BinanceExchangeEvidenceProvider(
            transport=transport,
        )
    return mirror, adapter, evidence_provider


def _binance_http_base_url(config: NodeConfig) -> str:
    if config.binance.environment == "live":
        return "https://fapi.binance.com"
    return "https://testnet.binancefuture.com"


def _build_live_entry_mark_snapshot_provider(
    config: NodeConfig,
) -> Any:
    if config.binance.environment != "live":
        return None
    base_url = _binance_http_base_url(config)
    opener = None
    proxy_url = config.binance.proxy_url
    if proxy_url:
        opener = build_opener(
            ProxyHandler({"http": proxy_url, "https": proxy_url})
        )

    def current_mark(instrument_id: str) -> Any:
        symbol = _binance_symbol_from_instrument_id(instrument_id)
        if symbol is False:
            return False
        query = urlencode({"symbol": symbol})
        request = Request(
            f"{base_url}/fapi/v1/premiumIndex?{query}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            if opener is None:
                response_context = urlopen(request, timeout=2)
            else:
                response_context = opener.open(request, timeout=2)
            with response_context as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        mark_price = str(payload.get("markPrice") or "").strip()
        raw_time = payload.get("time")
        try:
            time_ms = int(raw_time)
        except (TypeError, ValueError, OverflowError):
            return False
        if not mark_price or time_ms <= 0:
            return False
        return SimpleNamespace(
            value=mark_price,
            ts_event=time_ms * 1_000_000,
        )

    return current_mark


def _binance_symbol_from_instrument_id(
    instrument_id: str,
) -> str | bool:
    normalized = str(instrument_id or "").strip().upper()
    if not normalized.endswith("-PERP.BINANCE"):
        return False
    symbol = normalized[: -len("-PERP.BINANCE")]
    if not symbol:
        return False
    return symbol


def _build_live_canary_portfolio_baseline_provider(
    config: NodeConfig,
    exchange_evidence_provider: Any,
) -> Any:
    live_canary_account = (
        is_live_canary_account(config.account_id)
        and config.binance.environment == "live"
    )
    if not live_canary_account:
        return None
    if exchange_evidence_provider is None:
        raise RuntimeError(
            "live canary account requires an exchange evidence provider"
        )
    cached_snapshot = getattr(
        exchange_evidence_provider,
        "cached_snapshot",
        None,
    )
    if not callable(cached_snapshot):
        raise RuntimeError(
            "live canary account exchange evidence lacks cached snapshots"
        )

    from execution_domain.control_plane import portfolio_baseline_sha256
    from runtime.exchange_cancel_adapter import (
        CONTROL_PLANE_FRESHNESS_BUDGET_SECONDS,
    )

    def current_baseline(target_symbol: str) -> str | bool:
        snapshot = cached_snapshot(
            max_age_seconds=CONTROL_PLANE_FRESHNESS_BUDGET_SECONDS,
        )
        if snapshot is False:
            return False
        return portfolio_baseline_sha256(snapshot, target_symbol)

    return current_baseline


def _build_strategy_config(
    config: NodeConfig,
    lifecycle: Any,
    route: AccountRoute,
    risk_config: RiskLimitConfig,
    intent_execution_inbox_path: Path,
) -> Any:
    from strategy.intent_execution_strategy import IntentExecutionStrategyConfig

    del lifecycle
    kwargs: dict[str, Any] = dict(
        account_id=config.account_id,
        node_id=config.node_id,
        trading_state="HALTED",
        environment=config.binance.environment,
        release_id=_live_canary_release_id(config) or "",
        live_canary_execution_path=str(
            _live_canary_execution_path(route)
        ),
        intent_execution_inbox_path=str(
            intent_execution_inbox_path
        ),
        live_entry_notional_inventory=(
            build_live_entry_notional_inventory(risk_config)
        ),
    )
    # Live Binance accounts here run in Hedge Mode: every order needs a positionSide,
    # which the exec client derives from a per-side position_id. The default (NETTING)
    # leaves position_id None and the adapter raises "position_id was None"; HEDGING
    # makes Nautilus manage a position per side so the adapter can set positionSide.
    # Testnet stays one-way (the tested path) with the default OMS.
    if config.binance.environment == "live":
        from nautilus_trader.model.enums import OmsType

        kwargs["oms_type"] = OmsType.HEDGING
    return IntentExecutionStrategyConfig(**kwargs)


def _live_canary_execution_path(route: AccountRoute) -> Path:
    return route.spool_path.with_suffix(
        ".live-canary-execution.json"
    )


def _intent_execution_inbox_path(route: AccountRoute) -> Path:
    return route.spool_path.with_name("intent_execution_inbox.json")


def _live_canary_release_id(config: NodeConfig) -> str | None:
    if (
        not is_live_canary_account(config.account_id)
        or config.binance.environment != "live"
    ):
        return None
    release_id = os.environ.get("TRADER_RELEASE_ID", "").strip()
    if not release_id:
        raise ValueError(
            "TRADER_RELEASE_ID is required for live canary accounts"
        )
    return release_id


def _account_a_live_release_id(config: NodeConfig) -> str | None:
    return _live_canary_release_id(config)


def _build_strategy(
    runtime: AccountRuntime,
    *,
    fatal_callback: RuntimeFatalCallback,
) -> Any:
    from strategy.intent_execution_strategy import IntentExecutionStrategy

    strategy = IntentExecutionStrategy(runtime.strategy_config)
    set_durable_io_fatal_handler = getattr(
        strategy,
        "set_durable_io_fatal_handler",
        None,
    )
    if not callable(set_durable_io_fatal_handler):
        raise RuntimeError(
            "strategy lacks durable I/O fatal handler injection"
        )
    set_durable_io_fatal_handler(fatal_callback)
    strategy.set_trading_state_getter(lambda: runtime.lifecycle.trading_state)
    set_rollout_phase_getter = getattr(
        strategy,
        "set_live_rollout_phase_getter",
        None,
    )
    if not callable(set_rollout_phase_getter):
        raise RuntimeError(
            "strategy lacks live rollout phase getter injection"
        )
    set_rollout_phase_getter(lambda: runtime.lifecycle.rollout_phase)
    set_live_open_gate_getter = getattr(
        strategy,
        "set_live_open_gate_getter",
        None,
    )
    if not callable(set_live_open_gate_getter):
        raise RuntimeError(
            "strategy lacks live open gate getter injection"
        )
    set_live_open_gate_getter(
        lambda: runtime.lifecycle.live_open_gate
    )
    strategy.set_denial_reporter(_build_denial_reporter(runtime))
    strategy.set_protection_event_reporter(
        _build_protection_event_reporter(runtime)
    )
    if runtime.config.binance.environment == "live":
        mark_provider = runtime.live_entry_mark_snapshot_provider
        set_mark_provider = getattr(
            strategy,
            "set_live_entry_mark_snapshot_getter",
            None,
        )
        if mark_provider is None or not callable(set_mark_provider):
            raise RuntimeError(
                "live strategy lacks mark snapshot provider"
            )
        set_mark_provider(mark_provider)
    if (
        is_live_canary_account(runtime.config.account_id)
        and runtime.config.binance.environment == "live"
    ):
        baseline_provider = (
            runtime.live_canary_portfolio_baseline_provider
        )
        set_baseline_provider = getattr(
            strategy,
            "set_live_canary_portfolio_baseline_getter",
            None,
        )
        if baseline_provider is None or not callable(
            set_baseline_provider
        ):
            raise RuntimeError(
                "live canary strategy lacks portfolio baseline provider"
            )
        set_baseline_provider(baseline_provider)
        set_halt_handler = getattr(
            strategy,
            "set_live_canary_halt_handler",
            None,
        )
        set_risk_reporter = getattr(
            strategy,
            "set_live_canary_risk_reporter",
            None,
        )
        if not callable(set_halt_handler):
            raise RuntimeError(
                "live canary strategy lacks halt handler injection"
            )
        if not callable(set_risk_reporter):
            raise RuntimeError(
                "live canary strategy lacks risk reporter injection"
            )
        set_halt_handler(
            lambda reason: _force_live_canary_halt(runtime, reason)
        )
        set_risk_reporter(
            _build_live_canary_risk_reporter(runtime, strategy)
        )
    strategy.set_exchange_cancel_adapter(
        runtime.exchange_cancel_adapter,
        runtime.exchange_state_mirror,
    )
    set_evidence_provider = getattr(
        strategy,
        "set_exchange_evidence_provider",
        None,
    )
    if not callable(set_evidence_provider):
        raise RuntimeError(
            "strategy lacks exchange evidence provider injection"
        )
    set_evidence_provider(
        getattr(runtime, "exchange_evidence_provider", False)
    )
    terminal_exchange_worker = _build_terminal_exchange_worker(
        runtime,
        strategy,
    )
    if terminal_exchange_worker:
        set_worker = getattr(
            strategy,
            "set_terminal_exchange_worker",
            None,
        )
        if not callable(set_worker):
            terminal_exchange_worker.stop()
            raise RuntimeError(
                "strategy lacks terminal exchange worker injection"
            )
        set_worker(
            terminal_exchange_worker,
            lambda reason: _force_terminal_exchange_halt(
                runtime,
                reason,
            ),
        )
    return strategy


def _build_terminal_exchange_worker(
    runtime: AccountRuntime,
    strategy: Any,
) -> Any:
    from runtime import exchange_cancel_adapter

    worker_type = getattr(
        exchange_cancel_adapter,
        "TerminalExchangeWorker",
        None,
    )
    if worker_type is None:
        if getattr(exchange_cancel_adapter, "__file__", None):
            raise RuntimeError(
                "runtime exchange adapter lacks TerminalExchangeWorker"
            )
        return False

    def degraded(reason: str) -> None:
        _report_runtime_incident(
            runtime,
            "terminal_exchange_lane_degraded",
            reason,
        )

    worker = worker_type(
        account_id=runtime.config.account_id,
        mirror=runtime.exchange_state_mirror,
        adapter=runtime.exchange_cancel_adapter,
        result_publisher=(
            strategy.enqueue_terminal_exchange_result
        ),
        on_degraded=degraded,
        on_halt=lambda reason: _force_terminal_exchange_halt(
            runtime,
            reason,
        ),
    )
    _start_background_worker(runtime, worker)
    _register_health_provider(
        runtime,
        "terminal_exchange_worker",
        lambda: _terminal_exchange_health_snapshot(worker),
    )
    return worker


def _terminal_exchange_health_snapshot(worker: Any) -> dict[str, Any]:
    snapshot = worker.snapshot()
    queue_pressure = "normal"
    if snapshot.degraded:
        queue_pressure = "degraded"
    circuit_state = "closed"
    fatal_failure = ""
    process_liveness = bool(snapshot.running)
    if snapshot.halted:
        circuit_state = "open"
        fatal_failure = snapshot.last_error or "terminal exchange halted"
        process_liveness = False
    return {
        "process_liveness": process_liveness,
        "accepted": snapshot.accepted,
        "completed": snapshot.completed,
        "failed": snapshot.failed,
        "timed_out": snapshot.timed_out,
        "rejected": snapshot.rejected,
        "last_progress_monotonic": (
            snapshot.last_progress_monotonic
        ),
        "lanes": {
            "terminal_exchange": {
                "queue_depth": snapshot.queue_depth,
                "queue_capacity": snapshot.queue_capacity,
                "queue_pressure": queue_pressure,
                "circuit_state": circuit_state,
                "fatal_failure": fatal_failure,
            }
        },
    }


def _force_terminal_exchange_halt(
    runtime: AccountRuntime,
    reason: str,
) -> None:
    halt_reason = str(reason).strip()
    if not halt_reason:
        halt_reason = "terminal exchange runtime safety halt"
    force_halt = getattr(runtime.lifecycle, "force_halt", None)
    if callable(force_halt):
        force_halt(halt_reason)
    else:
        apply_state = getattr(
            runtime.lifecycle,
            "apply_operator_state",
            None,
        )
        if callable(apply_state):
            from execution_domain.control_plane import TradingState

            apply_state(TradingState.HALTED, halt_reason)
    _report_runtime_incident(
        runtime,
        "terminal_exchange_lane_halt",
        halt_reason,
    )


def _build_denial_reporter(runtime: AccountRuntime) -> Callable[[Any, Any], None]:
    from runtime.bounded_task_worker import BoundedTaskWorker

    def handle(task: tuple[Any, Any]) -> None:
        intent, denial = task
        try:
            from execution_domain.control_plane import IntentAckStatus

            status = IntentAckStatus.REJECTED
        except ModuleNotFoundError:
            status = "rejected"
        detail = (
            f"denied:{getattr(denial, 'reason', '')}:"
            f"{getattr(denial, 'detail', '')}"
        )[:200]
        runtime.control_plane.ack_intent(
            account_id=runtime.config.account_id,
            node_id=runtime.config.node_id,
            intent_id=getattr(intent, "intent_id"),
            status=status,
            detail=detail,
        )

    worker = BoundedTaskWorker(
        f"{runtime.config.node_id}.strategy-denial-ack",
        handle,
        capacity=256,
        on_overflow=lambda reason: _mark_runtime_dependency_failed(
            runtime,
            "intent_stream",
            reason,
        ),
        on_error=lambda reason: _mark_runtime_dependency_failed(
            runtime,
            "intent_stream",
            reason,
        ),
    )
    _start_background_worker(runtime, worker)

    def report(intent: Any, denial: Any) -> None:
        worker.submit((intent, denial))

    return report


def _build_protection_event_reporter(
    runtime: AccountRuntime,
) -> Callable[[dict[str, Any]], bool]:
    from runtime.bounded_task_worker import BoundedTaskWorker

    def handle(event: dict[str, Any]) -> None:
        from projection.contracts import ExecutionEventEnvelopeV1

        event_type = str(event["event_type"])
        event_key = str(event["event_key"])
        material = json.dumps(
            {
                "account_id": runtime.config.account_id,
                "node_id": runtime.config.node_id,
                "event_type": event_type,
                "event_key": event_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        event_id = "strategy-" + sha256(material.encode("utf-8")).hexdigest()
        ts_event = event.get("ts_event")
        if not isinstance(ts_event, datetime):
            ts_event = datetime.now(timezone.utc)
        if ts_event.tzinfo is None:
            ts_event = ts_event.replace(tzinfo=timezone.utc)
        payload = dict(event.get("payload") or {})
        payload["event_key"] = event_key
        payload["instrument_id"] = str(event.get("instrument_id") or "")
        intent_id = UUID(str(event["intent_id"]))
        envelope = ExecutionEventEnvelopeV1(
            event_id=event_id,
            node_id=runtime.config.node_id,
            account_id=runtime.config.account_id,
            intent_id=intent_id,
            client_order_id=event.get("client_order_id"),
            event_type=event_type,
            ts_event=ts_event,
            ts_ingest=datetime.now(timezone.utc),
            payload=payload,
        )
        runtime.projection_actor.ingest_event(envelope)
        runtime.projection_actor.flush()

    worker = BoundedTaskWorker(
        f"{runtime.config.node_id}.strategy-protection-event",
        handle,
        capacity=1024,
        on_overflow=lambda reason: _mark_runtime_dependency_failed(
            runtime,
            "projection",
            reason,
        ),
        on_error=lambda reason: _mark_runtime_dependency_failed(
            runtime,
            "projection",
            reason,
        ),
    )
    _start_background_worker(runtime, worker)

    def report(event: dict[str, Any]) -> bool:
        return worker.submit(dict(event))

    return report


def _build_live_canary_risk_reporter(
    runtime: AccountRuntime,
    strategy: Any,
) -> Callable[[dict[str, Any]], bool]:
    from runtime.bounded_task_worker import BoundedTaskWorker

    def handle(task: dict[str, Any]) -> None:
        decisions = strategy.process_live_canary_risk_task(task)
        for decision in decisions:
            strategy.publish_live_canary_loss_decision(decision)

    def fail_closed(reason: str) -> None:
        _force_live_canary_halt(
            runtime,
            f"live canary risk worker failed: {reason}",
        )

    worker = BoundedTaskWorker(
        f"{runtime.config.node_id}.live-canary-risk",
        handle,
        capacity=128,
        on_overflow=fail_closed,
        on_error=fail_closed,
    )
    _start_background_worker(runtime, worker)
    _register_health_provider(
        runtime,
        "live_canary_risk_worker",
        worker.snapshot,
    )

    def report(task: dict[str, Any]) -> bool:
        return worker.submit(dict(task))

    return report


def _force_live_canary_halt(
    runtime: AccountRuntime,
    reason: str,
) -> None:
    halt_reason = str(reason).strip()
    if not halt_reason:
        halt_reason = "live canary runtime safety halt"
    force_halt = getattr(runtime.lifecycle, "force_halt", None)
    if callable(force_halt):
        force_halt(halt_reason)
    else:
        apply_state = getattr(
            runtime.lifecycle,
            "apply_operator_state",
            None,
        )
        if callable(apply_state):
            from execution_domain.control_plane import TradingState

            apply_state(TradingState.HALTED, halt_reason)
    _report_runtime_incident(
        runtime,
        "live_canary_loss_limit",
        halt_reason,
    )


def _start_background_worker(runtime: AccountRuntime, worker: Any) -> None:
    worker.start()
    runtime.background_workers.append(worker)


def _stop_background_workers(runtime: Any) -> None:
    workers = getattr(runtime, "background_workers", None)
    if not isinstance(workers, list):
        return
    errors: list[Exception] = []
    for worker in reversed(workers):
        try:
            stopped = worker.stop()
            if stopped is False:
                errors.append(
                    RuntimeError(
                        "background worker remained alive after stop"
                    )
                )
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise RuntimeError(
            f"background worker cleanup failed with {len(errors)} error(s)"
        ) from errors[0]
    workers.clear()


def _mark_runtime_dependency_failed(
    runtime: AccountRuntime,
    dependency_name: str,
    reason: str,
) -> None:
    dependency = _dependency_by_value(dependency_name)
    marker = getattr(runtime.lifecycle, "mark_dependency_failed", None)
    if dependency is None or not callable(marker):
        return
    marker(dependency, reason)
    reporter = runtime.incident_reporter
    if reporter is not None:
        _report_runtime_incident(
            runtime,
            f"{dependency_name}_runtime_failure",
            f"{dependency_name} failed: {reason}",
        )


def _report_runtime_incident(
    runtime: AccountRuntime,
    reason: str,
    summary: str,
) -> bool:
    reporter = runtime.incident_reporter
    if reporter is None:
        return False
    submitted = bool(reporter(reason, summary))
    if not submitted:
        return False
    lock = runtime.incident_state_lock
    with lock:
        runtime.active_runtime_incident_reasons.add(reason)
        runtime.resolved_runtime_incident_reasons.discard(reason)
    return True


def _resolve_runtime_incident(
    runtime: AccountRuntime,
    reason: str,
    summary: str,
) -> bool:
    resolver = runtime.incident_resolver
    if resolver is None:
        return False
    lock = runtime.incident_state_lock
    with lock:
        if reason in runtime.pending_incident_resolutions:
            return False
        if (
            reason in runtime.resolved_runtime_incident_reasons
            and reason not in runtime.active_runtime_incident_reasons
        ):
            return False
        runtime.pending_incident_resolutions.add(reason)
    submitted = bool(resolver(reason, summary))
    if submitted:
        return True
    with lock:
        runtime.pending_incident_resolutions.discard(reason)
    return False


def _configure_runtime_incident_reporter(
    runtime: AccountRuntime,
) -> None:
    if runtime.config.binance.environment != "live":
        return
    from execution_domain.control_plane import (
        IncidentSeverity,
        ProductionIncidentReport,
        ProductionIncidentResolution,
    )
    from runtime.bounded_task_worker import BoundedTaskWorker

    if not hasattr(runtime, "incident_state_lock"):
        runtime.incident_state_lock = Lock()
    if not hasattr(runtime, "active_runtime_incident_reasons"):
        runtime.active_runtime_incident_reasons = set()
    if not hasattr(runtime, "pending_incident_resolutions"):
        runtime.pending_incident_resolutions = set()
    if not hasattr(runtime, "resolved_runtime_incident_reasons"):
        runtime.resolved_runtime_incident_reasons = set()

    def handle(task: tuple[str, str, str]) -> None:
        operation, reason, summary = task
        if operation == "report":
            runtime.control_plane.report_incident(
                runtime.config.node_id,
                ProductionIncidentReport(
                    account_id=runtime.config.account_id,
                    severity=IncidentSeverity.P1,
                    reason=reason,
                    summary=summary[:2000],
                ),
            )
            return
        if operation != "resolve":
            raise RuntimeError(
                f"unsupported production incident operation: {operation}"
            )
        try:
            runtime.control_plane.resolve_incident(
                runtime.config.node_id,
                ProductionIncidentResolution(
                    account_id=runtime.config.account_id,
                    reason=reason,
                    summary=summary[:2000],
                ),
            )
        except Exception:
            with runtime.incident_state_lock:
                runtime.pending_incident_resolutions.discard(reason)
            raise
        with runtime.incident_state_lock:
            runtime.pending_incident_resolutions.discard(reason)
            runtime.active_runtime_incident_reasons.discard(reason)
            runtime.resolved_runtime_incident_reasons.add(reason)

    def mark_reporter_failed(reason: str) -> None:
        dependency = _dependency_by_value("control_plane")
        marker = getattr(
            runtime.lifecycle,
            "mark_dependency_failed",
            None,
        )
        if dependency is None or not callable(marker):
            return
        marker(
            dependency,
            f"production incident reporter failed: {reason}",
        )

    worker = BoundedTaskWorker(
        f"{runtime.config.node_id}.production-incident",
        handle,
        capacity=64,
        on_overflow=mark_reporter_failed,
        on_error=mark_reporter_failed,
    )
    _start_background_worker(runtime, worker)
    _register_health_provider(
        runtime,
        "production_incident_reporter",
        worker.snapshot,
    )

    def report(reason: str, summary: str) -> bool:
        return worker.submit(("report", reason, summary))

    def resolve(reason: str, summary: str) -> bool:
        return worker.submit(("resolve", reason, summary))

    runtime.incident_reporter = report
    runtime.incident_resolver = resolve


def _check_redis(config: NodeConfig) -> bool:
    parsed = urlparse(config.redis.url)
    if parsed.hostname is None:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=2):
            return True
    except OSError:
        return False


def _check_control_plane(runtime: AccountRuntime) -> bool:
    try:
        live_environment = (
            runtime.config.binance.environment == "live"
        )
        if not live_environment:
            runtime.lifecycle.send_heartbeat()
            return True
        evidence_provider = runtime.exchange_evidence_provider
        if evidence_provider is None:
            return False
        snapshot = evidence_provider.snapshot(force_refresh=True)
        heartbeat = runtime.lifecycle.build_heartbeat(
            exchange_evidence=snapshot,
        )
        runtime.control_plane.heartbeat(
            runtime.config.node_id,
            heartbeat,
        )
        return True
    except Exception:
        return False


def _check_adapter(runtime: AccountRuntime) -> bool:
    try:
        from runtime.binance_adapter_config import build_binance_client_configs

        build_binance_client_configs(runtime.config)
        return True
    except Exception:
        return False


def _mark_dependency(runtime: AccountRuntime, dependency_name: str, healthy: bool) -> None:
    dependency = _dependency_by_value(dependency_name)
    if dependency is None:
        return
    if healthy:
        runtime.lifecycle.mark_dependency_ready(dependency)
    else:
        _mark_runtime_dependency_failed(
            runtime,
            dependency_name,
            "startup readiness check failed",
        )


def _dependency_by_value(value: str) -> Any:
    try:
        from runtime.lifecycle import DependencyName

        for dependency in DependencyName:
            if dependency.value == value:
                return dependency
    except ModuleNotFoundError:
        return value
    return None


class _NautilusIntentPublisher:
    def __init__(self) -> None:
        self._publishers: list[Any] = []

    def publish(self, intent: Any) -> None:
        for publisher in self._publishers:
            publisher.publish(intent)

    def attach(self, publisher: Any) -> None:
        self._publishers.append(publisher)


class _UnavailableControlPlaneClient:
    def fetch_intents(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("control-plane client dependencies are unavailable")

    def ack_intent(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("control-plane client dependencies are unavailable")

    def post_events(self, *args: Any, **kwargs: Any) -> list[str]:
        raise RuntimeError("control-plane client dependencies are unavailable")

    def heartbeat(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("control-plane client dependencies are unavailable")

    def report_incident(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("control-plane client dependencies are unavailable")

    def poll_commands(self, *args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("control-plane client dependencies are unavailable")

    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("control-plane client dependencies are unavailable")

    def latest_snapshot_generated_at(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("control-plane client dependencies are unavailable")


@dataclass(frozen=True)
class _LocalHealthResponse:
    status_code: int
    body: dict[str, Any]


class _LocalHealthService:
    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle

    def liveness(self) -> _LocalHealthResponse:
        restart_required = self._lifecycle.restart_required
        return _LocalHealthResponse(
            status_code=503 if restart_required else 200,
            body={
                "live": not restart_required,
                "account_id": self._lifecycle.config.account_id,
                "node_id": self._lifecycle.config.node_id,
                "trading_state": str(
                    getattr(self._lifecycle.trading_state, "value", self._lifecycle.trading_state)
                ),
                "actor_tick_age_seconds": self._lifecycle.actor_tick_age_seconds,
                "restart_required": restart_required,
            },
        )

    def readiness(self) -> _LocalHealthResponse:
        readiness = self._lifecycle.readiness
        reconciliation = self._lifecycle.reconciliation
        restart_required = self._lifecycle.restart_required
        ready = readiness.ready and not restart_required
        return _LocalHealthResponse(
            status_code=200 if ready else 503,
            body={
                "ready": ready,
                "missing": [
                    str(getattr(dependency, "value", dependency))
                    for dependency in readiness.missing
                ],
                "trading_state": str(
                    getattr(self._lifecycle.trading_state, "value", self._lifecycle.trading_state)
                ),
                "halt_reason": self._lifecycle.halt_reason,
                "actor_tick_age_seconds": self._lifecycle.actor_tick_age_seconds,
                "restart_required": restart_required,
                "reconciliation_status": reconciliation.status,
                "reconciliation_proof_age_seconds": (
                    reconciliation.proof_age_seconds
                ),
                "reconciliation_proof_fresh": reconciliation.fresh,
            },
        )


@dataclass(frozen=True)
class _LocalApprovedIntentDataClient:
    account_id: str
    node_id: str
    offset_path: Path


class _LocalTradingState:
    HALTED = "HALTED"


class _LocalReadiness:
    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = tuple(missing)
        self.ready = not self.missing


@dataclass(frozen=True)
class _LocalReconciliation:
    status: str = "missing"
    proof_age_seconds: float | None = None
    fresh: bool = False


class _LocalLifecycle:
    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self.trading_state = _LocalTradingState.HALTED
        self.halt_reason = "startup"
        self._ready: set[str] = set()
        self._projection_lag_ms = 0
        self._last_event_id: str | None = None
        self.actor_tick_age_seconds = 0.0
        self.restart_required = False
        self.reconciliation = _LocalReconciliation()

    @property
    def readiness(self) -> _LocalReadiness:
        return _LocalReadiness(
            dependency
            for dependency in (
                "instruments",
                "redis",
                "control_plane",
                "intent_stream",
                "command_stream",
                "reconciliation",
                "projection",
            )
            if dependency not in self._ready
        )

    def mark_dependency_ready(self, dependency: Any) -> None:
        value = str(getattr(dependency, "value", dependency))
        if value == "reconciliation":
            return
        self._ready.add(value)

    def mark_dependency_failed(self, dependency: Any, reason: str) -> None:
        self._ready.discard(str(getattr(dependency, "value", dependency)))
        self.trading_state = _LocalTradingState.HALTED
        self.halt_reason = f"{getattr(dependency, 'value', dependency)} failed: {reason}"

    def record_projection_progress(
        self, projection_lag_ms: int, last_event_id: str | None = None
    ) -> None:
        self._projection_lag_ms = projection_lag_ms
        self._last_event_id = last_event_id

    def build_heartbeat(self) -> Any:
        return {
            "ts": datetime.now(timezone.utc),
            "trading_state": self.trading_state,
            "readiness": self.readiness.ready,
            "projection_lag_ms": self._projection_lag_ms,
            "reconciliation_state": "DEGRADED",
            "last_event_id": self._last_event_id,
        }

    def send_heartbeat(self) -> None:
        raise RuntimeError("control-plane client dependencies are unavailable")

    def begin_reconciliation(self, *, halt_active: bool = False) -> None:
        self._ready.discard("reconciliation")
        self.reconciliation = _LocalReconciliation()


class _UnavailableReconciliationCallback:
    def __call__(self, **kwargs: Any) -> None:
        raise RuntimeError("reconciliation callback dependencies are unavailable")
