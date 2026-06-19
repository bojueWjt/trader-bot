from __future__ import annotations


REQUIRED_SECTION_NAMES = ["Account", "Trades", "Signals", "Risk", "Positions"]


def build_fallback_report(date: str, reason: str) -> dict:
    return {
        "date": date,
        "llm_used": False,
        "sections": [
            {"name": section_name, "content": "Data unavailable."}
            for section_name in REQUIRED_SECTION_NAMES
        ],
        "reason": reason,
    }
