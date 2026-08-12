from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from config.node_config import NodeConfigError, load_node_config
from persistence.nautilus_config import (
    DEFAULT_MESSAGE_BUS_AUTOTRIM_MINS,
    DEFAULT_REDIS_RUNTIME_SAFETY_CRITICAL_WINDOW_SECONDS,
    DEFAULT_REDIS_RUNTIME_SAFETY_SAMPLE_INTERVAL_SECONDS,
    DEFAULT_REDIS_RUNTIME_SAFETY_SCAN_COUNT,
    DEFAULT_REDIS_STREAM_MAX_BYTES,
    DEFAULT_REDIS_STREAM_MAX_ENTRIES,
    DEFAULT_REDIS_TOTAL_STREAM_MAX_BYTES,
    MESSAGE_BUS_AUTOTRIM_MINS_ENV,
    build_cache_config,
    build_live_exec_engine_kwargs,
    build_message_bus_config,
    build_nautilus_persistence_config,
    derive_cache_config_payload,
    derive_message_bus_config_payload,
    derive_nautilus_cache_key_root,
    derive_nautilus_message_bus_stream_root,
    derive_redis_key_prefix,
    derive_redis_runtime_safety_config,
)


class PersistenceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["BINANCE_ACCOUNT_A_API_KEY"] = "account-a-key"
        os.environ["BINANCE_ACCOUNT_A_API_SECRET"] = "account-a-secret"
        os.environ["CONTROL_PLANE_ACCOUNT_A_TOKEN"] = "node-a-token"
        os.environ["BINANCE_ACCOUNT_B_API_KEY"] = "account-b-key"
        os.environ["BINANCE_ACCOUNT_B_API_SECRET"] = "account-b-secret"
        os.environ["CONTROL_PLANE_ACCOUNT_B_TOKEN"] = "node-b-token"
        os.environ["BINANCE_ACCOUNT_C_API_KEY"] = "account-c-key"
        os.environ["BINANCE_ACCOUNT_C_API_SECRET"] = "account-c-secret"
        os.environ["CONTROL_PLANE_ACCOUNT_C_TOKEN"] = "node-c-token"
        os.environ["BINANCE_ACCOUNT_D_API_KEY"] = "account-d-key"
        os.environ["BINANCE_ACCOUNT_D_API_SECRET"] = "account-d-secret"
        os.environ["CONTROL_PLANE_ACCOUNT_D_TOKEN"] = "node-d-token"

    def test_four_accounts_derive_disjoint_cache_and_bus_prefixes(self) -> None:
        accounts = [
            load_node_config(
                SERVICE_ROOT
                / "config"
                / "examples"
                / f"account-{suffix}.sandbox.json"
            )
            for suffix in ("a", "b", "c", "d")
        ]

        prefixes = {
            derive_redis_key_prefix(account, namespace)
            for account in accounts
            for namespace in ("cache", "message-bus")
        }

        self.assertEqual(len(prefixes), 8)
        self.assertTrue(all(prefix.startswith("nautilus:account-") for prefix in prefixes))
        for account in accounts:
            self.assertIn(
                account.trader_id,
                derive_redis_key_prefix(account, "cache"),
            )
            self.assertNotIn(
                account.instance_id,
                derive_redis_key_prefix(account, "cache"),
            )
        self.assertEqual(
            len(
                {
                    derive_cache_config_payload(account)["account_key_prefix"]
                    for account in accounts
                }
            ),
            4,
        )
        self.assertEqual(
            len(
                {
                    derive_message_bus_config_payload(account)["streams_prefix"]
                    for account in accounts
                }
            ),
            4,
        )

    def test_cache_and_message_bus_payloads_use_redis_database_backend(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )

        cache = derive_cache_config_payload(account)
        bus = derive_message_bus_config_payload(account)

        self.assertEqual(cache["database"]["type"], "redis")
        self.assertEqual(bus["database"]["type"], "redis")
        self.assertEqual(cache["account_key_prefix"], derive_redis_key_prefix(account, "cache"))
        self.assertEqual(
            bus["streams_prefix"],
            derive_redis_key_prefix(account, "message-bus"),
        )
        self.assertTrue(cache["use_trader_prefix"])
        self.assertFalse(cache["use_instance_id"])
        self.assertTrue(bus["use_trader_prefix"])
        self.assertTrue(bus["use_trader_id"])
        self.assertFalse(bus["use_instance_id"])
        self.assertEqual(bus["autotrim_mins"], DEFAULT_MESSAGE_BUS_AUTOTRIM_MINS)
        self.assertNotEqual(
            cache["account_key_prefix"],
            bus["streams_prefix"],
        )

    def test_nonzero_redis_database_is_rejected_before_nautilus_mapping(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        nonzero_database = replace(
            account,
            redis=replace(
                account.redis,
                url="redis://localhost:6379/1",
            ),
        )

        with self.assertRaisesRegex(ValueError, "non-zero Redis database"):
            derive_cache_config_payload(nonzero_database)

    def test_runtime_instance_rotation_reuses_true_nautilus_key_roots(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        restarted = replace(account, instance_id="INSTANCE-ACCOUNT-A-RESTARTED")

        self.assertEqual(
            derive_nautilus_cache_key_root(account),
            derive_nautilus_cache_key_root(restarted),
        )
        self.assertEqual(
            derive_nautilus_message_bus_stream_root(account),
            derive_nautilus_message_bus_stream_root(restarted),
        )
        self.assertIn(account.account_id, derive_nautilus_message_bus_stream_root(account))
        self.assertIn(account.trader_id, derive_nautilus_cache_key_root(account))
        self.assertIn(account.trader_id, derive_nautilus_message_bus_stream_root(account))
        self.assertNotIn(account.instance_id, derive_nautilus_cache_key_root(account))
        self.assertNotIn(account.instance_id, derive_nautilus_message_bus_stream_root(account))

    def test_fenced_generations_use_disjoint_cache_and_message_bus_roots(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        first_generation = "00000000-0000-4000-8000-000000000001"
        second_generation = "00000000-0000-4000-8000-000000000002"

        first = build_nautilus_persistence_config(
            account,
            persistence_instance_id=first_generation,
        )
        second = build_nautilus_persistence_config(
            account,
            persistence_instance_id=second_generation,
        )

        self.assertTrue(first.cache["use_instance_id"])
        self.assertTrue(first.message_bus["use_instance_id"])
        self.assertEqual(
            first.cache["key_root"],
            f"trader-{account.trader_id}:{first_generation}",
        )
        self.assertEqual(
            first.message_bus["stream_root"],
            ":".join(
                (
                    f"trader-{account.trader_id}",
                    first_generation,
                    derive_redis_key_prefix(account, "message-bus"),
                )
            ),
        )
        self.assertEqual(
            first.redis_runtime_safety.stream_root,
            first.message_bus["stream_root"],
        )
        self.assertEqual(
            second.redis_runtime_safety.stream_root,
            second.message_bus["stream_root"],
        )
        self.assertNotEqual(first.cache["key_root"], second.cache["key_root"])
        self.assertNotEqual(
            first.message_bus["stream_root"],
            second.message_bus["stream_root"],
        )

    def test_runtime_safety_config_targets_active_generation_stream_root(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        active_generation = "00000000-0000-4000-8000-000000000001"

        safety = derive_redis_runtime_safety_config(
            account,
            persistence_instance_id=active_generation,
        )

        self.assertEqual(
            safety.stream_root,
            derive_nautilus_message_bus_stream_root(
                account,
                active_generation,
            ),
        )
        self.assertEqual(
            safety.stream_max_entries,
            DEFAULT_REDIS_STREAM_MAX_ENTRIES,
        )
        self.assertEqual(
            safety.stream_max_bytes,
            DEFAULT_REDIS_STREAM_MAX_BYTES,
        )
        self.assertEqual(
            safety.total_stream_max_bytes,
            DEFAULT_REDIS_TOTAL_STREAM_MAX_BYTES,
        )
        self.assertEqual(
            safety.scan_count,
            DEFAULT_REDIS_RUNTIME_SAFETY_SCAN_COUNT,
        )
        self.assertEqual(
            safety.sample_interval_seconds,
            DEFAULT_REDIS_RUNTIME_SAFETY_SAMPLE_INTERVAL_SECONDS,
        )
        self.assertEqual(
            safety.critical_window_seconds,
            DEFAULT_REDIS_RUNTIME_SAFETY_CRITICAL_WINDOW_SECONDS,
        )

    def test_runtime_safety_config_rejects_environment_drift(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        active_generation = "00000000-0000-4000-8000-000000000001"
        environment = {
            "NAUTILUS_REDIS_STREAM_MAX_ENTRIES": "25000",
            "NAUTILUS_REDIS_STREAM_MAX_BYTES": "16777216",
            "NAUTILUS_REDIS_TOTAL_STREAM_MAX_BYTES": "67108864",
            "NAUTILUS_REDIS_RUNTIME_SAFETY_SCAN_COUNT": "200",
            "NAUTILUS_REDIS_RUNTIME_SAFETY_SAMPLE_INTERVAL_SECONDS": "2.5",
            "NAUTILUS_REDIS_RUNTIME_SAFETY_CRITICAL_WINDOW_SECONDS": "12",
            "NAUTILUS_REDIS_RUNTIME_SAFETY_THREAD_JOIN_TIMEOUT_SECONDS": "3",
        }

        with (
            patch.dict(os.environ, environment, clear=False),
            self.assertRaisesRegex(
                ValueError,
                "Redis runtime resource env overrides are forbidden",
            ),
        ):
            derive_redis_runtime_safety_config(
                account,
                persistence_instance_id=active_generation,
            )

    def test_runtime_safety_config_rejects_invalid_environment_values(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        invalid_values = (
            ("NAUTILUS_REDIS_STREAM_MAX_ENTRIES", "0"),
            ("NAUTILUS_REDIS_STREAM_MAX_BYTES", "-1"),
            ("NAUTILUS_REDIS_TOTAL_STREAM_MAX_BYTES", "1.5"),
            ("NAUTILUS_REDIS_RUNTIME_SAFETY_SCAN_COUNT", "five"),
            (
                "NAUTILUS_REDIS_RUNTIME_SAFETY_SAMPLE_INTERVAL_SECONDS",
                "0",
            ),
            (
                "NAUTILUS_REDIS_RUNTIME_SAFETY_CRITICAL_WINDOW_SECONDS",
                "nan",
            ),
            (
                "NAUTILUS_REDIS_RUNTIME_SAFETY_THREAD_JOIN_TIMEOUT_SECONDS",
                "-1",
            ),
        )

        for name, value in invalid_values:
            with (
                self.subTest(name=name, value=value),
                patch.dict(os.environ, {name: value}, clear=True),
                self.assertRaisesRegex(ValueError, name),
            ):
                derive_redis_runtime_safety_config(account)

        with (
            patch.dict(
                os.environ,
                {
                    "NAUTILUS_REDIS_STREAM_MAX_BYTES": "2048",
                    "NAUTILUS_REDIS_TOTAL_STREAM_MAX_BYTES": "1024",
                },
                clear=True,
            ),
            self.assertRaisesRegex(
                ValueError,
                "TOTAL_STREAM_MAX_BYTES",
            ),
        ):
            derive_redis_runtime_safety_config(account)

    def test_testnet_persistence_stays_stable_without_fenced_generation(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )

        persistence = build_nautilus_persistence_config(account)

        self.assertFalse(persistence.cache["use_instance_id"])
        self.assertFalse(persistence.message_bus["use_instance_id"])
        self.assertEqual(
            persistence.cache["key_root"],
            f"trader-{account.trader_id}",
        )

    def test_autotrim_environment_override_reaches_real_message_bus_constructor(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        captured: dict[str, dict[str, object]] = {}

        class FakeDatabaseConfig:
            def __init__(self, **kwargs: object) -> None:
                captured["database"] = kwargs

        class FakeCacheConfig:
            def __init__(self, **kwargs: object) -> None:
                captured["cache"] = kwargs

        class FakeMessageBusConfig:
            def __init__(self, **kwargs: object) -> None:
                captured["message_bus"] = kwargs

        fake_config_module = ModuleType("nautilus_trader.config")
        fake_config_module.DatabaseConfig = FakeDatabaseConfig
        fake_config_module.CacheConfig = FakeCacheConfig
        fake_config_module.MessageBusConfig = FakeMessageBusConfig
        fake_package = ModuleType("nautilus_trader")
        fake_package.config = fake_config_module

        with (
            patch.dict(
                sys.modules,
                {
                    "nautilus_trader": fake_package,
                    "nautilus_trader.config": fake_config_module,
                },
            ),
            patch.dict(
                os.environ,
                {MESSAGE_BUS_AUTOTRIM_MINS_ENV: "90"},
            ),
        ):
            build_cache_config(account)
            build_message_bus_config(account)

        self.assertFalse(captured["cache"]["use_instance_id"])
        self.assertTrue(captured["message_bus"]["use_trader_prefix"])
        self.assertFalse(captured["message_bus"]["use_instance_id"])
        self.assertEqual(captured["message_bus"]["autotrim_mins"], 90)

    def test_autotrim_override_rejects_invalid_values(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )

        for value in ("", "0", "-1", "1.5", "forever"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {MESSAGE_BUS_AUTOTRIM_MINS_ENV: value},
                    clear=False,
                ),
                self.assertRaises(ValueError),
            ):
                derive_message_bus_config_payload(account)

    def test_existing_config_loads_with_safe_reconciliation_defaults(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )

        self.assertTrue(account.cache.enabled)
        self.assertEqual(account.cache.backend, "redis")
        self.assertTrue(account.message_bus.enabled)
        self.assertEqual(account.message_bus.backend, "redis")
        self.assertTrue(account.reconciliation.startup)
        self.assertTrue(account.reconciliation.continuous)
        self.assertGreaterEqual(account.reconciliation.lookback_mins, 60)

    def test_live_exec_engine_kwargs_enable_startup_and_continuous_reconciliation(self) -> None:
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )

        kwargs = build_live_exec_engine_kwargs(account)

        self.assertTrue(kwargs["reconciliation"])
        self.assertGreaterEqual(kwargs["reconciliation_lookback_mins"], 60)
        self.assertGreater(kwargs["open_check_interval_secs"], 0)
        self.assertGreaterEqual(kwargs["open_check_lookback_mins"], 60)
        self.assertGreater(kwargs["position_check_interval_secs"], 0)
        self.assertGreaterEqual(kwargs["position_check_lookback_mins"], 60)

    def test_cache_and_message_bus_cannot_be_disabled(self) -> None:
        base_path = SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        raw = json.loads(base_path.read_text(encoding="utf-8"))
        raw["cache"] = {"enabled": False, "backend": "redis"}
        raw["message_bus"] = {"enabled": False, "backend": "redis"}

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(raw, handle)
            temp_path = Path(handle.name)
        try:
            with self.assertRaises(NodeConfigError):
                load_node_config(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
