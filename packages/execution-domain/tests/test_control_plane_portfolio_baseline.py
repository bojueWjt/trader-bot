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
ROBOT_OPEN_CLIENT_ORDER_ID = "B" + ("a" * 32) + "01"
ROBOT_STOP_CLIENT_ORDER_ID = "B" + ("b" * 32) + "02"


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


def test_portfolio_baseline_canonicalizes_owned_target_evidence() -> None:
    original = {
        "positions": [
            {
                "symbol": "SOLUSDT-PERP",
                "quantity": "1.00",
                "entry_price": "100.00",
                "mark_price": "2400",
                "tags": ["intent_id=owned-position"],
            },
            {
                "symbol": "SOLUSDT",
                "quantity": "0.5",
            },
            {
                "symbol": "XAUUSDT-PERP",
                "quantity": "2",
                "tags": ["intent_id=other-symbol"],
            },
        ],
        "regular_orders": [
            {
                "symbol": "SOLUSDT",
                "client_order_id": ROBOT_OPEN_CLIENT_ORDER_ID,
                "quantity": "0.10",
                "price": "100.00",
            },
            {
                "symbol": "SOLUSDT",
                "client_order_id": "manual-order",
                "price": "99",
            },
            {
                "symbol": "BTCUSDT",
                "client_order_id": ROBOT_STOP_CLIENT_ORDER_ID,
                "price": "60000",
            },
        ],
        "algo_orders": [],
    }
    equivalent = {
        "positions": [
            {
                "quantity": "1",
                "symbol": "solusdt",
                "entry_price": "100",
                "mark_price": "2500",
                "tags": ["intent_id=owned-position"],
            },
            {
                "symbol": "SOLUSDT",
                "quantity": "9",
            },
        ],
        "regular_orders": [
            {
                "price": "101",
                "client_order_id": "manual-order",
                "symbol": "SOLUSDT",
            },
            {
                "price": "100",
                "quantity": "0.1",
                "client_order_id": ROBOT_OPEN_CLIENT_ORDER_ID,
                "symbol": "SOLUSDT",
            },
        ],
        "algo_orders": [],
    }

    assert (
        portfolio_baseline_sha256(original, "SOLUSDT")
        == portfolio_baseline_sha256(equivalent, "SOLUSDT")
    )


def test_portfolio_baseline_matches_exchange_mirror_aliases() -> None:
    node_snapshot = {
        "positions": [
            {
                "symbol": "SOLUSDT",
                "quantity": "-0.250",
                "position_side": "SHORT",
                "entry_price": "3000.0",
                "mark_price": "2999",
                "tags": ["intent_id=owned-position"],
            }
        ],
        "regular_orders": [
            {
                "symbol": "SOLUSDT",
                "position_side": "LONG",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": "0.050",
                "price": "61536.50",
                "stop_price": "0",
                "reduce_only": False,
                "client_order_id": ROBOT_OPEN_CLIENT_ORDER_ID,
                "venue_order_id": "1092374819069",
                "order_kind": "regular",
            }
        ],
        "algo_orders": [
            {
                "symbol": "SOLUSDT",
                "position_side": "SHORT",
                "side": "BUY",
                "order_type": "STOP_MARKET",
                "quantity": "5.0",
                "price": "0.0",
                "stop_price": "1085.0",
                "reduce_only": True,
                "client_order_id": ROBOT_STOP_CLIENT_ORDER_ID,
                "venue_order_id": "1000002511312702",
                "order_kind": "algo",
            }
        ],
    }
    exchange_mirror = {
        "positions": [
            {
                "symbol": "SOLUSDT",
                "position_amt": "-0.250",
                "position_side": "SHORT",
                "entry_price": "3000.0",
                "mark_price": "2998",
                "unrealized_pnl": "0.5",
                "tags": ["intent_id=owned-position"],
            }
        ],
        "open_orders": [
            {
                "symbol": "SOLUSDT",
                "position_side": "LONG",
                "side": "BUY",
                "type": "LIMIT",
                "quantity": "0.050",
                "price": "61536.50",
                "trigger_price": "0",
                "reduce_only": False,
                "client_order_id": ROBOT_OPEN_CLIENT_ORDER_ID,
                "venue_order_id": 1092374819069,
            }
        ],
        "algo_orders": [
            {
                "symbol": "SOLUSDT",
                "position_side": "SHORT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "quantity": "5.0",
                "price": "0.0",
                "trigger_price": "1085.0",
                "reduce_only": True,
                "client_order_id": ROBOT_STOP_CLIENT_ORDER_ID,
                "venue_order_id": 1000002511312702,
            }
        ],
    }

    assert (
        portfolio_baseline_sha256(node_snapshot, "SOLUSDT")
        == portfolio_baseline_sha256(exchange_mirror, "SOLUSDT")
    )


def test_portfolio_baseline_ignores_position_risk_market_refresh() -> None:
    original = {
        "positions": [
            {
                "symbol": "SOLUSDT",
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
                "tags": ["intent_id=owned-position"],
            }
        ],
        "regular_orders": [],
        "algo_orders": [],
    }
    refreshed = {
        "positions": [
            {
                "symbol": "SOLUSDT",
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
                "tags": ["intent_id=owned-position"],
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


def test_portfolio_baseline_changes_with_owned_target_order_price() -> None:
    heartbeat = {
        "positions": [],
        "regular_orders": [
            {
                "symbol": "SOLUSDT",
                "client_order_id": ROBOT_OPEN_CLIENT_ORDER_ID,
                "price": "3000",
            }
        ],
        "algo_orders": [],
    }
    changed = {
        "positions": [],
        "regular_orders": [
            {
                "symbol": "SOLUSDT",
                "client_order_id": ROBOT_OPEN_CLIENT_ORDER_ID,
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
