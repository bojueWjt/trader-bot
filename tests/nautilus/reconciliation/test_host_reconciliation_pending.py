from __future__ import annotations

import unittest


@unittest.skip("待 hk 容器执行: requires nautilus_trader==1.227.0, Redis, and Binance testnet")
class HostReconciliationPendingTests(unittest.TestCase):
    def test_restart_with_open_position_recovers_from_cache_and_startup_reconciliation(self) -> None:
        raise AssertionError("host-only placeholder must be implemented in hk container")

    def test_true_redis_cache_and_message_bus_are_usable_by_nautilus(self) -> None:
        raise AssertionError("host-only placeholder must be implemented in hk container")

    def test_continuous_reconciliation_replays_do_not_duplicate_fills(self) -> None:
        raise AssertionError("host-only placeholder must be implemented in hk container")


if __name__ == "__main__":
    unittest.main()
