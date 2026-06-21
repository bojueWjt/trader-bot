from __future__ import annotations

import pytest

import governor
from order_management.state_descriptor import load_state_descriptor
from policy import RiskPolicy
from runtime.lifecycle import TradingLifecycle


POLICY = RiskPolicy(default_account_id="acct-1")


def _decision(action: str) -> dict:
    return {
        "classification": {
            "message_type": "new_signal",
            "action": action,
            "ambiguous": False,
            "ambiguity_reasons": [],
        },
        "intent": {
            "account_scope": "single",
            "target_account_id": "acct-1",
            "target_position_id": "pos-1",
            "instrument_symbol": "BTCUSDT",
            "side": "long",
            "entry": {"type": "market", "price": 100.0, "price_min": None, "price_max": None},
            "stop_loss": 90.0,
            "take_profits": [110.0],
            "leverage": 2.0,
            "valid_until": None,
        },
    }


def _positions():
    return [{"account_id": "acct-1", "position_id": "pos-1", "instrument_id": "BTCUSDT", "notional": 0}]


@pytest.mark.parametrize("mode", ["ACTIVE", "REDUCING", "HALTED"])
def test_governor_enforces_frozen_halted_permission_matrix(mode):
    matrix = load_state_descriptor()["halted_semantics"]["permission_matrix"][mode]

    for action, allowed in matrix.items():
        out = governor.evaluate(
            _decision(action),
            positions=_positions(),
            risk_state={"mode": mode},
            policy=POLICY,
        )
        assert (out.status == "approved") is allowed, (mode, action, out.status, out.reason)


def test_runtime_lifecycle_uses_same_permission_matrix():
    lifecycle = TradingLifecycle()

    lifecycle.apply_operator_state("HALTED", reason="test")
    assert lifecycle.action_allowed("open_position") is False
    assert lifecycle.action_allowed("add_position") is False
    assert lifecycle.action_allowed("cancel_all") is True
    assert lifecycle.action_allowed("partial_close") is True
    assert lifecycle.action_allowed("close_all") is True

    lifecycle.apply_operator_state("REDUCING", reason="test")
    assert lifecycle.action_allowed("open_position") is False
    assert lifecycle.action_allowed("close_position") is True
