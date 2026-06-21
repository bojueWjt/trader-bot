from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from account_budget import AccountBudget
from order_management.freshness import FreshnessConfig, FreshnessState
from position_sizing import SizingRequest, fixed_risk_size
from risk_config import RiskConfig
from settings.service import SettingsService, SettingsServiceError, apply_patch_to_settings


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_settings_invalid_patch_reports_all_structural_errors_without_db_access() -> None:
    service = SettingsService(_ConnectionThatMustNotBeUsed())

    with pytest.raises(SettingsServiceError) as exc:
        service.patch_settings(
            scope="global",
            scope_key=None,
            patch={
                "unknown": {"field": 1},
                "entry": {
                    "max_slippage_bps": -1,
                    "unknown_field": 1,
                },
                "general": "not-an-object",
            },
            expected_version=0,
            reason="invalid patch",
            request_id="req-settings-invalid",
            actor={"actor_id": "risk-admin", "role": "risk_admin"},
        )

    assert exc.value.code == "validation_failed"
    assert {error["path"] for error in exc.value.errors} == {
        "unknown",
        "entry.max_slippage_bps",
        "entry.unknown_field",
        "general",
    }


def test_settings_patch_merge_deletes_empty_categories_and_preserves_unmentioned_fields() -> None:
    merged = apply_patch_to_settings(
        {
            "entry": {"max_slippage_bps": 25, "entry_timeout_seconds": 30},
            "general": {"order_manager_enabled": True},
        },
        {
            "entry": {"max_slippage_bps": None},
            "general": {"order_manager_enabled": None},
        },
    )

    assert merged == {"entry": {"entry_timeout_seconds": 30}}


def test_settings_version_conflict_is_fail_closed_before_insert() -> None:
    service = SettingsService(_SettingsConflictConnection(version=2))

    with pytest.raises(SettingsServiceError) as exc:
        service.patch_settings(
            scope="account",
            scope_key="acct-om8-unit",
            patch={"entry": {"max_slippage_bps": 10}},
            expected_version=1,
            reason="stale patch",
            request_id="req-settings-conflict",
            actor={"actor_id": "risk-admin", "role": "risk_admin"},
        )

    assert exc.value.code == "version_conflict"
    assert exc.value.status_code == 409
    assert service.conn.insert_attempts == 0


@pytest.mark.parametrize(
    "entry, stop, reason",
    [
        (Decimal("100"), Decimal("100"), "no verifiable stop distance"),
        (Decimal("100"), Decimal("-1"), "no verifiable stop distance"),
    ],
)
def test_position_sizing_fails_closed_for_zero_or_negative_stop_distance(
    entry: Decimal,
    stop: Decimal,
    reason: str,
) -> None:
    result = fixed_risk_size(
        _budget(equity="10000", free_margin="5000"),
        SizingRequest(
            account_id="acct-om8-unit",
            instrument_id="BTCUSDT",
            side="long",
            entry_price=entry,
            stop_price=stop,
        ),
        config=RiskConfig(),
    )

    assert result.status == "needs_review"
    assert result.reason == reason
    assert result.quantity == Decimal("0")
    assert result.notional == Decimal("0")


def test_position_sizing_fails_closed_when_account_has_no_equity() -> None:
    result = fixed_risk_size(
        _budget(equity="0", free_margin="0"),
        SizingRequest(
            account_id="acct-om8-unit",
            instrument_id="BTCUSDT",
            side="long",
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        ),
        config=RiskConfig(),
    )

    assert result.status == "needs_review"
    assert result.reason == "account budget stale or unavailable"
    assert result.quantity == Decimal("0")


def test_freshness_empty_heartbeat_does_not_mark_any_channel_fresh() -> None:
    state = FreshnessState(config=FreshnessConfig())

    state.record_heartbeat({}, observed_at=NOW)
    state.record_heartbeat(None, observed_at=NOW)

    assert state.market_data_last_seen_at is None
    assert state.account_data_last_seen_at is None
    assert state.execution_event_last_seen_at is None
    assert state.projection_applied_at is None
    assert state.reconciliation_verified_at is None
    assert set(state.stale_reasons(now=NOW)) == {
        "market_data",
        "account_data",
        "execution_event",
        "projection",
        "reconciliation",
    }


def _budget(*, equity: str, free_margin: str) -> AccountBudget:
    return AccountBudget(
        account_id="acct-om8-unit",
        currency="USDT",
        equity=Decimal(equity),
        margin=Decimal("0"),
        free_margin=Decimal(free_margin),
        margin_ratio=Decimal("0"),
        updated_at=NOW,
        stale_reasons=[],
    )


class _ConnectionThatMustNotBeUsed:
    insert_attempts = 0

    def cursor(self, *args: Any, **kwargs: Any):
        raise AssertionError("invalid settings patch should fail before DB access")


class _SettingsConflictConnection:
    def __init__(self, *, version: int) -> None:
        self.version = version
        self.insert_attempts = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *args: Any, **kwargs: Any) -> "_SettingsConflictCursor":
        return _SettingsConflictCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _SettingsConflictCursor:
    def __init__(self, conn: _SettingsConflictConnection) -> None:
        self.conn = conn
        self._row: dict[str, Any] | None = None

    def __enter__(self) -> "_SettingsConflictCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.lower().split())
        if "from order_management_settings" in normalized:
            scope, scope_key = params or ("", "")
            self._row = {
                "scope": scope,
                "scope_key": scope_key,
                "version": self.conn.version,
                "settings": {"entry": {"max_slippage_bps": 5}},
                "created_by": "seed",
                "reason": "seed",
                "request_id": "req-seed",
                "created_at": NOW,
            }
            return
        if normalized.startswith("insert"):
            self.conn.insert_attempts += 1
            raise AssertionError("version conflict should happen before insert")

    def fetchone(self):
        return self._row
