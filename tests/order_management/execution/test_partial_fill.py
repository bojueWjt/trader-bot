from __future__ import annotations

from decimal import Decimal

from order_management.partial_fill import PartialFillRequest, handle_partial_fill


def test_keep_remainder_floors_to_increment() -> None:
    decision = handle_partial_fill(
        PartialFillRequest(
            policy="keep_remainder",
            original_quantity=Decimal("1.000"),
            filled_quantity=Decimal("0.333"),
            quantity_increment=Decimal("0.05"),
        )
    )

    assert decision.action == "keep_remainder"
    assert decision.remainder_quantity == Decimal("0.65")


def test_cancel_remainder_requests_cancel_for_floor_remainder() -> None:
    decision = handle_partial_fill(
        PartialFillRequest(
            policy="cancel_remainder",
            original_quantity=Decimal("1"),
            filled_quantity=Decimal("0.25"),
            quantity_increment=Decimal("0.1"),
        )
    )

    assert decision.action == "cancel_remainder"
    assert decision.remainder_quantity == Decimal("0.7")


def test_convert_remainder_to_market_requires_own_slippage_cap() -> None:
    missing = handle_partial_fill(
        PartialFillRequest(
            policy="convert_remainder_to_market",
            original_quantity=Decimal("1"),
            filled_quantity=Decimal("0.4"),
            quantity_increment=Decimal("0.1"),
        )
    )
    converted = handle_partial_fill(
        PartialFillRequest(
            policy="convert_remainder_to_market",
            original_quantity=Decimal("1"),
            filled_quantity=Decimal("0.4"),
            quantity_increment=Decimal("0.1"),
            market_conversion_max_slippage_bps=Decimal("25"),
        )
    )

    assert missing.action == "needs_review"
    assert missing.reason == "market_conversion_slippage_required"
    assert converted.action == "convert_remainder_to_market"
    assert converted.market_conversion_max_slippage_bps == Decimal("25")


def test_abort_and_reduce_filled_reduces_only_filled_quantity() -> None:
    decision = handle_partial_fill(
        PartialFillRequest(
            policy="abort_and_reduce_filled",
            original_quantity=Decimal("1"),
            filled_quantity=Decimal("0.27"),
            quantity_increment=Decimal("0.1"),
        )
    )

    assert decision.action == "abort_and_reduce_filled"
    assert decision.reduce_quantity == Decimal("0.2")

