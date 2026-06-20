from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO_ROOT / "infra" / "compose" / "multi-account.sandbox.yml"


class MultiAccountComposeTopologyTests(unittest.TestCase):
    def test_compose_declares_two_isolated_account_nodes(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertIn("nautilus-node-account-a:", compose)
        self.assertIn("nautilus-node-account-b:", compose)
        self.assertIn("${NAUTILUS_NODE_IMAGE:-", compose)
        self.assertIn("NAUTILUS_ACCOUNT_ID: account-a", compose)
        self.assertIn("NAUTILUS_ACCOUNT_ID: account-b", compose)
        self.assertIn("TRADING_INITIAL_STATE: HALTED", compose)
        self.assertIn("BINANCE_ENVIRONMENT: testnet", compose)
        self.assertIn("REDIS_KEY_PREFIX: nautilus:account-a:node-a", compose)
        self.assertIn("REDIS_KEY_PREFIX: nautilus:account-b:node-b", compose)
        self.assertIn("account-a-spool:", compose)
        self.assertIn("account-b-spool:", compose)
        self.assertIn("binance_account_a_api_key", compose)
        self.assertIn("binance_account_b_api_key", compose)
        self.assertIn("control_plane_account_a_token", compose)
        self.assertIn("control_plane_account_b_token", compose)
        self.assertIn("internal: true", compose)
        self.assertNotIn("\n    ports:", compose)


if __name__ == "__main__":
    unittest.main()
