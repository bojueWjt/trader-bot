from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))


class NodeAppAssemblyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        os.environ["BINANCE_ACCOUNT_A_API_KEY"] = "account-a-key"
        os.environ["BINANCE_ACCOUNT_A_API_SECRET"] = "account-a-secret"
        os.environ["CONTROL_PLANE_ACCOUNT_A_TOKEN"] = "node-a-token"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_build_account_runtime_is_halted_testnet_and_lists_component_order(self) -> None:
        from app.node import build_account_runtime

        with tempfile.TemporaryDirectory() as tmp:
            runtime = build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
                spool_root=Path(tmp),
            )

        self.assertEqual(runtime.config.account_id, "account-a")
        self.assertEqual(runtime.config.binance.environment, "testnet")
        self.assertEqual(str(getattr(runtime.lifecycle.trading_state, "value", runtime.lifecycle.trading_state)), "HALTED")
        self.assertEqual(runtime.route.initial_trading_state, "HALTED")
        self.assertEqual(runtime.route.environment, "testnet")
        self.assertEqual(
            runtime.risk_config.max_order_submit_rate,
            "50/00:00:01",
        )
        self.assertEqual(
            tuple(component.name for component in runtime.components),
            (
                "config",
                "control_plane",
                "lifecycle",
                "route",
                "persistence",
                "risk",
                "binance_adapter",
                "intent_data_client",
                "projection_actor",
                "exchange_state_mirror",
                "exchange_cancel_adapter",
                "intent_execution_strategy",
                "trading_node_config",
                "control_plane_session",
                "trading_node",
            ),
        )

    def test_missing_credentials_fail_closed_before_runtime_exists(self) -> None:
        from app.node import build_account_runtime
        from config.node_config import CredentialResolutionError

        for name in list(os.environ):
            if name.startswith(("BINANCE_ACCOUNT_A_", "CONTROL_PLANE_ACCOUNT_A_")):
                os.environ.pop(name, None)

        with self.assertRaises(CredentialResolutionError):
            build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
            )

    def test_readiness_probe_separates_liveness_and_reports_all_required_dependencies(self) -> None:
        from app.node import build_account_runtime

        runtime = build_account_runtime(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        live = runtime.health.liveness()
        ready = runtime.health.readiness()

        self.assertEqual(live.status_code, 200)
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(
            set(ready.body["missing"]),
            {
                "instruments",
                "redis",
                "control_plane",
                "intent_stream",
                "command_stream",
                "reconciliation",
                "projection",
            },
        )

    def test_nautilus_build_step_uses_injected_builder_and_keeps_host_verify_todos(self) -> None:
        from app.node import build_account_runtime

        calls: list[str] = []

        class FakeNode:
            def __init__(self, config: object) -> None:
                self.config = config
                self.added: list[tuple[str, object]] = []

            def add_data_client_factory(self, name: str, factory: object) -> None:
                self.added.append(("data_factory", name, factory))

            def add_exec_client_factory(self, name: str, factory: object) -> None:
                self.added.append(("exec_factory", name, factory))

            def add_data_client(self, client: object) -> None:
                self.added.append(("data_client", client))

            def add_actor(self, actor: object) -> None:
                self.added.append(("actor", actor))

            def add_strategy(self, strategy: object) -> None:
                self.added.append(("strategy", strategy))

        def fake_builder(runtime):
            calls.append(runtime.config.account_id)
            return FakeNode(config={"node_id": runtime.config.node_id})

        runtime = build_account_runtime(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
            trading_node_builder=fake_builder,
        )

        self.assertEqual(calls, ["account-a"])
        self.assertIsInstance(runtime.trading_node, FakeNode)
        self.assertTrue(runtime.nautilus_api_todos)
        self.assertTrue(
            all("TODO(host-verify)" in item for item in runtime.nautilus_api_todos)
        )

    def test_host_trading_node_builder_registers_strategy_and_actors_on_trader(self) -> None:
        from app.node import build_account_runtime, build_nautilus_trading_node

        fatal_reasons: list[str] = []
        fatal_callback = fatal_reasons.append
        with tempfile.TemporaryDirectory() as tmp, _fake_nautilus_modules() as assembled:
            runtime = build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
                spool_root=Path(tmp),
            )
            node = build_nautilus_trading_node(
                runtime,
                runtime_fatal_callback=fatal_callback,
            )

        self.assertIs(node, assembled["node"])
        self.assertEqual(
            node.added,
            [
                ("data_factory", "BINANCE", assembled["data_factory"]),
                ("exec_factory", "BINANCE", assembled["exec_factory"]),
            ],
        )
        self.assertEqual(
            [type(strategy).__name__ for strategy in node.trader.strategies],
            ["IntentExecutionStrategy"],
        )
        strategy = node.trader.strategies[0]
        self.assertIs(
            strategy.durable_io_fatal_handler,
            fatal_callback,
        )
        self.assertEqual(len(runtime.background_workers), 3)
        self.assertIs(
            strategy.durable_io_cleanup_worker(),
            runtime.background_workers[0],
        )
        self.assertIs(
            strategy.external_io_cleanup_worker(),
            runtime.background_workers[1],
        )
        self.assertTrue(callable(strategy.intent_receipt_handler))
        self.assertTrue(callable(strategy.denial_reporter))
        self.assertTrue(callable(strategy.protection_event_reporter))
        self.assertEqual(
            strategy.exchange_cancel_dependencies,
            (
                runtime.exchange_cancel_adapter,
                runtime.exchange_state_mirror,
            ),
        )
        self.assertEqual(
            [type(actor).__name__ for actor in node.trader.actors],
            ["IntentPublisherActor", "ExecutionProjectionActor", "CommandPollerActor"],
        )
        session = runtime.control_plane_session
        self.assertIsNotNone(session)
        self.assertFalse(session.snapshot().started)
        self.assertFalse(session.snapshot().consumers_ready)
        for actor in node.trader.actors:
            self.assertIs(actor._control_plane_session, session)
            self.assertFalse(actor._manage_control_plane_session)
        command_actor = node.trader.actors[2]
        self.assertEqual(
            command_actor._command_journal.path,
            runtime.route.spool_path.with_name(
                "operator-command-journal.json"
            ),
        )
        self.assertEqual(node.config.kwargs["trader_id"], runtime.config.trader_id)
        self.assertNotIn("instance_id", node.config.kwargs)

    def test_host_trading_node_builder_rejects_duplicate_session_owner(self) -> None:
        from app.node import build_account_runtime, build_nautilus_trading_node

        with tempfile.TemporaryDirectory() as tmp, _fake_nautilus_modules():
            runtime = build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
                spool_root=Path(tmp),
            )
            build_nautilus_trading_node(runtime)

            with self.assertRaisesRegex(
                RuntimeError,
                "session owner is already assembled",
            ):
                build_nautilus_trading_node(runtime)

    def test_shared_session_liveness_wiring_preserves_readiness_dependencies(self) -> None:
        from app.node import build_account_runtime, build_nautilus_trading_node

        with tempfile.TemporaryDirectory() as tmp, _fake_nautilus_modules():
            runtime = build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
                spool_root=Path(tmp),
            )
            build_nautilus_trading_node(runtime)
            session = runtime.control_plane_session
            session.start()
            try:
                live = runtime.health.liveness()
                ready = runtime.health.readiness()

                self.assertEqual(live.status_code, 200)
                self.assertTrue(live.body["live"])
                self.assertFalse(session.snapshot().consumers_ready)
                self.assertEqual(
                    set(ready.body["missing"]),
                    {
                        "instruments",
                        "redis",
                        "control_plane",
                        "intent_stream",
                        "command_stream",
                        "reconciliation",
                        "projection",
                    },
                )
            finally:
                self.assertTrue(session.stop(time.monotonic() + 1.0))

            dead = runtime.health.liveness()
            self.assertEqual(dead.status_code, 503)
            self.assertFalse(dead.body["live"])

    def test_consumer_progress_wiring_reads_actor_progress_property(self) -> None:
        from app.node import _control_plane_consumer_progress

        missing_property = {"command": types.SimpleNamespace()}
        stalled = {
            "command": types.SimpleNamespace(
                control_plane_consumer_last_progress_at=False
            )
        }
        progressing = {
            "command": types.SimpleNamespace(
                control_plane_consumer_last_progress_at=123.5
            )
        }

        self.assertTrue(
            _control_plane_consumer_progress(missing_property)
        )
        self.assertFalse(_control_plane_consumer_progress(stalled))
        self.assertEqual(
            _control_plane_consumer_progress(progressing),
            123.5,
        )


