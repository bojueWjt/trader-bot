from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from account_budget import AccountBudget
from risk_config import RiskConfig
from position_sizing import SizingRequest, fixed_risk_size


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_fixed_risk_sizing_matches_plan_formula_and_floors_to_increment() -> None:
    budget = _budget(equity="10000", free_margin="5000")
    request = SizingRequest(
        account_id="acct-om3-a",
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="long",
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        quantity_step=Decimal("0.01"),
        min_quantity=Decimal("0.01"),
        min_notional=Decimal("10"),
        remaining_daily_budget=Decimal("80"),
        existing_total_open_risk=Decimal("10"),
        remaining_instrument_exposure=Decimal("5000"),
        remaining_correlated_exposure=Decimal("5000"),
    )

    result = fixed_risk_size(
        budget,
        request,
        config=RiskConfig(max_total_open_risk_pct=Decimal("0.10"), max_notional_per_order=Decimal("50000")),
    )

    assert result.status == "approved"
    assert result.risk_amount == Decimal("80")
    assert result.stop_distance_pct == Decimal("0.05")
    assert result.raw_notional == Decimal("1600")
    assert result.allowed_notional == Decimal("1600")
    assert result.quantity == Decimal("16.00")
    assert result.headroom["max_notional_per_order"] == Decimal("50000")


def test_fixed_risk_sizing_uses_smallest_notional_cap() -> None:
    budget = _budget(equity="10000", free_margin="10000")
    request = SizingRequest(
        account_id="acct-om3-a",
        instrument_id="BTCUSDT",
        side="long",
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        quantity_step=Decimal("0.1"),
        min_quantity=Decimal("0.1"),
        min_notional=Decimal("10"),
        remaining_daily_budget=Decimal("1000"),
        existing_total_open_risk=Decimal("0"),
        remaining_instrument_exposure=Decimal("750"),
        remaining_correlated_exposure=Decimal("2000"),
    )

    result = fixed_risk_size(budget, request, config=RiskConfig())

    assert result.status == "approved"
    assert result.risk_amount == Decimal("100.00")
    assert result.raw_notional == Decimal("1000")
    assert result.allowed_notional == Decimal("750")
    assert result.quantity == Decimal("7.5")


def test_fixed_risk_sizing_fails_closed_without_verifiable_stop() -> None:
    budget = _budget(equity="10000", free_margin="5000")
    request = SizingRequest(
        account_id="acct-om3-a",
        instrument_id="BTCUSDT",
        side="long",
        entry_price=Decimal("100"),
        stop_price=None,
    )

    result = fixed_risk_size(budget, request, config=RiskConfig())

    assert result.status == "needs_review"
    assert result.quantity == Decimal("0")
    assert "stop" in result.reason


def test_fixed_risk_sizing_fails_closed_when_quantity_below_exchange_minimum() -> None:
    budget = _budget(equity="100", free_margin="10")
    request = SizingRequest(
        account_id="acct-om3-a",
        instrument_id="BTCUSDT",
        side="long",
        entry_price=Decimal("50000"),
        stop_price=Decimal("49900"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.01"),
        min_notional=Decimal("10"),
        remaining_daily_budget=Decimal("1"),
        existing_total_open_risk=Decimal("0"),
        remaining_instrument_exposure=Decimal("100000"),
        remaining_correlated_exposure=Decimal("100000"),
    )

    result = fixed_risk_size(budget, request, config=RiskConfig())

    assert result.status == "rejected"
    assert result.quantity == Decimal("0")
    assert "minimum" in result.reason


def _budget(*, equity: str, free_margin: str) -> AccountBudget:
    return AccountBudget(
        account_id="acct-om3-a",
        currency="USDT",
        equity=Decimal(equity),
        margin=Decimal("0"),
        free_margin=Decimal(free_margin),
        margin_ratio=Decimal("0"),
        updated_at=NOW,
        stale_reasons=[],
    )

