from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from account_budget import AccountBudget
from risk_config import RiskConfig, decimal_value


@dataclass(frozen=True)
class SizingRequest:
    account_id: str
    instrument_id: str
    side: str
    entry_price: Decimal
    stop_price: Decimal | None = None
    quantity_step: Decimal = Decimal("1")
    min_quantity: Decimal = Decimal("0")
    min_notional: Decimal = Decimal("0")
    remaining_daily_budget: Decimal | None = None
    existing_total_open_risk: Decimal = Decimal("0")
    remaining_instrument_exposure: Decimal | None = None
    remaining_correlated_exposure: Decimal | None = None
    leverage: Decimal | None = None

    def __post_init__(self) -> None:
        for name in (
            "entry_price",
            "stop_price",
            "quantity_step",
            "min_quantity",
            "min_notional",
            "remaining_daily_budget",
            "existing_total_open_risk",
            "remaining_instrument_exposure",
            "remaining_correlated_exposure",
            "leverage",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, decimal_value(value))


@dataclass(frozen=True)
class SizingResult:
    status: str
    reason: str
    quantity: Decimal
    notional: Decimal
    risk_amount: Decimal
    stop_distance_pct: Decimal
    raw_notional: Decimal
    allowed_notional: Decimal
    headroom: dict[str, Decimal] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)


def fixed_risk_size(
    budget: AccountBudget,
    request: SizingRequest,
    *,
    config: RiskConfig | None = None,
) -> SizingResult:
    config = config or RiskConfig()
    checks: list[dict[str, Any]] = []

    if not budget.can_take_new_risk:
        return _blocked("needs_review", "account budget stale or unavailable", checks)

    entry = request.entry_price
    stop = request.stop_price
    if entry <= 0 or stop is None or stop <= 0 or stop == entry:
        return _blocked("needs_review", "no verifiable stop distance", checks)
    stop_distance_pct = abs(entry - stop) / entry
    checks.append({"name": "stop_distance", "passed": True, "value": stop_distance_pct})

    per_trade = budget.equity * config.risk_per_trade_pct
    remaining_daily = request.remaining_daily_budget if request.remaining_daily_budget is not None else per_trade
    total_budget = budget.equity * config.max_total_open_risk_pct
    remaining_total = max(total_budget - request.existing_total_open_risk, Decimal("0"))
    risk_amount = min(per_trade, remaining_daily, remaining_total)
    if risk_amount <= 0:
        return _blocked("rejected", "no remaining risk budget", checks)

    raw_notional = risk_amount / stop_distance_pct
    remaining_instrument = (
        request.remaining_instrument_exposure
        if request.remaining_instrument_exposure is not None
        else config.max_instrument_exposure
    )
    remaining_correlated = (
        request.remaining_correlated_exposure
        if request.remaining_correlated_exposure is not None
        else config.max_correlated_exposure
    )
    leverage = request.leverage if request.leverage is not None else config.max_leverage
    available_margin = max(budget.free_margin - config.min_free_margin - config.reserve_balance, Decimal("0"))
    available_margin_adjusted = available_margin * leverage
    headroom = {
        "risk_per_trade": per_trade,
        "daily": remaining_daily,
        "total": remaining_total,
        "raw_notional": raw_notional,
        "max_notional_per_order": config.max_notional_per_order,
        "instrument": remaining_instrument,
        "correlated": remaining_correlated,
        "available_margin_adjusted": available_margin_adjusted,
    }
    allowed_notional = min(
        raw_notional,
        config.max_notional_per_order,
        remaining_instrument,
        remaining_correlated,
        available_margin_adjusted,
    )
    quantity = floor_to_increment(allowed_notional / entry, request.quantity_step)
    notional = quantity * entry
    if quantity < request.min_quantity or notional < request.min_notional:
        return SizingResult(
            status="rejected",
            reason="quantity below exchange minimum",
            quantity=Decimal("0"),
            notional=Decimal("0"),
            risk_amount=risk_amount,
            stop_distance_pct=stop_distance_pct,
            raw_notional=raw_notional,
            allowed_notional=allowed_notional,
            headroom=headroom,
            checks=checks + [{"name": "exchange_minimum", "passed": False}],
        )

    return SizingResult(
        status="approved",
        reason="fixed risk size approved",
        quantity=quantity,
        notional=notional,
        risk_amount=risk_amount,
        stop_distance_pct=stop_distance_pct,
        raw_notional=raw_notional,
        allowed_notional=allowed_notional,
        headroom=headroom,
        checks=checks,
    )


def floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    units = (value / increment).to_integral_value(rounding=ROUND_FLOOR)
    return units * increment


def _blocked(status: str, reason: str, checks: list[dict[str, Any]]) -> SizingResult:
    return SizingResult(
        status=status,
        reason=reason,
        quantity=Decimal("0"),
        notional=Decimal("0"),
        risk_amount=Decimal("0"),
        stop_distance_pct=Decimal("0"),
        raw_notional=Decimal("0"),
        allowed_notional=Decimal("0"),
        checks=checks,
    )

