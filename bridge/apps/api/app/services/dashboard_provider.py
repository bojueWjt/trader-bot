"""Dashboard data provider selection.

Production must never serve fixtures: outside APP_ENV=test the dashboard returns a
real empty state with an explicit `unavailable` risk_state until it is wired to the
real PostgreSQL projection (control-plane SystemSnapshotV1 API). The fixture-backed
DashboardFakeAdapter is constructable only under APP_ENV=test.
"""

from __future__ import annotations

import os
from typing import Any

from app.services.dashboard_fake_adapter import DashboardFakeAdapter


class EmptyDashboardAdapter:
    """Real empty state — no fixtures, no fabricated numbers."""

    def overview(self) -> dict[str, Any]:
        return {
            "equity": 0.0,
            "margin_used": 0.0,
            "free_balance": 0.0,
            "realized_pnl_today": 0.0,
            "open_trade_count": 0,
            "open_order_count": 0,
            "risk_state": "unavailable",
        }

    def open_trades(self) -> dict[str, Any]:
        return {"open_trades": []}

    def events(self) -> dict[str, Any]:
        return {"events": []}


def dashboard_adapter():
    if os.environ.get("APP_ENV") == "test":
        return DashboardFakeAdapter()
    return EmptyDashboardAdapter()
