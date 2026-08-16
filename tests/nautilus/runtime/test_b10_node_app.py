from __future__ import annotations

import os
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch
from urllib.error import HTTPError


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
        os.environ["TRADER_RELEASE_ID"] = "release-a"
        os.environ["TRADER_RELEASE_IMAGE_DIGEST"] = "sha256:" + ("1" * 64)
        os.environ["TRADER_RELEASE_CONFIG_SHA256"] = "2" * 64
        os.environ["TRADER_RELEASE_DEPENDENCY_LOCK_SHA256"] = "3" * 64
        os.environ[
            "TRADER_RELEASE_SCHEMA_EPOCH"
        ] = "0015_refresh_evidence_command"

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
                "exchange_evidence_provider",
                "intent_execution_strategy",
                "trading_node_config",
                "control_plane_session",
                "redis_runtime_safety_guard",
                "trading_node",
            ),
        )
        self.assertEqual(
            runtime.intent_data_client._intent_execution_inbox._path,
            Path(runtime.strategy_config.intent_execution_inbox_path),
        )

    def test_build_account_runtime_passes_configured_proxy_to_signed_transport(
        self,
    ) -> None:
        from app.node import build_account_runtime
        from runtime import exchange_cancel_adapter

        proxy_url = "https://100.107.72.78:13128"
        config_path = (
            SERVICE_ROOT
            / "config"
            / "examples"
            / "account-a.sandbox.json"
        )
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["binance"]["proxy_url"] = proxy_url

        with tempfile.TemporaryDirectory() as tmp:
            temporary_root = Path(tmp)
            temporary_config = temporary_root / "account-a.proxy.json"
            temporary_config.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            with patch.object(
                exchange_cancel_adapter,
                "SignedBinanceTransport",
            ) as transport_type:
                transport_type.return_value = object()
                build_account_runtime(
                    temporary_config,
                    spool_root=temporary_root / "spool",
                )

        self.assertEqual(
            transport_type.call_args.kwargs["proxy_url"],
            proxy_url,
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

    def test_live_accounts_use_release_bound_risk_config(
        self,
    ) -> None:
        from app.node import _build_risk_limit_config, _build_route
        from config.node_config import RiskNodeConfig, load_node_config

        config = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        for env_name in (
            "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON",
            "NAUTILUS_MAX_ORDER_SUBMIT_RATE",
            "NAUTILUS_MAX_ORDER_MODIFY_RATE",
        ):
            os.environ.pop(env_name, None)

        for account_id, node_id in (
            ("account-a", "node-a"),
            ("account-b", "node-b"),
            ("account-c", "node-c"),
            ("account-d", "node-d"),
        ):
            with self.subTest(account_id=account_id):
                live_config = replace(
                    config,
                    account_id=account_id,
                    node_id=node_id,
                    redis=replace(
                        config.redis,
                        key_prefix=f"nautilus:{account_id}:{node_id}",
                    ),
                    binance=replace(
                        config.binance,
                        environment="live",
                    ),
                )
                os.environ["NAUTILUS_INITIAL_TRADING_STATE"] = "ACTIVE"
                route = _build_route(live_config, None)
                self.assertEqual(route.initial_trading_state, "HALTED")

                with self.assertRaisesRegex(
                    ValueError,
                    "requires risk configuration in node JSON",
                ):
                    _build_risk_limit_config(live_config)

                migrated = replace(
                    live_config,
                    risk=RiskNodeConfig(
                        max_notional_per_order={
                            "BTCUSDT-PERP.BINANCE": "100"
                        },
                        max_order_submit_rate="50/00:00:01",
                        max_order_modify_rate="1/00:00:01",
                    ),
                )
                limits = _build_risk_limit_config(migrated)
                self.assertEqual(
                    limits.max_notional_per_order,
                    {"BTCUSDT-PERP.BINANCE": "100"},
                )
                self.assertEqual(
                    limits.max_order_submit_rate,
                    "50/00:00:01",
                )
                self.assertEqual(
                    limits.max_order_modify_rate,
                    "1/00:00:01",
                )

                os.environ[
                    "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON"
                ] = json.dumps(
                    {"BTCUSDT-PERP.BINANCE": "12"}
                )
                try:
                    with self.assertRaisesRegex(
                        ValueError,
                        "env overrides are forbidden",
                    ):
                        _build_risk_limit_config(migrated)
                finally:
                    os.environ.pop(
                        "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON",
                        None,
                    )

    def test_live_canary_release_id_covers_all_execution_accounts(
        self,
    ) -> None:
        from app.node import _live_canary_release_id
        from config.node_config import load_node_config

        config = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )

        for account_id, node_id in (
            ("account-a", "node-a"),
            ("account-b", "node-b"),
            ("account-c", "node-c"),
            ("account-d", "node-d"),
        ):
            with self.subTest(account_id=account_id):
                live_config = replace(
                    config,
                    account_id=account_id,
                    node_id=node_id,
                    binance=replace(
                        config.binance,
                        environment="live",
                    ),
                )
                self.assertEqual(
                    _live_canary_release_id(live_config),
                    "release-a",
                )

        self.assertIs(_live_canary_release_id(config), None)

    def test_live_canary_baseline_provider_uses_fresh_cached_evidence(
        self,
    ) -> None:
        from app.node import _build_live_canary_portfolio_baseline_provider
        from execution_domain.control_plane import portfolio_baseline_sha256

        evidence = {
            "positions": [
                {"symbol": "ETHUSDT", "quantity": "0.1"}
            ],
            "regular_orders": [],
            "algo_orders": [],
        }

        class EvidenceProvider:
            def __init__(self) -> None:
                self.max_ages: list[float] = []
                self.cached: dict[str, Any] | bool = evidence

            def cached_snapshot(
                self,
                *,
                max_age_seconds: float,
            ) -> dict[str, Any] | bool:
                self.max_ages.append(max_age_seconds)
                return self.cached

        exchange_evidence = EvidenceProvider()
        for account_id in (
            "account-a",
            "account-b",
            "account-c",
            "account-d",
        ):
            with self.subTest(account_id=account_id):
                exchange_evidence.max_ages.clear()
                exchange_evidence.cached = evidence
                config = types.SimpleNamespace(
                    account_id=account_id,
                    binance=types.SimpleNamespace(
                        environment="live"
                    ),
                )
                provider = _build_live_canary_portfolio_baseline_provider(
                    config,
                    exchange_evidence,
                )

                self.assertTrue(callable(provider))
                self.assertEqual(
                    provider("SOLUSDT"),
                    portfolio_baseline_sha256(
                        evidence,
                        "SOLUSDT",
                    ),
                )
                self.assertEqual(exchange_evidence.max_ages, [5.0])

                exchange_evidence.cached = False

                self.assertIs(provider("SOLUSDT"), False)
                self.assertEqual(
                    exchange_evidence.max_ages,
                    [5.0, 5.0],
                )

    def test_live_canary_strategy_wiring_covers_all_execution_accounts(
        self,
    ) -> None:
        from app.node import _build_strategy
        from strategy.intent_execution_strategy import (
            IntentExecutionStrategyConfig,
        )

        risk_reporter = lambda _task: True
        for account_id, node_id in (
            ("account-a", "node-a"),
            ("account-b", "node-b"),
            ("account-c", "node-c"),
            ("account-d", "node-d"),
        ):
            with self.subTest(account_id=account_id):
                with tempfile.TemporaryDirectory() as state_dir:
                    baseline_provider = lambda _symbol: "4" * 64
                    halt_reasons: list[str] = []
                    runtime = types.SimpleNamespace(
                        config=types.SimpleNamespace(
                            account_id=account_id,
                            node_id=node_id,
                            binance=types.SimpleNamespace(
                                environment="live"
                            ),
                        ),
                        strategy_config=IntentExecutionStrategyConfig(
                            account_id=account_id,
                            node_id=node_id,
                            trading_state="HALTED",
                            environment="live",
                            release_id="release-a",
                            live_canary_execution_path=str(
                                Path(state_dir)
                                / "live-canary-execution.json"
                            ),
                        ),
                        lifecycle=types.SimpleNamespace(
                            trading_state="HALTED",
                            rollout_phase="account_a_canary",
                            force_halt=halt_reasons.append,
                        ),
                        live_canary_portfolio_baseline_provider=(
                            baseline_provider
                        ),
                        exchange_cancel_adapter=False,
                        exchange_state_mirror=False,
                        incident_reporter=None,
                    )
                    with (
                        patch(
                            "app.node._build_denial_reporter",
                            return_value=lambda *_args: None,
                        ),
                        patch(
                            "app.node._build_protection_event_reporter",
                            return_value=lambda _event: True,
                        ),
                        patch(
                            "app.node._build_live_canary_risk_reporter",
                            return_value=risk_reporter,
                        ),
                        patch(
                            "app.node._build_terminal_exchange_worker",
                            return_value=False,
                        ),
                    ):
                        strategy = _build_strategy(
                            runtime,
                            fatal_callback=lambda _reason: None,
                        )

                    self.assertIs(
                        strategy._live_canary_portfolio_baseline,
                        baseline_provider,
                    )
                    self.assertIs(
                        strategy._live_canary_risk_reporter,
                        risk_reporter,
                    )
                    self.assertTrue(
                        callable(strategy._live_canary_halt_handler)
                    )
                    strategy._live_canary_halt_handler("forced-risk")
                    self.assertEqual(halt_reasons, ["forced-risk"])

    def test_readiness_probe_separates_liveness_and_reports_all_required_dependencies(self) -> None:
        from app.node import build_account_runtime

        runtime = build_account_runtime(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        live = runtime.health.liveness()
        ready = runtime.health.readiness()

        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.body["actor_tick_age_seconds"], 0.0)
        self.assertIs(live.body["restart_required"], False)
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.body["actor_tick_age_seconds"], 0.0)
        self.assertIs(ready.body["restart_required"], False)
        self.assertEqual(ready.body["reconciliation_status"], "missing")
        self.assertIsNone(ready.body["reconciliation_proof_age_seconds"])
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

    def test_startup_checks_keep_reconciliation_pending_until_host_proof_callback(
        self,
    ) -> None:
        from app.node import build_account_runtime, run_startup_readiness_checks
        from execution_domain.contracts import ReconciliationState

        runtime = build_account_runtime(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        with (
            patch("app.node._check_redis", return_value=True),
            patch("app.node._check_control_plane", return_value=True),
            patch("app.node._check_adapter", return_value=True),
        ):
            run_startup_readiness_checks(runtime)

        self.assertIn("reconciliation", {
            dependency.value for dependency in runtime.lifecycle.readiness.missing
        })
        self.assertEqual(runtime.lifecycle.reconciliation.status, "in_flight")

        proof = runtime.reconciliation_callback(
            account_id=runtime.config.account_id,
            node_id=runtime.config.node_id,
            release_id="release-a",
            state=ReconciliationState.HEALTHY,
            orders=[
                {"venue_order_id": "order-2"},
                {"venue_order_id": "order-1"},
            ],
            positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0"}],
            fills=[{"trade_id": "fill-1"}],
            completed_at=datetime.now(timezone.utc),
        )

        self.assertEqual(proof.orders.count, 2)
        self.assertEqual(proof.positions.count, 1)
        self.assertEqual(proof.fills.count, 1)
        self.assertEqual(len(proof.orders.digest), 64)
        self.assertNotIn("reconciliation", {
            dependency.value for dependency in runtime.lifecycle.readiness.missing
        })
        self.assertEqual(runtime.lifecycle.reconciliation.status, "healthy")

    def test_live_startup_control_plane_check_requires_fresh_exchange_evidence(
        self,
    ) -> None:
        from app.node import _check_control_plane

        evidence = {
            "positions": [],
            "regular_orders": [],
            "algo_orders": [],
            "fetched_at": datetime.now(timezone.utc),
        }

        class EvidenceProvider:
            def __init__(self) -> None:
                self.force_refresh_values: list[bool] = []

            def snapshot(
                self,
                *,
                force_refresh: bool = False,
            ) -> dict[str, Any]:
                self.force_refresh_values.append(force_refresh)
                return evidence

        class Lifecycle:
            def __init__(self) -> None:
                self.exchange_evidence: list[dict[str, Any]] = []

            def build_heartbeat(
                self,
                *,
                exchange_evidence: dict[str, Any],
            ) -> object:
                self.exchange_evidence.append(exchange_evidence)
                return types.SimpleNamespace(account_id="account-a")

            def send_heartbeat(self) -> None:
                raise AssertionError(
                    "live startup must send exchange evidence"
                )

        class ControlPlane:
            def __init__(self) -> None:
                self.heartbeats: list[tuple[str, object]] = []

            def heartbeat(self, node_id: str, heartbeat: object) -> object:
                self.heartbeats.append((node_id, heartbeat))
                return object()

        provider = EvidenceProvider()
        lifecycle = Lifecycle()
        control_plane = ControlPlane()
        runtime = types.SimpleNamespace(
            config=types.SimpleNamespace(
                account_id="account-a",
                node_id="node-a",
                binance=types.SimpleNamespace(environment="live"),
            ),
            lifecycle=lifecycle,
            control_plane=control_plane,
            exchange_evidence_provider=provider,
        )

        self.assertTrue(_check_control_plane(runtime))
        self.assertEqual(provider.force_refresh_values, [True])
        self.assertEqual(lifecycle.exchange_evidence, [evidence])
        self.assertEqual(
            control_plane.heartbeats,
            [("node-a", control_plane.heartbeats[0][1])],
        )

    def test_host_builder_exposes_reconciliation_callback_registration_seam(
        self,
    ) -> None:
        from app.node import build_account_runtime, build_nautilus_trading_node

        registered: list[object] = []

        def register(node: object, callback: object) -> None:
            registered.extend((node, callback))

        with tempfile.TemporaryDirectory() as tmp, _fake_nautilus_modules() as assembled:
            runtime = build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
                spool_root=Path(tmp),
            )
            node = build_nautilus_trading_node(
                runtime,
                reconciliation_callback_registrar=register,
            )

        self.assertEqual(registered, [assembled["node"], runtime.reconciliation_callback])
        self.assertIs(node, assembled["node"])

    def test_host_builder_acquires_redis_lease_before_trading_node_init(
        self,
    ) -> None:
        from app.node import build_account_runtime, build_nautilus_trading_node

        events: list[str] = []
        fatal_reasons: list[str] = []
        fatal_callback = fatal_reasons.append
        persistence_instance_id = "00000000-0000-4000-8000-00000000002a"
        redis_fencing_epoch = "11111111-1111-4111-8111-111111111111"

        class Lease:
            def acquire(self) -> object:
                events.append("lease.acquire")
                return types.SimpleNamespace(
                    namespace="trader-TRADER-ACCOUNT-A",
                    persistence_instance_id=persistence_instance_id,
                    persistence_namespace=(
                        "trader-TRADER-ACCOUNT-A:"
                        f"{persistence_instance_id}"
                    ),
                    fencing_token=42,
                    redis_fencing_epoch=redis_fencing_epoch,
                )

            def release(self) -> None:
                events.append("lease.release")

        with (
            tempfile.TemporaryDirectory() as tmp,
            _fake_nautilus_modules() as assembled,
            patch(
                "app.node._build_redis_namespace_lease",
                return_value=Lease(),
            ),
        ):
            assembled["events"] = events
            assembled["record_config_events"] = True
            runtime = build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
                spool_root=Path(tmp),
            )
            runtime.config = replace(
                runtime.config,
                binance=replace(runtime.config.binance, environment="live"),
            )
            runtime.live_canary_portfolio_baseline_provider = (
                lambda _symbol: "4" * 64
            )
            build_nautilus_trading_node(
                runtime,
                runtime_fatal_callback=fatal_callback,
            )
            heartbeat = runtime.lifecycle.build_heartbeat()
            requests: list[Any] = []
            headers = Message()
            headers["X-Writer-Fence-Rejected"] = "true"

            def reject_stale_writer(request: Any, timeout: float) -> Any:
                del timeout
                requests.append(request)
                raise HTTPError(
                    url=request.full_url,
                    code=409,
                    msg="Conflict",
                    hdrs=headers,
                    fp=BytesIO(b'{"detail":"stale node writer"}'),
                )

            with (
                patch(
                    "execution_domain.http_client.urlopen",
                    side_effect=reject_stale_writer,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "stale node writer",
                ),
            ):
                runtime.control_plane.poll_commands(
                    runtime.config.node_id,
                    None,
                )
            runtime.namespace_lease_guard.close()

        self.assertEqual(
            events[:5],
            [
                "lease.acquire",
                "redis_safety.sample",
                "redis_safety.start",
                "trading_node.config",
                "trading_node.init",
            ],
        )
        self.assertEqual(
            assembled["node"].config.kwargs["instance_id"],
            f"uuid4:{persistence_instance_id}",
        )
        self.assertTrue(assembled["cache_config"].kwargs["use_instance_id"])
        self.assertTrue(
            assembled["message_bus_config"].kwargs["use_instance_id"]
        )
        self.assertEqual(
            runtime.persistence.cache["key_root"],
            (
                "trader-TRADER-ACCOUNT-A:"
                f"{persistence_instance_id}"
            ),
        )
        self.assertEqual(
            heartbeat.redis_fencing_epoch,
            redis_fencing_epoch,
        )
        self.assertEqual(heartbeat.lease_fencing_token, 42)
        writer_identity = runtime.control_plane.writer_identity
        self.assertIsNot(writer_identity, False)
        self.assertEqual(
            writer_identity.redis_fencing_epoch,
            redis_fencing_epoch,
        )
        self.assertEqual(
            writer_identity.runtime_generation,
            runtime.lifecycle.runtime_generation,
        )
        self.assertEqual(writer_identity.lease_fencing_token, 42)
        self.assertEqual(
            runtime.control_plane_session.kwargs[
                "fatal_termination_hook"
            ],
            fatal_callback,
        )
        strategy = assembled["node"].trader.strategies[0]
        self.assertIs(
            strategy.live_canary_portfolio_baseline_getter,
            runtime.live_canary_portfolio_baseline_provider,
        )
        self.assertEqual(
            requests[0].get_header("X-redis-fencing-epoch"),
            redis_fencing_epoch,
        )
        self.assertEqual(
            requests[0].get_header("X-runtime-generation"),
            runtime.lifecycle.runtime_generation,
        )
        self.assertEqual(
            requests[0].get_header("X-lease-fencing-token"),
            "42",
        )
        self.assertEqual(
            fatal_reasons,
            [
                "control-plane rejected stale writer: "
                "stale node writer"
            ],
        )
        persistence_component = next(
            component
            for component in runtime.components
            if component.name == "persistence"
        )
        self.assertIs(persistence_component.value, runtime.persistence)
        self.assertEqual(events.count("lease.release"), 1)

    def test_live_host_builder_fails_closed_on_invalid_generation_metadata(
        self,
    ) -> None:
        from app.node import build_account_runtime, build_nautilus_trading_node

        events: list[str] = []

        class Lease:
            def acquire(self) -> object:
                events.append("lease.acquire")
                return types.SimpleNamespace(
                    namespace="trader-TRADER-ACCOUNT-A",
                    persistence_instance_id=(
                        "00000000-0000-4000-8000-00000000002a"
                    ),
                    persistence_namespace="trader-TRADER-ACCOUNT-B:wrong",
                    fencing_token=42,
                    redis_fencing_epoch=(
                        "11111111-1111-4111-8111-111111111111"
                    ),
                )

            def release(self) -> None:
                events.append("lease.release")

        with (
            tempfile.TemporaryDirectory() as tmp,
            _fake_nautilus_modules() as assembled,
            patch(
                "app.node._build_redis_namespace_lease",
                return_value=Lease(),
            ),
        ):
            assembled["events"] = events
            assembled["record_config_events"] = True
            runtime = build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
                spool_root=Path(tmp),
            )
            runtime.config = replace(
                runtime.config,
                binance=replace(runtime.config.binance, environment="live"),
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "persistence identity does not match",
            ):
                build_nautilus_trading_node(runtime)

        self.assertEqual(events, ["lease.acquire", "lease.release"])

    def test_host_builder_releases_redis_lease_when_node_init_fails(
        self,
    ) -> None:
        from app.node import build_account_runtime, build_nautilus_trading_node

        events: list[str] = []

        class Lease:
            def acquire(self) -> object:
                events.append("lease.acquire")
                return object()

            def release(self) -> None:
                events.append("lease.release")

        def fail_init(config: object) -> None:
            del config
            events.append("trading_node.init")
            raise RuntimeError("kernel init failed")

        with (
            tempfile.TemporaryDirectory() as tmp,
            _fake_nautilus_modules(),
            patch(
                "app.node._build_redis_namespace_lease",
                return_value=Lease(),
            ),
        ):
            sys.modules["nautilus_trader.live.node"].TradingNode = fail_init
            runtime = build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
                spool_root=Path(tmp),
            )
            with self.assertRaisesRegex(RuntimeError, "kernel init failed"):
                build_nautilus_trading_node(runtime)

        self.assertEqual(
            events,
            [
                "lease.acquire",
                "trading_node.init",
                "lease.release",
            ],
        )

    def test_namespace_lease_loss_halts_sticky_and_triggers_fatal(self) -> None:
        from app.node import _build_namespace_lease_lost_callback
        from runtime.lifecycle import DependencyName

        class Lifecycle:
            def __init__(self) -> None:
                self.trading_state = "ACTIVE"
                self.halt_reason = ""
                self.failed_dependencies: list[object] = []

            def mark_dependency_failed(
                self,
                dependency: object,
                reason: str,
            ) -> None:
                del reason
                self.failed_dependencies.append(dependency)

            def mark_dependency_ready(self, dependency: object) -> None:
                del dependency

            def invalidate_lease(self, reason: str) -> None:
                self.trading_state = "HALTED"
                self.halt_reason = reason

        lifecycle = Lifecycle()
        fatal_reasons: list[str] = []
        callback = _build_namespace_lease_lost_callback(
            lifecycle,
            fatal_reasons.append,
        )

        callback("Redis namespace lease refresh failed: fenced")

        self.assertEqual(
            str(getattr(lifecycle.trading_state, "value", lifecycle.trading_state)),
            "HALTED",
        )
        self.assertEqual(
            lifecycle.halt_reason,
            "Redis namespace lease refresh failed: fenced",
        )
        self.assertIn(DependencyName.REDIS, lifecycle.failed_dependencies)
        self.assertEqual(
            fatal_reasons,
            ["Redis namespace lease refresh failed: fenced"],
        )

        lifecycle.mark_dependency_ready(DependencyName.REDIS)

        self.assertEqual(
            str(getattr(lifecycle.trading_state, "value", lifecycle.trading_state)),
            "HALTED",
        )

    def test_writer_fence_identity_binds_control_plane_and_exchange_mirror(
        self,
    ) -> None:
        from app.node import _configure_control_plane_writer_fence

        class FencedDependency:
            def __init__(self) -> None:
                self.identities: list[dict[str, Any]] = []
                self.fatal_hooks: list[Any] = []
                self.transport_hooks: list[Any] = []

            def bind_writer_identity(self, **identity: Any) -> None:
                self.identities.append(identity)

            def bind_fatal_fence_hook(self, hook: Any) -> None:
                self.fatal_hooks.append(hook)

            def bind_fatal_transport_hook(self, hook: Any) -> None:
                self.transport_hooks.append(hook)

        control_plane = FencedDependency()
        exchange_mirror = FencedDependency()
        runtime = types.SimpleNamespace(
            control_plane=control_plane,
            exchange_state_mirror=exchange_mirror,
            lifecycle=types.SimpleNamespace(
                runtime_generation="runtime-generation-a",
            ),
        )
        guard = types.SimpleNamespace(
            record=types.SimpleNamespace(
                redis_fencing_epoch=(
                    "11111111-1111-4111-8111-111111111111"
                ),
                fencing_token=42,
            )
        )
        fatal_reasons: list[str] = []

        _configure_control_plane_writer_fence(
            runtime,
            guard,
            fatal_reasons.append,
        )

        expected_identity = {
            "redis_fencing_epoch": (
                "11111111-1111-4111-8111-111111111111"
            ),
            "runtime_generation": "runtime-generation-a",
            "lease_fencing_token": 42,
        }
        self.assertEqual(control_plane.identities, [expected_identity])
        self.assertEqual(exchange_mirror.identities, [expected_identity])
        self.assertEqual(
            control_plane.fatal_hooks,
            [fatal_reasons.append],
        )
        self.assertEqual(
            control_plane.transport_hooks,
            [fatal_reasons.append],
        )
        self.assertEqual(
            exchange_mirror.fatal_hooks,
            [fatal_reasons.append],
        )

    def test_local_health_fallback_exposes_actor_watchdog_state(self) -> None:
        from app.node import _LocalHealthService, _LocalLifecycle

        lifecycle = _LocalLifecycle(
            types.SimpleNamespace(account_id="account-a", node_id="node-a")
        )
        lifecycle._ready.update(
            {
                "instruments",
                "redis",
                "control_plane",
                "intent_stream",
                "command_stream",
                "reconciliation",
                "projection",
            }
        )
        lifecycle.actor_tick_age_seconds = 61.0
        lifecycle.restart_required = True
        health = _LocalHealthService(lifecycle)

        live = health.liveness()
        ready = health.readiness()

        self.assertEqual(live.status_code, 503)
        self.assertIs(live.body["live"], False)
        self.assertEqual(live.body["actor_tick_age_seconds"], 61.0)
        self.assertIs(live.body["restart_required"], True)
        self.assertEqual(ready.status_code, 503)
        self.assertIs(ready.body["ready"], False)
        self.assertEqual(ready.body["actor_tick_age_seconds"], 61.0)
        self.assertIs(ready.body["restart_required"], True)

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

        restart_ages: list[float] = []
        restart_callback = restart_ages.append
        with tempfile.TemporaryDirectory() as tmp, _fake_nautilus_modules() as assembled:
            runtime = build_account_runtime(
                SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json",
                spool_root=Path(tmp),
            )
            node = build_nautilus_trading_node(
                runtime,
                restart_required_callback=restart_callback,
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
        self.assertEqual(
            strategy.config.live_entry_notional_inventory,
            tuple(
                sorted(
                    runtime.risk_config.max_notional_per_order.items()
                )
            ),
        )
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
            strategy.config.intent_execution_inbox_path,
            str(runtime.intent_data_client._intent_execution_inbox._path),
        )
        self.assertEqual(
            [type(actor).__name__ for actor in node.trader.actors],
            ["IntentPublisherActor", "ExecutionProjectionActor", "CommandPollerActor"],
        )
        command_actor = node.trader.actors[-1]
        self.assertIs(command_actor._restart_required_callback, restart_callback)
        self.assertEqual(node.config.kwargs["trader_id"], runtime.config.trader_id)
        self.assertNotIn("instance_id", node.config.kwargs)
        self.assertFalse(assembled["cache_config"].kwargs["use_instance_id"])
        self.assertFalse(
            assembled["message_bus_config"].kwargs["use_instance_id"]
        )

    def test_default_actor_restart_callback_exits_process_through_injected_seam(
        self,
    ) -> None:
        from app.node import (
            ACTOR_WATCHDOG_EXIT_CODE,
            _build_actor_restart_required_callback,
        )

        exit_codes: list[int] = []
        callback = _build_actor_restart_required_callback(
            exit_process=exit_codes.append,
        )

        callback(60.25)

        self.assertEqual(exit_codes, [ACTOR_WATCHDOG_EXIT_CODE])


class NautilusActorAdapterTest(unittest.TestCase):
    def test_async_intent_publish_completes_on_actor_thread_before_poll_returns(self) -> None:
        from app.nautilus_actors import IntentPublisherActor

        client = _AsyncIntentClient()
        lifecycle = _RecordingLifecycle()
        message_bus = _RecordingMessageBus()

        class _BoundIntentPublisherActor(IntentPublisherActor):
            def _message_bus(self) -> Any:
                return message_bus

        actor = _BoundIntentPublisherActor(client, lifecycle=lifecycle)
        actor_thread_id = threading.get_ident()

        actor._on_poll_timer()
        self.assertTrue(client.publish_started.wait(timeout=1))
        self.assertFalse(client.poll_returned.is_set())
        self.assertEqual(message_bus.published, [])

        try:
            actor._on_poll_timer()
            self.assertTrue(client.poll_returned.wait(timeout=1))
            self.assertEqual(
                message_bus.published,
                [("intents.account-a", client.intent)],
            )
            self.assertEqual(message_bus.publish_thread_ids, [actor_thread_id])
            self.assertNotEqual(client.poll_thread_id, actor_thread_id)
            self.assertEqual(
                lifecycle.ready_dependencies[-1].value,
                "intent_stream",
            )
        finally:
            actor.on_stop()

    def test_intent_actor_stop_releases_worker_waiting_for_publication(self) -> None:
        from app.nautilus_actors import IntentPublisherActor

        client = _AsyncIntentClient()
        actor = IntentPublisherActor(client)

        actor._on_poll_timer()
        self.assertTrue(client.publish_started.wait(timeout=1))
        actor.on_stop()

        self.assertTrue(client.poll_failed.wait(timeout=1))
        self.assertIn("stopped", str(client.error))

    def test_intent_publication_backlog_is_bounded(self) -> None:
        from app.nautilus_actors import IntentPublisherActor

        actor = IntentPublisherActor(
            _PlainIntentClient(),
            pending_intent_limit=1,
            publication_enqueue_timeout_seconds=0.01,
        )
        first_error: list[BaseException] = []

        def publish_first() -> None:
            try:
                actor._queued_publisher.publish(
                    types.SimpleNamespace(account_id="account-a")
                )
            except BaseException as exc:
                first_error.append(exc)

        worker = threading.Thread(target=publish_first)
        worker.start()
        self.assertTrue(_wait_until(lambda: actor._pending_intents.qsize() == 1))

        try:
            with self.assertRaisesRegex(RuntimeError, "backlog full"):
                actor._queued_publisher.publish(
                    types.SimpleNamespace(account_id="account-a")
                )
        finally:
            actor.on_stop()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(first_error), 1)
        self.assertIn("stopped", str(first_error[0]))

    def test_stale_intent_poll_marks_stream_failed(self) -> None:
        from app.nautilus_actors import IntentPublisherActor

        lifecycle = _RecordingLifecycle()
        actor = IntentPublisherActor(
            _PlainIntentClient(),
            lifecycle=lifecycle,
            stale_after_seconds=0,
        )

        try:
            actor._evaluate_poll_staleness()
            dependency, reason = lifecycle.failed_dependencies[0]
            self.assertEqual(dependency.value, "intent_stream")
            self.assertEqual(reason, "approved intent poll stale")
        finally:
            actor.on_stop()

    def test_intent_poll_timer_returns_while_network_poll_is_blocked(self) -> None:
        from app.nautilus_actors import IntentPublisherActor

        client = _BlockingIntentClient()
        actor = IntentPublisherActor(client)

        callback = threading.Thread(target=actor._on_poll_timer)
        callback.start()
        self.assertTrue(client.started.wait(timeout=1))
        callback.join(timeout=0.2)

        try:
            self.assertFalse(callback.is_alive())
        finally:
            client.release.set()
            callback.join(timeout=1)
            actor.on_stop()

    def test_async_command_is_applied_on_actor_thread(self) -> None:
        from app.nautilus_actors import CommandPollerActor
        from execution_domain.control_plane import CommandType, NodeCommand

        command = NodeCommand(command_id="cmd-halt", type=CommandType.HALT)
        control_plane = _CommandControlPlane(command)
        lifecycle = _RecordingLifecycle()
        actor = CommandPollerActor(
            control_plane=control_plane,
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
        )
        actor_thread_id = threading.get_ident()

        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._command_future.done()))

        try:
            actor._on_poll_timer()
            self.assertEqual(len(lifecycle.applied_states), 1)
            state, reason, thread_id = lifecycle.applied_states[0]
            self.assertEqual(state.value, "HALTED")
            self.assertEqual(reason, "operator_command")
            self.assertEqual(thread_id, actor_thread_id)
        finally:
            actor.on_stop()

    def test_failed_command_ack_retries_without_reapplying_command(self) -> None:
        from app.nautilus_actors import CommandPollerActor
        from execution_domain.control_plane import CommandType, NodeCommand

        command = NodeCommand(command_id="cmd-halt", type=CommandType.HALT)
        control_plane = _CommandControlPlane(command, ack_failures=1)
        lifecycle = _RecordingLifecycle()
        actor = CommandPollerActor(
            control_plane=control_plane,
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
        )

        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._command_future.done()))
        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._ack_future.done()))
        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._ack_future.done()))

        try:
            actor._on_poll_timer()
            self.assertEqual(len(lifecycle.applied_states), 1)
            self.assertEqual(control_plane.ack_attempts, 2)
        finally:
            actor.on_stop()

    def test_stale_command_cycle_marks_both_dependencies_failed(self) -> None:
        from app.nautilus_actors import CommandPollerActor

        lifecycle = _RecordingLifecycle()
        actor = CommandPollerActor(
            control_plane=object(),
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
            stale_after_seconds=0,
        )

        try:
            actor._evaluate_poll_staleness()
            failed = {
                dependency.value: reason
                for dependency, reason in lifecycle.failed_dependencies
            }
            self.assertEqual(
                failed,
                {
                    "control_plane": "control-plane heartbeat stale",
                    "command_stream": "operator command poll stale",
                },
            )
        finally:
            actor.on_stop()

    def test_command_poll_timer_returns_while_control_plane_is_blocked(self) -> None:
        from app.nautilus_actors import CommandPollerActor

        control_plane = _BlockingCommandControlPlane()
        lifecycle = _RecordingLifecycle()
        actor = CommandPollerActor(
            control_plane=control_plane,
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
        )

        callback = threading.Thread(target=actor._on_poll_timer)
        callback.start()
        self.assertTrue(control_plane.started.wait(timeout=1))
        callback.join(timeout=0.2)

        try:
            self.assertFalse(callback.is_alive())
        finally:
            control_plane.release.set()
            callback.join(timeout=1)
            actor.on_stop()

    def test_blocking_command_url_does_not_stall_heartbeat_lane(self) -> None:
        from app.nautilus_actors import CommandPollerActor
        from execution_domain.http_client import HttpControlPlaneClient

        command_started = threading.Event()
        command_release = threading.Event()
        heartbeat_calls = 0

        def urlopen(request: Any, timeout: float) -> Any:
            nonlocal heartbeat_calls
            del timeout
            if request.full_url.endswith("/heartbeat"):
                heartbeat_calls += 1
                return _JsonResponse(b"")
            if "/commands?" in request.full_url:
                command_started.set()
                command_release.wait(timeout=2)
                return _JsonResponse(b'{"commands":[]}')
            raise AssertionError(request.full_url)

        client = HttpControlPlaneClient(
            base_url="https://control-plane.invalid",
            token="token",
            node_id="node-a",
            account_id="account-a",
        )
        actor = CommandPollerActor(
            control_plane=client,
            lifecycle=_RecordingLifecycle(),
            node_id="node-a",
            account_id="account-a",
        )

        with patch("execution_domain.http_client.urlopen", side_effect=urlopen):
            actor._on_poll_timer()
            self.assertTrue(command_started.wait(timeout=1))
            self.assertTrue(_wait_until(lambda: heartbeat_calls >= 1))
            self.assertTrue(
                _wait_until(
                    lambda: (
                        actor._heartbeat_future is not None
                        and actor._heartbeat_future.done()
                    )
                )
            )
            actor._on_poll_timer()
            self.assertTrue(_wait_until(lambda: heartbeat_calls >= 2))
            command_release.set()
            self.assertTrue(_wait_until(lambda: actor._command_future.done()))
            actor.on_stop()

    def test_blocking_heartbeat_does_not_stall_command_poll_lane(self) -> None:
        from app.nautilus_actors import CommandPollerActor

        control_plane = _IndependentlyBlockingControlPlane(block_heartbeat=True)
        actor = CommandPollerActor(
            control_plane=control_plane,
            lifecycle=_RecordingLifecycle(),
            node_id="node-a",
            account_id="account-a",
            worker_shutdown_wait_seconds=2.0,
        )

        actor._on_poll_timer()
        self.assertTrue(control_plane.heartbeat_started.wait(timeout=1))
        self.assertTrue(_wait_until(lambda: control_plane.command_calls >= 1))
        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: control_plane.command_calls >= 2))

        control_plane.heartbeat_release.set()
        self.assertTrue(_wait_until(lambda: actor._heartbeat_future.done()))
        actor.on_stop()

    def test_blocking_ack_does_not_stall_heartbeat_or_command_lanes(self) -> None:
        from app.nautilus_actors import CommandPollerActor
        from execution_domain.control_plane import CommandType, NodeCommand

        control_plane = _BlockingAckControlPlane(
            NodeCommand(command_id="cmd-halt", type=CommandType.HALT)
        )
        actor = CommandPollerActor(
            control_plane=control_plane,
            lifecycle=_RecordingLifecycle(),
            node_id="node-a",
            account_id="account-a",
        )

        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._command_future.done()))
        actor._on_poll_timer()
        self.assertTrue(control_plane.ack_started.wait(timeout=1))
        heartbeat_calls = control_plane.heartbeat_calls
        command_calls = control_plane.command_calls

        actor._on_poll_timer()
        self.assertTrue(
            _wait_until(lambda: control_plane.heartbeat_calls > heartbeat_calls)
        )
        self.assertTrue(
            _wait_until(lambda: control_plane.command_calls > command_calls)
        )

        control_plane.ack_release.set()
        self.assertTrue(_wait_until(lambda: actor._ack_future.done()))
        actor.on_stop()

    def test_async_heartbeat_includes_open_orders_snapshot(self) -> None:
        from app.nautilus_actors import CommandPollerActor

        control_plane = _HeartbeatCaptureControlPlane()
        lifecycle = _RecordingLifecycle()
        cache = _OpenOrdersCache()

        class _BoundCommandPollerActor(CommandPollerActor):
            def _cache(self) -> Any:
                return cache

        actor = _BoundCommandPollerActor(
            control_plane=control_plane,
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
        )

        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._heartbeat_future.done()))

        try:
            heartbeat = control_plane.heartbeats[0]
            self.assertEqual(
                heartbeat.open_orders,
                (
                    {
                        "client_order_id": "order-1",
                        "instrument_id": "BTCUSDT-PERP.BINANCE",
                        "side": "BUY",
                        "quantity": "0.001",
                    },
                ),
            )
        finally:
            actor.on_stop()

    def test_async_heartbeat_fetches_exchange_evidence_on_worker_lane(self) -> None:
        from app.nautilus_actors import CommandPollerActor

        control_plane = _HeartbeatCaptureControlPlane()
        lifecycle = _RecordingLifecycle()
        provider = _ExchangeEvidenceProvider()
        actor = CommandPollerActor(
            control_plane=control_plane,
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
            exchange_evidence_provider=provider,
        )
        actor_thread_id = threading.get_ident()

        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._heartbeat_future.done()))

        try:
            heartbeat = control_plane.heartbeats[0]
            self.assertNotEqual(provider.thread_id, actor_thread_id)
            self.assertEqual(
                heartbeat.positions,
                ({"symbol": "ETHUSDT", "quantity": "-0.01"},),
            )
            self.assertEqual(
                heartbeat.regular_orders,
                ({"symbol": "BTCUSDT", "client_order_id": "regular-1"},),
            )
            self.assertEqual(
                heartbeat.algo_orders,
                ({"symbol": "ETHUSDT", "client_order_id": "algo-1"},),
            )
            self.assertEqual(provider.force_refresh_values, [False])
        finally:
            actor.on_stop()

    def test_refresh_evidence_command_forces_exchange_snapshot_heartbeat(self) -> None:
        from app.nautilus_actors import CommandPollerActor
        from execution_domain.control_plane import (
            CommandAckStatus,
            CommandType,
            NodeCommand,
        )

        control_plane = _HeartbeatCaptureControlPlane()
        lifecycle = _RecordingLifecycle()
        provider = _ExchangeEvidenceProvider()
        actor = CommandPollerActor(
            control_plane=control_plane,
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
            exchange_evidence_provider=provider,
        )

        status, error = actor._apply(
            NodeCommand(
                command_id="cmd-refresh",
                type=CommandType.REFRESH_EVIDENCE,
            )
        )

        try:
            self.assertEqual(status, CommandAckStatus.COMPLETED)
            self.assertIsNone(error)
            self.assertEqual(provider.force_refresh_values, [True])
            self.assertEqual(len(control_plane.heartbeats), 1)
            self.assertEqual(lifecycle.applied_states, [])
        finally:
            actor.on_stop()

    def test_command_timer_records_actor_watchdog_tick(self) -> None:
        from app.nautilus_actors import CommandPollerActor

        actor = CommandPollerActor(
            control_plane=_CommandBatchControlPlane([]),
            lifecycle=_RecordingLifecycle(),
            node_id="node-a",
            account_id="account-a",
        )
        watchdog = _RecordingWatchdog()
        actor._tick_watchdog = watchdog

        actor._on_poll_timer()

        try:
            self.assertEqual(watchdog.tick_calls, 1)
        finally:
            actor.on_stop()

    def test_command_actor_has_no_synchronous_network_poll_entrypoint(self) -> None:
        from app.nautilus_actors import CommandPollerActor

        actor = CommandPollerActor(
            control_plane=_CommandBatchControlPlane([]),
            lifecycle=_RecordingLifecycle(),
            node_id="node-a",
            account_id="account-a",
        )

        self.assertFalse(hasattr(actor, "poll_once"))

    def test_command_ack_retry_is_bounded_and_halts_command_stream(self) -> None:
        from app.nautilus_actors import CommandPollerActor
        from execution_domain.control_plane import CommandType, NodeCommand

        command = NodeCommand(command_id="cmd-halt", type=CommandType.HALT)
        control_plane = _CommandControlPlane(command, ack_failures=100)
        lifecycle = _RecordingLifecycle()
        actor = CommandPollerActor(
            control_plane=control_plane,
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
            max_ack_attempts=2,
        )

        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._command_future.done()))
        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._ack_future.done()))
        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._ack_future.done()))
        actor._on_poll_timer()

        try:
            self.assertEqual(control_plane.ack_attempts, 2)
            self.assertEqual(len(actor._pending_acks), 0)
            failed = {
                dependency.value: reason
                for dependency, reason in lifecycle.failed_dependencies
            }
            self.assertEqual(
                failed["command_stream"],
                "command ack retry limit reached for cmd-halt",
            )
        finally:
            actor.on_stop()

    def test_pending_command_ack_backlog_is_bounded(self) -> None:
        from app.nautilus_actors import CommandPollerActor
        from execution_domain.control_plane import CommandType, NodeCommand

        commands = [
            NodeCommand(command_id=f"cmd-{index}", type=CommandType.HALT)
            for index in range(4)
        ]
        lifecycle = _RecordingLifecycle()
        actor = CommandPollerActor(
            control_plane=_CommandBatchControlPlane(commands),
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
            max_pending_acks=2,
        )

        actor._on_poll_timer()
        self.assertTrue(_wait_until(lambda: actor._command_future.done()))
        actor._on_poll_timer()

        try:
            self.assertLessEqual(len(actor._pending_acks), 2)
            self.assertLessEqual(len(actor._applied_commands), 2)
            failed = {
                dependency.value: reason
                for dependency, reason in lifecycle.failed_dependencies
            }
            self.assertEqual(
                failed["command_stream"],
                "command ack backlog full",
            )
        finally:
            actor.on_stop()

    def test_actor_stop_joins_cooperative_network_workers(self) -> None:
        from app.nautilus_actors import CommandPollerActor

        control_plane = _IndependentlyBlockingControlPlane(
            block_heartbeat=True,
            block_commands=True,
        )
        actor = CommandPollerActor(
            control_plane=control_plane,
            lifecycle=_RecordingLifecycle(),
            node_id="node-a",
            account_id="account-a",
        )
        actor._on_poll_timer()
        self.assertTrue(control_plane.heartbeat_started.wait(timeout=1))
        self.assertTrue(control_plane.command_started.wait(timeout=1))
        control_plane.heartbeat_release.set()
        control_plane.command_release.set()

        actor.on_stop()

        self.assertTrue(
            _wait_until(
                lambda: not any(
                thread.name.startswith("operator-commands.poll.")
                for thread in threading.enumerate()
                )
            )
        )

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
        started_at = time.monotonic()
        accepted = actor.on_event("order-event")
        callback_elapsed = time.monotonic() - started_at

        self.assertTrue(accepted)
        self.assertLess(callback_elapsed, 0.05)
        self.assertTrue(
            _wait_until(lambda: "order-event" in projection.events)
        )
        self.assertTrue(msgbus.subscriptions)
        topic, handler = msgbus.subscriptions[0]
        self.assertIn("order", topic)
        handler("position-event")
        self.assertTrue(
            _wait_until(lambda: "position-event" in projection.events)
        )
        actor.on_stop()


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
        self.assertEqual(compose.count("restart: unless-stopped"), 4)

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


