from __future__ import annotations

from typing import Any, Literal, TypedDict


DashboardSeverity = Literal["info", "warning", "critical"]
DashboardSide = Literal["long", "short"]
DashboardTradeStatus = Literal["open"]


class DashboardEvent(TypedDict):
    event_id: str
    event_type: str
    occurred_at: str
    severity: DashboardSeverity
    message: str
    correlation_id: str
    source: str


class DashboardOverview(TypedDict):
    bot_status: str
    run_mode: str
    equity: float
    free_balance: float
    margin_used: float
    realized_pnl_today: float
    unrealized_pnl: float
    open_trade_count: int
    open_order_count: int
    today_signal_count: int
    today_executed_count: int
    risk_state: str
    recent_events: list[DashboardEvent]


class DashboardOpenTrade(TypedDict):
    trade_id: str
    signal_id: str
    pair: str
    side: DashboardSide
    entry_price: float
    current_price: float
    stake_amount: float
    leverage: float
    unrealized_pnl: float
    opened_at: str
    status: DashboardTradeStatus


class DashboardOpenTradesResponse(TypedDict):
    open_trades: list[DashboardOpenTrade]


class DashboardEventsResponse(TypedDict):
    events: list[DashboardEvent]


class SystemSnapshotAccount(TypedDict):
    equity: float
    balance: float
    margin_used: float
    margin_free: float


class SystemSnapshotPosition(TypedDict):
    symbol: str
    side: DashboardSide
    size: float
    entry_price: float
    unrealized_pnl: float
    trade_id: str
    signal_id: str
    current_price: float
    leverage: float
    opened_at: str
    status: str


class SystemSnapshotSignal(TypedDict):
    id: str
    symbol: str
    direction: DashboardSide
    status: str
    created_at: str
    source_trade_id: str


class SystemSnapshotRisk(TypedDict):
    daily_loss: float
    drawdown: float
    limits: dict[str, Any]
    current_usage: dict[str, Any]


class SystemSnapshotEvent(TypedDict):
    type: str
    message: str
    ts: str


class SystemSnapshotResponse(TypedDict):
    account: SystemSnapshotAccount
    positions: list[SystemSnapshotPosition]
    signals: list[SystemSnapshotSignal]
    risk: SystemSnapshotRisk
    events: list[SystemSnapshotEvent]
