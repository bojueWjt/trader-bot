from __future__ import annotations

from typing import Literal, TypedDict


RiskState = Literal[
    "normal",
    "warning",
    "blocked_new_entries",
    "kill_switch_enabled",
    "live_readonly",
]


class RiskDecision(TypedDict):
    decision: str
    risk_state: RiskState
    reason_codes: list[str]
    single_trade_risk_usage_pct: float
    total_open_risk_usage_pct: float
    daily_loss_usage_pct: float