class _BlockingIntentClient:
    def __init__(self) -> None:
        self._publisher = _AttachablePublisher()
        self.started = threading.Event()
        self.release = threading.Event()

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        del limit, wait_ms
        self.started.set()
        self.release.wait(timeout=2)
        return 0


class _AsyncIntentClient:
    def __init__(self) -> None:
        self._publisher = _AttachablePublisher()
        self.intent = types.SimpleNamespace(account_id="account-a")
        self.publish_started = threading.Event()
        self.poll_returned = threading.Event()
        self.poll_failed = threading.Event()
        self.poll_thread_id: int | None = None
        self.error: BaseException | None = None

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        del limit, wait_ms
        self.poll_thread_id = threading.get_ident()
        self.publish_started.set()
        try:
            self._publisher.publish(self.intent)
        except BaseException as exc:
            self.error = exc
            self.poll_failed.set()
            raise
        self.poll_returned.set()
        return 1


class _BlockingCommandControlPlane:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def heartbeat(self, node_id: str, heartbeat: Any) -> None:
        del node_id, heartbeat

    def poll_commands(self, node_id: str, after: str | None) -> list[Any]:
        del node_id, after
        self.started.set()
        self.release.wait(timeout=2)
        return []


class _IndependentlyBlockingControlPlane:
    def __init__(
        self,
        *,
        block_heartbeat: bool = False,
        block_commands: bool = False,
    ) -> None:
        self.block_heartbeat = block_heartbeat
        self.block_commands = block_commands
        self.heartbeat_started = threading.Event()
        self.heartbeat_release = threading.Event()
        self.command_started = threading.Event()
        self.command_release = threading.Event()
        self.heartbeat_calls = 0
        self.command_calls = 0

    def heartbeat(self, node_id: str, heartbeat: Any) -> None:
        del node_id, heartbeat
        self.heartbeat_calls += 1
        self.heartbeat_started.set()
        if self.block_heartbeat:
            self.heartbeat_release.wait(timeout=2)

    def poll_commands(self, node_id: str, after: str | None) -> list[Any]:
        del node_id, after
        self.command_calls += 1
        self.command_started.set()
        if self.block_commands:
            self.command_release.wait(timeout=2)
        return []


