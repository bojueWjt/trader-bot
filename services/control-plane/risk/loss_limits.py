from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from risk_config import RiskConfig, decimal_value


@dataclass(frozen=True)
class LossLimitState:
    account_id: str
    mode: str
    day_key: str
    day_start_equity: Decimal
    peak_equity: Decimal
    breached_at: datetime | None = None
    cooldown_until: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "day_start_equity", decimal_value(self.day_start_equity))
        object.__setattr__(self, "peak_equity", decimal_value(self.peak_equity))
        object.__setattr__(self, "mode", self.mode.upper())


@dataclass(frozen=True)
class LossLimitDecision:
    mode: str
    reason: str
    daily_loss: Decimal
    drawdown_pct: Decimal
    requires_confirmation: bool
    state: LossLimitState


def evaluate_loss_limits(
    state: LossLimitState,
    *,
    current_equity: Decimal,
    now: datetime,
    config: RiskConfig | None = None,
    tz: timezone = timezone.utc,
) -> LossLimitDecision:
    config = config or RiskConfig()
    current_equity = decimal_value(current_equity)
    now = _aware(now)
    day_key = _day_key(now, tz)
    if state.day_key != day_key:
        state = LossLimitState(
            account_id=state.account_id,
            mode=state.mode,
            day_key=day_key,
            day_start_equity=current_equity,
            peak_equity=max(current_equity, state.peak_equity),
            breached_at=state.breached_at,
            cooldown_until=state.cooldown_until,
        )

    peak = max(state.peak_equity, current_equity)
    daily_loss = max(state.day_start_equity - current_equity, Decimal("0"))
    drawdown_pct = ((peak - current_equity) / peak) if peak > 0 else Decimal("0")

    if config.max_drawdown_pct > 0 and drawdown_pct >= config.max_drawdown_pct:
        breached = replace(
            state,
            mode="HALT",
            peak_equity=peak,
            breached_at=state.breached_at or now,
            cooldown_until=_cooldown_until(now, config),
        )
        return LossLimitDecision("HALT", "max drawdown breached", daily_loss, drawdown_pct, False, breached)

    if config.daily_loss_limit > 0 and daily_loss >= config.daily_loss_limit:
        breached = replace(
            state,
            mode="REDUCING",
            peak_equity=peak,
            breached_at=state.breached_at or now,
            cooldown_until=_cooldown_until(now, config),
        )
        return LossLimitDecision("REDUCING", "daily loss limit breached", daily_loss, drawdown_pct, False, breached)

    requires_confirmation = (
        state.mode in {"REDUCING", "HALT", "HALTED"}
        and state.cooldown_until is not None
        and now >= _aware(state.cooldown_until)
    )
    mode = state.mode if state.mode in {"REDUCING", "HALT", "HALTED"} else "ACTIVE"
    next_state = replace(state, mode=mode, peak_equity=peak)
    reason = "cooldown ended; explicit confirm required" if requires_confirmation else "within loss limits"
    return LossLimitDecision(mode, reason, daily_loss, drawdown_pct, requires_confirmation, next_state)


def confirm_loss_limit_reset(
    state: LossLimitState,
    *,
    current_equity: Decimal,
    now: datetime,
    tz: timezone = timezone.utc,
) -> LossLimitState:
    current_equity = decimal_value(current_equity)
    return LossLimitState(
        account_id=state.account_id,
        mode="ACTIVE",
        day_key=_day_key(_aware(now), tz),
        day_start_equity=current_equity,
        peak_equity=current_equity,
    )


def _cooldown_until(now: datetime, config: RiskConfig) -> datetime | None:
    if config.loss_cooldown_seconds <= 0:
        return None
    return now + timedelta(seconds=config.loss_cooldown_seconds)


def _day_key(now: datetime, tz: timezone) -> str:
    return now.astimezone(tz).date().isoformat()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
