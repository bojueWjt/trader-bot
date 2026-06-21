from __future__ import annotations

import pytest

import governor
from execution_domain.identifiers import (
    PositionKey,
    canonical_account_id,
    canonical_instrument_key,
    canonical_position_key,
    instruments_match,
    parse_instrument,
    venue_symbol,
)
from order_management.identifiers import instruments_match as service_instruments_match
from policy import RiskPolicy


def _decision(*, instrument: str = "BTCUSDT", target_position_id: str = "pos-1") -> dict:
    return {
        "classification": {
            "message_type": "position_update",
            "action": "move_stop_to_entry",
            "ambiguous": False,
            "ambiguity_reasons": [],
        },
        "intent": {
            "account_scope": "single",
            "target_account_id": "acct-1",
            "target_position_id": target_position_id,
            "instrument_symbol": instrument,
            "side": "long",
            "entry": {"type": "market", "price": 100.0, "price_min": None, "price_max": None},
            "stop_loss": 90.0,
            "take_profits": [110.0],
            "leverage": 2.0,
            "valid_until": None,
        },
    }


def test_venue_symbol_strips_venue_product_and_normalizes_case():
    assert venue_symbol("BTCUSDT") == "BTCUSDT"
    assert venue_symbol("BTCUSDT-PERP.BINANCE") == "BTCUSDT"
    assert venue_symbol(" ethusdt ") == "ETHUSDT"
    assert venue_symbol("ethusdt-perp.binance") == "ETHUSDT"


def test_parse_instrument_preserves_raw_and_extracts_parts():
    parsed = parse_instrument("BTCUSDT-PERP.BINANCE")

    assert parsed.raw == "BTCUSDT-PERP.BINANCE"
    assert parsed.venue_symbol == "BTCUSDT"
    assert parsed.venue == "BINANCE"
    assert parsed.product == "PERP"


def test_canonical_key_and_match_do_not_use_raw_string_equality():
    assert canonical_instrument_key("BTCUSDT-PERP.BINANCE") == "BTCUSDT"
    assert instruments_match("BTCUSDT", "BTCUSDT-PERP.BINANCE")
    assert service_instruments_match("ethusdt", "ETHUSDT-PERP.BINANCE")
    assert "BTCUSDT" != "BTCUSDT-PERP.BINANCE"
    assert not instruments_match("BTCUSDT", "ETHUSDT-PERP.BINANCE")


def test_account_id_is_stripped_and_empty_is_rejected():
    assert canonical_account_id(" acct-1 ") == "acct-1"
    with pytest.raises(ValueError, match="account_id"):
        canonical_account_id("  ")


def test_position_key_documents_netting_external_and_both_hedge_modes():
    assert PositionKey("acct-1", "BTCUSDT-PERP.BINANCE").key() == "acct-1:BTCUSDT"
    assert canonical_position_key("acct-1", "BTCUSDT-PERP.BINANCE") == "acct-1:BTCUSDT"
    assert canonical_position_key("acct-1", "BTCUSDT-PERP.BINANCE", side="long") == (
        "acct-1:BTCUSDT:long"
    )
    assert canonical_position_key("acct-1", "btcusdt", side="SHORT") == "acct-1:BTCUSDT:short"

    with pytest.raises(ValueError, match="side"):
        canonical_position_key("acct-1", "BTCUSDT", side="both")


def test_governor_update_target_uses_canonical_instrument_match():
    positions = [
        {
            "account_id": "acct-1",
            "position_id": "pos-1",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "notional": 0,
        }
    ]

    decision = _decision(instrument="BTCUSDT")
    out = governor.evaluate(
        decision,
        positions=positions,
        risk_state={"mode": "ACTIVE"},
        policy=RiskPolicy(default_account_id="acct-1"),
    )

    assert out.status == "approved"