class _CommandControlPlane:
    def __init__(self, command: Any, ack_failures: int = 0) -> None:
        self.command = command
        self.ack_failures = ack_failures
        self.ack_attempts = 0
        self.acked = False

    def heartbeat(self, node_id: str, heartbeat: Any) -> None:
        del node_id, heartbeat

    def poll_commands(self, node_id: str, after: str | None) -> list[Any]:
        del node_id, after
        if self.acked:
            return []
        return [self.command]

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: Any,
        error: str | None = None,
    ) -> None:
        del node_id, command_id, status, error
        self.ack_attempts += 1
        if self.ack_attempts <= self.ack_failures:
            raise RuntimeError("ack unavailable")
        self.acked = True


class _CommandBatchControlPlane:
    def __init__(self, commands: list[Any]) -> None:
        self.commands = commands

    def heartbeat(self, node_id: str, heartbeat: Any) -> None:
        del node_id, heartbeat

    def poll_commands(self, node_id: str, after: str | None) -> list[Any]:
        del node_id, after
        return self.commands

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: Any,
        error: str | None = None,
    ) -> None:
        del node_id, command_id, status, error


class _BlockingAckControlPlane:
    def __init__(self, command: Any) -> None:
        self.command = command
        self.heartbeat_calls = 0
        self.command_calls = 0
        self.ack_started = threading.Event()
        self.ack_release = threading.Event()

    def heartbeat(self, node_id: str, heartbeat: Any) -> None:
        del node_id, heartbeat
        self.heartbeat_calls += 1

    def poll_commands(self, node_id: str, after: str | None) -> list[Any]:
        del node_id, after
        self.command_calls += 1
        return [self.command]

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: Any,
        error: str | None = None,
    ) -> None:
        del node_id, command_id, status, error
        self.ack_started.set()
        self.ack_release.wait(timeout=2)


