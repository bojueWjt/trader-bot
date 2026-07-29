from __future__ import annotations

from datetime import datetime, timedelta, timezone

from order_management.freshness import FreshnessConfig, FreshnessState


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_empty_payload_heartbeat_does_not_advance_data_freshness() -> None:
    state = FreshnessState(config=FreshnessConfig())

    state.record_heartbeat({}, observed_at=NOW)

    assert state.market_data_last_seen_at is None
    assert state.account_data_last_seen_at is None
    assert state.execution_event_last_seen_at is None
    assert state.projection_applied_at is None
    assert state.reconciliation_verified_at is None


def test_any_stale_critical_datum_blocks_new_risk_but_allows_reduce_actions() -> None:
    config = FreshnessConfig(
        market_data_stale_seconds=10,
        account_data_stale_seconds=15,
        execution_event_stale_seconds=30,
        projection_lag_threshold_ms=5000,
        reconciliation_stale_seconds=300,
    )
    old = NOW - timedelta(seconds=301)
    state = FreshnessState(
        config=config,
        market_data_last_seen_at=NOW,
        account_data_last_seen_at=NOW,
        execution_event_last_seen_at=NOW,
        projection_applied_at=NOW,
        reconciliation_verified_at=old,
    )

    assert state.max_new_risk_notional(requested_notional=1000, now=NOW) == 0
    assert state.is_action_allowed("open_position", now=NOW) is False
    assert state.is_action_allowed("partial_close", now=NOW) is True
    assert state.is_action_allowed("cancel", now=NOW) is True