class NautilusActorAdapterTest(unittest.TestCase):
    def test_bulk_order_command_without_authorization_fails_closed(self) -> None:
        from app.nautilus_actors import CommandPollerActor
        from execution_domain.control_plane import (
            CommandAckStatus,
            CommandType,
            NodeCommand,
        )

        actor = CommandPollerActor(
            control_plane=object(),
            lifecycle=types.SimpleNamespace(),
            node_id="node-a",
            account_id="account-a",
        )

        status, error = actor._apply(
            NodeCommand(command_id="cmd-unauthorized", type=CommandType.CANCEL_ALL)
        )

        self.assertEqual(status, CommandAckStatus.FAILED)
        self.assertEqual(error, "authorization_source_required")

    def test_bulk_order_command_for_another_account_fails_closed(self) -> None:
        from app.nautilus_actors import CommandPollerActor
        from execution_domain.control_plane import (
            CommandAckStatus,
            CommandType,
            NodeCommand,
        )

        actor = CommandPollerActor(
            control_plane=object(),
            lifecycle=types.SimpleNamespace(),
            node_id="node-a",
            account_id="account-a",
        )
        command = NodeCommand(
            command_id="cmd-cross-account",
            type=CommandType.CANCEL_ALL,
            args={
                "account_id": "account-b",
                "authorization": {
                    "authorized_by_type": "user",
                    "authorized_by_id": "balen",
                    "source_message_id": "request-42",
                },
            },
        )

        status, error = actor._apply(command)

        self.assertEqual(status, CommandAckStatus.FAILED)
        self.assertEqual(error, "command_account_mismatch")

    def test_intent_publisher_actor_polls_plain_client_and_publishes_to_account_topic(self) -> None:
        from app.nautilus_actors import IntentPublisherActor

        client = _PlainIntentClient()
        message_bus = _RecordingMessageBus()

        class _BoundIntentPublisherActor(IntentPublisherActor):
            def _message_bus(self) -> Any:
                return message_bus

        actor = _BoundIntentPublisherActor(
            client,
            poll_limit=7,
            wait_ms=11,
            custom_data_builder=lambda intent: _CustomData(data_type="intent-type", data=intent),
        )

        self.assertEqual(actor.poll_once(), 1)

        self.assertEqual(client.poll_calls, [(7, 11)])
        self.assertEqual(
            message_bus.published,
            [("intents.account-a", client.intent)],
        )

    def test_projection_actor_forwards_events_and_subscribes_execution_topics(self) -> None:
        from app.nautilus_actors import ExecutionProjectionActor

        projection = _PlainProjection()
        msgbus = _RecordingMessageBus()

        # Nautilus Actor.msgbus is read-only and supplied on register; inject the
        # recording bus through the _subscription_targets seam instead of assigning it.
        class _BoundProjectionActor(ExecutionProjectionActor):
            def _subscription_targets(self):
                return (msgbus,)

        actor = _BoundProjectionActor(projection)
        actor.on_start()
        actor.on_event("order-event")

        self.assertIn("order-event", projection.events)
        self.assertTrue(msgbus.subscriptions)
        topic, handler = msgbus.subscriptions[0]
        self.assertIn("order", topic)
        handler("position-event")
        self.assertIn("position-event", projection.events)


