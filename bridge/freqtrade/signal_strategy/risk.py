from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from freqtrade.signal_strategy.domain import (
    BaseModel,
    Field,
    PriceGeometryResult,
    RiskPolicyResult,
    TradingSignal,
)


class RiskPolicy(BaseModel):
    max_single_trade_risk_pct: float = 1.0
    max_total_open_risk_pct: float = 5.0
    max_daily_realized_loss_pct: float = 3.0
    max_symbol_exposure_pct: float = 15.0
    max_correlated_group_exposure_pct: float = 35.0
    default_leverage: int = 3
    max_leverage: int = 5
    min_liquidation_buffer_pct: float = 3.0
    signal_max_age_minutes: int = 240
    cmp_max_age_minutes: int = 30
    allow_live_without_stop_loss: bool = False
    allow_live_without_take_profit: bool = False
    dry_run_allow_without_take_profit: bool = True


class RiskContext(BaseModel):
    equity: float = 0
    open_risk_pct: float = 0
    proposed_risk_pct: float = 0
    symbol_exposure_pct: float = 0
    correlated_group_exposure_pct: float = 0
    realized_pnl_today: float = 0
    day_start_equity: float = 0
    current_prices: dict[str, float] = Field(default_factory=dict)
    kill_switch_enabled: bool = False
    mode: str = "dry_run"
    manual_approved: bool = False
    allow_sl_only: bool = False
    now: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class PairLock(BaseModel):
    pair: str
    source: str
    expires_at: datetime


