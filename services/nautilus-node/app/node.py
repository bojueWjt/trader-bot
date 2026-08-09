from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
from uuid import UUID

from config.node_config import NodeConfig, load_node_config
from persistence.nautilus_config import (
    build_nautilus_persistence_config,
    build_trading_node_kwargs,
)
from projection.actor import LifecycleProjectionHealth, ProjectionActor
from projection.event_mapper import ProjectionConfig
from projection.spool import JsonExecutionSpool
from risk.config import RiskLimitConfig, build_live_risk_engine_kwargs
from routing.multi_account import AccountRoute
try:
    from runtime.health import HealthService
except ModuleNotFoundError:
    HealthService = None  # type: ignore[assignment]


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
_CONSUMER_PROGRESS_UNAVAILABLE = object()


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
    route: AccountRoute
    persistence: Any
    risk_config: RiskLimitConfig
    risk_engine_kwargs: dict[str, Any]
    intent_data_client: Any
    projection_actor: ProjectionActor
    exchange_state_mirror: Any
    exchange_cancel_adapter: Any
    strategy_config: Any
    trading_node_config_kwargs: dict[str, Any]
    control_plane_session: Any = None
    background_workers: list[Any] = field(default_factory=list)
    trading_node: Any = None
    background_workers: list[Any] = field(default_factory=list)
    components: tuple[NodeComponent, ...] = ()
    nautilus_api_todos: tuple[str, ...] = NAUTILUS_API_TODOS


