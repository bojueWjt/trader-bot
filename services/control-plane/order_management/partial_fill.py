from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR


POLICIES = frozenset(
    {
        "keep_remainder",
        "cancel_remainder",
        "convert_remainder_to_market",
        "abort_and_reduce_filled",
    }
)


@dataclass(frozen=True)
class PartialFillRequest:
    policy: str
    original_quantity: Decimal
    filled_quantity: Decimal
    quantity_increment: Decimal
    minimum_fill_ratio: Decimal = Decimal("0")
    market_conversion_max_slippage_bps: Decimal | None = None


@dataclass(frozen=True)
class PartialFillDecision:
    action: str
    reason: str
    remainder_quantity: Decimal = Decimal("0")
    reduce_quantity: Decimal = Decimal("0")
    market_conversion_max_slippage_bps: Decimal | None = None


def handle_partial_fill(request: PartialFillRequest) -> PartialFillDecision:
    if request.policy not in POLICIES:
        return PartialFillDecision("needs_review", "unknown_partial_fill_policy")
    remainder = _floor_to_increment(
        request.original_quantity - request.filled_quantity,
        request.quantity_increment,
    )
    filled = _floor_to_increment(request.filled_quantity, request.quantity_increment)
    if request.original_quantity <= 0:
        return PartialFillDecision("needs_review", "invalid_original_quantity")
    fill_ratio = request.filled_quantity / request.original_quantity
    if fill_ratio < request.minimum_fill_ratio:
        return PartialFillDecision("needs_review", "minimum_fill_ratio_not_met", remainder_quantity=remainder)
    if request.policy == "keep_remainder":
        return PartialFillDecision("keep_remainder", "policy_keep", remainder_quantity=remainder)
    if request.policy == "cancel_remainder":
        return PartialFillDecision("cancel_remainder", "policy_cancel", remainder_quantity=remainder)
    if request.policy == "convert_remainder_to_market":
        if request.market_conversion_max_slippage_bps is None:
            return PartialFillDecision(
                "needs_review",
                "market_conversion_slippage_required",
                remainder_quantity=remainder,
            )
        return PartialFillDecision(
            "convert_remainder_to_market",
            "policy_market_conversion",
            remainder_quantity=remainder,
            market_conversion_max_slippage_bps=request.market_conversion_max_slippage_bps,
        )
    return PartialFillDecision(
        "abort_and_reduce_filled",
        "policy_abort_reduce_filled",
        reduce_quantity=filled,
        remainder_quantity=remainder,
    )


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    if increment <= 0:
        return value
    units = (value / increment).to_integral_value(rounding=ROUND_FLOOR)
    return (units * increment).quantize(increment)
