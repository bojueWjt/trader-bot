from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: Decimal = Decimal("0.01")
    max_total_open_risk_pct: Decimal = Decimal("0.10")
    max_notional_per_order: Decimal = Decimal("50000")
    max_leverage: Decimal = Decimal("10")
    max_instrument_exposure: Decimal = Decimal("100000")
    max_correlated_exposure: Decimal = Decimal("150000")
    min_free_margin: Decimal = Decimal("0")
    reserve_balance: Decimal = Decimal("0")
    daily_loss_limit: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("0")
    loss_cooldown_seconds: int = 0

    def __post_init__(self) -> None:
        for name in (
            "risk_per_trade_pct",
            "max_total_open_risk_pct",
            "max_notional_per_order",
            "max_leverage",
            "max_instrument_exposure",
            "max_correlated_exposure",
            "min_free_margin",
            "reserve_balance",
            "daily_loss_limit",
            "max_drawdown_pct",
        ):
            object.__setattr__(self, name, decimal_value(getattr(self, name)))
        object.__setattr__(self, "loss_cooldown_seconds", int(self.loss_cooldown_seconds))


def decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))

