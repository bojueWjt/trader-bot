"""Batch 1.1 P1-9: the prompt must not escalate missing SL/TP into a block.

Operator directive (2026-08-28, own account): a missing-protection open is a
note in ambiguity_reasons only. The prompt must not instruct the model to set
classification.ambiguous=true or route to needs_review for that reason alone
(ambiguous=true feeds the governor's needs_review block).
"""

from __future__ import annotations

import re

from prompt import SYSTEM_PROMPT


def _protection_rule_line() -> str:
    lines = [
        line
        for line in SYSTEM_PROMPT.splitlines()
        if "stop_loss" in line and "take_profits" in line and line.lstrip().startswith("-")
    ]
    assert lines, "prompt must keep a rule covering opens without stop_loss/take_profits"
    assert len(lines) == 1, f"expected one protection rule line, got: {lines!r}"
    return lines[0]


def test_missing_protection_rule_is_note_only():
    line = _protection_rule_line()
    assert "ambiguity_reasons" in line, "the gap must still be recorded as a note"
    assert "NOT set classification.ambiguous=true" in line
    assert "needs_review" in line and "NOT" in line


def test_missing_protection_rule_never_instructs_ambiguous_true():
    line = _protection_rule_line()
    # No affirmative "set ... ambiguous=true" instruction may remain.
    assert not re.search(r"(?<!NOT )set classification\.ambiguous=true", line)
