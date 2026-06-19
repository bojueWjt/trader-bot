from app.services.secret_scanner import (
    check_freqtrade_api_binding,
    redact_text,
    scan_text_for_secrets,
)


def test_frontend_bundle_secret_scan_finds_exchange_key():
    text = "window.__APP__ = { BINANCE_API_KEY: 'abc123456789abc123456789abc12345' }"

    result = scan_text_for_secrets(text, source="bundle.js")

    assert result["passed"] is False
    assert result["findings"][0]["type"] == "api_key"


def test_report_secret_scan_passes_clean_report():
    result = scan_text_for_secrets("Daily report pnl is 1.2%.", source="daily.md")

    assert result["passed"] is True
    assert result["findings"] == []


def test_log_redaction_masks_sensitive_values():
    text = "password=hunter2 jwt=eyJabc secret: live-secret"

    redacted = redact_text(text)

    assert "hunter2" not in redacted
    assert "eyJabc" not in redacted
    assert "live-secret" not in redacted


def test_freqtrade_api_cannot_bind_publicly():
    assert check_freqtrade_api_binding("0.0.0.0:8080")["passed"] is False
    assert check_freqtrade_api_binding("[::]:8080")["passed"] is False
    assert check_freqtrade_api_binding("http://0.0.0.0:8080")["passed"] is False
    assert check_freqtrade_api_binding("127.0.0.1:8080")["passed"] is True
