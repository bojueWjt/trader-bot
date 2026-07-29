from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

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
    trading_node: Any = None
    components: tuple[NodeComponent, ...] = ()
    nautilus_api_todos: tuple[str, ...] = NAUTILUS_API_TODOS


TradingNodeBuilder = Callable[[AccountRuntime], Any]


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


def build_nautilus_trading_node(runtime: AccountRuntime) -> Any:
    """Build a Nautilus ``TradingNode`` for a fully assembled account runtime.

    This is intentionally host-only. The local development host does not have
    Nautilus installed; hk must execute this path against the pinned wheel.
    """

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
    from persistence.nautilus_config import (  # lazy: imports Nautilus config classes
        build_cache_config,
        build_live_exec_engine_config,
        build_message_bus_config,
    )
    from risk.config import build_live_risk_engine_config
    from runtime.binance_adapter_config import build_binance_client_configs

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
    strategy = _build_strategy(runtime)
    node.trader.add_strategy(strategy)
    node.trader.add_actor(IntentPublisherActor(runtime.intent_data_client))
    node.trader.add_actor(ExecutionProjectionActor(runtime.projection_actor))
    node.trader.add_actor(
        CommandPollerActor(
            runtime.control_plane,
            runtime.lifecycle,
            runtime.config.node_id,
            account_id=runtime.config.account_id,
        )
    )
    return node


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

        return ApprovedIntentDataClient(
            account_id=config.account_id,
            node_id=config.node_id,
            source=control_plane,
            publisher=_NautilusIntentPublisher(),
            offset_store=JsonIntentOffsetStore(route.spool_path.with_suffix(".offset.json")),
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


def _build_strategy(runtime: AccountRuntime) -> Any:
    from strategy.intent_execution_strategy import IntentExecutionStrategy

    strategy = IntentExecutionStrategy(runtime.strategy_config)
    strategy.set_trading_state_getter(lambda: runtime.lifecycle.trading_state)
    strategy.set_denial_reporter(_build_denial_reporter(runtime))
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

    def liveness(self) -> _LocalHealthResponse:
        return _LocalHealthResponse(
            status_code=200,
            body={
                "live": True,
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
                "trading_state": str(
                    getattr(self._lifecycle.trading_state, "value", self._lifecycle.trading_state)
                ),
                "halt_reason": self._lifecycle.halt_reason,
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


class _LocalLifecycle:
    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self.trading_state = _LocalTradingState.HALTED
        self.halt_reason = "startup"
        self._ready: set[str] = set()
        self._projection_lag_ms = 0
        self._last_event_id: str | None = None

    @property
    def readiness(self) -> _LocalReadiness:
        return _LocalReadiness(
            dependency
            for dependency in (
                "instruments",
                "redis",
                "control_plane",
                "reconciliation",
                "projection",
            )
            if dependency not in self._ready
        )

    def mark_dependency_ready(self, dependency: Any) -> None:
        self._ready.add(str(getattr(dependency, "value", dependency)))

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