class RiskGovernor:
    def __init__(self, policy: RiskPolicy | None = None) -> None:
        if policy:
            self.policy = policy
        else:
            self.policy = RiskPolicy()
        self.risk_state = "normal"
        self.pair_locks: dict[str, PairLock] = {}

    def refresh_daily_loss_state(self, day_start_equity: float, realized_pnl: float) -> str:
        loss_pct = 0.0
        if day_start_equity > 0 and realized_pnl < 0:
            loss_pct = abs(realized_pnl) / day_start_equity * 100

        if loss_pct >= self.policy.max_daily_realized_loss_pct:
            self.risk_state = "blocked_new_entries"
        else:
            self.risk_state = "normal"
        return self.risk_state

    def can_reduce_position(self) -> bool:
        return True

    def allow_reducing_trade(self) -> bool:
        return self.can_reduce_position()

    def lock_pair(self, pair: str, source: str, expires_at: datetime) -> None:
        self.pair_locks[pair] = PairLock(pair=pair, source=source, expires_at=expires_at)

    def validate_price_geometry(
        self,
        side: str,
        entry_price: float,
        stop_loss: float | None,
        take_profit_prices: list[float],
    ) -> PriceGeometryResult:
        if stop_loss is None:
            return PriceGeometryResult(valid=False, reason_codes=["stop_loss_missing"])

        if side == "long":
            return self._validate_long_geometry(entry_price, stop_loss, take_profit_prices)

        if side == "short":
            return self._validate_short_geometry(entry_price, stop_loss, take_profit_prices)

        return PriceGeometryResult(valid=False, reason_codes=["side_invalid"])

    def evaluate_new_signal(
        self,
        signal: TradingSignal,
        context: RiskContext | None = None,
    ) -> RiskPolicyResult:
        if context:
            active_context = context
        else:
            active_context = RiskContext()

        reason_codes: list[str] = []
        details: dict[str, Any] = {}

        self._apply_global_blocks(active_context, reason_codes)
        self._apply_live_safety(signal, active_context, reason_codes)
        self._apply_pair_lock(signal, active_context, reason_codes, details)
        self._apply_risk_limits(active_context, reason_codes, details)
        self._apply_leverage_limits(signal, reason_codes, details)
        self._apply_geometry(signal, active_context, reason_codes)
        self._apply_liquidation_buffer(signal, active_context, reason_codes, details)

        if reason_codes:
            return RiskPolicyResult(
                decision="blocked",
                reason_codes=sorted(set(reason_codes)),
                details=details,
            )

        return RiskPolicyResult(decision="approved", reason_codes=[], details=details)

    def _validate_long_geometry(
        self,
        entry_price: float,
        stop_loss: float,
        take_profit_prices: list[float],
    ) -> PriceGeometryResult:
        valid = stop_loss < entry_price
        valid = valid and all(price > entry_price for price in take_profit_prices)
        valid = valid and take_profit_prices == sorted(take_profit_prices)
        if valid:
            return PriceGeometryResult(valid=True)
        return PriceGeometryResult(valid=False, reason_codes=["long_price_geometry_invalid"])

    def _validate_short_geometry(
        self,
        entry_price: float,
        stop_loss: float,
        take_profit_prices: list[float],
    ) -> PriceGeometryResult:
        valid = stop_loss > entry_price
        valid = valid and all(price < entry_price for price in take_profit_prices)
        valid = valid and take_profit_prices == sorted(take_profit_prices, reverse=True)
        if valid:
            return PriceGeometryResult(valid=True)
        return PriceGeometryResult(valid=False, reason_codes=["short_price_geometry_invalid"])

    def _apply_global_blocks(self, context: RiskContext, reason_codes: list[str]) -> None:
        if context.kill_switch_enabled:
            reason_codes.append("kill_switch_enabled")

        if self.risk_state == "blocked_new_entries":
            reason_codes.append("daily_loss_limit")

    def _apply_live_safety(
        self,
        signal: TradingSignal,
        context: RiskContext,
        reason_codes: list[str],
    ) -> None:
        if context.mode != "live":
            return

        if not self.policy.allow_live_without_stop_loss and signal.stop_loss is None:
            reason_codes.append("live_stop_loss_required")

        if self.policy.allow_live_without_take_profit:
            return
        if context.manual_approved or context.allow_sl_only:
            return
        if signal.take_profits and signal.take_profit_parse_status == "parsed":
            return

        reason_codes.append("live_take_profit_required")

    def _apply_pair_lock(
        self,
        signal: TradingSignal,
        context: RiskContext,
        reason_codes: list[str],
        details: dict[str, Any],
    ) -> None:
        pair_lock = self.pair_locks.get(signal.pair_freqtrade)
        if not pair_lock:
            return

        if pair_lock.expires_at <= context.now:
            del self.pair_locks[signal.pair_freqtrade]
            return

        reason_codes.append("pair_locked")
        details["pair_lock"] = {
            "source": pair_lock.source,
            "expires_at": pair_lock.expires_at.isoformat(),
        }

    def _apply_risk_limits(
        self,
        context: RiskContext,
        reason_codes: list[str],
        details: dict[str, Any],
    ) -> None:
        if context.proposed_risk_pct > self.policy.max_single_trade_risk_pct:
            reason_codes.append("single_trade_risk_limit")

        projected_open_risk = context.open_risk_pct + context.proposed_risk_pct
        details["projected_total_open_risk_pct"] = projected_open_risk
        if projected_open_risk > self.policy.max_total_open_risk_pct:
            reason_codes.append("total_open_risk_limit")

        if context.symbol_exposure_pct > self.policy.max_symbol_exposure_pct:
            reason_codes.append("symbol_exposure_limit")

        if context.correlated_group_exposure_pct > self.policy.max_correlated_group_exposure_pct:
            reason_codes.append("correlated_group_exposure_limit")

    def _apply_leverage_limits(
        self,
        signal: TradingSignal,
        reason_codes: list[str],
        details: dict[str, Any],
    ) -> None:
        if signal.leverage.selected <= self.policy.max_leverage:
            return

        reason_codes.append("max_leverage_exceeded")
        details["max_allowed_leverage"] = self.policy.max_leverage

    def _apply_geometry(
        self,
        signal: TradingSignal,
        context: RiskContext,
        reason_codes: list[str],
    ) -> None:
        entry_price = self._entry_price(signal, context)
        take_profit_prices = [take_profit.price for take_profit in signal.take_profits]
        if entry_price is False:
            return
        if not take_profit_prices:
            return

        geometry = self.validate_price_geometry(
            signal.side,
            entry_price,
            signal.stop_loss,
            take_profit_prices,
        )
        reason_codes.extend(geometry.reason_codes)

    def _apply_liquidation_buffer(
        self,
        signal: TradingSignal,
        context: RiskContext,
        reason_codes: list[str],
        details: dict[str, Any],
    ) -> None:
        entry_price = self._entry_price(signal, context)
        if entry_price is False:
            return
        if signal.stop_loss is None:
            return
        if signal.leverage.selected <= 1:
            return

        liquidation_price = self._estimate_liquidation_price(
            signal.side,
            entry_price,
            signal.leverage.selected,
        )
        buffer_pct = abs(signal.stop_loss - liquidation_price) / entry_price * 100
        details["estimated_liquidation_price"] = liquidation_price
        details["liquidation_buffer_pct"] = buffer_pct
        if buffer_pct >= self.policy.min_liquidation_buffer_pct:
            return

        reason_codes.append("liquidation_buffer_too_small")
        suggested_leverage = self._suggest_leverage(signal.side, entry_price, signal.stop_loss)
        if suggested_leverage:
            details["suggested_leverage"] = suggested_leverage

    def _entry_price(self, signal: TradingSignal, context: RiskContext) -> float | bool:
        if signal.entry.primary_price:
            return signal.entry.primary_price

        current_price = context.current_prices.get(signal.pair_freqtrade)
        if current_price:
            return current_price

        return False

    def _estimate_liquidation_price(self, side: str, entry_price: float, leverage: int) -> float:
        maintenance_offset = 0.015
        if side == "long":
            return entry_price * (1 - (1 / leverage) + maintenance_offset)
        return entry_price * (1 + (1 / leverage) - maintenance_offset)

    def _suggest_leverage(self, side: str, entry_price: float, stop_loss: float) -> int | bool:
        for leverage in range(1, self.policy.max_leverage + 1):
            liquidation_price = self._estimate_liquidation_price(side, entry_price, leverage)
            buffer_pct = abs(stop_loss - liquidation_price) / entry_price * 100
            if buffer_pct >= self.policy.min_liquidation_buffer_pct:
                return leverage
        return False
