"""Pure, fail-closed adaptation of a validated HermesDecisionV1.

This module performs no I/O and grants no user authorization. The caller must
validate the decision schema and persist the decision before sending the result.
The operator endpoint remains responsible for claim fencing, channel ownership,
live position checks and risk sizing. A client_ref groups a source message;
signal_claim.stable_action_or_leg_id identifies this decision's business action.

V1 has no close quantity or take-profit quantities, and no verified entry cost.
Consequently partial_close, replace_take_profits and move_stop_to_entry cannot
be safely adapted. Entry actions require a stop for the operator's risk sizing;
this adapter never invents notional, prices, stop losses or parent references.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any


_NO_TRADE = frozenset({"hold", "ignore", "needs_review"})
_ENTRIES = frozenset({"open_position", "add_position"})
_MANAGEMENT = frozenset({"close_position", "move_stop_loss"})
_UNSUPPORTED = {
    "partial_close": "HermesDecisionV1 has no explicit close quantity",
    "replace_take_profits": "HermesDecisionV1 has no take-profit quantities",
    "move_stop_to_entry": "HermesDecisionV1 has no verified position entry cost",
}


def _field(task: Any, name: str) -> Any:
    if isinstance(task, Mapping):
        return task.get(name)
    return getattr(task, name, None)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} requires an explicit nonempty string")
    return value


def _positive(value: Any, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} requires an explicit positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} requires a finite positive number")
    return value


def _entry(intent: dict[str, Any]) -> dict[str, Any]:
    entry = intent.get("entry")
    if not isinstance(entry, dict):
        raise ValueError("entry requires an explicit entry object")
    entry_type = entry.get("type")
    if entry_type not in {"market", "limit", "zone"}:
        raise ValueError("entry.type must explicitly be market, limit or zone")
    if entry_type == "limit":
        _positive(entry.get("price"), "entry.price")
    if entry_type == "zone":
        lower = _positive(entry.get("price_min"), "entry.price_min")
        upper = _positive(entry.get("price_max"), "entry.price_max")
        if lower > upper:
            raise ValueError("entry.price_min must not exceed entry.price_max")
    return deepcopy(entry)


def build_operator_request(
    task: Any,
    decision: dict[str, Any],
    *,
    entry_ref: str | None = None,
) -> dict[str, Any] | None:
    """Build one operator body; return None for non-trades, ValueError if unsafe.

    ``task`` is a SignalTask or a mapping containing its persisted fields.
    ``entry_ref`` must be supplied from an explicit reply/reference or verified
    attribution lookup. related_task_id denotes an edit, never a parent entry.
    Entry sizing is deliberately omitted: the operator's existing stop-based
    risk engine owns it. Absolute valid_until is preserved for ingress checking.
    """
    classification = decision.get("classification")
    if not isinstance(classification, dict):
        raise ValueError("classification is required")
    action = classification.get("action")
    if action in _NO_TRADE:
        return None
    if classification.get("ambiguous") is not False:
        raise ValueError("ambiguous trading decision cannot be submitted")
    if classification.get("message_type") in {"analysis", "noise", "ambiguous"}:
        raise ValueError("non-trading message cannot carry an executable action")
    if action in _UNSUPPORTED:
        raise ValueError(f"unsupported action {action}: {_UNSUPPORTED[action]}")
    if action not in _ENTRIES | _MANAGEMENT:
        raise ValueError(f"unsupported action: {action}")

    identity = {}
    for name in (
        "source_platform", "channel_id", "source_message_id", "edit_version", "account_id"
    ):
        value = _text(_field(task, name), f"task.{name}")
        if "|" in value:
            raise ValueError(f"task.{name} contains the business identity delimiter")
        identity[name] = value
    account_id = identity["account_id"]
    if account_id == "unassigned":
        raise ValueError("task.account_id must be assigned")
    intent = decision.get("intent")
    if not isinstance(intent, dict):
        raise ValueError("intent is required")
    if intent.get("account_scope") != "single":
        raise ValueError("account_scope must be single for an account-scoped task")
    if intent.get("target_account_id") != account_id:
        raise ValueError("target_account_id must match task.account_id")
    # Route overrides are outside V1. Never silently accept an enriched payload
    # that can be mistaken for authority to select a different destination.
    if "route" in decision or "route" in intent:
        raise ValueError("route override is unsupported; task owns account routing")

    task_id = _text(_field(task, "task_id"), "task.task_id")
    run_id = _text(_field(task, "processing_run_id"), "task.processing_run_id")
    raw_message_id = _text(_field(task, "raw_message_id"), "task.raw_message_id")
    claim_token = _text(_field(task, "claim_token"), "task.claim_token")
    attempt = _field(task, "attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("task.attempt requires a positive integer")
    if decision.get("processing_run_id") != run_id:
        raise ValueError("decision.processing_run_id must match task.processing_run_id")
    if decision.get("raw_message_id") != raw_message_id:
        raise ValueError("decision.raw_message_id must match task.raw_message_id")
    decision_id = _text(decision.get("decision_id"), "decision.decision_id")

    raw_symbol = _text(intent.get("instrument_symbol"), "instrument_symbol")
    match = re.fullmatch(r"([A-Z0-9]+USDT)(?:\.PERP(?:\.BINANCE)?)?", raw_symbol)
    if match is None:
        raise ValueError("instrument_symbol must explicitly identify a USDT instrument")
    side = intent.get("side")
    if side not in {"long", "short"}:
        raise ValueError("side must explicitly identify long or short")
    channel = identity["channel_id"]
    message_id = identity["source_message_id"]
    numeric_channel = channel.lstrip("-")
    if numeric_channel.isdigit() and message_id.isdigit():
        client_ref = f"tg-sig-c{numeric_channel}-m{message_id}"
    else:
        client_ref = f"shadow:{task_id}"
    stable_action_id = "|".join([*identity.values(), action])
    body = {
        "account_id": account_id,
        "action": action,
        "intended_action": action,
        "symbol": match.group(1),
        "side": side,
        "reason": f"Validated Hermes decision {decision_id} for signal task {task_id}",
        "source": f"hermes-signal:{identity['source_platform']}",
        "created_by_service": f"hermes-signal:{identity['source_platform']}",
        "source_message_id": message_id,
        "source_channel": channel,
        "channel": channel,
        "source_identity": identity,
        "authorized_by_type": "channel",
        "authorized_by_id": channel,
        "client_ref": client_ref,
        "decision_id": decision_id,
        "raw_message_id": raw_message_id,
        "signal_claim": {
            "task_id": task_id,
            "processing_run_id": run_id,
            "claim_token": claim_token,
            "attempt": attempt,
            "stable_action_or_leg_id": stable_action_id,
        },
    }
    target_position_id = intent.get("target_position_id")
    if target_position_id is not None:
        body["target_position_id"] = _text(target_position_id, "target_position_id")
    if intent.get("valid_until") is not None:
        body["valid_until"] = _text(intent["valid_until"], "valid_until")
    if entry_ref is not None:
        body["entry_ref"] = _text(entry_ref, "entry_ref")

    if action in _ENTRIES:
        body["entry"] = _entry(intent)
        body["stop_loss"] = _positive(intent.get("stop_loss"), "stop_loss for risk sizing")
        take_profits = intent.get("take_profits")
        if not isinstance(take_profits, list):
            raise ValueError("take_profits must be a list")
        body["take_profits"] = [
            _positive(price, "take_profits[]") for price in take_profits
        ]
        leverage = intent.get("leverage")
        if leverage is not None:
            body["leverage"] = _positive(leverage, "leverage")
    else:
        body["entry_ref"] = _text(entry_ref, "management entry_ref")
        body["position_side"] = side
        if action == "move_stop_loss":
            body["stop_loss"] = _positive(intent.get("stop_loss"), "stop_loss")
    return body