TradingNodeBuilder = Callable[[AccountRuntime], Any]
RuntimeFatalCallback = Callable[[str], None]


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
    route = _build_route(config, spool_root)
    persistence = build_nautilus_persistence_config(config)
    risk_config = _build_risk_limit_config()
    risk_engine_kwargs = build_live_risk_engine_kwargs(risk_config)
    intent_data_client = _build_intent_data_client(config, control_plane, lifecycle, route)
    projection_actor = _build_projection_actor(config, control_plane, lifecycle, route)
    exchange_state_mirror, exchange_cancel_adapter = _build_exchange_cancel_dependencies(
        config
    )
    strategy_config = _build_strategy_config(config, lifecycle)
    trading_node_config_kwargs = build_trading_node_kwargs(config)

    runtime = AccountRuntime(
        config=config,
        control_plane=control_plane,
        lifecycle=lifecycle,
        health=health,
        route=route,
        persistence=persistence,
        risk_config=risk_config,
        risk_engine_kwargs=risk_engine_kwargs,
        intent_data_client=intent_data_client,
        projection_actor=projection_actor,
        exchange_state_mirror=exchange_state_mirror,
        exchange_cancel_adapter=exchange_cancel_adapter,
        strategy_config=strategy_config,
        trading_node_config_kwargs=trading_node_config_kwargs,
    )
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
    runtime_fatal_callback: RuntimeFatalCallback | None = None,
) -> Any:
    """Build a Nautilus ``TradingNode`` for a fully assembled account runtime.

    This is intentionally host-only. The local development host does not have
    Nautilus installed; hk must execute this path against the pinned wheel.
    """

    if runtime.control_plane_session is not None:
        raise RuntimeError(
            "control-plane session owner is already assembled"
        )

    from nautilus_trader.adapters.binance.factories import (  # type: ignore[import-not-found]
        BinanceLiveDataClientFactory,
        BinanceLiveExecClientFactory,
    )
    from nautilus_trader.config import TradingNodeConfig  # type: ignore[import-not-found]
    from nautilus_trader.live.node import TradingNode  # type: ignore[import-not-found]

    from app.nautilus_actors import (
        CommandPollerActor,
        ExecutionProjectionActor,
        IntentPublisherActor,
    )
    from commands.durable_command_journal import (
        DurableCommandJournal,
    )
    from persistence.nautilus_config import (  # lazy: imports Nautilus config classes
        build_cache_config,
        build_live_exec_engine_config,
        build_message_bus_config,
    )
    from risk.config import build_live_risk_engine_config
    from runtime.binance_adapter_config import build_binance_client_configs
    from runtime.control_plane_session import NodeControlPlaneSession

    fatal_callback = runtime_fatal_callback
    if fatal_callback is None:
        fatal_callback = lambda _reason: os._exit(75)

    data_client_config, exec_client_config = build_binance_client_configs(runtime.config)
    node_config = TradingNodeConfig(
        # NOTE: instance_id is intentionally omitted. Nautilus 1.227.0 requires a
        # core UUID4 (rejects a plain string) and auto-generates a fresh per-process
        # UUID4. Per-account isolation + restart-persistent redis keys come from the
        # stable trader_id prefix (CacheConfig.use_trader_prefix), not instance_id.
        trader_id=runtime.config.trader_id,
        cache=build_cache_config(runtime.config),
        message_bus=build_message_bus_config(runtime.config),
        risk_engine=build_live_risk_engine_config(runtime.risk_config),
        exec_engine=build_live_exec_engine_config(runtime.config),
        data_clients={"BINANCE": data_client_config},
        exec_clients={"BINANCE": exec_client_config},
    )
    node = TradingNode(config=node_config)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)
    strategy = _build_strategy(
        runtime,
        fatal_callback=fatal_callback,
    )
    strategy.set_durable_io_actor_dispatcher(
        _node_actor_dispatcher(node)
    )
    node.trader.add_strategy(strategy)
    actor_holder: dict[str, Any] = {}
    session_config = runtime.config.control_plane.session
    session = NodeControlPlaneSession(
        heartbeat=lambda: actor_holder[
            "command"
        ].session_send_heartbeat(),
        command_poll=lambda capacity: actor_holder[
            "command"
        ].session_poll_commands(capacity),
        command_apply=lambda command: actor_holder[
            "command"
        ].session_apply_command(command),
        command_ack=lambda acknowledgement: actor_holder[
            "command"
        ].session_ack_command(acknowledgement),
        intent_replay=_intent_replay_callback(
            runtime.intent_data_client
        ),
        intent_fetch=lambda capacity: runtime.intent_data_client.fetch_once(
            limit=capacity,
            wait_ms=0,
        ),
        intent_deliver=runtime.intent_data_client.deliver,
        execution_event_sink=lambda event: actor_holder[
            "projection"
        ].session_flush_execution_event(event),
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
        fatal_termination_hook=lambda reason: (
            _mark_control_plane_session_fatal(runtime, reason)
        ),
        consumer_ready=lambda: _control_plane_consumers_ready(
            actor_holder
        ),
        consumer_progress=lambda: _control_plane_consumer_progress(
            actor_holder
        ),
        thread_name_prefix=(
            f"node-control-plane.{runtime.config.account_id}"
        ),
    )
    intent_actor = IntentPublisherActor(
        runtime.intent_data_client,
        lifecycle=runtime.lifecycle,
        control_plane_session=session,
        manage_control_plane_session=False,
        worker_shutdown_wait_seconds=(
            session_config.shutdown_timeout_seconds
        ),
    )
    projection_actor = ExecutionProjectionActor(
        runtime.projection_actor,
        control_plane_session=session,
        manage_control_plane_session=False,
        worker_shutdown_wait_seconds=(
            session_config.shutdown_timeout_seconds
        ),
        fatal_callback=lambda reason: _mark_dependency_reason(
            runtime,
            "projection",
            reason,
            hard=True,
        ),
        degraded_callback=lambda reason: _mark_dependency_reason(
            runtime,
            "projection",
            reason,
            hard=False,
        ),
    )
    command_actor = CommandPollerActor(
        runtime.control_plane,
        runtime.lifecycle,
        runtime.config.node_id,
        account_id=runtime.config.account_id,
        command_journal=DurableCommandJournal(
            runtime.route.spool_path.with_name(
                "operator-command-journal.json"
            ),
            account_id=runtime.config.account_id,
            node_id=runtime.config.node_id,
        ),
        control_plane_session=session,
        manage_control_plane_session=False,
        worker_shutdown_wait_seconds=(
            session_config.shutdown_timeout_seconds
        ),
    )
    actor_holder.update(
        {
            "command": command_actor,
            "intent": intent_actor,
            "projection": projection_actor,
        }
    )
    runtime.control_plane_session = session
    _wire_control_plane_session_liveness(runtime, session)
    node.trader.add_actor(intent_actor)
    node.trader.add_actor(projection_actor)
    node.trader.add_actor(command_actor)
    return node


