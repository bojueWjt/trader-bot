from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from execution_domain.contracts import ApprovedTradeIntentV1


def _intent(**overrides) -> dict:
    intent = {
        "schema_version": "1.0",
        "intent_id": "11111111-1111-4111-8111-111111111111",
        "decision_id": "22222222-2222-4222-8222-222222222222",
        "risk_decision_id": "33333333-3333-4333-8333-333333333333",
        "account_id": "acct-1",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "action": "open_position",
        "order_plan": {"entry": "legacy-open-object"},
        "risk_budget": {
            "risk_fraction": 0.01,
            "max_notional": 10000,
            "max_leverage": 3,
        },
        "target_position_id": None,
        "valid_until": "2026-06-21T12:00:00Z",
        "idempotency_key": "a" * 64,
        "approved_at": "2026-06-21T11:00:00Z",
    }
    intent.update(overrides)
    return intent


def test_legacy_intent_without_order_management_fields_still_validates():
    model = ApprovedTradeIntentV1.model_validate(_intent())

    assert model.execution is None
    assert model.protection is None
    assert model.sizing is None


def test_open_intent_accepts_execution_protection_and_sizing():
    model = ApprovedTradeIntentV1.model_validate(
        _intent(
            execution={
                "order_type": "limit",
                "time_in_force": "GTC",
                "post_only": True,
                "reduce_only": False,
                "max_slippage_bps": "12.5",
                "limit_price": "25000.5",
                "zone": {"low": "24900", "high": "25100"},
            },
            protection={
                "require_stop": True,
                "stop": {
                    "type": "stop_market",
                    "trigger_basis": "mark",
                    "distance_pct": "1.5",
                },
                "take_profits": [
                    {"price": "26000", "fraction": "0.4", "time_in_force": "GTC"},
                    {"r_multiple": "2.0", "fraction": "0.6", "time_in_force": "IOC"},
                ],
                "breakeven": {"enabled": True, "trigger_r": "1.0", "offset_bps": "5"},
                "trailing": {
                    "enabled": True,
                    "activation_r": "1.5",
                    "callback_rate": "0.3",
                    "min_step": "10",
                },
            },
            sizing={"quantity": "0.25"},
        )
    )

    assert model.execution.limit_price == Decimal("25000.5")
    assert model.protection.take_profits[1].r_multiple == Decimal("2.0")
    assert model.sizing.quantity == Decimal("0.25")


@pytest.mark.parametrize(
    "action",
    [
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
        "partial_close",
        "close_position",
    ],
)
def test_management_actions_must_not_carry_sizing(action: str):
    with pytest.raises(ValidationError, match="management actions must not carry sizing"):
        ApprovedTradeIntentV1.model_validate(
            _intent(action=action, target_position_id="pos-1", sizing={"fraction": "0.5"})
        )


@pytest.mark.parametrize(
    "execution",
    [
        {"order_type": "market"},
        {"limit_price": "25000"},
        {"zone": {"low": "24900", "high": "25100"}},
    ],
)
def test_management_actions_must_not_carry_entry_only_execution_fields(execution: dict):
    with pytest.raises(ValidationError, match="entry-only execution fields"):
        ApprovedTradeIntentV1.model_validate(
            _intent(action="move_stop_loss", target_position_id="pos-1", execution=execution)
        )


def test_take_profit_level_requires_price_xor_r_multiple():
    with pytest.raises(ValidationError, match="price or r_multiple"):
        ApprovedTradeIntentV1.model_validate(
            _intent(
                protection={
                    "take_profits": [
                        {"price": "26000", "r_multiple": "2.0", "fraction": "0.5"}
                    ]
                }
            )
        )


def test_take_profit_level_requires_fraction_xor_quantity():
    with pytest.raises(ValidationError, match="fraction or quantity"):
        ApprovedTradeIntentV1.model_validate(
            _intent(
                protection={
                    "take_profits": [
                        {"price": "26000", "fraction": "0.5", "quantity": "0.1"}
                    ]
                }
            )
        )


def test_take_profit_fraction_sum_must_not_exceed_one():
    with pytest.raises(ValidationError, match="take_profit fractions"):
        ApprovedTradeIntentV1.model_validate(
            _intent(
                protection={
                    "take_profits": [
                        {"price": "26000", "fraction": "0.7"},
                        {"price": "27000", "fraction": "0.4"},
                    ]
                }
            )
        )


def test_sizing_requires_quantity_xor_fraction():
    with pytest.raises(ValidationError, match="quantity or fraction"):
        ApprovedTradeIntentV1.model_validate(_intent(sizing={"quantity": "1", "fraction": "0.5"}))


def test_stop_requires_price_xor_distance_pct():
    with pytest.raises(ValidationError, match="price or distance_pct"):
        ApprovedTradeIntentV1.model_validate(
            _intent(
                protection={
                    "stop": {
                        "type": "stop_market",
                        "trigger_basis": "mark",
                        "price": "24500",
                        "distance_pct": "1.5",
                    }
                }
            )
        )
