from __future__ import annotations

import importlib.util
import os
import unittest


HAS_NAUTILUS = importlib.util.find_spec("nautilus_trader") is not None
RUN_HK = os.environ.get("RUN_HK_NAUTILUS_TESTS") == "1"


@unittest.skipUnless(
    HAS_NAUTILUS and RUN_HK,
    "待 hk 容器执行：requires nautilus_trader and RUN_HK_NAUTILUS_TESTS=1",
)
class IntentExecutionStrategyHkTest(unittest.TestCase):
    def test_market_open_submits_real_order_in_simulated_engine(self) -> None:
        self.skipTest(
            "TODO(host-verify): build BacktestEngine/simulated venue harness for "
            "CustomData -> IntentExecutionStrategy -> submitted MARKET order."
        )

    def test_add_position_submits_real_limit_order_in_simulated_engine(self) -> None:
        self.skipTest(
            "TODO(host-verify): build BacktestEngine/simulated venue harness with "
            "existing same-side position -> submitted LIMIT add order."
        )


if __name__ == "__main__":
    unittest.main()
