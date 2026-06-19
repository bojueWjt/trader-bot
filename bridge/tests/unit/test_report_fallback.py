import re

from app.services.report_fallback import build_fallback_report


def test_build_fallback_report_has_required_sections_without_numbers():
    report = build_fallback_report("2026-06-10", "snapshot unavailable")

    assert report["date"] == "2026-06-10"
    assert report["llm_used"] is False
    assert report["reason"] == "snapshot unavailable"
    assert [section["name"] for section in report["sections"]] == [
        "Account",
        "Trades",
        "Signals",
        "Risk",
        "Positions",
    ]
    for section in report["sections"]:
        assert "Data unavailable" in section["content"]
        assert re.search(r"\d", section["content"]) is None
