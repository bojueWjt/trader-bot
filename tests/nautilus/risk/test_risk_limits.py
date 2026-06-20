from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from risk.config import RiskLimitConfig, build_live_risk_engine_kwargs  # noqa: E402
from risk.limits import (  # noqa: E402
    InstrumentPrecision,
    LimitOrderRequest,
    RiskLimitMirror,
    TradingStateOrderAction,
    TradingStateOrderGate,
    TradingStateOrderRequest,
)


class RiskConfigTests(unittest.TestCase):
    def test_live_risk_engine_config_is_not_bypassed_and_contains_limits(self) -> None:
        limits = RiskLimitConfig(
            max_notional_per_order={"BTCUSDT-PERP.BINANCE": "1000"},
            max_order_submit_rate="2/00:01:00",
            max_order_modify_rate="5/00:01:00",
        )

        payload = build_live_risk_engine_kwargs(limits)

        self.assertIs(payload["bypass"], False)
        self.assertEqual(payload["max_notional_per_order"], {"BTCUSDT-PERP.BINANCE": "1000"})
        self.assertEqual(payload["max_order_submit_rate"], "2/00:01:00")
        self.assertEqual(payload["max_order_modify_rate"], "5/00:01:00")


class RiskLimitMirrorTests(unittest.TestCase):
    def test_order_denied_when_notional_exceeds_configured_limit(self) -> None:
        mirror = RiskLimitMirror(
            RiskLimitConfig(
                max_notional_per_order={"BTCUSDT-PERP.BINANCE": "1000"},
                max_order_submit_rate="10/00:01:00",
                max_order_modify_rate="10/00:01:00",
            )
        )
        request = LimitOrderRequest(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.2",
            price="6000",
            ts=datetime(2026, 6, 19, 12, tzinfo=timezone.utc),
        )

        decision = mirror.check_submit(request)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max_notional_exceeded")

    def test_order_denied_when_submit_rate_exceeds_configured_limit(self) -> None:
        mirror = RiskLimitMirror(
            RiskLimitConfig(
                max_notional_per_order={"BTCUSDT-PERP.BINANCE": "100000"},
                max_order_submit_rate="2/00:01:00",
                max_order_modify_rate="10/00:01:00",
            )
        )
        first = LimitOrderRequest(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.01",
            price="100",
            ts=datetime(2026, 6, 19, 12, tzinfo=timezone.utc),
        )
        second = LimitOrderRequest(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.01",
            price="100",
            ts=first.ts + timedelta(seconds=1),
        )
        third = LimitOrderRequest(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.01",
            price="100",
            ts=first.ts + timedelta(seconds=2),
        )

        self.assertTrue(mirror.check_submit(first).allowed)
        self.assertTrue(mirror.check_submit(second).allowed)
        decision = mirror.check_submit(third)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "submit_rate_exceeded")

    def test_order_denied_when_price_or_quantity_violates_instrument_precision(self) -> None:
        mirror = RiskLimitMirror(
            RiskLimitConfig(
                max_notional_per_order={"BTCUSDT-PERP.BINANCE": "100000"},
                max_order_submit_rate="10/00:01:00",
                max_order_modify_rate="10/00:01:00",
                instrument_precision={
                    "BTCUSDT-PERP.BINANCE": InstrumentPrecision(
                        price_increment="0.10",
                        quantity_increment="0.001",
                    )
                },
            )
        )
        request = LimitOrderRequest(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.0001",
            price="100.05",
            ts=datetime(2026, 6, 19, 12, tzinfo=timezone.utc),
        )

        decision = mirror.check_submit(request)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "instrument_precision")


class TradingStateOrderGateTests(unittest.TestCase):
    def test_halted_denies_new_orders_but_allows_cancel(self) -> None:
        gate = TradingStateOrderGate()

        submit = gate.check(
            "HALTED",
            TradingStateOrderRequest(action=TradingStateOrderAction.SUBMIT),
        )
        cancel = gate.check(
            "HALTED",
            TradingStateOrderRequest(action=TradingStateOrderAction.CANCEL),
        )

        self.assertFalse(submit.allowed)
        self.assertEqual(submit.reason, "trading_halted")
        self.assertTrue(cancel.allowed)

    def test_reducing_allows_reduce_only_submit_and_denies_increasing_submit(self) -> None:
        gate = TradingStateOrderGate()

        reduce_only = gate.check(
            "REDUCING",
            TradingStateOrderRequest(
                action=TradingStateOrderAction.SUBMIT,
                reduce_only=True,
            ),
        )
        increasing = gate.check(
            "REDUCING",
            TradingStateOrderRequest(action=TradingStateOrderAction.SUBMIT),
        )

        self.assertTrue(reduce_only.allowed)
        self.assertFalse(increasing.allowed)
        self.assertEqual(increasing.reason, "trading_reducing")


if __name__ == "__main__":
    unittest.main()
