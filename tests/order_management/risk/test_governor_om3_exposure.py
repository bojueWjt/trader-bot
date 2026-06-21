from __future__ import annotations

import governor
from policy import RiskPolicy


def test_governor_exposure_uses_canonical_matching_and_includes_proposed_notional() -> None:
    decision = _decision(sizing={"notional": 600})
    positions = [
        {
            "account_id": "acct-om3-a",
            "position_id": "pos-1",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "notional": 500,
        }
    ]

    out = governor.evaluate(
        decision,
        positions=positions,
        risk_state={"mode": "ACTIVE"},
        policy=RiskPolicy(default_account_id="acct-om3-a", max_instrument_notional=1000),
    )

    assert out.status == "rejected"
    assert any(c["name"] == "instrument_exposure" and not c["passed"] for c in out.checks)


def _decision(*, sizing: dict) -> dict:
    return {
        "classification": {
            "message_type": "new_signal",
            "action": "open_position",
            "ambiguous": False,
            "ambiguity_reasons": [],
        },
        "intent": {
            "account_scope": "single",
            "target_account_id": "acct-om3-a",
            "target_position_id": None,
            "instrument_symbol": "BTCUSDT",
            "side": "long",
            "entry": {"type": "market", "price": 100.0, "price_min": None, "price_max": None},
            "stop_loss": 90.0,
            "take_profits": [110.0],
            "leverage": 3.0,
            "sizing": sizing,
            "valid_until": None,
        },
    }

