from __future__ import annotations

from decimal import Decimal

from risk_config import RiskConfig
from exchange_filters import InstrumentMetadata, OrderFilterRequest, validate_exchange_filters


class FakeMetadataSource:
    def __init__(self, metadata: InstrumentMetadata) -> None:
        self.metadata = metadata

    def get_instrument(self, instrument_id: str) -> InstrumentMetadata:
        assert instrument_id in {"BTCUSDT", "BTCUSDT-PERP.BINANCE"}
        return self.metadata


def _metadata() -> InstrumentMetadata:
    return InstrumentMetadata(
        instrument_id="BTCUSDT",
        min_notional=Decimal("10"),
        min_quantity=Decimal("0.001"),
        quantity_step=Decimal("0.001"),
        price_tick=Decimal("0.10"),
        min_price=Decimal("1"),
        max_price=Decimal("1000000"),
        max_leverage=Decimal("20"),
        allowed_margin_modes=frozenset({"cross", "isolated"}),
    )


def test_exchange_filters_accept_valid_order() -> None:
    result = validate_exchange_filters(
        OrderFilterRequest(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity=Decimal("0.010"),
            price=Decimal("50000.10"),
            leverage=Decimal("10"),
            margin_mode="cross",
        ),
        metadata_source=FakeMetadataSource(_metadata()),
        config=RiskConfig(max_leverage=Decimal("10")),
    )

    assert result.status == "approved"
    assert all(check["passed"] for check in result.checks)


def test_exchange_filters_reject_min_notional_step_tick_and_leverage() -> None:
    result = validate_exchange_filters(
        OrderFilterRequest(
            instrument_id="BTCUSDT",
            quantity=Decimal("0.0005"),
            price=Decimal("30000.03"),
            leverage=Decimal("25"),
            margin_mode="portfolio",
        ),
        metadata_source=FakeMetadataSource(_metadata()),
        config=RiskConfig(max_leverage=Decimal("10")),
    )

    failed = {check["name"] for check in result.checks if not check["passed"]}
    assert result.status == "rejected"
    assert failed == {"min_quantity", "quantity_step", "price_tick", "leverage", "margin_mode"}
