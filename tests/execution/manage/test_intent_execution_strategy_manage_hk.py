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
class IntentExecutionStrategyManageHkTest(unittest.TestCase):
    def test_stop_loss_replace_cancels_old_stop_and_submits_reduce_only_stop_market(self) -> None:
        self.skipTest(
            "TODO(host-verify): build BacktestEngine/simulated venue harness for "
            "move_stop_loss -> cancel existing stop_loss lifecycle order -> submit "
            "reduce_only STOP_MARKET."
        )

    def test_take_profit_replace_cancels_old_tps_and_submits_reduce_only_conditional_tps(self) -> None:
        self.skipTest(
            "TODO(host-verify): build BacktestEngine/simulated venue harness for "
            "replace_take_profits -> cancel existing take_profit lifecycle orders "
            "-> submit reduce_only LIMIT_IF_TOUCHED orders."
        )

    def test_restart_rebuilds_stop_and_take_profit_lifecycle_from_cache(self) -> None:
        self.skipTest(
            "TODO(host-verify): seed open position plus open lifecycle orders, "
            "restart IntentExecutionStrategy, and verify cache reconciliation "
            "rebuilds target position uniqueness and stop/TP replacement state."
        )


if __name__ == "__main__":
    unittest.main()
