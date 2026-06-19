from __future__ import annotations

from typing import Any

from app.contracts.dashboard import (
    SystemSnapshotEvent,
    SystemSnapshotPosition,
    SystemSnapshotResponse,
    SystemSnapshotSignal,
)
from app.services.dashboard_fake_adapter import DashboardFakeAdapter


class SystemSnapshotAdapter:
    def __init__(self, dashboard_adapter: DashboardFakeAdapter | None = None):
        self.dashboard_adapter = dashboard_adapter or DashboardFakeAdapter()

    def snapshot(self) -> SystemSnapshotResponse:
        overview = self.dashboard_adapter.overview()
        open_trades = self.dashboard_adapter.open_trades()["open_trades"]
        events = self.dashboard_adapter.events()["events"]

        equity = float(overview["equity"])
        margin_used = float(overview["margin_used"])
        margin_free = float(overview["free_balance"])
        realized_pnl_today = float(overview["realized_pnl_today"])

        positions = [_position_from_trade(trade) for trade in open_trades]
        signals = [_signal_from_trade(trade) for trade in open_trades]

        return {
            "account": {
                "equity": equity,
                "balance": equity,
                "margin_used": margin_used,
                "margin_free": margin_free,
            },
            "positions": positions,
            "signals": signals,
            "risk": {
                "daily_loss": max(0.0, -realized_pnl_today),
                "drawdown": 0.0,
                "limits": {
                    "max_daily_loss": 0.0,
                    "max_drawdown": 0.0,
                    "max_margin_used": 0.0,
                },
                "current_usage": {
                    "margin_used": margin_used,
                    "margin_used_pct": _ratio(margin_used, equity),
                    "open_trade_count": int(overview["open_trade_count"]),
                    "open_order_count": int(overview["open_order_count"]),
                    "risk_state": overview["risk_state"],
                },
            },
            "events": [_event_from_dashboard_event(event) for event in events],
        }


def _position_from_trade(trade: dict[str, Any]) -> SystemSnapshotPosition:
    return {
        "symbol": trade["pair"],
        "side": trade["side"],
        "size": float(trade["stake_amount"]),
        "entry_price": float(trade["entry_price"]),
        "unrealized_pnl": float(trade["unrealized_pnl"]),
        "trade_id": trade["trade_id"],
        "signal_id": trade["signal_id"],
        "current_price": float(trade["current_price"]),
        "leverage": float(trade["leverage"]),
        "opened_at": trade["opened_at"],
        "status": trade["status"],
    }


def _signal_from_trade(trade: dict[str, Any]) -> SystemSnapshotSignal:
    return {
        "id": trade["signal_id"],
        "symbol": trade["pair"],
        "direction": trade["side"],
        "status": "entered" if trade["status"] == "open" else trade["status"],
        "created_at": trade["opened_at"],
        "source_trade_id": trade["trade_id"],
    }


def _event_from_dashboard_event(event: dict[str, Any]) -> SystemSnapshotEvent:
    return {
        "type": event["event_type"],
        "message": event["message"],
        "ts": event["occurred_at"],
    }


def _ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator
