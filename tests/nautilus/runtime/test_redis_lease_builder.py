from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.node import _build_redis_namespace_lease
from config.node_config import load_node_config
from persistence.nautilus_config import derive_nautilus_cache_key_root


class RedisLeaseBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._environment = patch.dict(
            os.environ,
            {
                "BINANCE_ACCOUNT_A_API_KEY": "account-a-key",
                "BINANCE_ACCOUNT_A_API_SECRET": "account-a-secret",
                "CONTROL_PLANE_ACCOUNT_A_TOKEN": "node-a-token",
            },
            clear=False,
        )
        self._environment.start()
        config = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        self.testnet_config = config
        self.live_config = replace(
            config,
            binance=replace(config.binance, environment="live"),
        )

    def tearDown(self) -> None:
        self._environment.stop()

    def test_non_live_environment_skips_lease_and_release_requirement(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "persistence.redis_resp_client.RedisRespClient",
                side_effect=AssertionError("Redis client should not be constructed"),
            ),
            patch(
                "persistence.redis_namespace_lease.RedisNamespaceLease",
                side_effect=AssertionError("lease should not be constructed"),
            ),
        ):
            lease = _build_redis_namespace_lease(self.testnet_config)

        self.assertIsNone(lease)

    def test_live_environment_requires_non_blank_release_id(self) -> None:
        for environment in ({}, {"TRADER_RELEASE_ID": " \t "}):
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
                self.assertRaisesRegex(
                    RuntimeError,
                    "TRADER_RELEASE_ID is required",
                ),
            ):
                _build_redis_namespace_lease(self.live_config)

    def test_live_environment_builds_stable_fenced_namespace_identity(self) -> None:
        redis_client = object()
        lease = object()
        release_uuid = UUID("12345678-1234-5678-1234-567812345678")

        with (
            patch.dict(
                os.environ,
                {"TRADER_RELEASE_ID": "  release-20260808-a  "},
                clear=True,
            ),
            patch(
                "persistence.redis_resp_client.RedisRespClient",
                return_value=redis_client,
            ) as redis_client_type,
            patch(
                "persistence.redis_namespace_lease.RedisNamespaceLease",
                return_value=lease,
            ) as lease_type,
            patch("app.node.socket.gethostname", return_value="hk-node-01"),
            patch("app.node.os.getpid", return_value=4242),
            patch("app.node.uuid4", return_value=release_uuid),
        ):
            built = _build_redis_namespace_lease(self.live_config)

        self.assertIs(built, lease)
        redis_client_type.assert_called_once_with(self.live_config.redis.url)
        lease_type.assert_called_once_with(
            redis_client,
            namespace=derive_nautilus_cache_key_root(self.live_config),
            owner=(
                f"{self.live_config.node_id}:hk-node-01:4242:"
                "12345678123456781234567812345678"
            ),
            release_id="release-20260808-a",
        )


if __name__ == "__main__":
    unittest.main()
