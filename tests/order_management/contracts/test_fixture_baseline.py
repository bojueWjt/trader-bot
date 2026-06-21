from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution_domain.idempotency import RequestId, intent_idempotency_key
from execution_domain.identifiers import (
    canonical_account_id,
    canonical_position_key,
    instruments_match,
    parse_instrument,
    venue_symbol,
)


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "order_management" / "fixtures"
STATE_DESCRIPTOR = ROOT / "packages" / "contracts" / "v1" / "order_state.v1.json"


def _load(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_order_state_transition_fixtures_against_descriptor():
    descriptor = _load(STATE_DESCRIPTOR)
    valid = _load(FIXTURES / "order_state" / "valid_transition.json")
    invalid = _load(FIXTURES / "order_state" / "invalid_transition.json")

    transitions = {
        (transition["from"], transition["to"], transition["trigger"])
        for transition in descriptor["domains"][valid["domain"]]["transitions"]
    }
    assert (valid["from"], valid["to"], valid["trigger"]) in transitions
    assert (invalid["from"], invalid["to"], invalid["trigger"]) not in transitions


def test_identifier_fixtures_cover_positive_and_negative_cases():
    valid = _load(FIXTURES / "identifiers" / "valid_cases.json")
    invalid = _load(FIXTURES / "identifiers" / "invalid_cases.json")

    for case in valid["instrument_cases"]:
        parsed = parse_instrument(case["raw"])
        assert venue_symbol(case["raw"]) == case["venue_symbol"]
        assert parsed.venue == case["venue"]
        assert parsed.product == case["product"]
        assert instruments_match(case["match"], case["raw"])

    for case in valid["position_keys"]:
        assert canonical_position_key(case["account_id"], case["instrument"], case.get("side")) == case["key"]

    for value in invalid["account_ids"]:
        with pytest.raises(ValueError, match="account_id"):
            canonical_account_id(value)
    for case in invalid["position_keys"]:
        with pytest.raises(ValueError, match="side"):
            canonical_position_key(case["account_id"], case["instrument"], case["side"])


def test_idempotency_fixtures_cover_positive_and_negative_cases():
    fixture = _load(FIXTURES / "idempotency" / "cases.json")

    for case in fixture["intent_keys"]:
        assert intent_idempotency_key(
            case["decision_id"],
            case["action"],
            case["instrument"],
            case["account"],
        ) == case["expected"]

    for value in fixture["invalid_request_ids"]:
        with pytest.raises(ValueError, match="request_id"):
            RequestId.from_value(value)