def _node_actor_dispatcher(
    node: Any,
) -> Callable[[Callable[[], None]], None]:
    kernel = getattr(node, "kernel", None)
    loop = getattr(kernel, "loop", None)
    dispatcher = getattr(loop, "call_soon_threadsafe", None)
    if not callable(dispatcher):
        raise RuntimeError(
            "TradingNode actor loop dispatcher is unavailable"
        )
    return dispatcher


def run_startup_readiness_checks(runtime: AccountRuntime) -> None:
    _mark_dependency(runtime, "redis", _check_redis(runtime.config))
    _mark_dependency(runtime, "control_plane", _check_control_plane(runtime))
    _mark_dependency(runtime, "instruments", _check_adapter(runtime))
    _mark_dependency(runtime, "reconciliation", bool(runtime.risk_engine_kwargs))
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
        NodeComponent("intent_execution_strategy", runtime.strategy_config),
        NodeComponent("trading_node_config", runtime.trading_node_config_kwargs),
        NodeComponent(
            "control_plane_session",
            runtime.control_plane_session,
        ),
        NodeComponent("trading_node", runtime.trading_node),
    )


def _intent_replay_callback(intent_data_client: Any) -> Callable[[], Any] | None:
    for name in ("replay_pending", "replay_durable_inbox"):
        callback = getattr(intent_data_client, name, None)
        if callable(callback):
            return callback
    return None


def _mark_control_plane_session_fatal(
    runtime: AccountRuntime,
    reason: str,
) -> None:
    _mark_dependency_reason(
        runtime,
        "control_plane",
        f"control-plane session fatal: {reason}",
        hard=True,
    )


def _control_plane_consumers_ready(
    actor_holder: dict[str, Any],
) -> bool:
    for name in ("intent", "projection", "command"):
        actor = actor_holder.get(name)
        if actor is None:
            return False
        if not _actor_consumer_ready(actor):
            return False
    return True


def _actor_consumer_ready(actor: Any) -> bool:
    explicit = getattr(
        actor,
        "control_plane_consumer_ready",
        None,
    )
    if callable(explicit):
        return bool(explicit())
    if explicit is not None:
        return bool(explicit)
    running = getattr(actor, "is_running", False)
    if callable(running):
        return bool(running())
    return bool(running)


def _control_plane_consumer_progress(
    actor_holder: dict[str, Any],
) -> float | bool | None:
    actor = actor_holder.get("command")
    if actor is None:
        return False
    progress = getattr(
        actor,
        "control_plane_consumer_last_progress_at",
        _CONSUMER_PROGRESS_UNAVAILABLE,
    )
    if progress is _CONSUMER_PROGRESS_UNAVAILABLE:
        return True
    if callable(progress):
        return progress()
    return progress


def _wire_control_plane_session_liveness(
    runtime: AccountRuntime,
    session: Any,
) -> None:
    setter = getattr(
        runtime.health,
        "set_process_liveness_provider",
        None,
    )
    if not callable(setter):
        return
    setter(lambda: _session_process_liveness(session))


def _session_process_liveness(session: Any) -> bool:
    snapshot = session.snapshot()
    if not snapshot.started and not snapshot.stopped:
        return True
    return bool(snapshot.process_liveness)


def _mark_dependency_reason(
    runtime: AccountRuntime,
    dependency_name: str,
    reason: str,
    *,
    hard: bool,
) -> None:
    dependency = _dependency_by_value(dependency_name)
    if dependency is None:
        return
    method_name = "mark_dependency_degraded"
    if hard:
        method_name = "mark_dependency_failed"
    marker = getattr(runtime.lifecycle, method_name, None)
    if callable(marker):
        marker(dependency, reason)


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


