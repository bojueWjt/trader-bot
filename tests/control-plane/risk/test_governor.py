from __future__ import annotations

import pytest

import governor
from policy import RiskPolicy

POLICY = RiskPolicy(default_account_id="acct-1")


def make_decision(
    *,
    action="open_position",
    message_type="new_signal",
    side="long",
    instrument="BTCUSDT",
    entry_price=100.0,
    stop_loss=90.0,
    take_profits=(110.0, 120.0),
    leverage=3.0,
    target_position_id=None,
    target_account_id="acct-1",
    ambiguous=False,
):
    return {
        "classification": {
            "message_type": message_type,
            "action": action,
            "ambiguous": ambiguous,
            "ambiguity_reasons": [],
        },
        "intent": {
            "account_scope": "single",
            "target_account_id": target_account_id,
            "target_position_id": target_position_id,
            "instrument_symbol": instrument,
            "side": side,
            "entry": {"type": "market", "price": entry_price, "price_min": None, "price_max": None},
            "stop_loss": stop_loss,
            "take_profits": list(take_profits),
            "leverage": leverage,
            "valid_until": None,
        },
    }


def ev(decision, *, positions=None, risk_state=None, policy=POLICY):
    # Default to an explicit ACTIVE risk context so geometry/leverage/update tests
    # reach those checks. Tests that pass risk_state (HALTED/REDUCING/exposure/{})
    # override this. A missing context now fails closed (risk_context_incomplete).
    if risk_state is None:
        risk_state = {"mode": "ACTIVE"}
    return governor.evaluate(decision, positions=positions or [], risk_state=risk_state, policy=policy)


def test_missing_risk_state_fails_closed():
    out = ev(make_decision(), risk_state={})  # no mode -> incomplete -> no new risk
    assert out.status == "needs_review"
    assert "risk_context_incomplete" in out.reason


# --- non-actionable ---------------------------------------------------------------


@pytest.mark.parametrize("action", ["hold", "ignore", "needs_review"])
def test_non_actionable_needs_review(action):
    assert ev(make_decision(action=action)).status == "needs_review"


def test_ambiguous_needs_review():
    assert ev(make_decision(ambiguous=True)).status == "needs_review"


# --- geometry ---------------------------------------------------------------------


def test_valid_long_geometry_approved():
    assert ev(make_decision(side="long", entry_price=100, stop_loss=90, take_profits=(110, 120))).status == "approved"


def test_long_stop_above_entry_rejected():
    out = ev(make_decision(side="long", entry_price=100, stop_loss=105, take_profits=(110,)))
    assert out.status == "rejected" and "geometry" in out.reason or any(
        c["name"] == "geometry" and not c["passed"] for c in out.checks
    )


def test_long_take_profit_below_entry_rejected():
    assert ev(make_decision(side="long", entry_price=100, stop_loss=90, take_profits=(95,))).status == "rejected"


def test_valid_short_geometry_approved():
    assert ev(make_decision(side="short", entry_price=100, stop_loss=110, take_profits=(90, 80))).status == "approved"


def test_short_stop_below_entry_rejected():
    assert ev(make_decision(side="short", entry_price=100, stop_loss=90, take_profits=(80,))).status == "rejected"


def test_short_take_profits_not_descending_rejected():
    assert ev(make_decision(side="short", entry_price=100, stop_loss=110, take_profits=(80, 90))).status == "rejected"


# --- risk units -------------------------------------------------------------------


def test_leverage_over_cap_rejected():
    assert ev(make_decision(leverage=50.0)).status == "rejected"


def test_instrument_not_whitelisted_rejected():
    assert ev(make_decision(instrument="DOGEUSDT")).status == "rejected"


def test_price_precision_rejected():
    assert ev(make_decision(entry_price=100.123, stop_loss=90.0)).status == "rejected"


# --- update actions ---------------------------------------------------------------


def test_update_message_cannot_open():
    out = ev(make_decision(message_type="position_update", action="open_position"))
    assert out.status == "rejected"


def test_update_without_target_needs_review():
    out = ev(make_decision(action="close_position", target_position_id=None))
    assert out.status == "needs_review"


def test_update_target_unique_match_approved():
    positions = [{"account_id": "acct-1", "position_id": "pos-1", "instrument_id": "BTCUSDT", "notional": 0}]
    out = ev(make_decision(action="move_stop_to_entry", target_position_id="pos-1"), positions=positions)
    assert out.status == "approved"


def test_update_target_no_match_needs_review():
    out = ev(make_decision(action="close_position", target_position_id="pos-x"), positions=[])
    assert out.status == "needs_review"


def test_update_target_ambiguous_match_needs_review():
    positions = [
        {"account_id": "acct-1", "position_id": "pos-1", "instrument_id": "BTCUSDT", "notional": 0},
        {"account_id": "acct-1", "position_id": "pos-1", "instrument_id": "BTCUSDT", "notional": 0},
    ]
    out = ev(make_decision(action="close_position", target_position_id="pos-1"), positions=positions)
    assert out.status == "needs_review"


# --- kill switch / exposure -------------------------------------------------------


def test_kill_switch_halted_rejects_new_risk():
    out = ev(make_decision(), risk_state={"mode": "HALTED"})
    assert out.status == "rejected" and "HALTED" in out.reason


def test_reducing_blocks_opening():
    out = ev(make_decision(action="add_position"), risk_state={"mode": "REDUCING"})
    assert out.status == "rejected"


def test_instrument_exposure_cap_rejects():
    # exposure now comes from the live positions projection, not a risk_state counter.
    positions = [{"account_id": "acct-1", "instrument_id": "BTCUSDT", "notional": 100_000.0}]
    out = ev(make_decision(), positions=positions, risk_state={"mode": "ACTIVE"})
    assert out.status == "rejected"
    assert "instrument" in out.reason or "notional" in out.reason


def test_account_unresolved_needs_review():
    policy = RiskPolicy(default_account_id=None)
    out = ev(make_decision(target_account_id=None), policy=policy)
    assert out.status == "needs_review"
