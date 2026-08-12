from __future__ import annotations

import json
import importlib.util
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_ROOT = REPO_ROOT / "services" / "nautilus-node"
MODULE_PATH = NODE_ROOT / "app" / "health_server.py"
SPEC = importlib.util.spec_from_file_location("node_health_server", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
health_server = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = health_server
SPEC.loader.exec_module(health_server)
build_health_server = health_server.build_health_server
build_release_version_response = health_server.build_release_version_response


class _Response:
    status_code = 200
    body = {"ok": True}


class _Health:
    def liveness(self) -> _Response:
        return _Response()

    def readiness(self) -> _Response:
        return _Response()


class _Runtime:
    health = _Health()


RELEASE_ENV = {
    "TRADER_RELEASE_COMMIT": "a" * 40,
    "TRADER_RELEASE_IMAGE_DIGEST": "sha256:" + ("b" * 64),
    "TRADER_RELEASE_ID": "c" * 64,
    "TRADER_RELEASE_DEPENDENCY_LOCK_SHA256": "d" * 64,
    "TRADER_RELEASE_CONFIG_SHA256": "e" * 64,
    "TRADER_RELEASE_SCHEMA_EPOCH": "trader-v3-release/v2",
}


def test_version_response_requires_complete_release_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in RELEASE_ENV:
        monkeypatch.delenv(key, raising=False)

    response = build_release_version_response()

    assert response.status_code == 503
    assert response.body["complete"] is False
    assert set(response.body["missing"]) == {
        "git_sha",
        "image_digest",
        "build_id",
        "dependency_lock_sha256",
        "config_sha256",
        "schema_epoch",
    }


def test_version_endpoint_exposes_reviewed_release_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in RELEASE_ENV.items():
        monkeypatch.setenv(key, value)
    started_at = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)
    response = build_release_version_response(started_at=started_at)

    assert response.status_code == 200
    assert response.body == {
        "git_sha": RELEASE_ENV["TRADER_RELEASE_COMMIT"],
        "image_digest": RELEASE_ENV["TRADER_RELEASE_IMAGE_DIGEST"],
        "build_id": RELEASE_ENV["TRADER_RELEASE_ID"],
        "dependency_lock_sha256": RELEASE_ENV[
            "TRADER_RELEASE_DEPENDENCY_LOCK_SHA256"
        ],
        "config_sha256": RELEASE_ENV["TRADER_RELEASE_CONFIG_SHA256"],
        "schema_epoch": RELEASE_ENV["TRADER_RELEASE_SCHEMA_EPOCH"],
        "started_at": "2026-08-08T08:00:00+00:00",
        "complete": True,
        "missing": [],
    }

    server = build_health_server(_Runtime(), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urlopen(f"http://127.0.0.1:{port}/version", timeout=2) as raw:
            payload = json.load(raw)
        assert payload["complete"] is True
        assert payload["git_sha"] == RELEASE_ENV["TRADER_RELEASE_COMMIT"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
