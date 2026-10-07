"""Offline contract tests for signal -> operator adaptation."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from signal_operator import build_operator_request


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def task():
    return SimpleNamespace(
        task_id="11111111-1111-4111-8111-111111111112",
        raw_message_id="22222222-2222-4222-8222-222222222222",
        processing_run_id="33333333-3333-4333-8333-333333333333",
        claim_token="55555555-5555-4555-8555-555555555555",
        attempt=1,
        source_platform="telegram",
        channel_id="-100123",
        source_message_id="456",
        edit_version="0",
        account_id="account-a",
        action="evaluate",
        related_task_id="an-edit-is-not-a-parent-entry",
    )


@pytest.fixture
def decision():
    candidate = json.loads(
        (ROOT / "packages/contracts/v1/examples/valid/hermes_decision.open_limit.json")
        .read_text()
    )
    candidate["intent"]["target_account_id"] = "account-a"
    schema = json.loads((ROOT / "packages/contracts/v1/hermes_decision.v1.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(candidate)
    return candidate


@pytest.mark.parametrize("action", ["open_position", "add_position"])
def test_entry_preserves_action_and_channel_authority(task, decision, action):
    decision["classification"]["action"] = action
    original = deepcopy(decision)
    body = build_operator_request(task, decision)
    assert body["action"] == body["intended_action"] == action
    assert body["account_id"] == "account-a"
    assert body["authorized_by_type"] == "channel"
    assert body["authorized_by_id"] == body["channel"] == body["source_channel"] == "-100123"
    assert body["symbol"] == "BTCUSDT"
    assert body["entry"] == decision["intent"]["entry"]
    assert body["stop_loss"] == 64000
    assert body["take_profits"] == [66000, 67250]
    assert body["valid_until"] == decision["intent"]["valid_until"]
    assert "notional_usdt" not in body
    assert "quantity" not in body
    assert body["client_ref"] == "tg-sig-c100123-m456"
    assert body["created_by_service"] == body["source"]
    assert body["source_message_id"] == task.source_message_id
    assert body["signal_claim"] == {
        "task_id": task.task_id,
        "processing_run_id": task.processing_run_id,
        "claim_token": task.claim_token,
        "attempt": 1,
        "stable_action_or_leg_id": f"telegram|-100123|456|0|account-a|{action}",
    }
    body["entry"]["price"] = 1
    assert decision == original


def test_business_identity_survives_attempt_but_distinguishes_action_and_edit(task, decision):
    first = build_operator_request(task, decision)
    task.attempt = 2
    task.claim_token = "66666666-6666-4666-8666-666666666666"
    task.processing_run_id = "77777777-7777-4777-8777-777777777777"
    decision["processing_run_id"] = task.processing_run_id
    second = build_operator_request(task, decision)
    assert first["signal_claim"]["stable_action_or_leg_id"] == second["signal_claim"]["stable_action_or_leg_id"]
    assert first["signal_claim"]["claim_token"] != second["signal_claim"]["claim_token"]
    decision["classification"]["action"] = "add_position"
    third = build_operator_request(task, decision)
    task.edit_version = "1"
    fourth = build_operator_request(task, decision)
    assert len({b["signal_claim"]["stable_action_or_leg_id"] for b in [first, third, fourth]}) == 3
    assert len({b["client_ref"] for b in [first, second, third, fourth]}) == 1


@pytest.mark.parametrize("action", ["close_position", "move_stop_loss"])
def test_management_requires_explicit_parent_and_preserves_position(task, decision, action):
    decision["classification"].update(action=action, message_type="position_update")
    decision["intent"].update(side="short", target_position_id="BTCUSDT.PERP.BINANCE-short")
    with pytest.raises(ValueError, match="management entry_ref"):
        build_operator_request(task, decision)
    body = build_operator_request(task, decision, entry_ref="tg-sig-c100123-m123")
    assert body["entry_ref"] == "tg-sig-c100123-m123"
    assert body["position_side"] == body["side"] == "short"
    assert body["target_position_id"] == "BTCUSDT.PERP.BINANCE-short"
    assert body["action"] == action
    assert "entry" not in body
    assert "parent_intent_id" not in body


@pytest.mark.parametrize("action", ["hold", "ignore", "needs_review"])
def test_non_trades_produce_no_request(task, decision, action):
    decision["classification"].update(action=action, ambiguous=True)
    assert build_operator_request(task, decision) is None


@pytest.mark.parametrize("action,reason", [
    ("partial_close", "close quantity"),
    ("replace_take_profits", "take-profit quantities"),
    ("move_stop_to_entry", "entry cost"),
    ("cancel_order", "unsupported action"),
])
def test_v1_actions_missing_execution_parameters_fail(task, decision, action, reason):
    decision["classification"]["action"] = action
    with pytest.raises(ValueError, match=reason):
        build_operator_request(task, decision)


@pytest.mark.parametrize("scope", ["all", "unassigned"])
def test_scoped_task_never_expands_or_guesses_account(task, decision, scope):
    decision["intent"]["account_scope"] = scope
    with pytest.raises(ValueError, match="account_scope"):
        build_operator_request(task, decision)


@pytest.mark.parametrize("account", [None, "account-b", ""])
def test_target_account_must_match(task, decision, account):
    decision["intent"]["target_account_id"] = account
    with pytest.raises(ValueError, match="target_account_id"):
        build_operator_request(task, decision)


@pytest.mark.parametrize("field,value", [
    ("claim_token", None), ("processing_run_id", None), ("attempt", 0),
    ("attempt", True), ("account_id", ""), ("channel_id", ""),
])
def test_missing_claim_or_source_fields_fail(task, decision, field, value):
    setattr(task, field, value)
    with pytest.raises(ValueError, match=field):
        build_operator_request(task, decision)


@pytest.mark.parametrize("field,value", [
    ("stop_loss", None), ("stop_loss", 0), ("stop_loss", True),
    ("stop_loss", float("nan")), ("side", None), ("side", "buy"),
    ("instrument_symbol", "BTC"),
])
def test_missing_or_invalid_execution_parameters_fail(task, decision, field, value):
    decision["intent"][field] = value
    with pytest.raises(ValueError, match=field):
        build_operator_request(task, decision)


@pytest.mark.parametrize("entry,reason", [
    ({"type": "none"}, "entry.type"),
    ({"type": "limit", "price": None}, "entry.price"),
    ({"type": "zone", "price_min": 20, "price_max": 10}, "price_min"),
])
def test_entry_type_and_prices_are_never_invented(task, decision, entry, reason):
    decision["intent"]["entry"] = entry
    with pytest.raises(ValueError, match=reason):
        build_operator_request(task, decision)


def test_ambiguous_and_route_overrides_cannot_submit(task, decision):
    decision["classification"]["ambiguous"] = True
    with pytest.raises(ValueError, match="ambiguous"):
        build_operator_request(task, decision)
    decision["classification"]["ambiguous"] = False
    decision["intent"]["route"] = {"account_id": "account-b"}
    with pytest.raises(ValueError, match="route"):
        build_operator_request(task, decision)


@pytest.mark.parametrize("field", ["raw_message_id", "processing_run_id"])
def test_decision_is_bound_to_claimed_task(task, decision, field):
    decision[field] = "88888888-8888-4888-8888-888888888888"
    with pytest.raises(ValueError, match=field):
        build_operator_request(task, decision)


def test_mapping_tasks_and_non_telegram_identity(task, decision):
    values = vars(task).copy()
    values.update(source_platform="discord", channel_id="signals", source_message_id="msg-1")
    body = build_operator_request(values, decision)
    assert body["source_identity"]["source_platform"] == "discord"
    assert body["client_ref"] == f"shadow:{task.task_id}"
    assert body["source"] == "hermes-signal:discord"
