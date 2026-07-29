from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from order_management.errors import ClassifiedExecutionError, ErrorCategory


@dataclass(frozen=True)
class RetryPolicyConfig:
    max_retries: int = 3
    base_delay_seconds: int = 1
    max_delay_seconds: int = 60
    jitter_fraction: Decimal = Decimal("0.1")
    random_fraction: Callable[[], Decimal] = lambda: Decimal("0")


@dataclass(frozen=True)
class RetryDecision:
    action: str
    reason: str
    delay_seconds: int | None = None
    next_attempt_at: datetime | None = None
    category: ErrorCategory | None = None


@dataclass
class RetryPolicy:
    config: RetryPolicyConfig = field(default_factory=RetryPolicyConfig)
    metrics: dict[str, int] = field(
        default_factory=lambda: {"retryable": 0, "permanent": 0, "unknown": 0, "exhausted": 0}
    )

    def decide(
        self,
        error: ClassifiedExecutionError,
        *,
        attempt: int,
        now: datetime | None = None,
    ) -> RetryDecision:
        now = _aware(now or datetime.now(timezone.utc))
        if error.category == ErrorCategory.UNKNOWN:
            self.metrics["unknown"] += 1
            return RetryDecision("reconcile", error.reason, category=error.category)
        if error.category == ErrorCategory.PERMANENT:
            self.metrics["permanent"] += 1
            return RetryDecision("terminal", error.reason, category=error.category)
        self.metrics["retryable"] += 1
        if attempt >= self.config.max_retries:
            self.metrics["exhausted"] += 1
            return RetryDecision("terminal", "max_retries_exhausted", category=error.category)
        delay = self._delay_seconds(attempt)
        return RetryDecision(
            "retry",
            error.reason,
            delay_seconds=delay,
            next_attempt_at=now + timedelta(seconds=delay),
            category=error.category,
        )

    def _delay_seconds(self, attempt: int) -> int:
        base = Decimal(self.config.base_delay_seconds) * (Decimal("2") ** max(attempt - 1, 0))
        jitter = base * self.config.jitter_fraction * self.config.random_fraction()
        return int(min(base + jitter, Decimal(self.config.max_delay_seconds)))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
