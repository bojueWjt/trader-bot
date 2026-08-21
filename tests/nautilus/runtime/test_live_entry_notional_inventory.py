from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from risk.config import (  # noqa: E402
    RiskLimitConfig,
    build_live_entry_notional_inventory,
    build_live_risk_engine_kwargs,
)


def test_live_risk_config_builds_serializable_entry_inventory_snapshot() -> None:
    config = RiskLimitConfig(
        max_notional_per_order={
            "*": "10",
            "BTCUSDT-PERP.BINANCE": "100.00",
            "SOLUSDT-PERP.BINANCE": "12",
        },
        max_order_submit_rate="50/00:00:01",
        max_order_modify_rate="1/00:00:01",
    )

    risk_engine_kwargs = build_live_risk_engine_kwargs(config)

    assert build_live_entry_notional_inventory(config) == (
        ("*", "10"),
        ("BTCUSDT-PERP.BINANCE", "100.00"),
        ("SOLUSDT-PERP.BINANCE", "12"),
    )
    assert risk_engine_kwargs["max_notional_per_order"] == {
        "BTCUSDT-PERP.BINANCE": "100.00",
        "SOLUSDT-PERP.BINANCE": "12",
    }
