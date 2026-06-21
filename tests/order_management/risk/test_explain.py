from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from account_budget import AccountBudget
from exchange_filters import FilterResult
from explain import build_risk_explain
from position_sizing import SizingResult


def test_risk_explain_includes_inputs_limits_quantity_and_headroom_without_secrets() -> None:
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    budget = AccountBudget(
        account_id="acct-om3-a",
        currency="USDT",
        equity=Decimal("10000"),
        margin=Decimal("1000"),
        free_margin=Decimal("8000"),
        margin_ratio=Decimal("0.10"),
        updated_at=now,
        stale_reasons=[],
    )
    sizing = SizingResult(
        status="approved",
        reason="sized",
        quantity=Decimal("1.5"),
        notional=Decimal("1500"),
        risk_amount=Decimal("100"),
        stop_distance_pct=Decimal("0.05"),
        raw_notional=Decimal("2000"),
        allowed_notional=Decimal("1500"),
        headroom={"max_notional_per_order": Decimal("1500"), "instrument": Decimal("9000")},
        checks=[{"name": "stop_distance", "passed": True}],
    )

    explain = build_risk_explain(
        account_budget=budget,
        sizing_result=sizing,
        exchange_result=FilterResult(
            status="approved",
            reason="filters passed",
            checks=[{"name": "min_notional", "passed": True}],
            metadata={"api_key": "should-not-leak", "min_notional": Decimal("10")},
        ),
        extra_inputs={"secret_token": "should-not-leak", "entry_price": Decimal("1000")},
    )

    assert explain["inputs"]["equity"] == "10000"
    assert explain["inputs"]["stop_distance_pct"] == "0.05"
    assert explain["final"]["quantity"] == "1.5"
    assert explain["headroom"]["instrument"] == "9000"
    assert "should-not-leak" not in str(explain)
    assert "api_key" not in str(explain)
    assert "secret_token" not in str(explain)

