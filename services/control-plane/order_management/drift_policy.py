from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class DriftDecision:
    action: str
    next_state: str
    mode: str
    reason: str
    payload: dict[str, Any]


class DriftPolicyEngine:
    def __init__(
        self,
        *,
        policy: str,
        safe_adopt_lifecycle_roles: Iterable[str] = (),
    ) -> None:
        if policy not in {"adopt", "cancel", "review", "halt"}:
            raise ValueError("drift policy must be adopt, cancel, review, or halt")
        self.policy = policy
        self.safe_adopt_lifecycle_roles = set(safe_adopt_lifecycle_roles)

    def handle_orphan_order(self, order: dict[str, Any]) -> DriftDecision:
        if not _order_identity_resolvable(order):
            return _needs_review(order, "unresolvable_order_identity")
        if self.policy == "adopt":
            role = str(order.get("lifecycle_role") or "")
            if role in self.safe_adopt_lifecycle_roles:
                return DriftDecision("adopt", "adopted", "ACTIVE", "safe_whitelist", order)
            return _needs_review(order, "adopt_not_whitelisted")
        if self.policy == "cancel":
            return DriftDecision("cancel", "cancel_requested", "REDUCING", "policy_cancel", order)
        if self.policy == "halt":
            return DriftDecision("halt", "HALTED", "HALTED", "policy_halt", order)
        return _needs_review(order, "policy_review")

    def handle_external_position(self, position: dict[str, Any]) -> DriftDecision:
        if not _position_identity_resolvable(position):
            return _needs_review(position, "unresolvable_position_identity")
        if self.policy == "adopt":
            role = str(position.get("lifecycle_role") or "external_position")
            if role in self.safe_adopt_lifecycle_roles:
                return DriftDecision("adopt", "adopted", "ACTIVE", "safe_whitelist", position)
            return _needs_review(position, "adopt_not_whitelisted")
        if self.policy == "cancel":
            return DriftDecision("cancel", "close_requested", "REDUCING", "policy_cancel", position)
        if self.policy == "halt":
            return DriftDecision("halt", "HALTED", "HALTED", "policy_halt", position)
        return _needs_review(position, "policy_review")


def _order_identity_resolvable(order: dict[str, Any]) -> bool:
    return bool(
        order.get("venue_symbol")
        and (order.get("client_order_id") or order.get("venue_order_id"))
    )


def _position_identity_resolvable(position: dict[str, Any]) -> bool:
    return bool(position.get("position_key") or position.get("venue_symbol"))


def _needs_review(payload: dict[str, Any], reason: str) -> DriftDecision:
    return DriftDecision("review", "needs_review", "HALTED", reason, payload)
