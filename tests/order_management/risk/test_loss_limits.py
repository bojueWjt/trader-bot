from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from risk_config import RiskConfig
from loss_limits import LossLimitState, confirm_loss_limit_reset, evaluate_loss_limits


def test_daily_loss_budget_resets_on_configured_utc_day() -> None:
    state = LossLimitState(
        account_id="acct-om3-a",
        mode="ACTIVE",
        day_key="2026-06-20",
        day_start_equity=Decimal("10000"),
        peak_equity=Decimal("10000"),
    )
    now = datetime(2026, 6, 21, 0, 1, tzinfo=timezone.utc)

    decision = evaluate_loss_limits(
        state,
        current_equity=Decimal("9900"),
        now=now,
        config=RiskConfig(daily_loss_limit=Decimal("500")),
        tz=timezone.utc,
    )

    assert decision.mode == "ACTIVE"
    assert decision.daily_loss == Decimal("0")
    assert decision.state.day_key == "2026-06-21"
    assert decision.state.day_start_equity == Decimal("9900")


def test_daily_loss_limit_breach_enters_reducing_with_cooldown() -> None:
    state = LossLimitState(
        account_id="acct-om3-a",
        mode="ACTIVE",
        day_key="2026-06-21",
        day_start_equity=Decimal("10000"),
        peak_equity=Decimal("10000"),
    )
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)

    decision = evaluate_loss_limits(
        state,
        current_equity=Decimal("9400"),
        now=now,
        config=RiskConfig(daily_loss_limit=Decimal("500"), loss_cooldown_seconds=60),
    )

    assert decision.mode == "REDUCING"
    assert decision.daily_loss == Decimal("600")
    assert decision.state.cooldown_until == now + timedelta(seconds=60)


def test_cooldown_end_does_not_auto_reactivate_without_confirm() -> None:
    now = datetime(2026, 6, 21, 12, 2, tzinfo=timezone.utc)
    state = LossLimitState(
        account_id="acct-om3-a",
        mode="REDUCING",
        day_key="2026-06-21",
        day_start_equity=Decimal("10000"),
        peak_equity=Decimal("10000"),
        breached_at=now - timedelta(minutes=2),
        cooldown_until=now - timedelta(minutes=1),
    )

    decision = evaluate_loss_limits(
        state,
        current_equity=Decimal("9700"),
        now=now,
        config=RiskConfig(daily_loss_limit=Decimal("500"), loss_cooldown_seconds=60),
    )

    assert decision.mode == "REDUCING"
    assert decision.requires_confirmation is True

    confirmed = confirm_loss_limit_reset(decision.state, current_equity=Decimal("9700"), now=now)
    assert confirmed.mode == "ACTIVE"
    assert confirmed.day_start_equity == Decimal("9700")


def test_max_drawdown_breach_enters_halt() -> None:
    state = LossLimitState(
        account_id="acct-om3-a",
        mode="ACTIVE",
        day_key="2026-06-21",
        day_start_equity=Decimal("10000"),
        peak_equity=Decimal("12000"),
    )

    decision = evaluate_loss_limits(
        state,
        current_equity=Decimal("10000"),
        now=datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc),
        config=RiskConfig(max_drawdown_pct=Decimal("0.10")),
    )

    assert decision.mode == "HALT"
    assert decision.drawdown_pct == Decimal("0.1666666666666666666666666667")

