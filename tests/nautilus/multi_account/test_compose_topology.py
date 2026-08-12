from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO_ROOT / "infra" / "compose" / "multi-account.sandbox.yml"
CONFIG_DIR = REPO_ROOT / "infra" / "compose" / "config"


class MultiAccountComposeTopologyTests(unittest.TestCase):
    def test_compose_declares_four_isolated_account_nodes(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")

        # Per-account node services use a pinned image and node entrypoint.
        for suffix, port in zip(("a", "b", "c", "d"), range(8081, 8085)):
            self.assertIn(f"nautilus-node-account-{suffix}:", compose)
            self.assertIn(
                f"NODE_CONFIG_PATH: /app/config/account-{suffix}.sandbox.json",
                compose,
            )
            self.assertIn(f"NAUTILUS_HEALTH_PORT: {port}", compose)
            self.assertIn(f"account-{suffix}-spool:", compose)
            self.assertIn(
                f"binance_account_{suffix}_api_key",
                compose,
            )
            self.assertIn(
                f"control_plane_account_{suffix}_token",
                compose,
            )
        self.assertIn("${NAUTILUS_NODE_IMAGE:-", compose)
        # internal-only network, no published ports
        self.assertIn("internal: true", compose)
        self.assertNotIn("\n    ports:", compose)

    def test_per_account_config_files_are_isolated_and_default_safe(self) -> None:
        configs = [
            json.loads(
                (CONFIG_DIR / f"account-{suffix}.sandbox.json").read_text("utf-8")
            )
            for suffix in ("a", "b", "c", "d")
        ]

        self.assertEqual(
            {config["account_id"] for config in configs},
            {"account-a", "account-b", "account-c", "account-d"},
        )
        self.assertEqual(
            {config["binance"]["environment"] for config in configs},
            {"testnet"},
        )
        self.assertEqual(len({config["trader_id"] for config in configs}), 4)
        self.assertEqual(
            len({config["instance_id"] for config in configs}),
            4,
        )
        self.assertEqual(
            len({config["redis"]["key_prefix"] for config in configs}),
            4,
        )
        self.assertEqual(
            len(
                {
                    config["control_plane"]["token"]["env"]
                    for config in configs
                }
            ),
            4,
        )


if __name__ == "__main__":
    unittest.main()
