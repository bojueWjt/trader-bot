from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from config.node_config import NodeConfigError, load_node_config  # noqa: E402
from persistence.nautilus_config import (  # noqa: E402
    build_live_exec_engine_kwargs,
    derive_cache_config_payload,
    derive_message_bus_config_payload,
    derive_redis_key_prefix,
)


class PersistenceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["BINANCE_ACCOUNT_A_API_KEY"] = "account-a-key"
        os.environ["BINANCE_ACCOUNT_A_API_SECRET"] = "account-a-secret"
        os.environ["CONTROL_PLANE_ACCOUNT_A_TOKEN"] = "node-a-token"
        os.environ["BINANCE_ACCOUNT_B_API_KEY"] = "account-b-key"
        os.environ["BINANCE_ACCOUNT_B_API_SECRET"] = "account-b-secret"
        os.environ["CONTROL_PLANE_ACCOUNT_B_TOKEN"] = "node-b-token"

    def test_two_accounts_derive_disjoint_cache_and_bus_prefixes(self) -> None:
        account_a = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        account_b = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-b.sandbox.json"
        )

        prefixes = {
            derive_redis_key_prefix(account_a, "cache"),
            derive_redis_key_prefix(account_a, "message-bus"),
            derive_redis_key_prefix(account_b, "cache"),
            derive_redis_key_prefix(account_b, "message-bus"),
        }

        self.assertEqual(len(prefixes), 4)
        self.assertTrue(all(prefix.startswith("nautilus:account-") for prefix in prefixes))
        self.assertIn(account_a.trader_id, derive_redis_key_prefix(account_a, "cache"))
        self.assertIn(account_a.instance_id, derive_redis_key_prefix(account_a, "cache"))
        self.assertNotEqual(
            derive_cache_config_payload(account_a)["account_key_prefix"],
            derive_cache_config_payload(account_b)["account_key_prefix"],
        )
        self.assertNotEqual(
            derive_message_bus_config_payload(account_a)["streams_prefix"],
            derive_message_bus_config_payload(account_b)["streams_prefix"],
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
        self.assertTrue(cache["use_instance_id"])
        self.assertTrue(bus["use_trader_id"])
        self.assertTrue(bus["use_instance_id"])
        self.assertNotEqual(
            cache["account_key_prefix"],
            bus["streams_prefix"],
        )

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
