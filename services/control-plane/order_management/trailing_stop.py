from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class AppTrailingConfig:
    callback_rate: Decimal
    min_step: Decimal = Decimal("0")
    update_interval_seconds: int = 10


@dataclass(frozen=True)
class AppTrailingState:
    side: str
    high_watermark: Decimal | None
    low_watermark: Decimal | None
    stop_price: Decimal
    last_updated_at: datetime | None = None


@dataclass(frozen=True)
class TrailingDecision:
    action: str
    reason: str
    stop_price: Decimal
    high_watermark: Decimal | None = None
    low_watermark: Decimal | None = None


class TrailingStopController:
    def __init__(self, config: AppTrailingConfig) -> None:
        self._config = config

    def evaluate(
        self,
        state: AppTrailingState,
        *,
        current_price: Decimal,
        price_stale: bool,
        data_connected: bool,
        now: datetime,
    ) -> TrailingDecision:
        if price_stale or not data_connected:
            return TrailingDecision(
                "blocked",
                "market_data_unavailable",
                state.stop_price,
                state.high_watermark,
                state.low_watermark,
            )

        current = Decimal(str(current_price))
        if _is_long(state.side):
            high = max(state.high_watermark or current, current)
            desired = (high * (Decimal("1") - self._config.callback_rate)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if desired <= state.stop_price:
                return TrailingDecision("hold", "never_loosen", state.stop_price, high, state.low_watermark)
            if desired - state.stop_price < self._config.min_step:
                return TrailingDecision("hold", "min_step_not_reached", state.stop_price, high, state.low_watermark)
            if _rate_limited(state.last_updated_at, now, self._config.update_interval_seconds):
                return TrailingDecision("wait", "rate_limited", state.stop_price, high, state.low_watermark)
            return TrailingDecision("update", "trail_advanced", desired, high, state.low_watermark)

        low = min(state.low_watermark or current, current)
        desired = (low * (Decimal("1") + self._config.callback_rate)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if desired >= state.stop_price:
            return TrailingDecision("hold", "never_loosen", state.stop_price, state.high_watermark, low)
        if state.stop_price - desired < self._config.min_step:
            return TrailingDecision("hold", "min_step_not_reached", state.stop_price, state.high_watermark, low)
        if _rate_limited(state.last_updated_at, now, self._config.update_interval_seconds):
            return TrailingDecision("wait", "rate_limited", state.stop_price, state.high_watermark, low)
        return TrailingDecision("update", "trail_advanced", desired, state.high_watermark, low)


def _rate_limited(last_updated_at: datetime | None, now: datetime, seconds: int) -> bool:
    if last_updated_at is None:
        return False
    last = last_updated_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return (current.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() < seconds


def _is_long(side: str) -> bool:
    return str(side).lower() in {"long", "buy"}

