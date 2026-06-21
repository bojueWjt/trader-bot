from __future__ import annotations

from decimal import Decimal

import pytest

from .fake_venue import FakeVenue, VenueFault, VenueTimeout


def test_accept_then_unknown_fault_accepts_order_but_raises_uncertain_timeout() -> None:
    venue = FakeVenue(account_id="acct-sim-fault")
    venue.inject_fault("entry-1", VenueFault.ACCEPT_THEN_UNKNOWN)

    with pytest.raises(VenueTimeout, match="accepted_unknown"):
        venue.submit_order(
            client_order_id="entry-1",
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("1"),
        )

    assert venue.order("entry-1").status == "accepted"
    assert [event.event_type for event in venue.drain_events()] == ["OrderSubmitted", "OrderAccepted"]


def test_timeout_fault_does_not_create_local_venue_order() -> None:
    venue = FakeVenue(account_id="acct-sim-fault")
    venue.inject_fault("entry-timeout", VenueFault.TIMEOUT)

    with pytest.raises(VenueTimeout, match="timeout"):
        venue.submit_order(
            client_order_id="entry-timeout",
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("1"),
        )

    assert venue.find_order("entry-timeout") is None
    assert venue.drain_events() == []


def test_duplicate_and_out_of_order_event_injection_is_deterministic() -> None:
    venue = FakeVenue(account_id="acct-sim-fault")
    venue.submit_order(
        client_order_id="entry-events",
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1"),
        price=Decimal("64000"),
    )
    venue.fill("entry-events", quantity=Decimal("1"), price=Decimal("64000"))
    events = venue.drain_events()

    duplicated = venue.with_duplicate_events(events, event_ids=[events[-1].event_id])
    out_of_order = venue.out_of_order(events)

    assert [event.event_id for event in duplicated].count(events[-1].event_id) == 2
    assert [event.event_type for event in out_of_order] == list(
        reversed([event.event_type for event in events])
    )
