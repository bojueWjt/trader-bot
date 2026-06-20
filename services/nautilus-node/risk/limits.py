from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

from .config import RiskLimitConfig


@dataclass(frozen=True)
class InstrumentPrecision:
    price_increment: str
    quantity_increment: str


@dataclass(frozen=True)
class LimitOrderRequest:
    instrument_id: str
    quantity: str
    price: str
    ts: datetime


@dataclass(frozen=True)
class RiskLimitDecision:
    allowed: bool
    reason: str = ""


class TradingStateOrderAction(str, Enum):
    SUBMIT = "submit"
    MODIFY = "modify"
    CANCEL = "cancel"


@dataclass(frozen=True)
class TradingStateOrderRequest:
    action: TradingStateOrderAction
    reduce_only: bool = False


class TradingStateOrderGate:
    """Pure trading-state gate used before orders reach Nautilus RiskEngine.

    Nautilus RiskEngine remains authoritative for order limits. This gate mirrors
    the node lifecycle rule that HALTED blocks new risk while still allowing
    cancels, and REDUCING only permits reduce-only order flow.

    TODO(host-verify): map these project states to the exact Nautilus
    ``TradingState`` enum/value in the hk container once Nautilus is installed.
    """

    def check(
        self,
        trading_state: Any,
        request: TradingStateOrderRequest,
    ) -> RiskLimitDecision:
        state = _state_value(trading_state)
        if request.action is TradingStateOrderAction.CANCEL:
            return RiskLimitDecision(True)
        if state == "ACTIVE":
            return RiskLimitDecision(True)
        if state == "HALTED":
            return RiskLimitDecision(False, "trading_halted")
        if state == "REDUCING":
            if request.reduce_only:
                return RiskLimitDecision(True)
            return RiskLimitDecision(False, "trading_reducing")
        return RiskLimitDecision(False, "unknown_trading_state")


class RiskLimitMirror:
    """Deterministic mirror for local tests; it is not a RiskEngine replacement.

    Runtime order submission must still go through Nautilus RiskEngine with
    ``bypass=False``. This mirror gives host-independent unit tests for the same
    configured limits while hk verifies Nautilus integration.
    """

    def __init__(self, config: RiskLimitConfig) -> None:
        self._config = config
        self._submit_rate = _parse_rate(config.max_order_submit_rate)
        self._modify_rate = _parse_rate(config.max_order_modify_rate)
        self._submit_timestamps: list[datetime] = []
        self._modify_timestamps: list[datetime] = []

    def check_submit(self, request: LimitOrderRequest) -> RiskLimitDecision:
        notional_decision = self._check_notional(request)
        if not notional_decision.allowed:
            return notional_decision
        precision_decision = self._check_precision(request)
        if not precision_decision.allowed:
            return precision_decision
        return self._check_rate(self._submit_timestamps, self._submit_rate, request.ts, "submit_rate_exceeded")

    def check_modify(self, request: LimitOrderRequest) -> RiskLimitDecision:
        precision_decision = self._check_precision(request)
        if not precision_decision.allowed:
            return precision_decision
        return self._check_rate(self._modify_timestamps, self._modify_rate, request.ts, "modify_rate_exceeded")

    def _check_notional(self, request: LimitOrderRequest) -> RiskLimitDecision:
        limit_raw = self._config.max_notional_per_order.get(request.instrument_id)
        if limit_raw is None:
            return RiskLimitDecision(False, "max_notional_missing")
        quantity = _decimal(request.quantity, "quantity")
        price = _decimal(request.price, "price")
        if quantity <= 0 or price <= 0:
            return RiskLimitDecision(False, "non_positive_order")
        notional = quantity * price
        if notional > _decimal(str(limit_raw), "max_notional"):
            return RiskLimitDecision(False, "max_notional_exceeded")
        return RiskLimitDecision(True)

    def _check_precision(self, request: LimitOrderRequest) -> RiskLimitDecision:
        precision = self._config.instrument_precision.get(request.instrument_id)
        if precision is None:
            return RiskLimitDecision(True)
        price_increment = _decimal(
            str(_precision_value(precision, "price_increment")),
            "price_increment",
        )
        quantity_increment = _decimal(
            str(_precision_value(precision, "quantity_increment")),
            "quantity_increment",
        )
        if not _is_multiple(_decimal(request.price, "price"), price_increment):
            return RiskLimitDecision(False, "instrument_precision")
        if not _is_multiple(_decimal(request.quantity, "quantity"), quantity_increment):
            return RiskLimitDecision(False, "instrument_precision")
        return RiskLimitDecision(True)

    def _check_rate(
        self,
        timestamps: list[datetime],
        rate: tuple[int, timedelta],
        ts: datetime,
        reason: str,
    ) -> RiskLimitDecision:
        count, window = rate
        cutoff = ts - window
        while timestamps and timestamps[0] <= cutoff:
            del timestamps[0]
        if len(timestamps) >= count:
            return RiskLimitDecision(False, reason)
        timestamps.append(ts)
        return RiskLimitDecision(True)


def _parse_rate(value: str) -> tuple[int, timedelta]:
    count_raw, interval = value.split("/", 1)
    hours, minutes, seconds = (int(part) for part in interval.split(":", 2))
    return int(count_raw), timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _decimal(value: str, label: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be decimal") from exc


def _is_multiple(value: Decimal, increment: Decimal) -> bool:
    if increment <= 0:
        raise ValueError("increment must be positive")
    return value.remainder_near(increment) == 0


def _precision_value(precision: Any, field_name: str) -> Any:
    if isinstance(precision, Mapping):
        return precision[field_name]
    return getattr(precision, field_name)


def _state_value(trading_state: Any) -> str:
    return str(getattr(trading_state, "value", trading_state))
