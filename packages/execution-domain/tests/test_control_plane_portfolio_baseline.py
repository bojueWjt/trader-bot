from __future__ import annotations

import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from execution_domain.control_plane import (  # noqa: E402
    portfolio_baseline_sha256,
)


EMPTY_PORTFOLIO_BASELINE_SHA256 = (
    "6ae771d5d317b151109d3933059c0c46ec02368fc826444c9a805bdaa775813f"
)


def test_empty_portfolio_baseline_has_stable_digest() -> None:
    heartbeat = {
        "positions": [],
        "regular_orders": [],
        "algo_orders": [],
    }

    assert (
        portfolio_baseline_sha256(heartbeat, "SOLUSDT")
        == EMPTY_PORTFOLIO_BASELINE_SHA256
    )


def test_portfolio_baseline_canonicalizes_non_target_evidence() -> None:
    original = {
        "positions": [
            {
                "symbol": "XAUUSDT-PERP",
                "quantity": "1.00",
                "mark_price": "2400",
            },
            {
                "symbol": "SOLUSDT",
                "quantity": "0.5",
            },
        ],
        "regular_orders": [
            {
                "symbol": "ETHUSDT",
                "client_order_id": "eth-order",
                "quantity": "0.10",
                "price": "3000.00",
                "legs": [
                    {"price": "3200.0", "quantity": "0.05"},
                    {"price": "3100", "quantity": "0.050"},
                ],
            },
            {
                "symbol": "BTCUSDT",
                "client_order_id": "btc-order",
                "price": "60000",
            },
        ],
        "algo_orders": [],
    }
    equivalent = {
        "positions": [
            {
                "quantity": "1",
                "symbol": "xauusdt",
                "mark_price": "2500",
            }
        ],
        "regular_orders": [
            {
                "price": "60000.0",
                "client_order_id": "btc-order",
                "symbol": "btcusdt",
            },
            {
                "legs": [
                    {"quantity": "0.05", "price": "3100.00"},
                    {"quantity": "0.0500", "price": "3200"},
                ],
                "price": "3000",
                "quantity": "0.1",
                "client_order_id": "eth-order",
                "symbol": "ETHUSDT",
            },
        ],
        "algo_orders": [],
    }

    assert (
        portfolio_baseline_sha256(original, "SOLUSDT")
        == portfolio_baseline_sha256(equivalent, "SOLUSDT")
    )


def test_portfolio_baseline_ignores_position_risk_market_refresh() -> None:
    original = {
        "positions": [
            {
                "symbol": "ETHUSDT",
                "quantity": "-0.25",
                "position_side": "SHORT",
                "entry_price": "3000",
                "mark_price": "2999",
                "notional": "-749.75",
                "initial_margin": "37.4875",
                "position_initial_margin": "37.4875",
                "open_order_initial_margin": "0",
                "maint_margin": "3.74875",
                "unrealized_pnl": "0.25",
                "unrealized_profit": "0.25",
                "liquidation_price": "4000",
                "update_time": 1_787_034_181_545,
            }
        ],
        "regular_orders": [],
        "algo_orders": [],
    }
    refreshed = {
        "positions": [
            {
                "symbol": "ETHUSDT",
                "quantity": "-0.250",
                "position_side": "SHORT",
                "entry_price": "3000.0",
                "mark_price": "2995",
                "notional": "-748.75",
                "initial_margin": "37.4375",
                "position_initial_margin": "37.4375",
                "open_order_initial_margin": "1.25",
                "maint_margin": "3.74375",
                "unrealized_pnl": "1.25",
                "unrealized_profit": "1.25",
                "liquidation_price": "3998",
                "update_time": 1_787_034_184_520,
            }
        ],
        "regular_orders": [],
        "algo_orders": [],
    }

    assert (
        portfolio_baseline_sha256(original, "SOLUSDT")
        == portfolio_baseline_sha256(refreshed, "SOLUSDT")
    )

    refreshed["positions"][0]["entry_price"] = "3001"

    assert (
        portfolio_baseline_sha256(original, "SOLUSDT")
        != portfolio_baseline_sha256(refreshed, "SOLUSDT")
    )


def test_portfolio_baseline_changes_with_non_target_order_price() -> None:
    heartbeat = {
        "positions": [],
        "regular_orders": [
            {
                "symbol": "ETHUSDT",
                "client_order_id": "eth-order",
                "price": "3000",
            }
        ],
        "algo_orders": [],
    }
    changed = {
        "positions": [],
        "regular_orders": [
            {
                "symbol": "ETHUSDT",
                "client_order_id": "eth-order",
                "price": "3001",
            }
        ],
        "algo_orders": [],
    }

    assert (
        portfolio_baseline_sha256(heartbeat, "SOLUSDT")
        != portfolio_baseline_sha256(changed, "SOLUSDT")
    )


@pytest.mark.parametrize(
    "heartbeat",
    (
        {
            "positions": False,
            "regular_orders": [],
            "algo_orders": [],
        },
        {
            "positions": [],
            "regular_orders": ["invalid-row"],
            "algo_orders": [],
        },
    ),
)
def test_portfolio_baseline_rejects_invalid_snapshot_shape(
    heartbeat: dict,
) -> None:
    with pytest.raises(ValueError):
        portfolio_baseline_sha256(heartbeat, "SOLUSDT")
