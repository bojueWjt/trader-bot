from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from strategy.price_guard import PriceGuard, PriceGuardConfig, PriceReference


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_price_guard_allows_fresh_market_reference_inside_slippage() -> None:
    guard = PriceGuard(_Store(PriceReference(mark_price=Decimal("100"), last_event_at=NOW, stale=False)))

    decision = guard.check(
        account_id="acct",
        venue_symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        intended_price=Decimal("100.05"),
        max_slippage_bps=Decimal("10"),
        now=NOW,
    )

    assert decision.allowed is True
    assert decision.reason == "price_guard_passed"


def test_price_guard_rejects_stale_reference() -> None:
    guard = PriceGuard(
        _Store(PriceReference(mark_price=Decimal("100"), last_event_at=NOW - timedelta(seconds=31), stale=False)),
        config=PriceGuardConfig(stale_after_seconds=30),
    )

    decision = guard.check(
        account_id="acct",
        venue_symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        intended_price=Decimal("100"),
        max_slippage_bps=Decimal("10"),
        now=NOW,
    )

    assert decision.allowed is False
    assert decision.reason == "price_reference_stale"


def test_price_guard_rejects_limit_deviation_above_cap() -> None:
    guard = PriceGuard(_Store(PriceReference(mark_price=Decimal("100"), last_price=Decimal("100"), last_event_at=NOW, stale=False)))

    decision = guard.check(
        account_id="acct",
        venue_symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        intended_price=Decimal("102"),
        max_slippage_bps=Decimal("50"),
        now=NOW,
    )

    assert decision.allowed is False
    assert decision.reason == "price_deviation_exceeded"
    assert decision.deviation_bps == Decimal("200")


def test_price_guard_fails_closed_when_slippage_cap_has_no_intended_price() -> None:
    guard = PriceGuard(_Store(PriceReference(mark_price=Decimal("100"), last_event_at=NOW, stale=False)))

    decision = guard.check(
        account_id="acct",
        venue_symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        intended_price=None,
        max_slippage_bps=Decimal("10"),
        now=NOW,
    )

    assert decision.allowed is False
    assert decision.reason == "intended_price_required"


def test_retries_recheck_price_guard_and_cannot_bypass_freshness() -> None:
    store = _Store(PriceReference(mark_price=Decimal("100"), last_event_at=NOW, stale=False))
    guard = PriceGuard(store)

    first = guard.check(
        account_id="acct",
        venue_symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        intended_price=Decimal("100"),
        max_slippage_bps=Decimal("10"),
        now=NOW,
    )
    store.reference = PriceReference(mark_price=Decimal("100"), last_event_at=NOW - timedelta(minutes=2), stale=False)
    second = guard.check(
        account_id="acct",
        venue_symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        intended_price=Decimal("100"),
        max_slippage_bps=Decimal("10"),
        now=NOW,
    )

    assert first.allowed is True
    assert second.allowed is False
    assert store.calls == 2


@dataclass
class _Store:
    reference: PriceReference
    calls: int = 0

    def load_reference(self, *, account_id: str, venue_symbol: str) -> PriceReference | None:
        self.calls += 1
        return self.reference
