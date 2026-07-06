from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from fixtures import (
    FIXTURE_RISK,
    NOW,
    SHUQIN_BTC_MARKET,
    SHUQIN_BTC_SHORT,
    SHUQIN_SOL_MARKET,
    SHUQIN_SOL_SHORT,
    VALID_UNTIL,
)
from zone_ladder import (
    ExpandDenied,
    MarketContext,
    RiskContext,
    ZoneSignal,
    expand_zone_to_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ORDER_PLAN_SCHEMA = json.loads(
    (REPO_ROOT / "packages" / "contracts" / "v1" / "approved_trade_intent.v1.json").read_text(
        encoding="utf-8"
    )
)["properties"]["order_plan"]
ORDER_PLAN_VALIDATOR = Draft202012Validator(
    ORDER_PLAN_SCHEMA,
    format_checker=FormatChecker(),
)


def _market(p0: float, *, price_increment: str = "0.01", quantity_increment: str = "0.000001"):
    return MarketContext(
        p0=p0,
        now=NOW,
        price_increment=price_increment,
        quantity_increment=quantity_increment,
    )


def _risk(
    budget: float = 100.0,
    *,
    max_notional: float = 1_000_000.0,
    max_leverage: float = 5.0,
) -> RiskContext:
    return RiskContext(
        risk_budget_usd=budget,
        max_notional=max_notional,
        max_leverage=max_leverage,
    )


def _long_signal(
    *,
    price_min: float = 99.0,
    price_max: float = 100.0,
    stop_loss: float | None = 95.0,
    take_profits: tuple[float, ...] = (105.0,),
    valid_until: datetime | None = VALID_UNTIL,
) -> ZoneSignal:
    return ZoneSignal(
        side="long",
        price_min=price_min,
        price_max=price_max,
        stop_loss=stop_loss,
        take_profits=take_profits,
        valid_until=valid_until,
    )


def _short_signal(
    *,
    price_min: float = 100.0,
    price_max: float = 101.0,
    stop_loss: float | None = 105.0,
    take_profits: tuple[float, ...] = (95.0,),
    valid_until: datetime | None = VALID_UNTIL,
) -> ZoneSignal:
    return ZoneSignal(
        side="short",
        price_min=price_min,
        price_max=price_max,
        stop_loss=stop_loss,
        take_profits=take_profits,
        valid_until=valid_until,
    )


def _assert_valid_order_plan(plan: dict) -> None:
    errors = sorted(ORDER_PLAN_VALIDATOR.iter_errors(plan), key=lambda e: list(e.path))
    assert errors == []


def _by_id(plan: dict) -> dict[str, dict]:
    return {tranche["tranche_id"]: tranche for tranche in plan["tranches"]}


def _qty(tranche: dict) -> Decimal:
    return Decimal(tranche["quantity"])


def _notional(plan: dict) -> Decimal:
    return sum(_qty(t) * Decimal(str(t["price"])) for t in plan["tranches"])


def test_chase_long_at_boundary_uses_marketable_cap_and_rests_deeper_tranches():
    plan = expand_zone_to_plan(_long_signal(), _market(100.35), _risk())

    _assert_valid_order_plan(plan)
    tranches = _by_id(plan)
    assert plan["mode"] == "zone_ladder"
    assert [t["style"] for t in plan["tranches"]] == ["chase", "rest", "rest"]
    assert tranches["t1_near"]["price"] == 100.0
    assert tranches["t1_near"]["limit_cap"] == pytest.approx(100.35)
    assert tranches["t2_mid"]["price"] == 99.5
    assert tranches["t3_deep"]["price"] == 99.15


def test_chase_short_mirrors_boundary_math_and_caps_downward():
    plan = expand_zone_to_plan(_short_signal(), _market(99.8), _risk())

    _assert_valid_order_plan(plan)
    tranches = _by_id(plan)
    assert [t["style"] for t in plan["tranches"]] == ["chase", "rest", "rest"]
    assert tranches["t1_near"]["price"] == 100.0
    assert tranches["t1_near"]["limit_cap"] == pytest.approx(99.65)
    assert tranches["t2_mid"]["price"] == 100.5
    assert tranches["t3_deep"]["price"] == 100.85
    assert plan["invalidation"]["cancel_on_close_beyond"] == 101.2


def test_regular_long_rests_all_three_depths_when_price_is_outside_chase_window():
    plan = expand_zone_to_plan(_long_signal(), _market(101.0), _risk())

    _assert_valid_order_plan(plan)
    assert [t["tranche_id"] for t in plan["tranches"]] == ["t1_near", "t2_mid", "t3_deep"]
    assert [t["style"] for t in plan["tranches"]] == ["rest", "rest", "rest"]
    assert [t["price"] for t in plan["tranches"]] == [100.0, 99.5, 99.15]


def test_regular_shuqin_btc_short_fixture_waits_for_retrace_to_near_edge():
    plan = expand_zone_to_plan(SHUQIN_BTC_SHORT, SHUQIN_BTC_MARKET, FIXTURE_RISK)

    _assert_valid_order_plan(plan)
    assert [t["style"] for t in plan["tranches"]] == ["rest", "rest", "rest"]
    assert [t["price"] for t in plan["tranches"]] == [62300.0, 62500.0, 62640.0]
    assert _by_id(plan)["t1_near"]["price"] == 62300.0


def test_stale_long_inside_prunes_unfavorable_mid_and_merges_weight_to_deep():
    plan = expand_zone_to_plan(_long_signal(), _market(99.4), _risk())

    _assert_valid_order_plan(plan)
    tranches = _by_id(plan)
    assert [t["tranche_id"] for t in plan["tranches"]] == ["t1_near", "t3_deep"]
    assert tranches["t1_near"]["style"] == "chase"
    assert tranches["t1_near"]["risk_weight"] == pytest.approx(0.55)
    assert tranches["t3_deep"]["risk_weight"] == pytest.approx(0.45)


def test_stale_shuqin_sol_short_fixture_prunes_equal_mid_and_merges_weight_to_deep():
    plan = expand_zone_to_plan(SHUQIN_SOL_SHORT, SHUQIN_SOL_MARKET, FIXTURE_RISK)

    _assert_valid_order_plan(plan)
    tranches = _by_id(plan)
    assert [t["tranche_id"] for t in plan["tranches"]] == ["t1_near", "t3_deep"]
    assert tranches["t1_near"]["style"] == "chase"
    assert tranches["t3_deep"]["price"] == 82.85
    assert tranches["t3_deep"]["risk_weight"] == pytest.approx(0.45)


def test_stale_long_penetrated_zone_returns_needs_review():
    denied = expand_zone_to_plan(_long_signal(), _market(98.5), _risk())

    assert denied == ExpandDenied(reason="needs_review", detail="zone_penetrated")


def test_stale_short_penetrated_zone_returns_needs_review():
    denied = expand_zone_to_plan(_short_signal(), _market(101.5), _risk())

    assert denied == ExpandDenied(reason="needs_review", detail="zone_penetrated")


def test_too_narrow_long_zone_collapses_to_single_midpoint_limit():
    plan = expand_zone_to_plan(
        _long_signal(price_min=100.0, price_max=100.1, stop_loss=95.0),
        _market(101.0),
        _risk(),
    )

    _assert_valid_order_plan(plan)
    assert plan["mode"] == "single_limit"
    assert len(plan["tranches"]) == 1
    assert plan["tranches"][0] | {"quantity": plan["tranches"][0]["quantity"]} == {
        "tranche_id": "t1_near",
        "seq": 1,
        "price": 100.05,
        "quantity": plan["tranches"][0]["quantity"],
        "risk_weight": 1.0,
        "style": "rest",
    }


def test_too_narrow_short_zone_collapses_to_single_midpoint_limit():
    plan = expand_zone_to_plan(
        _short_signal(price_min=100.0, price_max=100.1, stop_loss=105.0),
        _market(99.0),
        _risk(),
    )

    _assert_valid_order_plan(plan)
    assert plan["mode"] == "single_limit"
    assert len(plan["tranches"]) == 1
    assert plan["tranches"][0]["price"] == 100.05
    assert plan["tranches"][0]["risk_weight"] == pytest.approx(1.0)
    assert plan["tranches"][0]["style"] == "rest"


def test_sizing_normalizes_each_tranche_risk_to_budget_weight():
    plan = expand_zone_to_plan(
        _long_signal(stop_loss=95.0),
        _market(101.0, quantity_increment="0.00000001"),
        _risk(120.0),
    )

    _assert_valid_order_plan(plan)
    for tranche in plan["tranches"]:
        loss = float(_qty(tranche) * abs(Decimal(str(tranche["price"])) - Decimal("95.0")))
        assert loss == pytest.approx(120.0 * tranche["risk_weight"], rel=1e-8, abs=1e-6)


def test_sizing_caps_total_notional_by_scaling_all_tranches_proportionally():
    plan = expand_zone_to_plan(
        _long_signal(stop_loss=95.0),
        _market(101.0),
        _risk(1000.0, max_notional=1000.0),
    )

    _assert_valid_order_plan(plan)
    assert _notional(plan) <= Decimal("1000.0")

    ratios = []
    for tranche in plan["tranches"]:
        realized_loss = _qty(tranche) * abs(
            Decimal(str(tranche["price"])) - Decimal(str(plan["stop_loss"]))
        )
        intended_loss = Decimal("1000.0") * Decimal(str(tranche["risk_weight"]))
        ratios.append(realized_loss / intended_loss)
    assert max(ratios) - min(ratios) < Decimal("0.00001")


def test_zero_quantity_tranche_merges_weight_into_next_deeper_tranche_and_recomputes():
    plan = expand_zone_to_plan(
        _long_signal(stop_loss=99.0),
        _market(101.0, quantity_increment="0.001"),
        _risk(0.0017),
    )

    _assert_valid_order_plan(plan)
    tranches = _by_id(plan)
    assert [t["tranche_id"] for t in plan["tranches"]] == ["t2_mid", "t3_deep"]
    assert tranches["t2_mid"]["risk_weight"] == pytest.approx(0.85)
    assert tranches["t2_mid"]["quantity"] == "0.002"
    assert tranches["t3_deep"]["risk_weight"] == pytest.approx(0.15)


def test_missing_stop_loss_returns_needs_review_denial():
    denied = expand_zone_to_plan(_long_signal(stop_loss=None), _market(101.0), _risk())

    assert denied == ExpandDenied(reason="needs_review", detail="missing_stop_loss")


def test_same_input_produces_byte_identical_json():
    first = expand_zone_to_plan(SHUQIN_BTC_SHORT, SHUQIN_BTC_MARKET, FIXTURE_RISK)
    second = expand_zone_to_plan(SHUQIN_BTC_SHORT, SHUQIN_BTC_MARKET, FIXTURE_RISK)

    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second,
        sort_keys=True,
        separators=(",", ":"),
    )