def _build_route(config: NodeConfig, spool_root: str | Path | None) -> AccountRoute:
    return AccountRoute(
        account_id=config.account_id,
        node_id=config.node_id,
        redis_key_prefix=config.redis.key_prefix,
        control_plane_base_url=config.control_plane.base_url,
        spool_root=spool_root or os.environ.get(
            "NAUTILUS_SPOOL_ROOT", "/var/lib/nautilus-node/spool"
        ),
        environment=config.binance.environment,
        # HALTED is the safe default (PLAN: live off by default). Overridable for
        # testnet acceptance until the operator RESUME command path is wired into the
        # node (host-verify gap: no command poller calls control_plane.poll_commands).
        initial_trading_state=os.environ.get("NAUTILUS_INITIAL_TRADING_STATE", "HALTED"),
    )


def _build_risk_limit_config() -> RiskLimitConfig:
    raw_limits = os.environ.get("NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON")
    if raw_limits:
        limits = json.loads(raw_limits)
        if not isinstance(limits, dict):
            raise ValueError("NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON must be an object")
        max_notional = {str(key): str(value) for key, value in limits.items()}
    else:
        max_notional = {"BTCUSDT-PERP.BINANCE": "1"}
    return RiskLimitConfig(
        max_notional_per_order=max_notional,
        max_order_submit_rate=os.environ.get(
            # Zone ladders and protection orders submit in one bounded burst.
            "NAUTILUS_MAX_ORDER_SUBMIT_RATE", "50/00:00:01"
        ),
        max_order_modify_rate=os.environ.get(
            "NAUTILUS_MAX_ORDER_MODIFY_RATE", "1/00:00:01"
        ),
    )


