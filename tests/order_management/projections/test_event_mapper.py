from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from projection.event_mapper import ProjectionConfig, ProjectionEventMapper


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_duplicate_business_event_gets_identical_stable_event_id() -> None:
    mapper = ProjectionEventMapper(
        ProjectionConfig(node_id="node-1", account_id="acct-1"),
        now=lambda: NOW,
    )
    first = _Event(
        "AccountBalance",
        ts_event=1_718_000_000_000_000_000,
        currency="USDT",
        balance=Decimal("100.00000001"),
        id="nautilus-a",
    )
    duplicate = _Event(
        "AccountBalance",
        ts_event=1_718_000_000_000_000_000,
        currency="USDT",
        balance=Decimal("100.00000001"),
        id="nautilus-b",
    )

    first_envelope = mapper.to_envelope(first)
    duplicate_envelope = mapper.to_envelope(duplicate)

    assert first_envelope is not None
    assert duplicate_envelope is not None
    assert first_envelope.event_id == duplicate_envelope.event_id
    assert first_envelope.event_type == "AccountBalance"


def test_order_position_and_account_events_are_mapped() -> None:
    mapper = ProjectionEventMapper(
        ProjectionConfig(node_id="node-1", account_id="acct-1"),
        now=lambda: NOW,
    )

    assert mapper.to_envelope(_Event("OrderSubmitted", ts_event=1)) is not None
    assert mapper.to_envelope(_Event("PositionChanged", ts_event=1)) is not None
    assert mapper.to_envelope(_Event("AccountMargin", ts_event=1)) is not None


def test_numeric_payload_values_round_trip_as_json_strings() -> None:
    mapper = ProjectionEventMapper(
        ProjectionConfig(node_id="node-1", account_id="acct-1"),
        now=lambda: NOW,
    )
    event = _Event(
        "OrderFilled",
        ts_event=1_718_000_000_000_000_000,
        instrument_id="BTCUSDT-PERP.BINANCE",
        quantity=Decimal("0.123456789123456789"),
        filled_qty=Decimal("0.123456789123456789"),
        leaves_qty=Decimal("0"),
        price=Decimal("65000.12345678"),
        avg_px=Decimal("65000.12345678"),
    )

    envelope = mapper.to_envelope(event)

    assert envelope is not None
    encoded = json.dumps(envelope.payload)
    decoded = json.loads(encoded)
    assert decoded["quantity"] == "0.123456789123456789"
    assert decoded["filled_qty"] == "0.123456789123456789"
    assert decoded["leaves_qty"] == "0"
    assert decoded["price"] == "65000.12345678"
    assert decoded["avg_px"] == "65000.12345678"
    assert all(not isinstance(value, float) for value in decoded.values())


@dataclass
class _Event:
    class_name: str
    ts_event: int
    client_order_id: str | None = None
    venue_order_id: str | None = None
    trade_id: str | None = None
    instrument_id: str | None = None
    quantity: Any = None
    filled_qty: Any = None
    leaves_qty: Any = None
    price: Any = None
    avg_px: Any = None
    balance: Any = None
    currency: str | None = None
    id: str = "ignored"

    @property
    def __class__(self) -> type[Any]:  # type: ignore[override]
        return type(self.class_name, (), {})
