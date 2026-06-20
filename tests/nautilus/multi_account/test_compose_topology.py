from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO_ROOT / "infra" / "compose" / "multi-account.sandbox.yml"
CONFIG_DIR = REPO_ROOT / "infra" / "compose" / "config"


class MultiAccountComposeTopologyTests(unittest.TestCase):
    def test_compose_declares_two_isolated_account_nodes(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")

        # two per-account node services, pinned image (overridable), node entrypoint
        self.assertIn("nautilus-node-account-a:", compose)
        self.assertIn("nautilus-node-account-b:", compose)
        self.assertIn("${NAUTILUS_NODE_IMAGE:-", compose)
        # each node is driven by its own mounted config file (account specifics live in JSON)
        self.assertIn("NODE_CONFIG_PATH: /app/config/account-a.sandbox.json", compose)
        self.assertIn("NODE_CONFIG_PATH: /app/config/account-b.sandbox.json", compose)
        # isolated spool volumes + per-account secrets
        self.assertIn("account-a-spool:", compose)
        self.assertIn("account-b-spool:", compose)
        self.assertIn("binance_account_a_api_key", compose)
        self.assertIn("binance_account_b_api_key", compose)
        self.assertIn("control_plane_account_a_token", compose)
        self.assertIn("control_plane_account_b_token", compose)
        # internal-only network, no published ports
        self.assertIn("internal: true", compose)
        self.assertNotIn("\n    ports:", compose)

    def test_per_account_config_files_are_isolated_and_default_safe(self) -> None:
        account_a = json.loads((CONFIG_DIR / "account-a.sandbox.json").read_text("utf-8"))
        account_b = json.loads((CONFIG_DIR / "account-b.sandbox.json").read_text("utf-8"))

        self.assertEqual(account_a["account_id"], "account-a")
        self.assertEqual(account_b["account_id"], "account-b")
        # default-safe: testnet only (iron rule #10)
        self.assertEqual(account_a["binance"]["environment"], "testnet")
        self.assertEqual(account_b["binance"]["environment"], "testnet")
        # isolation: trader_id and redis key prefix must differ across accounts
        self.assertNotEqual(account_a["trader_id"], account_b["trader_id"])
        self.assertNotEqual(
            account_a["redis"]["key_prefix"], account_b["redis"]["key_prefix"]
        )


if __name__ == "__main__":
    unittest.main()