class DeploymentFilesTest(unittest.TestCase):
    def test_compose_uses_real_node_image_readiness_healthcheck_and_no_public_ports(self) -> None:
        compose = (REPO_ROOT / "infra/compose/multi-account.sandbox.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("trader-bot/nautilus-node:", compose)
        self.assertIn("python -m app.healthcheck readiness", compose)
        self.assertIn("python -m app.healthcheck liveness", compose)
        self.assertNotIn("pending-b10", compose)
        self.assertNotIn("\n    ports:", compose)
        self.assertIn("internal: true", compose)

    def test_production_dockerfile_is_pinned_non_root_and_disables_runtime_pip(self) -> None:
        dockerfile = (REPO_ROOT / "infra/docker/nautilus/Dockerfile").read_text(
            encoding="utf-8"
        )
        requirements = (
            REPO_ROOT / "infra/docker/nautilus/requirements.node.lock.txt"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9",
            dockerfile,
        )
        self.assertIn("--require-hashes", dockerfile)
        self.assertIn("USER nautilus", dockerfile)
        self.assertIn("PIP_NO_INDEX=1", dockerfile)
        self.assertIn("nautilus-trader==1.227.0", requirements)
        self.assertIn("--hash=sha256:", requirements)


class _PlainIntentClient:
    def __init__(self) -> None:
        self._publisher = _AttachablePublisher()
        self.poll_calls: list[tuple[int, int]] = []
        self.intent = types.SimpleNamespace(account_id="account-a")

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        self.poll_calls.append((limit, wait_ms))
        self._publisher.publish(self.intent)
        return 1


class _AttachablePublisher:
    def __init__(self) -> None:
        self._publishers: list[Any] = []

    def attach(self, publisher: Any) -> None:
        self._publishers.append(publisher)

    def publish(self, intent: Any) -> None:
        for publisher in self._publishers:
            publisher.publish(intent)


class _CustomData:
    def __init__(self, data_type: Any, data: Any) -> None:
        self.data_type = data_type
        self.data = data


class _PlainProjection:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> str:
        self.events.append(event)
        return str(event)

    def flush(self) -> list[str]:
        return []


class _RecordingMessageBus:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[str, Any]] = []
        self.published: list[tuple[str, Any]] = []

    def subscribe(self, topic: str, handler: Any) -> None:
        self.subscriptions.append((topic, handler))

    def publish(self, topic: str, msg: Any) -> None:
        self.published.append((topic, msg))


