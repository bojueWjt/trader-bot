import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "hermes-profile"
    / "skills"
    / "trading"
    / "v3-trader"
    / "scripts"
    / "v3_report.py"
)
SPEC = importlib.util.spec_from_file_location("v3_report", SCRIPT)
v3_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v3_report)


def test_publish_prints_returned_url(monkeypatch, capsys, tmp_path):
    payload = tmp_path / "payload.json"
    payload.write_text('{"type":"daily","channel_views":[]}', encoding="utf-8")

    def fake_request_json(url, method, body, token, content_type="application/json"):
        assert method == "POST"
        assert url == "http://127.0.0.1:8090/reports"
        assert token is None
        assert content_type == "application/json"
        assert body
        return {"url": "https://hk.balen.wang/reports/a.html"}

    monkeypatch.setattr(v3_report, "request_json", fake_request_json)
    code = v3_report.main(["publish", "--type", "daily", "--json", str(payload)])

    assert code == 0
    assert capsys.readouterr().out.strip() == "https://hk.balen.wang/reports/a.html"


def test_upload_rejects_missing_file(tmp_path, capsys):
    code = v3_report.main(["upload", str(tmp_path / "missing.png")])

    assert code == 2
    assert "not found" in capsys.readouterr().err

