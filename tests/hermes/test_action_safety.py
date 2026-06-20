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


def _decision(message_type, action, target_position_id=None):
    return {
        "classification": {
            "message_type": message_type,
            "action": action,
            "ambiguous": False,
            "ambiguity_reasons": [],
        },
        "intent": {"target_position_id": target_position_id},
    }


def test_update_message_open_is_coerced_to_needs_review():
    d = _decision("position_update", "open_position")
    _enforce_action_safety(d)
    assert d["classification"]["action"] == "needs_review"
    assert any("cannot open" in r for r in d["classification"]["ambiguity_reasons"])


def test_close_without_target_is_coerced():
    d = _decision("close_update", "close_position", target_position_id=None)
    _enforce_action_safety(d)
    assert d["classification"]["action"] == "needs_review"


def test_move_stop_without_target_is_coerced():
    d = _decision("position_update", "move_stop_to_entry", target_position_id=None)
    _enforce_action_safety(d)
    assert d["classification"]["action"] == "needs_review"


def test_valid_open_is_untouched():
    d = _decision("new_signal", "open_position")
    _enforce_action_safety(d)
    assert d["classification"]["action"] == "open_position"


def test_valid_close_with_target_is_untouched():
    d = _decision("close_update", "close_position", target_position_id="pos-1")
    _enforce_action_safety(d)
    assert d["classification"]["action"] == "close_position"
