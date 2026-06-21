from __future__ import annotations

import pytest

from order_management.state_descriptor import legal_transition, load_state_descriptor


DESCRIPTOR = load_state_descriptor()


@pytest.mark.parametrize("domain", sorted(DESCRIPTOR["domains"]))
def test_state_machine_rejects_every_undeclared_non_self_transition(domain: str) -> None:
    descriptor = DESCRIPTOR["domains"][domain]
    states = set(descriptor["states"])
    edges = {(edge["from"], edge["to"]) for edge in descriptor["transitions"]}
    terminal = {state for state, meta in descriptor["states"].items() if meta.get("terminal")}
    any_state_targets = set(descriptor.get("any_state_to", {}).get("targets", []))
    illegal_pairs: list[tuple[str, str]] = []

    for source in states:
        for target in states:
            expected = (
                source == target
                or (source not in terminal and (source, target) in edges)
                or (source not in terminal and target in any_state_targets)
            )
            assert legal_transition(domain, source, target) is expected, (domain, source, target)
            if not expected:
                illegal_pairs.append((source, target))

    assert illegal_pairs, f"{domain} should have explicit illegal transitions"


@pytest.mark.parametrize("domain", sorted(DESCRIPTOR["domains"]))
def test_terminal_states_cannot_transition_to_other_states(domain: str) -> None:
    descriptor = DESCRIPTOR["domains"][domain]
    terminal = {state for state, meta in descriptor["states"].items() if meta.get("terminal")}
    states = set(descriptor["states"])

    assert terminal, f"{domain} must define terminal states"
    for source in terminal:
        for target in states - {source}:
            assert legal_transition(domain, source, target) is False