class _HeartbeatCaptureControlPlane:
    def __init__(self) -> None:
        self.heartbeats: list[Any] = []

    def heartbeat(self, node_id: str, heartbeat: Any) -> None:
        del node_id
        self.heartbeats.append(heartbeat)

    def poll_commands(self, node_id: str, after: str | None) -> list[Any]:
        del node_id, after
        return []


class _OpenOrdersCache:
    def orders_open(self) -> list[Any]:
        return [
            types.SimpleNamespace(
                client_order_id="order-1",
                instrument_id="BTCUSDT-PERP.BINANCE",
                side="BUY",
                quantity="0.001",
            )
        ]


class _ExchangeEvidenceProvider:
    def __init__(self) -> None:
        self.thread_id: int | None = None
        self.force_refresh_values: list[bool] = []

    def snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self.thread_id = threading.get_ident()
        self.force_refresh_values.append(force_refresh)
        return {
            "positions": [{"symbol": "ETHUSDT", "quantity": "-0.01"}],
            "regular_orders": [
                {"symbol": "BTCUSDT", "client_order_id": "regular-1"}
            ],
            "algo_orders": [
                {"symbol": "ETHUSDT", "client_order_id": "algo-1"}
            ],
            "fetched_at": datetime.now(timezone.utc),
        }


