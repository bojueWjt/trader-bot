"""Control-plane fraction passthrough. Independent of the node planner.

Frozen A-shape partial_close carries fraction and no quantity. Both the
string "0.7" and the number 0.7 must come out as fraction "0.7". Do not add
quantity to dodge a seam that drops fraction.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE_API = REPO_ROOT / "services" / "control-plane" / "api"
if str(CONTROL_PLANE_API) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE_API))

import read_api  # noqa: E402


REQUEST_SEMANTICS_SHA256 = "a" * 64

PRODUCTION_ORDER_PLAN = {
    "side": None,
    "entry": {"type": "market", "time_in_force": "IOC"},
    "fraction": "0.7",
    "leverage": None,
    "principal": {
        "kind": "operator",
        "scope": "global",
        "actor_id": "risk_admin",
        "account_id": None,
    },
    "stop_loss": None,
    "take_profits": [],
    "position_side": "long",
    "request_semantics": {
        "version": "operator-management-v1",
        "sha256": REQUEST_SEMANTICS_SHA256,
    },
}


def _through_execution_order_plan(fraction: str | float) -> dict:
    order_plan = dict(PRODUCTION_ORDER_PLAN)
    order_plan["fraction"] = fraction
    return read_api._execution_order_plan(
        order_plan,
        {"max_notional": 0.0, "risk_fraction": 0.0, "max_leverage": 1},
        "BTCUSDT",
        action="partial_close",
    )


class ExecutionOrderPlanFractionTest(unittest.TestCase):
    def _assert_fraction_passthrough(self, fraction: str | float) -> None:
        translated = _through_execution_order_plan(fraction)
        self.assertEqual(translated.get("fraction"), "0.7")
        self.assertNotIn("quantity", translated)
        self.assertEqual(translated.get("position_side"), "long")
        self.assertEqual(translated.get("type"), "market")

    def test_string_fraction_0_7_is_passed_through(self) -> None:
        self._assert_fraction_passthrough("0.7")

    def test_number_fraction_0_7_is_passed_through(self) -> None:
        self._assert_fraction_passthrough(0.7)


if __name__ == "__main__":
    unittest.main()
