"""Deterministic risk governor.

Pure function over a (already schema-validated) HermesDecisionV1 dict plus current
positions and risk state. It never re-interprets natural language; it only reasons
about structured fields. Fail-closed: anything uncertain becomes needs_review and
anything out of policy becomes rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from policy import (
    NON_ACTIONABLE_ACTIONS,
    OPENING_ACTIONS,
    UPDATE_ACTIONS,
    UPDATE_MESSAGE_TYPES,
    RiskPolicy,
)


@dataclass
class RiskDecision:
    status: str  # approved | rejected | needs_review
    account_id: str | None
    instrument_id: str | None
    risk_budget: dict[str, float]
    reason: str
    checks: list[dict[str, Any]] = field(default_factory=list)


class _Eval:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def ok(self, name: str) -> None:
        self.checks.append({"name": name, "passed": True})

    def fail(self, name: str, detail: str) -> None:
        self.checks.append({"name": name, "passed": False, "detail": detail})


def evaluate(
    decision: dict[str, Any],
    *,
    positions: list[dict[str, Any]],
    risk_state: dict[str, Any] | None,
    policy: RiskPolicy,
) -> RiskDecision:
    ev = _Eval()
    classification = decision["classification"]
    intent = decision["intent"]
    action = classification["action"]
    message_type = classification["message_type"]
    instrument = intent.get("instrument_symbol")
    risk_state = risk_state or {}

    def reject(name: str, detail: str, account_id: str | None = None) -> RiskDecision:
        ev.fail(name, detail)
        return RiskDecision("rejected", account_id, instrument, {}, detail, ev.checks)

    def review(name: str, detail: str, account_id: str | None = None) -> RiskDecision:
        ev.fail(name, detail)
        return RiskDecision("needs_review", account_id, instrument, {}, detail, ev.checks)

    # 0. non-actionable / ambiguous -> human review, never auto risk
    if classification["ambiguous"] or action in NON_ACTIONABLE_ACTIONS:
        return review("actionable", "ambiguous or non-actionable decision")
    ev.ok("actionable")

    # 1. account resolution (deterministic; no guessing)
    account_id = intent.get("target_account_id") or policy.default_account_id
    if not account_id:
        return review("account_resolution", "no target account and no default")
    ev.ok("account_resolution")

    # 2. instrument whitelist + precision
    if not instrument or instrument not in policy.instrument_whitelist:
        return reject("instrument_whitelist", f"instrument {instrument} not allowed", account_id)
    if not _precision_ok(intent, policy):
        return reject("price_precision", "price exceeds allowed precision", account_id)
    ev.ok("instrument_whitelist")

    # 2.5 risk context must be present. A missing/incomplete risk_state is NEVER
    # treated as a healthy ACTIVE account — fail closed (PLAN: incomplete risk
    # snapshot -> no new risk). Only an explicit risk_state row carries a mode.
    if risk_state.get("mode") is None:
        return review(
            "risk_context",
            "risk_context_incomplete: risk_state unavailable for account/instrument",
            account_id,
        )
    ev.ok("risk_context")

    # 3. update messages must never open
    if message_type in UPDATE_MESSAGE_TYPES and action in OPENING_ACTIONS:
        return reject("update_message_cannot_open", f"{message_type} produced {action}", account_id)
    ev.ok("update_message_cannot_open")

    # 4. update actions must reference exactly one real position
    if action in UPDATE_ACTIONS:
        target = intent.get("target_position_id")
        if not target:
            return review("update_target", "update action without target_position_id", account_id)
        matches = [
            p for p in positions
            if p.get("position_id") == target
            and p.get("account_id") == account_id
            and p.get("instrument_id") == instrument
        ]
        if len(matches) != 1:
            return review("update_target", f"target matched {len(matches)} positions", account_id)
        ev.ok("update_target")

    # 5. kill switch / risk state
    mode = (risk_state.get("mode") or "ACTIVE").upper()
    if mode == "HALTED":
        return reject("kill_switch", "risk_state HALTED: no new risk", account_id)
    if mode == "REDUCING" and action in OPENING_ACTIONS:
        return reject("kill_switch", "risk_state REDUCING: opening risk blocked", account_id)
    ev.ok("kill_switch")

    # 6. geometry for opening actions
    if action in OPENING_ACTIONS:
        geometry_error = _geometry_error(intent)
        if geometry_error:
            return reject("geometry", geometry_error, account_id)
        ev.ok("geometry")

    # 7. risk units
    leverage = intent.get("leverage")
    if leverage is not None and leverage > policy.max_leverage:
        return reject("max_leverage", f"leverage {leverage} > {policy.max_leverage}", account_id)
    risk_fraction = policy.default_risk_fraction
    if risk_fraction > policy.max_risk_fraction:
        return reject("max_risk_fraction", "configured risk fraction exceeds cap", account_id)
    risk_budget = {
        "risk_fraction": risk_fraction,
        "max_notional": policy.max_notional,
        "max_leverage": min(leverage or policy.max_leverage, policy.max_leverage),
    }
    ev.ok("risk_units")

    # 8. exposure caps (only opening actions add exposure)
    if action in OPENING_ACTIONS:
        # instrument exposure = live notional already open on this account+instrument
        # (from the positions projection), not a stale/never-written risk_state counter.
        existing_notional = sum(
            float(p.get("notional", 0) or 0)
            for p in positions
            if p.get("account_id") == account_id and p.get("instrument_id") == instrument
        )
        # open_risk_fraction stays from risk_state until the projection populates it.
        existing_risk = float(risk_state.get("open_risk_fraction", 0) or 0)
        if existing_notional >= policy.max_instrument_notional:
            return reject("instrument_exposure", "instrument notional cap reached", account_id)
        if existing_risk + risk_fraction > policy.max_total_risk_fraction:
            return reject("total_risk", "total open risk fraction cap reached", account_id)
        group = policy.correlated_group_for(instrument)
        if group is not None:
            group_notional = sum(
                float(p.get("notional", 0) or 0)
                for p in positions
                if p.get("account_id") == account_id and p.get("instrument_id") in group[1:]
            )
            if group_notional >= policy.max_correlated_notional:
                return reject("correlated_exposure", f"group {group[0]} notional cap", account_id)
        ev.ok("exposure")

    return RiskDecision("approved", account_id, instrument, risk_budget, "all checks passed", ev.checks)


def _precision_ok(intent: dict[str, Any], policy: RiskPolicy) -> bool:
    entry = intent.get("entry") or {}
    for value in (entry.get("price"), entry.get("price_min"), entry.get("price_max"), intent.get("stop_loss")):
        if value is None:
            continue
        if _decimal_places(value) > policy.price_precision:
            return False
    for tp in intent.get("take_profits") or []:
        if _decimal_places(tp) > policy.price_precision:
            return False
    return True


def _decimal_places(value: float) -> int:
    text = format(float(value), "f").rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _geometry_error(intent: dict[str, Any]) -> str | None:
    side = intent.get("side")
    entry = intent.get("entry") or {}
    price = entry.get("price")
    if price is None:
        price = entry.get("price_min")
    stop_loss = intent.get("stop_loss")
    take_profits = list(intent.get("take_profits") or [])

    # geometry is only checkable with a direction, reference price and a stop
    if side is None or price is None or stop_loss is None:
        return None

    if side == "long":
        if not stop_loss < price:
            return "long stop_loss must be below entry"
        if take_profits and not all(tp > price for tp in take_profits):
            return "long take_profits must be above entry"
        if take_profits != sorted(take_profits):
            return "long take_profits must be ascending"
    elif side == "short":
        if not stop_loss > price:
            return "short stop_loss must be above entry"
        if take_profits and not all(tp < price for tp in take_profits):
            return "short take_profits must be below entry"
        if take_profits != sorted(take_profits, reverse=True):
            return "short take_profits must be descending"
    return None