class _RecordingWatchdog:
    def __init__(self) -> None:
        self.tick_calls = 0

    def start(self) -> None:
        return

    def record_tick(self) -> None:
        self.tick_calls += 1

    def stop(self) -> None:
        return


class _RecordingLifecycle:
    def __init__(self) -> None:
        self.applied_states: list[tuple[Any, str, int]] = []
        self.ready_dependencies: list[Any] = []
        self.failed_dependencies: list[tuple[Any, str]] = []
        self.open_orders_provider: Any = None

    def set_open_orders_provider(self, provider: Any) -> None:
        self.open_orders_provider = provider

    def build_heartbeat(
        self,
        exchange_evidence: dict[str, Any] | None = None,
    ) -> Any:
        from execution_domain.contracts import ReconciliationState
        from execution_domain.control_plane import Heartbeat, TradingState

        open_orders = None
        if self.open_orders_provider is not None:
            open_orders = tuple(self.open_orders_provider())
        positions = None
        regular_orders = None
        algo_orders = None
        if exchange_evidence is not None:
            positions = tuple(exchange_evidence["positions"])
            regular_orders = tuple(exchange_evidence["regular_orders"])
            algo_orders = tuple(exchange_evidence["algo_orders"])
        return Heartbeat(
            account_id="account-a",
            ts=datetime.now(timezone.utc),
            trading_state=TradingState.HALTED,
            readiness=False,
            projection_lag_ms=0,
            reconciliation_state=ReconciliationState.DEGRADED,
            positions=positions,
            regular_orders=regular_orders,
            algo_orders=algo_orders,
            open_orders=open_orders,
        )

    def apply_operator_state(self, state: Any, reason: str) -> None:
        self.applied_states.append((state, reason, threading.get_ident()))

    def mark_dependency_ready(self, dependency: Any) -> None:
        self.ready_dependencies.append(dependency)

    def mark_dependency_failed(self, dependency: Any, reason: str) -> None:
        self.failed_dependencies.append((dependency, reason))


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
        self.publish_thread_ids: list[int] = []

    def subscribe(self, topic: str, handler: Any) -> None:
        self.subscriptions.append((topic, handler))

    def publish(self, topic: str, msg: Any) -> None:
        self.published.append((topic, msg))
        self.publish_thread_ids.append(threading.get_ident())