def _build_intent_data_client(
    config: NodeConfig,
    control_plane: Any,
    lifecycle: Any,
    route: AccountRoute,
) -> Any:
    try:
        from data_client.approved_intent_client import (
            ApprovedIntentDataClient,
            JsonIntentOffsetStore,
        )
        from data_client.durable_intent_inbox import (
            JsonDurableIntentInbox,
        )

        offset_path = route.spool_path.with_suffix(".offset.json")
        return ApprovedIntentDataClient(
            account_id=config.account_id,
            node_id=config.node_id,
            source=control_plane,
            publisher=_NautilusIntentPublisher(),
            offset_store=JsonIntentOffsetStore(offset_path),
            intent_inbox=JsonDurableIntentInbox(
                route.spool_path.with_suffix(".intent-inbox.json")
            ),
            trading_state=lambda: lifecycle.trading_state,
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
    return ProjectionActor(
        config=ProjectionConfig(node_id=config.node_id, account_id=config.account_id),
        sink=control_plane,
        spool=JsonExecutionSpool(route.spool_path),
        health=LifecycleProjectionHealth(lifecycle),
    )


def _build_exchange_cancel_dependencies(config: NodeConfig) -> tuple[Any, Any]:
    from runtime.exchange_cancel_adapter import (
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
    base_url = "https://testnet.binancefuture.com"
    if config.binance.environment == "live":
        base_url = "https://fapi.binance.com"
    transport = SignedBinanceTransport(
        base_url=base_url,
        api_key=config.binance.credentials.api_key,
        api_secret=config.binance.credentials.api_secret,
    )
    adapter = BinanceExchangeCancelAdapter(
        account_id=config.account_id,
        transport=transport,
    )
    return mirror, adapter


def _build_strategy_config(config: NodeConfig, lifecycle: Any) -> Any:
    from strategy.intent_execution_strategy import IntentExecutionStrategyConfig

    del lifecycle
    kwargs: dict[str, Any] = dict(
        account_id=config.account_id,
        node_id=config.node_id,
        trading_state="HALTED",
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


def _build_strategy(
    runtime: AccountRuntime,
    *,
    fatal_callback: RuntimeFatalCallback,
) -> Any:
    from strategy.intent_execution_strategy import IntentExecutionStrategy

    strategy = IntentExecutionStrategy(runtime.strategy_config)
    strategy.set_durable_io_fatal_handler(fatal_callback)
    _register_background_cleanup_worker(
        runtime,
        strategy,
        "durable_io_cleanup_worker",
    )
    _register_background_cleanup_worker(
        runtime,
        strategy,
        "external_io_cleanup_worker",
    )
    intent_receipt_handler = getattr(
        runtime.intent_data_client,
        "record_execution_terminal",
        None,
    )
    if callable(intent_receipt_handler):
        strategy.set_intent_receipt_handler(
            intent_receipt_handler
        )
    intent_receipt_transition_handler = getattr(
        runtime.intent_data_client,
        "persist_execution_receipt_transition",
        None,
    )
    intent_receipt_transition_setter = getattr(
        strategy,
        "set_intent_receipt_transition_handler",
        None,
    )
    if (
        callable(intent_receipt_transition_handler)
        and callable(intent_receipt_transition_setter)
    ):
        intent_receipt_transition_setter(
            intent_receipt_transition_handler
        )
    intent_receipt_status_getter = getattr(
        runtime.intent_data_client,
        "intent_receipt_status",
        None,
    )
    intent_receipt_status_setter = getattr(
        strategy,
        "set_intent_receipt_status_getter",
        None,
    )
    if (
        callable(intent_receipt_status_getter)
        and callable(intent_receipt_status_setter)
    ):
        intent_receipt_status_setter(
            intent_receipt_status_getter
        )
    inbox_fatal_setter = getattr(
        runtime.intent_data_client,
        "set_durable_inbox_fatal_handler",
        None,
    )
    if callable(inbox_fatal_setter):
        inbox_fatal_setter(fatal_callback)
    durable_inbox_worker = getattr(
        runtime.intent_data_client,
        "durable_inbox_cleanup_worker",
        None,
    )
    if callable(durable_inbox_worker):
        _register_background_cleanup_worker(
            runtime,
            runtime.intent_data_client,
            "durable_inbox_cleanup_worker",
        )
    strategy.set_trading_state_getter(lambda: runtime.lifecycle.trading_state)
    strategy.set_denial_reporter(_build_denial_reporter(runtime))
    strategy.set_protection_event_reporter(
        _build_protection_event_reporter(runtime)
    )
    strategy.set_exchange_cancel_adapter(
        runtime.exchange_cancel_adapter,
        runtime.exchange_state_mirror,
    )
    return strategy


def _build_denial_reporter(runtime: AccountRuntime) -> Callable[[Any, Any], None]:
    def report(intent: Any, denial: Any) -> None:
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

    return report


def _build_protection_event_reporter(
    runtime: AccountRuntime,
) -> Callable[[dict[str, Any]], bool]:
    def report(event: dict[str, Any]) -> bool:
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
        acked = runtime.control_plane.post_events(
            runtime.config.node_id,
            [envelope],
        )
        return event_id in {str(item) for item in acked}

    return report


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
        runtime.lifecycle.send_heartbeat()
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
        runtime.lifecycle.mark_dependency_failed(dependency, "startup readiness check failed")


def _dependency_by_value(value: str) -> Any:
    try:
        from runtime.lifecycle import DependencyName

        for dependency in DependencyName:
            if dependency.value == value:
                return dependency
    except ModuleNotFoundError:
        return value
    return None


def _stop_control_plane_session(runtime: Any) -> None:
    session = getattr(runtime, "control_plane_session", None)
    if session is None:
        return
    stopped = bool(session.stop(time.monotonic() + 1.0))
    if not stopped:
        raise RuntimeError(
            "control-plane session failed to drain before deadline"
        )
    runtime.control_plane_session = None


def _register_background_cleanup_worker(
    runtime: AccountRuntime,
    owner: Any,
    provider_name: str,
) -> Any:
    provider = getattr(owner, provider_name, None)
    if not callable(provider):
        raise RuntimeError(
            f"runtime component lacks cleanup provider {provider_name}"
        )
    worker = provider()
    stop = getattr(worker, "stop", None)
    if not callable(stop):
        raise RuntimeError(
            f"runtime cleanup provider {provider_name} lacks stop"
        )
    if all(existing is not worker for existing in runtime.background_workers):
        runtime.background_workers.append(worker)
    return worker


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


def _stop_redis_runtime_safety(runtime: Any) -> None:
    guard = getattr(runtime, "redis_runtime_safety_guard", None)
    client = getattr(runtime, "redis_runtime_safety_client", None)
    if guard is not None:
        try:
            stopped = guard.stop()
            if (
                stopped is False
                and not _cleanup_target_confirms_stopped(guard)
            ):
                raise RuntimeError(
                    "Redis runtime safety guard remained alive after stop"
                )
        except Exception as exc:
            raise RuntimeError(
                "Redis runtime safety cleanup failed"
            ) from exc
        runtime.redis_runtime_safety_guard = None
    if client is not None:
        try:
            client.close()
        except Exception as exc:
            raise RuntimeError(
                "Redis runtime safety cleanup failed"
            ) from exc
        runtime.redis_runtime_safety_client = None


def _cleanup_target_confirms_stopped(target: Any) -> bool:
    snapshot = getattr(target, "snapshot", None)
    if not callable(snapshot):
        return False
    try:
        state = snapshot()
    except Exception:
        return False
    if not isinstance(state, dict):
        return False
    return state.get("running") is False


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
        self._process_liveness: Callable[[], bool] | None = None

    def set_process_liveness_provider(
        self,
        provider: Callable[[], bool],
    ) -> None:
        self._process_liveness = provider

    def liveness(self) -> _LocalHealthResponse:
        live = self._resolve_process_liveness()
        return _LocalHealthResponse(
            status_code=200 if live else 503,
            body={
                "live": live,
                "account_id": self._lifecycle.config.account_id,
                "node_id": self._lifecycle.config.node_id,
                "trading_state": str(
                    getattr(self._lifecycle.trading_state, "value", self._lifecycle.trading_state)
                ),
            },
        )

    def readiness(self) -> _LocalHealthResponse:
        readiness = self._lifecycle.readiness
        return _LocalHealthResponse(
            status_code=200 if readiness.ready else 503,
            body={
                "ready": readiness.ready,
                "missing": [
                    str(getattr(dependency, "value", dependency))
                    for dependency in readiness.missing
                ],
                "degraded": [
                    {
                        "dependency": str(
                            getattr(dependency, "value", dependency)
                        ),
                        "reason": reason,
                    }
                    for dependency, reason in readiness.degraded
                ],
                "trading_state": str(
                    getattr(self._lifecycle.trading_state, "value", self._lifecycle.trading_state)
                ),
                "halt_reason": self._lifecycle.halt_reason,
            },
        )

    def _resolve_process_liveness(self) -> bool:
        provider = self._process_liveness
        if provider is None:
            return True
        try:
            return bool(provider())
        except Exception:
            return False


@dataclass(frozen=True)
class _LocalApprovedIntentDataClient:
    account_id: str
    node_id: str
    offset_path: Path


class _LocalTradingState:
    HALTED = "HALTED"


class _LocalReadiness:
    def __init__(
        self,
        missing: Iterable[str],
        degraded: Iterable[tuple[str, str]],
    ) -> None:
        self.missing = tuple(missing)
        self.degraded = tuple(degraded)
        self.ready = not self.missing


class _LocalLifecycle:
    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self.trading_state = _LocalTradingState.HALTED
        self.halt_reason = "startup"
        self._ready: set[str] = set()
        self._degraded: dict[str, str] = {}
        self._projection_lag_ms = 0
        self._last_event_id: str | None = None

    @property
    def readiness(self) -> _LocalReadiness:
        dependencies = (
            "instruments",
            "redis",
            "control_plane",
            "intent_stream",
            "command_stream",
            "reconciliation",
            "projection",
        )
        return _LocalReadiness(
            (
                dependency
                for dependency in dependencies
                if dependency not in self._ready
            ),
            (
                (dependency, self._degraded[dependency])
                for dependency in dependencies
                if dependency in self._degraded
            ),
        )

    def mark_dependency_ready(self, dependency: Any) -> None:
        value = str(getattr(dependency, "value", dependency))
        self._ready.add(value)
        self._degraded.pop(value, None)

    def mark_dependency_degraded(
        self,
        dependency: Any,
        reason: str,
    ) -> None:
        value = str(getattr(dependency, "value", dependency))
        self._degraded[value] = str(reason)

    def mark_dependency_failed(self, dependency: Any, reason: str) -> None:
        value = str(getattr(dependency, "value", dependency))
        self._ready.discard(value)
        self._degraded.pop(value, None)
        self.trading_state = _LocalTradingState.HALTED
        self.halt_reason = f"{value} failed: {reason}"

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
