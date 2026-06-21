from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .state_descriptor import halted_action_allowed, opening_actions


@dataclass(frozen=True)
class FreshnessConfig:
    market_data_stale_seconds: int = 10
    account_data_stale_seconds: int = 15
    execution_event_stale_seconds: int = 30
    projection_lag_threshold_ms: int = 5000
    reconciliation_stale_seconds: int = 300
    price_deviation_bps: int = 200
    evaluation_interval_seconds: int = 5
    protection_watchdog_interval_seconds: int = 10
    disconnect_action: str = "halt"


@dataclass
class FreshnessState:
    config: FreshnessConfig
    market_data_last_seen_at: datetime | None = None
    account_data_last_seen_at: datetime | None = None
    execution_event_last_seen_at: datetime | None = None
    projection_applied_at: datetime | None = None
    reconciliation_verified_at: datetime | None = None

    def record_heartbeat(self, payload: dict | None, *, observed_at: datetime) -> None:
        payload = payload or {}
        if payload.get("market_data"):
            self.market_data_last_seen_at = _aware(observed_at)
        if payload.get("account_data"):
            self.account_data_last_seen_at = _aware(observed_at)
        if payload.get("execution_event"):
            self.execution_event_last_seen_at = _aware(observed_at)
        if payload.get("projection_applied"):
            self.projection_applied_at = _aware(observed_at)
        if payload.get("reconciliation_verified"):
            self.reconciliation_verified_at = _aware(observed_at)

    def record_market_data(self, *, observed_at: datetime, payload: dict | None = None) -> None:
        if payload is None or payload:
            self.market_data_last_seen_at = _aware(observed_at)

    def record_account_data(self, *, observed_at: datetime, payload: dict | None = None) -> None:
        if payload is None or payload:
            self.account_data_last_seen_at = _aware(observed_at)

    def record_execution_event(self, *, observed_at: datetime, payload: dict | None = None) -> None:
        if payload is None or payload:
            self.execution_event_last_seen_at = _aware(observed_at)

    def record_projection_applied(self, *, observed_at: datetime, payload: dict | None = None) -> None:
        if payload is None or payload:
            self.projection_applied_at = _aware(observed_at)

    def record_reconciliation_verified(self, *, observed_at: datetime, payload: dict | None = None) -> None:
        if payload is None or payload:
            self.reconciliation_verified_at = _aware(observed_at)

    def stale_reasons(self, *, now: datetime) -> list[str]:
        now = _aware(now)
        checks = (
            ("market_data", self.market_data_last_seen_at, self.config.market_data_stale_seconds),
            ("account_data", self.account_data_last_seen_at, self.config.account_data_stale_seconds),
            ("execution_event", self.execution_event_last_seen_at, self.config.execution_event_stale_seconds),
            ("reconciliation", self.reconciliation_verified_at, self.config.reconciliation_stale_seconds),
        )
        stale: list[str] = []
        for name, timestamp, seconds in checks:
            if timestamp is None or (now - _aware(timestamp)).total_seconds() > seconds:
                stale.append(name)
        if self.projection_applied_at is None:
            stale.append("projection")
        else:
            lag_ms = (now - _aware(self.projection_applied_at)).total_seconds() * 1000
            if lag_ms > self.config.projection_lag_threshold_ms:
                stale.append("projection")
        return stale

    def is_stale(self, *, now: datetime) -> bool:
        return bool(self.stale_reasons(now=now))

    def max_new_risk_notional(self, *, requested_notional: int | float, now: datetime) -> int | float:
        if self.is_stale(now=now):
            return 0
        return requested_notional

    def is_action_allowed(self, action: str, *, now: datetime, mode: str = "ACTIVE") -> bool:
        if self.is_stale(now=now):
            return halted_action_allowed(action, "HALTED")
        if action in opening_actions():
            return mode.upper() == "ACTIVE"
        return halted_action_allowed(action, mode)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