class _JsonResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        del args

    def read(self) -> bytes:
        return self._body


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


@contextmanager
def _fake_nautilus_modules() -> Iterator[dict[str, Any]]:
    old_modules = dict(sys.modules)
    assembled: dict[str, Any] = {}

    class _Config:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class _ControlPlaneExchangeStateMirror(_Config):
        def bind_writer_identity(self, **identity: Any) -> None:
            self.writer_identity = identity

        def bind_fatal_fence_hook(self, hook: Any) -> None:
            self.fatal_fence_hook = hook

    class _TradingNodeConfig(_Config):
        def __init__(self, **kwargs: Any) -> None:
            if assembled.get("record_config_events"):
                assembled["events"].append("trading_node.config")
            super().__init__(**kwargs)

    class _CacheConfig(_Config):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            assembled["cache_config"] = self

    class _MessageBusConfig(_Config):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            assembled["message_bus_config"] = self

    class _UUID4:
        @classmethod
        def from_str(cls, value: str) -> str:
            return f"uuid4:{value}"

    class _TradingNode:
        def __init__(self, config: Any) -> None:
            events = assembled.get("events")
            if events is not None:
                events.append("trading_node.init")
            self.config = config
            self.trader = _Trader()
            self.kernel = types.SimpleNamespace(exec_engine=_ExecutionEngine())
            self.cache = _Cache()
            self.added: list[tuple[Any, ...]] = []
            assembled["node"] = self

        def add_data_client_factory(self, name: str, factory: Any) -> None:
            self.added.append(("data_factory", name, factory))

        def add_exec_client_factory(self, name: str, factory: Any) -> None:
            self.added.append(("exec_factory", name, factory))

        def add_stream_processor(self, processor: Any) -> None:
            self.added.append(("stream_processor", processor))

    class _ExecutionEngine:
        async def reconcile_execution_state(
            self,
            timeout_secs: float = 10.0,
        ) -> bool:
            del timeout_secs
            return True

    class _Cache:
        def orders(self) -> list[Any]:
            return []

        def positions(self) -> list[Any]:
            return []

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
            environment: str = "testnet",
            release_id: str = "",
            live_canary_execution_path: str = "",
            intent_execution_inbox_path: str = "",
            live_entry_notional_inventory: tuple[tuple[str, str], ...] = (),
            existing_intent_ids: tuple[str, ...] = (),
        ) -> None:
            self.account_id = account_id
            self.node_id = node_id
            self.trading_state = trading_state
            self.environment = environment
            self.release_id = release_id
            self.live_canary_execution_path = live_canary_execution_path
            self.intent_execution_inbox_path = intent_execution_inbox_path
            self.live_entry_notional_inventory = live_entry_notional_inventory
            self.existing_intent_ids = existing_intent_ids

    class IntentExecutionStrategy:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.durable_io_fatal_handler: Any = None
            self.trading_state_getter: Any = None
            self.denial_reporter: Any = None
            self.protection_event_reporter: Any = None
            self.live_canary_portfolio_baseline_getter: Any = None
            self.live_rollout_phase_getter: Any = None
            self.live_open_gate_getter: Any = None
            self.live_canary_risk_reporter: Any = None
            self.live_canary_halt_handler: Any = None
            self.exchange_cancel_dependencies: Any = None

        def set_durable_io_fatal_handler(
            self,
            handler: Any,
        ) -> None:
            self.durable_io_fatal_handler = handler

        def set_trading_state_getter(self, getter: Any) -> None:
            self.trading_state_getter = getter

        def set_denial_reporter(self, reporter: Any) -> None:
            self.denial_reporter = reporter

        def set_protection_event_reporter(self, reporter: Any) -> None:
            self.protection_event_reporter = reporter

        def set_live_canary_portfolio_baseline_getter(
            self,
            getter: Any,
        ) -> None:
            self.live_canary_portfolio_baseline_getter = getter

        def set_live_rollout_phase_getter(
            self,
            getter: Any,
        ) -> None:
            self.live_rollout_phase_getter = getter

        def set_live_open_gate_getter(
            self,
            getter: Any,
        ) -> None:
            self.live_open_gate_getter = getter

        def set_live_canary_risk_reporter(
            self,
            reporter: Any,
        ) -> None:
            self.live_canary_risk_reporter = reporter

        def set_live_canary_halt_handler(
            self,
            handler: Any,
        ) -> None:
            self.live_canary_halt_handler = handler

        def set_exchange_cancel_adapter(self, adapter: Any, mirror: Any) -> None:
            self.exchange_cancel_dependencies = (adapter, mirror)

    class _BoundedTaskWorker:
        def __init__(
            self,
            name: str,
            handler: Any,
            **kwargs: Any,
        ) -> None:
            del name, kwargs
            self._handler = handler

        def start(self) -> None:
            return

        def stop(self) -> None:
            return

        def submit(self, task: Any) -> bool:
            del task
            return True

        def snapshot(self) -> dict[str, Any]:
            return {"running": True}

    class _NodeControlPlaneSession:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.started = False

        def start(self) -> None:
            self.started = True

        def stop(self, deadline: float) -> bool:
            del deadline
            self.started = False
            return True

        def submit_execution_event(self, event: Any) -> str:
            del event
            return "accepted"

        def snapshot(self) -> dict[str, Any]:
            return {"started": self.started}

    class _RedisRuntimeSafetyGuard:
        def __init__(
            self,
            client: Any,
            *,
            config: Any,
            halt_callback: Any,
        ) -> None:
            del client, config, halt_callback

        def sample_now(self) -> dict[str, Any]:
            events = assembled.get("events")
            if events is not None:
                events.append("redis_safety.sample")
            return {"running": False}

        def start(self) -> bool:
            events = assembled.get("events")
            if events is not None:
                events.append("redis_safety.start")
            return True

        def stop(self) -> bool:
            events = assembled.get("events")
            if events is not None:
                events.append("redis_safety.stop")
            return True

        def snapshot(self) -> dict[str, Any]:
            return {"running": True}

    data_factory = object()
    exec_factory = object()
    assembled["data_factory"] = data_factory
    assembled["exec_factory"] = exec_factory

    _install_module("runtime", types.ModuleType("runtime"))
    runtime_worker = types.ModuleType("runtime.bounded_task_worker")
    runtime_worker.BoundedTaskWorker = _BoundedTaskWorker
    _install_module("runtime.bounded_task_worker", runtime_worker)
    runtime_session = types.ModuleType("runtime.control_plane_session")
    runtime_session.NodeControlPlaneSession = _NodeControlPlaneSession
    _install_module("runtime.control_plane_session", runtime_session)
    runtime_redis_safety = types.ModuleType("runtime.redis_safety")
    runtime_redis_safety.RedisRuntimeSafetyGuard = (
        _RedisRuntimeSafetyGuard
    )
    _install_module("runtime.redis_safety", runtime_redis_safety)
    runtime_binance_config = types.ModuleType("runtime.binance_adapter_config")
    runtime_binance_config.build_binance_client_configs = lambda config: (_Config(), _Config())
    _install_module("runtime.binance_adapter_config", runtime_binance_config)
    runtime_exchange_cancel = types.ModuleType("runtime.exchange_cancel_adapter")
    runtime_exchange_cancel.BinanceExchangeCancelAdapter = _Config
    runtime_exchange_cancel.BinanceExchangeEvidenceProvider = _Config
    runtime_exchange_cancel.ControlPlaneExchangeStateMirror = (
        _ControlPlaneExchangeStateMirror
    )
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
    sys.modules["nautilus_trader.config"].TradingNodeConfig = _TradingNodeConfig
    sys.modules["nautilus_trader.config"].CacheConfig = _CacheConfig
    sys.modules["nautilus_trader.config"].MessageBusConfig = _MessageBusConfig
    for name in (
        "DatabaseConfig",
        "LiveRiskEngineConfig",
        "LiveExecEngineConfig",
    ):
        setattr(sys.modules["nautilus_trader.config"], name, _Config)
    _install_module("nautilus_trader.core", types.ModuleType("nautilus_trader.core"))
    _install_module(
        "nautilus_trader.core.uuid",
        types.ModuleType("nautilus_trader.core.uuid"),
    )
    sys.modules["nautilus_trader.core.uuid"].UUID4 = _UUID4
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
