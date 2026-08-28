"""WP-E tests: hermes-worker deterministic backstop for unprotected opens.

Spec (docs/plans/2026-08-28-execution-state-arch-migration.md, WP-E 改动点 3,
softened per the 2026-08-28 operator directive — owner-operated account,
advisory only): an open decision whose take_profits are empty AND that has no
stop_loss proceeds normally, with the protection gap appended to
ambiguity_reasons as a note. Protected opens and non-open actions untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    REPO_ROOT / "services" / "hermes-worker",
    REPO_ROOT / "services" / "control-plane" / "db",
    REPO_ROOT / "services" / "control-plane",
):
    sys.path.insert(0, str(_p))

from worker import _enforce_action_safety  # noqa: E402


def _open_decision(take_profits, stop_loss, *, include_take_profits_key=True):
    intent = {
        "target_position_id": None,
        "stop_loss": stop_loss,
    }
    if include_take_profits_key:
        intent["take_profits"] = take_profits
    return {
        "classification": {
            "message_type": "new_signal",
            "action": "open_position",
            "ambiguous": False,
            "ambiguity_reasons": [],
        },
        "intent": intent,
    }


def _protection_reason_present(reasons):
    return any(
        ("take_profit" in reason)
        or ("take profit" in reason)
        or ("stop" in reason)
        or ("protection" in reason)
        for reason in (r.lower() for r in reasons)
    )


def test_open_without_take_profits_and_stop_loss_notes_but_proceeds():
    # 2026-08-28 operator directive (owner-operated account): the gap is
    # recorded on the decision, but the open is NOT held for review.
    d = _open_decision(take_profits=[], stop_loss=None)
    _enforce_action_safety(d)
    assert d["classification"]["ambiguous"] is False
    reasons = d["classification"]["ambiguity_reasons"]
    assert reasons, "empty-protection open must still record the gap"
    assert _protection_reason_present(reasons)


def test_open_with_missing_take_profits_field_notes_but_proceeds():
    # "take_profits 为空" covers a decision that never produced the field.
    d = _open_decision(
        take_profits=None,
        stop_loss=None,
        include_take_profits_key=False,
    )
    _enforce_action_safety(d)
    assert d["classification"]["ambiguous"] is False
    assert _protection_reason_present(d["classification"]["ambiguity_reasons"])


def test_open_with_stop_loss_only_is_not_marked():
    d = _open_decision(take_profits=[], stop_loss=64000)
    _enforce_action_safety(d)
    assert d["classification"]["ambiguous"] is False
    assert d["classification"]["ambiguity_reasons"] == []
    assert d["classification"]["action"] == "open_position"


def test_open_with_take_profits_is_not_marked():
    d = _open_decision(take_profits=[66000, 67250], stop_loss=None)
    _enforce_action_safety(d)
    assert d["classification"]["ambiguous"] is False
    assert d["classification"]["ambiguity_reasons"] == []
    assert d["classification"]["action"] == "open_position"


def test_fully_protected_open_is_not_marked():
    d = _open_decision(take_profits=[66000], stop_loss=64000)
    _enforce_action_safety(d)
    assert d["classification"]["ambiguous"] is False
    assert d["classification"]["ambiguity_reasons"] == []


def test_non_open_action_without_protection_is_not_marked_by_this_gate():
    # The completeness gate is open-only: a close with a target position and
    # no protection fields must not become ambiguous because of it.
    d = {
        "classification": {
            "message_type": "close_update",
            "action": "close_position",
            "ambiguous": False,
            "ambiguity_reasons": [],
        },
        "intent": {
            "target_position_id": "pos-1",
            "take_profits": [],
            "stop_loss": None,
        },
    }
    _enforce_action_safety(d)
    assert d["classification"]["ambiguous"] is False
    assert d["classification"]["ambiguity_reasons"] == []
    assert d["classification"]["action"] == "close_position"
