from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from execution_domain.contracts import (
    ApprovedTradeIntentV1,
    Breakeven,
    Execution,
    ExecutionZone,
    Protection,
    RiskBudget,
    Sizing,
    StopSpec,
    TakeProfitLevel,
    Trailing,
)
from order_management.state_descriptor import legal_transition, transition_target


ROOT = Path(__file__).resolve().parents[3]
APPROVED_SCHEMA = ROOT / "packages" / "contracts" / "v1" / "approved_trade_intent.v1.json"
ORDER_STATE = ROOT / "packages" / "contracts" / "v1" / "order_state.v1.json"


def test_extended_approved_trade_intent_schema_and_pydantic_fields_match() -> None:
    schema = _load(APPROVED_SCHEMA)

    assert set(ApprovedTradeIntentV1.model_fields) == set(schema["properties"])
    assert set(Execution.model_fields) == set(schema["properties"]["execution"]["properties"])
    assert set(ExecutionZone.model_fields) == set(schema["properties"]["execution"]["properties"]["zone"]["properties"])
    assert set(Protection.model_fields) == set(schema["properties"]["protection"]["properties"])
    assert set(StopSpec.model_fields) == set(schema["properties"]["protection"]["properties"]["stop"]["properties"])
    assert set(TakeProfitLevel.model_fields) == set(
        schema["properties"]["protection"]["properties"]["take_profits"]["items"]["properties"]
    )
    assert set(Breakeven.model_fields) == set(schema["properties"]["protection"]["properties"]["breakeven"]["properties"])
    assert set(Trailing.model_fields) == set(schema["properties"]["protection"]["properties"]["trailing"]["properties"])
    assert set(Sizing.model_fields) == set(schema["properties"]["sizing"]["properties"])
    assert set(RiskBudget.model_fields) == set(schema["properties"]["risk_budget"]["properties"])

    data = _rich_approved_trade_intent()
    _schema_errors(schema, data, expected_valid=True)
    model = ApprovedTradeIntentV1.model_validate(data)

    assert model.execution is not None
    assert model.execution.order_type == "limit"
    assert model.protection is not None
    assert model.protection.stop is not None
    assert model.sizing is not None


def test_extended_approved_trade_intent_rejects_extra_fields_in_schema_and_pydantic() -> None:
    schema = _load(APPROVED_SCHEMA)
    data = _rich_approved_trade_intent()
    data["execution"]["unexpected"] = True

    schema_errors = _schema_errors(schema, data, expected_valid=False)
    assert "Additional properties" in schema_errors[0].message
    with pytest.raises(ValidationError):
        ApprovedTradeIntentV1.model_validate(data)


def test_order_state_descriptor_transitions_are_internally_consistent() -> None:
    descriptor = _load(ORDER_STATE)

    for domain, domain_descriptor in descriptor["domains"].items():
        states = set(domain_descriptor["states"])
        grouped_targets: dict[tuple[str, str], set[str]] = defaultdict(set)
        for edge in domain_descriptor["transitions"]:
            assert edge["from"] in states
            assert edge["to"] in states
            assert legal_transition(domain, edge["from"], edge["to"]) is True
            grouped_targets[(edge["from"], edge["trigger"])].add(edge["to"])

        for (source, trigger), targets in grouped_targets.items():
            assert len(targets) == 1, f"{domain}.{source}.{trigger} has ambiguous targets"
            assert transition_target(domain, source, trigger) == next(iter(targets))

        terminal = {state for state, meta in domain_descriptor["states"].items() if meta.get("terminal")}
        for edge in domain_descriptor["transitions"]:
            assert edge["from"] not in terminal, f"{domain}: terminal {edge['from']} has outgoing edge"


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _schema_errors(schema: dict, data: dict, *, expected_valid: bool):
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.path))
    if expected_valid:
        assert not errors, errors[0].message if errors else ""
    else:
        assert errors
    return errors


def _rich_approved_trade_intent() -> dict:
    return {
        "schema_version": "1.0",
        "intent_id": "11111111-1111-4111-8111-111111111111",
        "decision_id": "22222222-2222-4222-8222-222222222222",
        "risk_decision_id": "33333333-3333-4333-8333-333333333333",
        "account_id": "acct-om8-contract",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "action": "open_position",
        "order_plan": {"side": "buy", "type": "limit"},
        "execution": {
            "order_type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "reduce_only": False,
            "max_slippage_bps": 5,
            "limit_price": 100,
            "zone": {"low": 99, "high": 101},
        },
        "protection": {
            "require_stop": True,
            "stop": {"type": "stop_market", "trigger_basis": "mark", "price": 95},
            "take_profits": [
                {"r_multiple": 1.5, "fraction": 0.5, "time_in_force": "GTC"},
                {"price": 120, "fraction": 0.5, "time_in_force": "GTC"},
            ],
            "breakeven": {"enabled": True, "trigger_r": 1, "offset_bps": 2},
            "trailing": {"enabled": True, "activation_r": 2, "callback_rate": 0.5, "min_step": 0},
        },
        "sizing": {"quantity": 1},
        "risk_budget": {"risk_fraction": 0.01, "max_notional": 1000, "max_leverage": 5},
        "target_position_id": None,
        "valid_until": "2026-06-21T12:05:00Z",
        "idempotency_key": "a" * 64,
        "approved_at": "2026-06-21T12:00:00Z",
    }
