from __future__ import annotations

import json
from collections import deque
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any


_DESCRIPTOR_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "contracts"
    / "v1"
    / "order_state.v1.json"
)


@lru_cache(maxsize=1)
def load_state_descriptor() -> dict[str, Any]:
    with _DESCRIPTOR_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def domain_descriptor(domain: str) -> dict[str, Any]:
    return load_state_descriptor()["domains"][domain]


def event_target(
    domain: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    current_quantity: Decimal | None = None,
) -> str | None:
    descriptor = domain_descriptor(domain)
    mapped = descriptor.get("event_state_map", {}).get(event_type)
    if mapped is None:
        return None
    if mapped == "self":
        return "self"
    if mapped != "derive":
        return str(mapped)
    if domain == "order" and event_type == "OrderFilled":
        return _derive_order_fill_state(payload)
    if domain == "position" and event_type == "PositionChanged":
        return _derive_position_changed_state(payload, current_quantity=current_quantity)
    raise ValueError(f"no derive rule for {domain}.{event_type}")


def legal_transition(domain: str, current: str, target: str) -> bool:
    if current == target:
        return True
    descriptor = domain_descriptor(domain)
    if _state_is_terminal(descriptor, current):
        return False
    for edge in descriptor.get("transitions", []):
        if edge["from"] == current and edge["to"] == target:
            return True
    return target in set(descriptor.get("any_state_to", {}).get("targets", []))


def transition_target(domain: str, current: str | None, trigger: str) -> str | None:
    descriptor = domain_descriptor(domain)
    matches = []
    for edge in descriptor.get("transitions", []):
        if edge.get("trigger") != trigger:
            continue
        if current is not None and edge.get("from") != current:
            continue
        matches.append(edge["to"])
    unique_targets = set(matches)
    if len(unique_targets) == 1:
        return matches[0]
    if current is None and not matches:
        all_trigger_targets = {
            edge["to"]
            for edge in descriptor.get("transitions", [])
            if edge.get("trigger") == trigger
        }
        if len(all_trigger_targets) == 1:
            return next(iter(all_trigger_targets))
    return None


def state_rank(domain: str, state: str | None) -> int:
    if state is None:
        return -1
    descriptor = domain_descriptor(domain)
    initial = descriptor["initial"]
    graph: dict[str, list[str]] = {}
    for edge in descriptor.get("transitions", []):
        graph.setdefault(edge["from"], []).append(edge["to"])
    ranks = {initial: 0}
    queue: deque[str] = deque([initial])
    while queue:
        node = queue.popleft()
        for nxt in graph.get(node, []):
            if nxt not in ranks:
                ranks[nxt] = ranks[node] + 1
                queue.append(nxt)
    for interrupt in descriptor.get("any_state_to", {}).get("targets", []):
        ranks.setdefault(interrupt, 10_000)
    return ranks.get(state, 10_000)


def halted_action_allowed(action: str, mode: str = "HALTED") -> bool:
    matrix = load_state_descriptor()["halted_semantics"]["permission_matrix"]
    return bool(matrix[str(mode).upper()][action])


def opening_actions() -> set[str]:
    return set(load_state_descriptor()["halted_semantics"]["opening_actions"])


def reduce_only_actions() -> set[str]:
    return set(load_state_descriptor()["halted_semantics"]["reduce_only_actions"])


def _derive_order_fill_state(payload: dict[str, Any]) -> str:
    quantity = _decimal(payload.get("quantity") or payload.get("qty"))
    filled = _decimal(payload.get("filled_qty"))
    leaves = _decimal(payload.get("leaves_qty"))
    if leaves is not None and leaves <= 0:
        return "filled"
    if quantity is not None and filled is not None and filled >= quantity:
        return "filled"
    return "partially_filled"


def _derive_position_changed_state(
    payload: dict[str, Any],
    *,
    current_quantity: Decimal | None,
) -> str:
    quantity = _decimal(payload.get("quantity") or payload.get("qty"))
    if quantity is not None and current_quantity is not None:
        if abs(quantity) < abs(current_quantity):
            return "reducing"
    return "open"


def _state_is_terminal(descriptor: dict[str, Any], state: str) -> bool:
    return bool(descriptor["states"].get(state, {}).get("terminal"))


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
