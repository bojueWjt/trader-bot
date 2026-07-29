from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from execution_domain.contracts import ApprovedTradeIntentV1


ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / "packages" / "contracts" / "v1" / "approved_trade_intent.v1.json"
FIXTURES = ROOT / "tests" / "order_management" / "fixtures" / "approved_trade_intent"


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _schema_validate(data: dict) -> None:
    schema = _load(SCHEMA)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.path))
    assert not errors, errors[0].message if errors else ""


@pytest.mark.parametrize(
    "fixture_name",
    [
        "legacy_intent.json",
        "valid_open_with_protection.json",
    ],
)
def test_approved_trade_intent_fixture_validates_with_schema_and_pydantic(fixture_name: str):
    data = _load(FIXTURES / fixture_name)

    _schema_validate(data)
    model = ApprovedTradeIntentV1.model_validate(data)
    dumped = json.loads(model.model_dump_json())
    reparsed = ApprovedTradeIntentV1.model_validate(dumped)

    assert reparsed.intent_id == model.intent_id
    assert reparsed.action == model.action


@pytest.mark.parametrize(
    "fixture_name, message",
    [
        ("invalid_management_with_sizing.json", "management actions must not carry sizing"),
        ("invalid_tp_fraction_sum.json", "take_profit fractions"),
    ],
)
def test_illegal_combinations_are_rejected_by_pydantic_before_execution_layer(
    fixture_name: str,
    message: str,
):
    data = _load(FIXTURES / fixture_name)

    _schema_validate(data)
    with pytest.raises(ValidationError, match=message):
        ApprovedTradeIntentV1.model_validate(data)
