from __future__ import annotations

import unittest


class NautilusRiskIntegrationHKTests(unittest.TestCase):
    @unittest.skip("TODO(hk): requires nautilus_trader==1.227.0 container")
    def test_live_risk_engine_denies_over_notional_order(self) -> None:
        raise NotImplementedError

    @unittest.skip("TODO(hk): requires nautilus_trader==1.227.0 container")
    def test_live_cancel_all_and_reduce_only_close_all_confirm_events(self) -> None:
        raise NotImplementedError


if __name__ == "__main__":
    unittest.main()
