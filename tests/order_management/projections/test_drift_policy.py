from __future__ import annotations

import pytest

from order_management.drift_policy import DriftPolicyEngine


def test_drift_policy_adopts_only_safe_whitelisted_orphans() -> None:
    engine = DriftPolicyEngine(policy="adopt", safe_adopt_lifecycle_roles={"entry"})

    safe = engine.handle_orphan_order(
        {"client_order_id": "coid-1", "venue_symbol": "BTCUSDT", "lifecycle_role": "entry"}
    )
    unsafe = engine.handle_orphan_order(
        {"client_order_id": "stop-1", "venue_symbol": "BTCUSDT", "lifecycle_role": "stop_loss"}
    )

    assert safe.action == "adopt"
    assert safe.next_state == "adopted"
    assert unsafe.action == "review"
    assert unsafe.next_state == "needs_review"


@pytest.mark.parametrize(
    ("policy", "expected_action", "expected_state"),
    [
        ("cancel", "cancel", "cancel_requested"),
        ("review", "review", "needs_review"),
        ("halt", "halt", "HALTED"),
    ],
)
def test_drift_policy_paths(policy: str, expected_action: str, expected_state: str) -> None:
    engine = DriftPolicyEngine(policy=policy)

    decision = engine.handle_orphan_order(
        {"client_order_id": "coid-1", "venue_symbol": "BTCUSDT", "lifecycle_role": "entry"}
    )

    assert decision.action == expected_action
    assert decision.next_state == expected_state


def test_unresolvable_identity_stays_halted_and_needs_review() -> None:
    engine = DriftPolicyEngine(policy="adopt", safe_adopt_lifecycle_roles={"entry"})

    decision = engine.handle_external_position({"quantity": "1"})

    assert decision.action == "review"
    assert decision.next_state == "needs_review"
    assert decision.mode == "HALTED"
