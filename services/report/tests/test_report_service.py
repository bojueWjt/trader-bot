import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("report_service", ROOT / "report_service.py")
report_service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report_service)


def base_payload():
    return {
        "type": "daily",
        "date": "2026-07-12",
        "title": "Daily",
        "sections": {"overview_md": "Intro **risk**\n\n- one\n- <two>"},
        "channel_views": [
            {
                "channel": "C02",
                "trader": "舒琴",
                "stance": "mixed",
                "summary": "BTC range-bound",
                "symbols": ["BTCUSDT"],
            }
        ],
        "images": [],
        "extra_metrics": [],
    }


def test_render_markdown_supports_paragraphs_bold_and_lists():
    html = report_service.render_markdown("Intro **risk**\n\n- one\n- <two>")

    assert "<p>Intro <strong>risk</strong></p>" in html
    assert "<ul><li>one</li><li>&lt;two&gt;</li></ul>" in html


def test_validate_payload_requires_known_type_and_valid_channel_views():
    with pytest.raises(report_service.ReportValidationError):
        report_service.normalize_payload({"type": "monthly"})

    with pytest.raises(report_service.ReportValidationError):
        report_service.normalize_payload({"type": "daily", "channel_views": [{"channel": "c"}]})

    payload = report_service.normalize_payload(base_payload())
    assert payload["type"] == "daily"
    assert payload["date"] == "2026-07-12"


def test_fetch_report_data_degrades_when_psycopg2_connect_fails(monkeypatch):
    class BrokenPsycopg:
        @staticmethod
        def connect(_dsn):
            raise RuntimeError("database offline")

    monkeypatch.setattr(report_service, "psycopg2", BrokenPsycopg())
    data = report_service.fetch_report_data("daily", "2026-07-12", "postgres://example")

    assert data["missing_data"]
    assert "database offline" in data["missing_data"][0]
    assert data["outcomes"]["trades"] == []


def test_html_injection_escapes_script_end():
    payload = base_payload()
    payload["title"] = "</script><script>alert(1)</script>"
    html = report_service.render_report_html(payload, report_service.empty_db_data())

    assert "window.REPORT_DATA =" in html
    assert "</script><script>alert(1)</script>" not in html
    assert "<\\/script>" in html


def test_write_report_uses_random_tokenized_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE", "https://hk.balen.wang")
    payload = base_payload()

    first = report_service.write_report(payload, report_service.empty_db_data(), tmp_path)
    second = report_service.write_report(payload, report_service.empty_db_data(), tmp_path)

    assert first["path"] != second["path"]
    assert Path(first["path"]).name.startswith("2026-07-12-daily-")
    assert first["url"].startswith("https://hk.balen.wang/reports/")


def test_asset_validation_accepts_only_supported_images_under_10mb():
    assert report_service.validate_asset("image/png", 10) == ".png"
    assert report_service.validate_asset("image/jpeg", 10) == ".jpg"
    assert report_service.validate_asset("image/webp", 10) == ".webp"

    with pytest.raises(report_service.ReportValidationError):
        report_service.validate_asset("image/gif", 10)

    with pytest.raises(report_service.ReportValidationError):
        report_service.validate_asset("image/jpeg", 10 * 1024 * 1024 + 1)


def test_json_for_script_never_contains_raw_script_end():
    text = report_service.json_for_script({"x": "</script><script>alert(1)</script>"})

    assert "</script>" not in text.lower()
    assert "<\\/script>" in text