@contextmanager
def _fake_nautilus_modules() -> Iterator[dict[str, Any]]:
    old_modules = dict(sys.modules)
    assembled: dict[str, Any] = {}

    class _Config:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class _TradingNode:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.trader = _Trader()
            self.added: list[tuple[Any, ...]] = []
            assembled["node"] = self

        def add_data_client_factory(self, name: str, factory: Any) -> None:
            self.added.append(("data_factory", name, factory))

        def add_exec_client_factory(self, name: str, factory: Any) -> None:
            self.added.append(("exec_factory", name, factory))

        def add_stream_processor(self, processor: Any) -> None:
            self.added.append(("stream_processor", processor))

    class _Trader:
        def __init__(self) -> None:
            self.strategies: list[Any] = []
            self.actors: list[Any] = []

        def add_strategy(self, strategy: Any) -> None:
            self.strategies.append(strategy)

        def add_actor(self, actor: Any) -> None:
            self.actors.append(actor)

    class _Enum:
        USDT_FUTURES = "USDT_FUTURES"
        TESTNET = "TESTNET"

    class IntentExecutionStrategyConfig:
        def __init__(
            self,
            account_id: str = "",
            node_id: str = "",
            trading_state: str = "HALTED",
            existing_intent_ids: tuple[str, ...] = (),
        ) -> None:
            self.account_id = account_id
            self.node_id = node_id
            self.trading_state = trading_state
            self.existing_intent_ids = existing_intent_ids

    class IntentExecutionStrategy:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.durable_io_fatal_handler: Any = None
            self.intent_receipt_handler: Any = None
            self.trading_state_getter: Any = None
            self.denial_reporter: Any = None
            self.protection_event_reporter: Any = None
            self.exchange_cancel_dependencies: Any = None
            self.cleanup_worker = types.SimpleNamespace(stop=lambda: True)
            self.external_cleanup_worker = types.SimpleNamespace(
                stop=lambda: True
            )

        def set_durable_io_fatal_handler(
            self,
            handler: Any,
        ) -> None:
            self.durable_io_fatal_handler = handler

        def durable_io_cleanup_worker(self) -> Any:
            return self.cleanup_worker

        def external_io_cleanup_worker(self) -> Any:
            return self.external_cleanup_worker

        def set_intent_receipt_handler(
            self,
            handler: Any,
        ) -> None:
            self.intent_receipt_handler = handler

        def set_trading_state_getter(self, getter: Any) -> None:
            self.trading_state_getter = getter

        def set_denial_reporter(self, reporter: Any) -> None:
            self.denial_reporter = reporter

        def set_protection_event_reporter(self, reporter: Any) -> None:
            self.protection_event_reporter = reporter

        def set_exchange_cancel_adapter(self, adapter: Any, mirror: Any) -> None:
            self.exchange_cancel_dependencies = (adapter, mirror)

    data_factory = object()
    exec_factory = object()
    assembled["data_factory"] = data_factory
    assembled["exec_factory"] = exec_factory

    _install_module("runtime", types.ModuleType("runtime"))
    runtime_binance_config = types.ModuleType("runtime.binance_adapter_config")
    runtime_binance_config.build_binance_client_configs = lambda config: (_Config(), _Config())
    _install_module("runtime.binance_adapter_config", runtime_binance_config)
    runtime_exchange_cancel = types.ModuleType("runtime.exchange_cancel_adapter")
    runtime_exchange_cancel.BinanceExchangeCancelAdapter = _Config
    runtime_exchange_cancel.ControlPlaneExchangeStateMirror = _Config
    runtime_exchange_cancel.SignedBinanceTransport = _Config
    _install_module("runtime.exchange_cancel_adapter", runtime_exchange_cancel)
    _install_module("strategy", types.ModuleType("strategy"))
    strategy_module = types.ModuleType("strategy.intent_execution_strategy")
    strategy_module.IntentExecutionStrategyConfig = IntentExecutionStrategyConfig
    strategy_module.IntentExecutionStrategy = IntentExecutionStrategy
    _install_module("strategy.intent_execution_strategy", strategy_module)
    _install_module("nautilus_trader", types.ModuleType("nautilus_trader"))
    _install_module("nautilus_trader.adapters", types.ModuleType("nautilus_trader.adapters"))
    _install_module("nautilus_trader.adapters.binance", types.ModuleType("nautilus_trader.adapters.binance"))
    _install_module("nautilus_trader.adapters.binance.factories", types.ModuleType("nautilus_trader.adapters.binance.factories"))
    sys.modules["nautilus_trader.adapters.binance.factories"].BinanceLiveDataClientFactory = data_factory
    sys.modules["nautilus_trader.adapters.binance.factories"].BinanceLiveExecClientFactory = exec_factory
    _install_module("nautilus_trader.adapters.binance.common", types.ModuleType("nautilus_trader.adapters.binance.common"))
    _install_module("nautilus_trader.adapters.binance.common.enums", types.ModuleType("nautilus_trader.adapters.binance.common.enums"))
    sys.modules["nautilus_trader.adapters.binance.common.enums"].BinanceAccountType = _Enum
    sys.modules["nautilus_trader.adapters.binance.common.enums"].BinanceEnvironment = _Enum
    _install_module("nautilus_trader.adapters.binance.config", types.ModuleType("nautilus_trader.adapters.binance.config"))
    sys.modules["nautilus_trader.adapters.binance.config"].BinanceDataClientConfig = _Config
    sys.modules["nautilus_trader.adapters.binance.config"].BinanceExecClientConfig = _Config
    sys.modules["nautilus_trader.adapters.binance.config"].BinanceInstrumentProviderConfig = _Config
    _install_module("nautilus_trader.config", types.ModuleType("nautilus_trader.config"))
    for name in (
        "TradingNodeConfig",
        "DatabaseConfig",
        "CacheConfig",
        "MessageBusConfig",
        "LiveRiskEngineConfig",
        "LiveExecEngineConfig",
    ):
        setattr(sys.modules["nautilus_trader.config"], name, _Config)
    _install_module("nautilus_trader.live", types.ModuleType("nautilus_trader.live"))
    _install_module("nautilus_trader.live.node", types.ModuleType("nautilus_trader.live.node"))
    sys.modules["nautilus_trader.live.node"].TradingNode = _TradingNode

    try:
        yield assembled
    finally:
        sys.modules.clear()
        sys.modules.update(old_modules)


def _install_module(name: str, module: types.ModuleType) -> None:
    sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
