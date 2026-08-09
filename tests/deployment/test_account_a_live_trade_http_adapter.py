from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / "scripts" / "account_a_live_trade_http_adapter.py"
RISK_TOKEN = "risk-admin-test-token"
NODE_TOKEN = "account-a-node-test-token"
NODE_ID = "nautilus-node-account-a"
ACCOUNT_ID = "account-a"
SYMBOL = "SOLUSDT"
OPEN_INTENT_ID = "11111111-1111-4111-8111-111111111111"
CLOSE_INTENT_ID = "22222222-2222-4222-8222-222222222222"
OPEN_CLIENT_ORDER_ID = "B1111111111114111811111111111111101"
CLOSE_CLIENT_ORDER_ID = "B2222222222224222822222222222222299"
EMPTY_PORTFOLIO_SHA256 = (
    "6ae771d5d317b151109d3933059c0c46ec02368fc826444c9a805bdaa775813f"
)


class Scenario:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.node_state = "HALTED"

    def handle(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | bool,
    ) -> tuple[int, dict[str, Any]]:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "headers": headers,
                "body": body,
            }
        )
        if method == "POST" and path == "/v1/commands":
            assert headers["authorization"] == f"Bearer {RISK_TOKEN}"
            assert isinstance(body, dict)
            self.node_state = str(body["type"])
            if self.node_state == "RESUME":
                self.node_state = "ACTIVE"
            return 200, {"command_id": "command-1", "status": "pending"}
        if method == "GET" and path == "/v1/nodes":
            assert headers["authorization"] == f"Bearer {RISK_TOKEN}"
            return 200, {
                "nodes": [
                    {
                        "node_id": NODE_ID,
                        "account_id": ACCOUNT_ID,
                        "trading_state": self.node_state,
                        "readiness": True,
                        "last_heartbeat_at": _now(),
                        "projection_lag_ms": 0,
                        "reconciliation_state": "healthy",
                    }
                ]
            }
        raise AssertionError(f"unexpected request: {method} {path}")


class FakeControlPlane:
    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario
        scenario_ref = scenario

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._dispatch()

            def do_POST(self) -> None:
                self._dispatch()

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _dispatch(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length)
                body: dict[str, Any] | bool = False
                if raw_body:
                    body = json.loads(raw_body)
                path = urlsplit(self.path).path
                headers = {
                    key.lower(): value
                    for key, value in self.headers.items()
                }
                status, payload = scenario_ref.handle(
                    self.command,
                    path,
                    headers,
                    body,
                )
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeControlPlane:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def test_resume_posts_command_polls_fresh_active_and_hashes_evidence(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "resume",
            _request(
                command="RESUME",
                scope="single-canary-round-trip",
                max_round_trips=1,
                side_effect_id="resume-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["accepted"] is True
    assert payload["action"] == "resume"
    assert payload["status"] == "READY"
    assert payload["trading_state"] == "ACTIVE"
    assert payload["source"] == "node"
    assert payload["side_effect_id"] == "resume-request-id"
    assert _canonical_sha256_without_evidence(payload) == payload[
        "evidence_sha256"
    ]
    command = scenario.requests[0]
    assert command["method"] == "POST"
    assert command["path"] == "/v1/commands"
    assert command["headers"]["x-request-id"] == "resume-request-id"
    assert command["body"]["scope"]["executor_identity"] == _identity()


def _invoke(
    action: str,
    request: dict[str, Any],
    tmp_path: Path,
    server_url: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    risk_path = tmp_path / "risk.token"
    node_path = tmp_path / "node.token"
    risk_path.write_text(RISK_TOKEN + "\n", encoding="utf-8")
    node_path.write_text(NODE_TOKEN + "\n", encoding="utf-8")
    risk_path.chmod(0o600)
    node_path.chmod(0o400)
    environment = os.environ.copy()
    environment.update(
        {
            "ACCOUNT_A_LIVE_TRADE_CONTROL_PLANE_URL": server_url,
            "ACCOUNT_A_LIVE_TRADE_RISK_ADMIN_TOKEN_FILE": str(risk_path),
            "ACCOUNT_A_LIVE_TRADE_NODE_TOKEN_FILE": str(node_path),
            "ACCOUNT_A_LIVE_TRADE_NODE_ID": NODE_ID,
            "ACCOUNT_A_LIVE_TRADE_HTTP_TIMEOUT_SECONDS": "0.2",
            "ACCOUNT_A_LIVE_TRADE_HTTP_MAX_ATTEMPTS": "2",
            "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0.01",
            "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "1",
            "ACCOUNT_A_LIVE_TRADE_FRESHNESS_SECONDS": "5",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(ADAPTER), action],
        input=json.dumps(request, sort_keys=True) + "\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=environment,
        cwd=REPO_ROOT,
    )
    payload = json.loads(completed.stdout)
    return completed, payload


def _request(**overrides: Any) -> dict[str, Any]:
    payload = {
        **_identity(),
        "mode": "live",
        "authorization_sha256": "5" * 64,
        "document_sha256": {
            "release_gate": "6" * 64,
            "safety_gate": "7" * 64,
            "emergency_close_gate": "8" * 64,
            "permit": "9" * 64,
        },
        "portfolio_baseline_sha256": EMPTY_PORTFOLIO_SHA256,
    }
    payload.update(overrides)
    return payload


def _identity() -> dict[str, str]:
    return {
        "account_id": ACCOUNT_ID,
        "symbol": SYMBOL,
        "release_id": "release-account-a-test",
        "image_digest": f"sha256:{'1' * 64}",
        "config_sha256": "2" * 64,
        "dependency_lock_sha256": "3" * 64,
        "permit_id": "permit-account-a-test",
        "intent_id": OPEN_INTENT_ID,
    }


def _canonical_sha256_without_evidence(payload: dict[str, Any]) -> str:
    canonical_payload = dict(payload)
    canonical_payload.pop("evidence_sha256")
    encoded = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
