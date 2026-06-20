from __future__ import annotations

import contextlib
import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "ingress"))

from ingress.http import IngressHTTPHandler, _require_ingress_token  # noqa: E402


@contextlib.contextmanager
def _server(token: str | None, database_url: str | None = None):
    IngressHTTPHandler.expected_token = token
    IngressHTTPHandler.database_url = database_url
    srv = ThreadingHTTPServer(("127.0.0.1", 0), IngressHTTPHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()


def _post(port: int, headers: dict[str, str]) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/telegram/raw", body=json.dumps({"x": 1}), headers=headers)
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status


def test_require_ingress_token_fails_closed(monkeypatch):
    monkeypatch.delenv("INGRESS_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        _require_ingress_token()
    monkeypatch.setenv("INGRESS_API_TOKEN", "secret")
    assert _require_ingress_token() == "secret"


def test_missing_token_is_401():
    with _server("secret") as port:
        assert _post(port, {"content-type": "application/json"}) == 401


def test_wrong_token_is_401():
    with _server("secret") as port:
        assert _post(port, {"authorization": "Bearer nope", "content-type": "application/json"}) == 401


def test_no_configured_token_denies_all():
    # fail closed: server with no expected_token rejects everything
    with _server(None) as port:
        assert _post(port, {"authorization": "Bearer anything"}) == 401


def test_correct_token_clears_auth():
    # a valid token passes the auth gate; downstream ingest fails without a real DB,
    # but the response must NOT be 401 (auth succeeded).
    with _server("secret") as port:
        status = _post(port, {"authorization": "Bearer secret", "content-type": "application/json"})
        assert status != 401
