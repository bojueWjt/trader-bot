"""WP-E contract tests: order_plan.protection_policy on approved_trade_intent v1.

Spec (docs/plans/2026-08-28-execution-state-arch-migration.md, WP-E 改动点 1):
- order_plan grows an OPTIONAL field `protection_policy` with enum
  ["complete", "stop_only", "deferred", "waived"].
- It is NOT required (backward compatible: existing intents without the field
  must keep validating).
- A valid example carrying the field must be shipped alongside the schema.
"""

import pytest

# Sibling test module in the same rootdir-inserted directory: reuse the
# canonical loader + validator so this matrix validates exactly the way the
# main contract suite does (jsonschema when available, minimal fallback else).
from test_contracts_v1 import (
    SCHEMAS,
    V1_ROOT,
    ContractValidationError,
    load_json,
    valid_examples,
    validate,
)

PROTECTION_POLICY_VALUES = ("complete", "stop_only", "deferred", "waived")


def _schema():
    return load_json(SCHEMAS["approved_trade_intent"])


def _base_intent():
    return load_json(
        V1_ROOT / "examples" / "valid" / "approved_trade_intent.open_position.json"
    )


def _intent_with_policy(policy):
    instance = _base_intent()
    order_plan = dict(instance.get("order_plan") or {})
    order_plan["protection_policy"] = policy
    instance["order_plan"] = order_plan
    return instance


def test_schema_declares_optional_protection_policy_enum():
    schema = _schema()
    order_plan = schema["properties"]["order_plan"]
    policy = order_plan.get("properties", {}).get("protection_policy")
    assert policy is not None, (
        "order_plan.protection_policy missing from approved_trade_intent.v1.json"
    )
    assert sorted(policy.get("enum", [])) == sorted(PROTECTION_POLICY_VALUES)
    # Optional by contract: must not be forced onto historical intents.
    assert "protection_policy" not in (order_plan.get("required") or [])
    assert "protection_policy" not in (schema.get("required") or [])


@pytest.mark.parametrize("policy", PROTECTION_POLICY_VALUES)
def test_each_protection_policy_value_validates(policy):
    validate(_intent_with_policy(policy), _schema())


def test_intent_without_protection_policy_still_validates():
    instance = _base_intent()
    assert "protection_policy" not in (instance.get("order_plan") or {})
    validate(instance, _schema())


@pytest.mark.parametrize(
    "bad_policy",
    [
        "partial",          # not in the enum
        "COMPLETE",         # enum is case-sensitive
        "stop-only",        # wrong spelling of stop_only
        "",                 # empty string
        1,                  # wrong type
        True,               # wrong type
        None,               # null is not an enum member
        ["stop_only"],      # wrong type (array)
    ],
)
def test_invalid_protection_policy_values_rejected(bad_policy):
    with pytest.raises(ContractValidationError):
        validate(_intent_with_policy(bad_policy), _schema())


def test_a_valid_example_carries_protection_policy():
    """WP-E ships a synced valid example whose order_plan uses the field."""
    carrying = []
    for path in valid_examples():
        if not path.name.startswith("approved_trade_intent"):
            continue
        instance = load_json(path)
        policy = (instance.get("order_plan") or {}).get("protection_policy")
        if policy is not None:
            assert policy in PROTECTION_POLICY_VALUES, (
                f"{path.name}: protection_policy {policy!r} outside contract enum"
            )
            carrying.append(path.name)
    assert carrying, (
        "no valid approved_trade_intent example carries "
        "order_plan.protection_policy (spec WP-E requires a synced example)"
    )
