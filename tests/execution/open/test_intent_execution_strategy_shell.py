from __future__ import annotations

import sys
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from strategy.intent_execution_planner import encode_client_order_id  # noqa: E402
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)


class StrategyShellTest(unittest.TestCase):
    def test_active_intent_ids_reads_client_order_id_and_tags(self) -> None:
        order_intent_id = uuid4()
        tagged_intent_id = uuid4()

        class _CacheStubStrategy(IntentExecutionStrategy):
            # Nautilus Actor.cache is read-only; override the cache-read helpers to
            # exercise _active_intent_ids without touching the real cache property.
            def _cache_orders(self, _instrument_id):
                return [
                    SimpleNamespace(client_order_id=encode_client_order_id(order_intent_id)),
                    SimpleNamespace(tags=[f"intent_id={tagged_intent_id}"]),
                ]

            def _cache_positions(self, _instrument_id):
                return ()

        strategy = _CacheStubStrategy(
            IntentExecutionStrategyConfig(account_id="account-a", trading_state="ACTIVE")
        )

        self.assertEqual(
            strategy._active_intent_ids("BTCUSDT-PERP.BINANCE"),
            {str(order_intent_id), str(tagged_intent_id)},
        )

    def test_trading_state_getter_accepts_enum_values(self) -> None:
        class TradingState(Enum):
            ACTIVE = "ACTIVE"

        strategy = IntentExecutionStrategy(
            IntentExecutionStrategyConfig(account_id="account-a")
        )
        strategy.set_trading_state_getter(lambda: TradingState.ACTIVE)

        self.assertEqual(strategy._trading_state(), "ACTIVE")


if __name__ == "__main__":
    unittest.main()
