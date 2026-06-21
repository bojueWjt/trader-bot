from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from order_management.trailing_stop import AppTrailingConfig, AppTrailingState, TrailingStopController
from strategy.protection import PositionProtectionSnapshot
from strategy.trailing_stop import TrailingStopSpec, build_trailing_stop_order


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_exchange_native_trailing_stop_is_preferred_when_supported() -> None:
    order = build_trailing_stop_order(
        intent_id="55555555-5555-4555-8555-555555555555",
        account_id="acct-om5-trail",
        instrument_id="BTCUSDT-PERP.BINANCE",
        position=PositionProtectionSnapshot(
            position_key="acct-om5-trail:BTCUSDT",
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
        ),
        spec=TrailingStopSpec(callback_rate=Decimal("0.3"), activation_price=Decimal("105")),
        exchange_supports_native=True,
    )

    assert order.native is True
    assert order.order_type == "TRAILING_STOP_MARKET"
    assert order.callback_rate == Decimal("0.3")
    assert "lifecycle_role=stop_loss" in order.tags


def test_app_side_trailing_rate_limits_updates_and_never_loosens() -> None:
    controller = TrailingStopController(
        AppTrailingConfig(callback_rate=Decimal("0.05"), min_step=Decimal("1"), update_interval_seconds=10)
    )
    state = AppTrailingState(
        side="long",
        high_watermark=Decimal("110"),
        low_watermark=None,
        stop_price=Decimal("104"),
        last_updated_at=NOW - timedelta(seconds=5),
    )

    rate_limited = controller.evaluate(
        state,
        current_price=Decimal("120"),
        price_stale=False,
        data_connected=True,
        now=NOW,
    )
    updated = controller.evaluate(
        state,
        current_price=Decimal("120"),
        price_stale=False,
        data_connected=True,
        now=NOW + timedelta(seconds=11),
    )
    loosen_attempt = controller.evaluate(
        AppTrailingState(
            side="long",
            high_watermark=Decimal("120"),
            low_watermark=None,
            stop_price=Decimal("114"),
            last_updated_at=NOW - timedelta(seconds=20),
        ),
        current_price=Decimal("112"),
        price_stale=False,
        data_connected=True,
        now=NOW + timedelta(seconds=21),
    )

    assert rate_limited.action == "wait"
    assert rate_limited.reason == "rate_limited"
    assert updated.action == "update"
    assert updated.stop_price == Decimal("114.00")
    assert loosen_attempt.action == "hold"
    assert loosen_attempt.reason == "never_loosen"
    assert loosen_attempt.stop_price == Decimal("114")


def test_app_side_trailing_disconnect_is_fail_safe_and_does_not_loosen() -> None:
    controller = TrailingStopController(
        AppTrailingConfig(callback_rate=Decimal("0.05"), min_step=Decimal("1"), update_interval_seconds=10)
    )
    state = AppTrailingState(
        side="long",
        high_watermark=Decimal("120"),
        low_watermark=None,
        stop_price=Decimal("114"),
        last_updated_at=NOW - timedelta(seconds=20),
    )

    result = controller.evaluate(
        state,
        current_price=Decimal("100"),
        price_stale=True,
        data_connected=False,
        now=NOW,
    )

    assert result.action == "blocked"
    assert result.reason == "market_data_unavailable"
    assert result.stop_price == Decimal("114")

