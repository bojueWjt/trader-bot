from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from order_management.errors import ErrorCategory, classify_execution_error
from strategy.retry_policy import RetryPolicy, RetryPolicyConfig


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_unknown_network_uncertain_does_not_auto_resubmit() -> None:
    classified = classify_execution_error("connection lost after submit", stage="submit_uncertain")
    policy = RetryPolicy(RetryPolicyConfig(max_retries=3, jitter_fraction=Decimal("0")))

    decision = policy.decide(classified, attempt=1, now=NOW)

    assert classified.category == ErrorCategory.UNKNOWN
    assert decision.action == "reconcile"
    assert decision.delay_seconds is None
    assert policy.metrics["unknown"] == 1


def test_retryable_error_uses_exponential_backoff_with_jitter() -> None:
    classified = classify_execution_error("HTTP 503 exchange unavailable", stage="pre_submit")
    policy = RetryPolicy(
        RetryPolicyConfig(
            max_retries=3,
            base_delay_seconds=2,
            max_delay_seconds=30,
            jitter_fraction=Decimal("0.5"),
            random_fraction=lambda: Decimal("1"),
        )
    )

    decision = policy.decide(classified, attempt=2, now=NOW)

    assert classified.category == ErrorCategory.RETRYABLE
    assert decision.action == "retry"
    assert decision.delay_seconds == 6
    assert decision.next_attempt_at.isoformat() == "2026-06-21T12:00:06+00:00"
    assert policy.metrics["retryable"] == 1


def test_permanent_error_is_terminal() -> None:
    classified = classify_execution_error("invalid quantity precision", stage="pre_submit")
    policy = RetryPolicy(RetryPolicyConfig(max_retries=3))

    decision = policy.decide(classified, attempt=0, now=NOW)

    assert classified.category == ErrorCategory.PERMANENT
    assert decision.action == "terminal"

