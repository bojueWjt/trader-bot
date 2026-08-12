from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid5

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / "scripts" / "account_a_live_trade_http_adapter.py"
RISK_TOKEN = "risk-admin-test-token"
NODE_TOKEN = "account-a-node-test-token"
NODE_ID = "nautilus-node-account-a"
WRITER_ID = "writer-account-a"
LEASE_ID = "lease-account-a"
FENCING_EPOCH = 42
ACCOUNT_ID = "account-a"
ROLLOUT_PHASE = "account_a_canary"
SYMBOL = "SOLUSDT"
OPEN_INTENT_ID = "11111111-1111-4111-8111-111111111111"
CLOSE_INTENT_ID = str(
    uuid5(
        UUID(OPEN_INTENT_ID),
        "trader-v3/account-a/SOLUSDT/close",
    )
)
OPEN_CLIENT_ORDER_ID = "B1111111111114111811111111111111101"
CLOSE_CLIENT_ORDER_ID = f"B{UUID(CLOSE_INTENT_ID).hex}01"
EMPTY_PORTFOLIO_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "positions": [],
            "open_orders": [],
            "algo_orders": [],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
).hexdigest()


@pytest.mark.parametrize(
    ("account_id", "rollout_phase", "environment_prefix"),
    [
        ("account-a", "account_a_canary", "ACCOUNT_A_LIVE_TRADE"),
        ("account-b", "account_b_rollout", "ACCOUNT_B_LIVE_TRADE"),
        ("account-c", "account_c_rollout", "ACCOUNT_C_LIVE_TRADE"),
        ("account-d", "account_d_rollout", "ACCOUNT_D_LIVE_TRADE"),
    ],
)
def test_config_selects_restricted_account_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account_id: str,
    rollout_phase: str,
    environment_prefix: str,
) -> None:
    namespace = runpy.run_path(str(ADAPTER))
    config_type = namespace["Config"]
    adapter_type = namespace["AccountALiveTradeHttpAdapter"]
    adapter_error = namespace["AdapterError"]
    risk_token = f"risk-token-{account_id}"
    node_token = f"node-token-{account_id}"
    risk_path = tmp_path / f"{account_id}-risk.token"
    node_path = tmp_path / f"{account_id}-node.token"
    risk_path.write_text(risk_token + "\n", encoding="ascii")
    node_path.write_text(node_token + "\n", encoding="ascii")
    risk_path.chmod(0o600)
    node_path.chmod(0o400)
    node_id = f"nautilus-node-{account_id}"
    monkeypatch.setenv("HARDENED_CANARY_ACCOUNT_ID", account_id)
    monkeypatch.setenv(
        f"{environment_prefix}_RISK_ADMIN_TOKEN_FILE",
        str(risk_path),
    )
    monkeypatch.setenv(
        f"{environment_prefix}_NODE_TOKEN_FILE",
        str(node_path),
    )
    monkeypatch.setenv(
        f"{environment_prefix}_NODE_ID",
        node_id,
    )

    config = config_type.from_environment()

    assert config.account_id == account_id
    assert config.rollout_phase == rollout_phase
    assert config.node_id == node_id
    assert config.risk_token == risk_token
    assert config.node_token == node_token
    adapter = adapter_type(config)
    conflicting_request = _request()
    conflicting_request["account_id"] = "account-a"
    conflicting_request["rollout_phase"] = "account_a_canary"
    if account_id == "account-a":
        conflicting_request["account_id"] = "account-b"
        conflicting_request["rollout_phase"] = "account_b_rollout"
    with pytest.raises(
        adapter_error,
        match="request account differs from selected target",
    ):
        adapter.dispatch("preflight", conflicting_request)


class Scenario:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.node_state = "HALTED"
        self.forced_responses: dict[
            tuple[str, str],
            tuple[int, dict[str, Any]],
        ] = {}
        self.operator_statuses: dict[str, dict[str, Any]] = {}
        self.exchange_state = _exchange_state()
        self.open_exchange_state = _exchange_state(
            position_quantity="0.07",
            client_order_id=OPEN_CLIENT_ORDER_ID,
            evidence_filled_quantity="0.07",
        )
        self.open_exchange_states: list[dict[str, Any]] = []
        self.exchange_states: list[dict[str, Any]] = []
        self.node_snapshot = _node_snapshot()
        self.node_snapshots: list[dict[str, Any]] | None = None
        self.refresh_exchange_mirror_on_close = True
        self.refresh_exchange_evidence_on_close = True
        self.close_exchange_state_after_post: dict[str, Any] | None = None
        self.close_response_delay_seconds = 0.0

    def handle(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | bool,
        query: dict[str, list[str]],
    ) -> tuple[int, dict[str, Any]]:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "headers": headers,
                "body": body,
            }
        )
        forced = self.forced_responses.get((method, path))
        if forced is not None:
            return forced
        if method == "POST" and path == "/v1/commands":
            assert headers["authorization"] == f"Bearer {RISK_TOKEN}"
            assert isinstance(body, dict)
            self.node_state = str(body["type"])
            if self.node_state == "RESUME":
                self.node_state = "ACTIVE"
            if self.node_state == "HALT":
                self.node_state = "HALTED"
            return 200, {"command_id": "command-1", "status": "pending"}
        if method == "POST" and path == "/v1/operator/orders":
            assert headers["authorization"] == f"Bearer {RISK_TOKEN}"
            assert isinstance(body, dict)
            if body["action"] == "partial_close":
                close_exchange_state = self.close_exchange_state_after_post
                if close_exchange_state is not None:
                    self.exchange_state = close_exchange_state
                observed_at = datetime.now(timezone.utc).isoformat()
                if self.refresh_exchange_mirror_on_close:
                    self.exchange_state["updated_at"] = observed_at
                    exchange_payload = self.exchange_state["payload"]
                    exchange_payload["fetched_at"] = observed_at
                if self.refresh_exchange_evidence_on_close:
                    evidence = self.exchange_state[
                        "opening_execution_evidence"
                    ]
                    items = evidence["items"]
                    for item in items:
                        item["observed_at"] = observed_at
                        source_evidence = item.get("source_evidence")
                        if not isinstance(source_evidence, list):
                            continue
                        for proof in source_evidence:
                            if isinstance(proof, dict):
                                proof["observed_at"] = observed_at
                delay_seconds = self.close_response_delay_seconds
                self.close_response_delay_seconds = 0.0
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
            return 200, {
                "intent_id": body["intent_id"],
                "status": "approved",
                "account_id": body["account_id"],
                "instrument_id": body["symbol"],
                "action": body["action"],
            }
        if (
            method == "POST"
            and path == f"/v1/nodes/{NODE_ID}/loss-monitor"
        ):
            assert headers["authorization"] == f"Bearer {NODE_TOKEN}"
            assert headers["x-node-id"] == NODE_ID
            assert headers["x-account-id"] == ACCOUNT_ID
            assert isinstance(body, dict)
            return 200, {"ok": True}
        if method == "GET" and path.startswith("/v1/operator/orders/"):
            assert headers["authorization"] == f"Bearer {RISK_TOKEN}"
            intent_id = path.rsplit("/", 1)[-1]
            status = self.operator_statuses.get(intent_id)
            if status is None:
                return 404, {"detail": "intent not found"}
            return 200, status
        if (
            method == "GET"
            and path == f"/v1/nodes/{NODE_ID}/exchange-state"
        ):
            assert headers["authorization"] == f"Bearer {NODE_TOKEN}"
            assert headers["x-node-id"] == NODE_ID
            assert headers["x-account-id"] == ACCOUNT_ID
            requested_client_ids = ",".join(
                query.get("client_order_ids", [])
            )
            exchange_state = self.exchange_state
            if OPEN_CLIENT_ORDER_ID in requested_client_ids:
                if self.open_exchange_states:
                    exchange_state = self.open_exchange_states[0]
                    if len(self.open_exchange_states) > 1:
                        self.open_exchange_states.pop(0)
                elif _exchange_state_has_client_order_id(
                    self.exchange_state,
                    CLOSE_CLIENT_ORDER_ID,
                ):
                    exchange_state = self.open_exchange_state
            elif self.exchange_states:
                exchange_state = self.exchange_states[0]
                if len(self.exchange_states) > 1:
                    self.exchange_states.pop(0)
            return 200, exchange_state
        if method == "GET" and path == "/v1/nodes":
            assert headers["authorization"] == f"Bearer {RISK_TOKEN}"
            snapshots = self.node_snapshots
            if snapshots is None:
                snapshots = [self.node_snapshot]
            nodes = []
            for snapshot in snapshots:
                node_snapshot = dict(snapshot)
                if node_snapshot.get("node_id") == NODE_ID:
                    node_snapshot["trading_state"] = self.node_state
                nodes.append(node_snapshot)
            return 200, {"nodes": nodes}
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
                parsed_url = urlsplit(self.path)
                path = parsed_url.path
                query = parse_qs(parsed_url.query)
                headers = {
                    key.lower(): value
                    for key, value in self.headers.items()
                }
                status, payload = scenario_ref.handle(
                    self.command,
                    path,
                    headers,
                    body,
                    query,
                )
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    return

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
    assert command["body"]["scope"]["release_id"] == _request()["release_id"]
    assert command["body"]["scope"]["canary_permit_id"] == _request()["permit_id"]


@pytest.mark.parametrize("action", ["resume", "halt"])
def test_state_polling_allows_missing_extended_identity_with_warnings(
    tmp_path: Path,
    action: str,
) -> None:
    scenario = Scenario()
    for field_name in ("writer_id", "lease_id", "fencing_epoch"):
        scenario.node_snapshot.pop(field_name)

    command = action.upper()
    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            action,
            _request(
                command=command,
                scope="single-canary-round-trip",
                max_round_trips=1,
                side_effect_id=f"{action}-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["accepted"] is True
    assert payload["warnings"] == [
        "node identity telemetry missing: writer_id",
        "node identity telemetry missing: lease_id",
        "node identity telemetry missing: fencing_epoch",
    ]


@pytest.mark.parametrize("action", ["resume", "halt"])
@pytest.mark.parametrize(
    ("field_name", "field_value", "expected_fragment"),
    [
        ("node_id", "nautilus-node-account-b", "node node_id mismatch"),
        ("account_id", "account-b", "node account mismatch"),
        ("writer_id", "writer-account-b", "node writer_id mismatch"),
        ("lease_id", "lease-account-b", "node lease_id mismatch"),
        (
            "fencing_epoch",
            FENCING_EPOCH + 1,
            "node fencing_epoch mismatch",
        ),
    ],
)
def test_state_polling_rejects_explicit_node_identity_conflict(
    tmp_path: Path,
    action: str,
    field_name: str,
    field_value: Any,
    expected_fragment: str,
) -> None:
    scenario = Scenario()
    scenario.node_snapshot[field_name] = field_value

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            action,
            _request(
                command=action.upper(),
                scope="single-canary-round-trip",
                max_round_trips=1,
                side_effect_id=f"{action}-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert expected_fragment in completed.stderr


def test_open_posts_limit_ioc_operator_intent(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "open",
            _request(
                client_order_id=OPEN_CLIENT_ORDER_ID,
                side="BUY",
                order_type="LIMIT",
                time_in_force="IOC",
                quantity="0.07",
                limit_price_usdt="100",
                max_actual_open_notional_usdt="12",
                side_effect_id="open-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["accepted"] is True
    assert payload["action"] == "open"
    assert payload["client_order_id"] == OPEN_CLIENT_ORDER_ID
    operator_request = next(
        request
        for request in scenario.requests
        if request["method"] == "POST"
        and request["path"] == "/v1/operator/orders"
    )
    assert operator_request["path"] == "/v1/operator/orders"
    assert operator_request["headers"]["x-request-id"] == "open-request-id"
    assert operator_request["body"]["entry"] == {
        "type": "limit",
        "price": "100",
        "time_in_force": "IOC",
    }
    assert operator_request["body"]["quantity"] == "0.07"
    assert operator_request["body"]["notional_usdt"] == "7"
    assert operator_request["body"]["canary_permit_id"] == _request()["permit_id"]


def test_open_rejects_quantity_other_than_live_canary_quantity(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "open",
            _request(
                client_order_id=OPEN_CLIENT_ORDER_ID,
                side="BUY",
                order_type="LIMIT",
                time_in_force="IOC",
                quantity="0.1",
                limit_price_usdt="100",
                max_actual_open_notional_usdt="12",
                side_effect_id="open-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "OPEN quantity must equal 0.07" in completed.stderr
    assert scenario.requests == []


def test_preflight_returns_explicit_exchange_authority(
    tmp_path: Path,
) -> None:
    scenario = Scenario()

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(phase="before-open"),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["action"] == "preflight"
    assert payload["exchange_authoritative"] is True
    assert payload["source"] == "exchange"
    assert payload["mirror_stale"] is False
    assert payload["available_usdt_balance"] == "100"


def test_portfolio_baseline_ignores_non_target_position_market_refresh(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    position = {
        "symbol": "ETHUSDT",
        "position_side": "LONG",
        "position_amt": "0.25",
        "entry_price": "100",
        "mark_price": "101",
        "unrealized_pnl": "0.25",
        "notional": "25.25",
        "liquidation_price": "50",
        "break_even_price": "100.1",
        "update_time_ms": 1_754_700_000_000,
        "adl": 1,
    }
    scenario.exchange_state["payload"]["positions"] = [position]

    with FakeControlPlane(scenario) as server:
        baseline = _preflight_portfolio_baseline(
            scenario,
            tmp_path,
            server.url,
        )
        position["mark_price"] = "102"
        position["unrealized_pnl"] = "0.5"
        position["notional"] = "25.5"
        position["liquidation_price"] = "51"
        position["break_even_price"] = "100.2"
        position["update_time_ms"] = 1_754_700_001_000
        position["adl"] = 2
        refreshed_baseline = _preflight_portfolio_baseline(
            scenario,
            tmp_path,
            server.url,
        )

    assert refreshed_baseline == baseline


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("symbol", "BTCUSDT"),
        ("position_side", "SHORT"),
        ("position_amt", "0.5"),
        ("entry_price", "99"),
        ("leverage", "10"),
        ("margin_type", "isolated"),
        ("isolated_margin", "5"),
        ("is_auto_add_margin", "true"),
    ],
)
def test_portfolio_baseline_binds_non_target_position_fields(
    tmp_path: Path,
    field_name: str,
    field_value: str,
) -> None:
    scenario = Scenario()
    position = {
        "symbol": "ETHUSDT",
        "position_side": "LONG",
        "position_amt": "0.25",
        "entry_price": "100",
        "mark_price": "101",
        "unrealized_pnl": "0.25",
        "leverage": "5",
        "margin_type": "cross",
        "isolated_margin": "0",
        "is_auto_add_margin": "false",
    }
    scenario.exchange_state["payload"]["positions"] = [position]

    with FakeControlPlane(scenario) as server:
        baseline = _preflight_portfolio_baseline(
            scenario,
            tmp_path,
            server.url,
        )
        position[field_name] = field_value
        changed_baseline = _preflight_portfolio_baseline(
            scenario,
            tmp_path,
            server.url,
        )

    assert changed_baseline != baseline


def test_portfolio_baseline_ignores_unknown_non_target_position_fields(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    position = {
        "symbol": "ETHUSDT",
        "position_side": "LONG",
        "position_amt": "0.25",
        "entry_price": "100",
        "mark_price": "101",
        "unrealized_pnl": "0.25",
        "future_exchange_field": "before",
    }
    scenario.exchange_state["payload"]["positions"] = [position]

    with FakeControlPlane(scenario) as server:
        baseline = _preflight_portfolio_baseline(
            scenario,
            tmp_path,
            server.url,
        )
        position["future_exchange_field"] = "after"
        refreshed_baseline = _preflight_portfolio_baseline(
            scenario,
            tmp_path,
            server.url,
        )

    assert refreshed_baseline == baseline


@pytest.mark.parametrize("collection_name", ["open_orders", "algo_orders"])
def test_portfolio_baseline_hashes_complete_non_target_orders(
    tmp_path: Path,
    collection_name: str,
) -> None:
    scenario = Scenario()
    order = {
        "symbol": "ETHUSDT",
        "order_id": "order-1",
        "price": "99",
        "status": "NEW",
    }
    scenario.exchange_state["payload"][collection_name] = [order]

    with FakeControlPlane(scenario) as server:
        baseline = _preflight_portfolio_baseline(
            scenario,
            tmp_path,
            server.url,
        )
        order["price"] = "100"
        changed_baseline = _preflight_portfolio_baseline(
            scenario,
            tmp_path,
            server.url,
        )

    assert changed_baseline != baseline


def test_preflight_waits_for_exchange_snapshot_after_resume_boundary(
    tmp_path: Path,
) -> None:
    not_before = datetime.now(timezone.utc)
    before_resume = not_before - timedelta(milliseconds=1)
    after_resume = not_before + timedelta(milliseconds=1)
    scenario = Scenario()
    scenario.exchange_states = [
        _exchange_state(fetched_at=before_resume),
        _exchange_state(fetched_at=after_resume),
    ]

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(
                phase="before-open",
                exchange_not_before=not_before.isoformat(),
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["fetched_at"] == after_resume.isoformat()


@pytest.mark.parametrize(
    ("status_code", "detail"),
    [
        (408, "node telemetry timeout"),
        (503, "node projection unavailable"),
    ],
)
def test_preflight_keeps_exchange_authority_when_node_telemetry_fails(
    tmp_path: Path,
    status_code: int,
    detail: str,
) -> None:
    scenario = Scenario()
    scenario.forced_responses[("GET", "/v1/nodes")] = (
        status_code,
        {"detail": detail},
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(phase="before-open"),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["exchange_authoritative"] is True
    assert payload["target_position_side"] == "FLAT"
    assert payload["target_position_quantity"] == "0"
    assert "node_snapshot" not in payload
    assert any(
        "node telemetry degraded" in warning
        and f"HTTP_{status_code}" in warning
        for warning in payload["warnings"]
    )


def test_preflight_durable_node_failure_is_hard(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.forced_responses[("GET", "/v1/nodes")] = (
        503,
        {"detail": "intent store unavailable"},
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(phase="before-open"),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "durable control-plane failure" in completed.stderr


def test_preflight_keeps_exchange_authority_when_node_is_missing(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.forced_responses[("GET", "/v1/nodes")] = (
        200,
        {"nodes": []},
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(phase="before-open"),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["exchange_authoritative"] is True
    assert "node_snapshot" not in payload
    assert any(
        "node is missing" in warning
        for warning in payload["warnings"]
    )


@pytest.mark.parametrize("status_code", [401, 403, 409])
def test_preflight_node_authorization_failure_is_hard(
    tmp_path: Path,
    status_code: int,
) -> None:
    scenario = Scenario()
    scenario.forced_responses[("GET", "/v1/nodes")] = (
        status_code,
        {"detail": "forbidden"},
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(phase="before-open"),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert f"HTTP {status_code}" in completed.stderr


def test_preflight_rejects_additional_account_nodes(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    foreign_node_one = {
        **_node_snapshot(),
        "node_id": "nautilus-node-account-a-replacement",
    }
    foreign_node_two = {
        **_node_snapshot(),
        "node_id": "nautilus-node-account-a-shadow",
    }
    scenario.node_snapshots = [
        scenario.node_snapshot,
        foreign_node_one,
        foreign_node_two,
    ]

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(phase="before-open"),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "multiple account nodes" in completed.stderr


def test_preflight_allows_missing_extended_node_identity_with_warnings(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    for field_name in ("writer_id", "lease_id", "fencing_epoch"):
        scenario.node_snapshot.pop(field_name)

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(phase="before-open"),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["action"] == "preflight"
    assert payload["warnings"] == [
        "node identity telemetry missing: writer_id",
        "node identity telemetry missing: lease_id",
        "node identity telemetry missing: fencing_epoch",
    ]


@pytest.mark.parametrize(
    ("field_name", "field_value", "expected_fragment"),
    [
        ("node_id", "nautilus-node-account-b", "node node_id mismatch"),
        ("account_id", "account-b", "node account mismatch"),
        ("writer_id", "writer-account-b", "node writer_id mismatch"),
        ("lease_id", "lease-account-b", "node lease_id mismatch"),
        (
            "fencing_epoch",
            FENCING_EPOCH + 1,
            "node fencing_epoch mismatch",
        ),
    ],
)
def test_preflight_rejects_explicit_node_identity_conflict(
    tmp_path: Path,
    field_name: str,
    field_value: Any,
    expected_fragment: str,
) -> None:
    scenario = Scenario()
    scenario.node_snapshot[field_name] = field_value

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(phase="before-open"),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert expected_fragment in completed.stderr


def test_preflight_missing_available_balance_is_advisory(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.exchange_state = _exchange_state(
        available_usdt_balance=False
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "preflight",
            _request(phase="before-open"),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert "available_usdt_balance" not in payload
    assert payload["warnings"] == [
        "available USDT balance telemetry missing"
    ]


def test_observe_uses_real_mirror_and_node_progress_evidence(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    node_loss_monitor_at = fetched_at + timedelta(milliseconds=100)
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.1",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        position_quantity="0.1",
        mark_price="101",
        unrealized_pnl="0.1",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )
    scenario.node_snapshot.update(
        {
            "actor_tick_at": node_loss_monitor_at.isoformat(),
            "loss_monitor_at": node_loss_monitor_at.isoformat(),
            "loss_monitor_healthy": True,
            "process_liveness": True,
        }
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["open_status"] == "FILLED"
    assert payload["filled_quantity"] == "0.1"
    assert payload["mark_fresh"] is True
    assert payload["loss_monitor_healthy"] is True
    assert payload["mark_at"] == fetched_at.isoformat()
    assert payload["loss_monitor_at"] == (
        node_loss_monitor_at.isoformat()
    )
    assert payload["loss_monitor_source"] == "exchange_mirror+node"
    assert payload["loss_monitor_evidence"] == {
        "actor_tick_at": node_loss_monitor_at.isoformat(),
        "exchange_mirror_fetched_at": fetched_at.isoformat(),
        "exchange_mirror_stale": False,
        "node_loss_monitor_at": node_loss_monitor_at.isoformat(),
        "node_snapshot_available": True,
    }
    assert payload["node_snapshot"]["node_id"] == NODE_ID
    assert payload["node_snapshot"]["loss_monitor_healthy"] is True
    publication = next(
        request
        for request in scenario.requests
        if request["path"] == f"/v1/nodes/{NODE_ID}/loss-monitor"
    )
    assert publication["body"] == {
        "account_id": ACCOUNT_ID,
        "loss_monitor_healthy": True,
        "loss_monitor_at": node_loss_monitor_at.isoformat(),
    }


def test_observe_returns_soft_failure_when_mirror_remains_stale(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.1",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        stale=True,
        position_quantity="0.1",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "0.01",
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["accepted"] is False
    assert payload["error_code"] == "EXCHANGE_MIRROR_STALE"
    assert "remained stale" in payload["reason"]


@pytest.mark.parametrize(
    ("authoritative", "observed_before_dispatch"),
    [
        (False, False),
        (True, True),
    ],
)
def test_observe_rejects_non_authoritative_or_pre_dispatch_absence(
    tmp_path: Path,
    authoritative: bool,
    observed_before_dispatch: bool,
) -> None:
    not_before = datetime.now(timezone.utc) - timedelta(seconds=1)
    observed_at = datetime.now(timezone.utc)
    if observed_before_dispatch:
        observed_at = not_before - timedelta(seconds=1)
    scenario = Scenario()
    status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0",
        average_fill_price="0",
    )
    status["orders"][0]["status"] = "EXPIRED"
    status["execution_events"] = []
    scenario.operator_statuses[OPEN_INTENT_ID] = status
    scenario.exchange_state = _exchange_state(
        fetched_at=datetime.now(timezone.utc),
        client_order_id=OPEN_CLIENT_ORDER_ID,
        evidence_state="definitively_absent",
        evidence_observed_at=observed_at,
        evidence_authoritative=authoritative,
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(
                open_client_order_id=OPEN_CLIENT_ORDER_ID,
                exchange_not_before=not_before.isoformat(),
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["open_status"] == "EXPIRED"
    assert payload["filled_quantity"] == "0"
    assert payload["exchange_evidence_state"] == "unknown"


def test_observe_treats_frozen_node_loss_monitor_as_advisory(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    frozen_at = fetched_at - timedelta(seconds=30)
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.1",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        position_quantity="0.1",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )
    scenario.node_snapshot.update(
        {
            "actor_tick_at": frozen_at.isoformat(),
            "loss_monitor_at": frozen_at.isoformat(),
            "loss_monitor_healthy": True,
        }
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["mark_fresh"] is True
    assert payload["loss_monitor_healthy"] is True
    assert payload["loss_monitor_at"] == fetched_at.isoformat()
    assert payload["health_evidence_degraded"] is True
    assert any(
        "node health telemetry stale: loss_monitor_at" in warning
        for warning in payload["warnings"]
    )
    assert payload["loss_monitor_evidence"][
        "node_loss_monitor_at"
    ] == frozen_at.isoformat()


def test_observe_degrades_when_optional_node_snapshot_is_unavailable(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.1",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        position_quantity="0.1",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )
    scenario.forced_responses[("GET", "/v1/nodes")] = (
        503,
        {"detail": "node projection unavailable"},
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["mark_fresh"] is True
    assert payload["loss_monitor_healthy"] is True
    assert payload["loss_monitor_at"] == fetched_at.isoformat()
    assert payload["loss_monitor_source"] == "exchange_mirror"
    assert payload["health_evidence_degraded"] is True
    assert "HTTP_503" in payload["warnings"][0]
    assert payload["loss_monitor_evidence"][
        "node_snapshot_available"
    ] is False


def test_observe_loss_monitor_publish_failure_is_advisory(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        position_quantity="0.07",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )
    scenario.node_snapshot.update(
        {
            "loss_monitor_healthy": False,
            "process_liveness": True,
        }
    )
    scenario.forced_responses[
        ("POST", f"/v1/nodes/{NODE_ID}/loss-monitor")
    ] = (503, {"detail": "loss monitor endpoint unavailable"})

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["loss_monitor_healthy"] is False
    assert payload["health_evidence_degraded"] is True
    assert any(
        "loss monitor publication degraded" in warning
        and "HTTP_503" in warning
        for warning in payload["warnings"]
    )
    assert any(
        request["path"] == f"/v1/nodes/{NODE_ID}/loss-monitor"
        for request in scenario.requests
    )


@pytest.mark.parametrize("status_code", [401, 403, 409])
def test_observe_loss_monitor_identity_conflict_is_hard(
    tmp_path: Path,
    status_code: int,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        position_quantity="0.07",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )
    scenario.forced_responses[
        ("POST", f"/v1/nodes/{NODE_ID}/loss-monitor")
    ] = (status_code, {"detail": "writer identity conflict"})

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "ownership/fencing conflict" in completed.stderr


def test_observe_durable_node_snapshot_failure_is_hard(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        position_quantity="0.07",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )
    scenario.forced_responses[("GET", "/v1/nodes")] = (
        503,
        {"detail": "journal fsync failed"},
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "durable control-plane failure" in completed.stderr


def test_observe_durable_loss_monitor_publication_failure_is_hard(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        position_quantity="0.07",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )
    scenario.forced_responses[
        ("POST", f"/v1/nodes/{NODE_ID}/loss-monitor")
    ] = (503, {"detail": "outbox durable write failed"})

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "durable control-plane failure" in completed.stderr


def test_observe_allows_missing_extended_identity_with_warnings(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    for field_name in ("writer_id", "lease_id", "fencing_epoch"):
        scenario.node_snapshot.pop(field_name)
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        position_quantity="0.07",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["health_evidence_degraded"] is True
    assert payload["warnings"] == [
        "node identity telemetry missing: writer_id",
        "node identity telemetry missing: lease_id",
        "node identity telemetry missing: fencing_epoch",
    ]


@pytest.mark.parametrize(
    ("field_name", "field_value", "expected_fragment"),
    [
        ("node_id", "nautilus-node-account-b", "node node_id mismatch"),
        ("account_id", "account-b", "node account mismatch"),
        ("writer_id", "writer-account-b", "node writer_id mismatch"),
        ("lease_id", "lease-account-b", "node lease_id mismatch"),
        (
            "fencing_epoch",
            FENCING_EPOCH + 1,
            "node fencing_epoch mismatch",
        ),
    ],
)
def test_observe_rejects_explicit_node_identity_conflict(
    tmp_path: Path,
    field_name: str,
    field_value: Any,
    expected_fragment: str,
) -> None:
    scenario = Scenario()
    scenario.node_snapshot[field_name] = field_value
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        position_quantity="0.07",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert expected_fragment in completed.stderr


def test_observe_accepts_45_second_exchange_mirror_with_60_second_window(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=45)
    node_progress_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.1",
        average_fill_price="100",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=fetched_at,
        position_quantity="0.1",
        client_order_id=OPEN_CLIENT_ORDER_ID,
    )
    scenario.node_snapshot.update(
        {
            "actor_tick_at": node_progress_at.isoformat(),
            "loss_monitor_at": node_progress_at.isoformat(),
            "loss_monitor_healthy": True,
            "process_liveness": True,
        }
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "observe",
            _request(open_client_order_id=OPEN_CLIENT_ORDER_ID),
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_FRESHNESS_SECONDS": "120",
            },
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["mark_fresh"] is True
    assert payload["loss_monitor_healthy"] is True
    assert payload["mark_at"] == fetched_at.isoformat()


def test_position_waits_for_exchange_sample_after_dispatch(
    tmp_path: Path,
) -> None:
    not_before = datetime.now(timezone.utc) - timedelta(milliseconds=500)
    pre_dispatch = not_before - timedelta(seconds=1)
    post_dispatch = datetime.now(timezone.utc)
    scenario = Scenario()
    scenario.exchange_states = [
        _exchange_state(
            fetched_at=pre_dispatch,
            client_order_id=OPEN_CLIENT_ORDER_ID,
            evidence_filled_quantity="0.07",
        ),
        _exchange_state(
            fetched_at=post_dispatch,
            position_quantity="0.1",
            client_order_id=OPEN_CLIENT_ORDER_ID,
            evidence_filled_quantity="0.07",
        ),
    ]

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "position",
            _request(exchange_not_before=not_before.isoformat()),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["position_quantity"] == "0.1"
    assert payload["fetched_at"] == post_dispatch.isoformat()
    exchange_requests = [
        request
        for request in scenario.requests
        if request["path"] == (
            f"/v1/nodes/{NODE_ID}/exchange-state"
        )
    ]
    assert len(exchange_requests) == 2


def test_close_posts_exact_reduce_only_and_waits_for_exchange_flat(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        client_order_id=CLOSE_CLIENT_ORDER_ID,
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["accepted"] is True
    assert payload["action"] == "close"
    assert payload["intent_id"] == CLOSE_INTENT_ID
    assert payload["client_order_id"] == CLOSE_CLIENT_ORDER_ID
    operator_request = next(
        request
        for request in scenario.requests
        if request["method"] == "POST"
        and request["path"] == "/v1/operator/orders"
    )
    assert operator_request["body"]["action"] == "partial_close"
    assert operator_request["body"]["quantity"] == "0.07"
    assert operator_request["body"]["position_side"] == "long"


def test_close_retry_recovers_exchange_fill_after_response_timeout(
    tmp_path: Path,
) -> None:
    dispatched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    scenario.open_exchange_state = _exchange_state(
        fetched_at=dispatched_at,
        position_quantity="0.22",
        client_order_id=OPEN_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.07",
    )
    scenario.exchange_state = scenario.open_exchange_state
    scenario.close_exchange_state_after_post = _exchange_state(
        position_quantity="0.15",
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.07",
    )
    scenario.close_response_delay_seconds = 0.05
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    close_request = _request(
        intent_id=CLOSE_INTENT_ID,
        open_intent_id=OPEN_INTENT_ID,
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        quantity="0.07",
        reduce_only=True,
        side="SELL",
        position_side="LONG",
        protected_baseline_quantity="0.15",
        protected_baseline_side="LONG",
        side_effect_id="close-timeout-request-id",
        attempt=1,
        exchange_not_before=dispatched_at.isoformat(),
        open_exchange_not_before=(
            dispatched_at - timedelta(seconds=1)
        ).isoformat(),
    )
    first_invocation = tmp_path / "first"
    second_invocation = tmp_path / "second"
    first_invocation.mkdir()
    second_invocation.mkdir()

    with FakeControlPlane(scenario) as server:
        first_completed, first_payload = _invoke(
            "close",
            close_request,
            first_invocation,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_HTTP_TIMEOUT_SECONDS": "0.01",
                "ACCOUNT_A_LIVE_TRADE_HTTP_MAX_ATTEMPTS": "1",
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )
        retry_request = dict(close_request)
        retry_request["attempt"] = 2
        second_completed, second_payload = _invoke(
            "close",
            retry_request,
            second_invocation,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    assert first_completed.returncode == 0, first_completed.stderr
    assert first_payload["accepted"] is False
    assert first_payload["error_code"] == "HTTP_TIMEOUT"
    assert second_completed.returncode == 0, second_completed.stderr
    assert second_payload["accepted"] is True
    assert second_payload["intent_id"] == CLOSE_INTENT_ID
    assert second_payload["client_order_id"] == CLOSE_CLIENT_ORDER_ID
    assert second_payload["filled_quantity"] == "0.07"
    assert second_payload["protected_baseline_quantity"] == "0.15"
    assert second_payload["protected_baseline_side"] == "LONG"
    operator_posts = [
        request
        for request in scenario.requests
        if request["method"] == "POST"
        and request["path"] == "/v1/operator/orders"
    ]
    assert len(operator_posts) == 1
    assert operator_posts[0]["headers"]["x-request-id"] == (
        "close-timeout-request-id"
    )


@pytest.mark.parametrize(
    ("request_overrides", "expected_error"),
    [
        (
            {"client_order_id": OPEN_CLIENT_ORDER_ID},
            "CLOSE client order ID does not bind intent",
        ),
        (
            {
                "open_intent_id": (
                    "22222222-2222-4222-8222-222222222222"
                )
            },
            "CLOSE intent does not bind the original OPEN intent",
        ),
        (
            {
                "open_client_order_id": (
                    "B2222222222224222822222222222222201"
                )
            },
            "CLOSE open client order ID does not bind OPEN intent",
        ),
        (
            {
                "quantity": "0.06",
                "exchange_confirmed_open_fill_quantity": "0.06",
            },
            "CLOSE retry quantity differs from existing "
            "exchange-confirmed fill",
        ),
        (
            {"protected_baseline_quantity": "0.14"},
            "CLOSE retry position differs from protected baseline",
        ),
    ],
)
def test_close_retry_preserves_bound_request_contracts(
    tmp_path: Path,
    request_overrides: dict[str, Any],
    expected_error: str,
) -> None:
    dispatched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    scenario.open_exchange_state = _exchange_state(
        fetched_at=dispatched_at,
        position_quantity="0.22",
        client_order_id=OPEN_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.07",
    )
    scenario.exchange_state = _exchange_state(
        position_quantity="0.15",
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.07",
    )
    close_request = _request(
        intent_id=CLOSE_INTENT_ID,
        open_intent_id=OPEN_INTENT_ID,
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        quantity="0.07",
        reduce_only=True,
        side="SELL",
        position_side="LONG",
        protected_baseline_quantity="0.15",
        protected_baseline_side="LONG",
        side_effect_id="close-timeout-request-id",
        attempt=2,
        exchange_not_before=dispatched_at.isoformat(),
        open_exchange_not_before=(
            dispatched_at - timedelta(seconds=1)
        ).isoformat(),
    )
    close_request.update(request_overrides)

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            close_request,
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    assert completed.returncode == 1
    assert payload == {}
    assert expected_error in completed.stderr
    operator_posts = [
        request
        for request in scenario.requests
        if request["method"] == "POST"
        and request["path"] == "/v1/operator/orders"
    ]
    assert operator_posts == []


def test_close_requires_exact_exchange_history_fill_when_flat(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0",
        average_fill_price="0",
    )
    scenario.exchange_state = _exchange_state(
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "0.01",
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "exchange history CLOSE filled_quantity must be positive" in (
        completed.stderr
    )


def test_close_uses_causal_exchange_history_when_projection_is_unavailable(
    tmp_path: Path,
) -> None:
    observed_at = datetime.now(timezone.utc)
    scenario = Scenario()
    scenario.forced_responses[
        ("GET", f"/v1/operator/orders/{CLOSE_INTENT_ID}")
    ] = (503, {"detail": "projection unavailable"})
    scenario.exchange_state = _exchange_state(
        fetched_at=observed_at,
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_state="confirmed_executed",
        evidence_observed_at=observed_at,
        evidence_filled_quantity="0.07",
        evidence_sources=("exchange_state.recent_order_history",),
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
                exchange_not_before=(
                    observed_at - timedelta(seconds=1)
                ).isoformat(),
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["accepted"] is True
    assert payload["filled_quantity"] == "0.07"
    assert payload["close_proof_source"] == "exchange_history"
    assert payload["enrichment_degraded"] is True
    assert "HTTP_503" in payload["warnings"][0]


def test_close_durable_projection_failure_is_hard(
    tmp_path: Path,
) -> None:
    observed_at = datetime.now(timezone.utc)
    scenario = Scenario()
    scenario.forced_responses[
        ("GET", f"/v1/operator/orders/{CLOSE_INTENT_ID}")
    ] = (503, {"detail": "evidence store unavailable"})
    scenario.exchange_state = _exchange_state(
        fetched_at=observed_at,
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_state="confirmed_executed",
        evidence_observed_at=observed_at,
        evidence_filled_quantity="0.07",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "durable control-plane failure" in completed.stderr


def test_close_uses_exchange_history_when_projection_quantity_conflicts(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.05",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.07",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["close_proof_source"] == "exchange_history"
    assert payload["enrichment_degraded"] is True
    assert "differs from exchange history" in payload["warnings"][0]


def test_close_rejects_projection_exact_when_exchange_quantity_conflicts(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.05",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "0.01",
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "exact CLOSE unproven" in completed.stderr


def test_close_rejects_exchange_history_observed_before_dispatch(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.refresh_exchange_evidence_on_close = False
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.07",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "0.01",
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "exact CLOSE unproven" in completed.stderr


def test_close_rejects_conflicting_exchange_history_quantities(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.07",
        evidence_sources=(
            "exchange_state.recent_order_history",
            "exchange_state.recent_algo_order_history",
        ),
    )
    source_evidence = scenario.exchange_state[
        "opening_execution_evidence"
    ]["items"][0]["source_evidence"]
    source_evidence[1]["filled_quantity"] = "0.05"

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert (
        "exchange history CLOSE evidence is ambiguous"
        in completed.stderr
    )


@pytest.mark.parametrize(
    ("field_name", "field_value", "error_text"),
    [
        (
            "state",
            "definitively_absent",
            "conflicts with CLOSE identity",
        ),
        ("filled_quantity", "0", "has no CLOSE fill"),
        ("account_id", "account-b", "conflicts with CLOSE identity"),
        (
            "instrument_id",
            "BTCUSDT-PERP.BINANCE",
            "conflicts with CLOSE symbol",
        ),
        ("venue_order_id", "venue-other", "CLOSE evidence is ambiguous"),
    ],
)
def test_close_rejects_mixed_conflicting_exchange_history_proof(
    tmp_path: Path,
    field_name: str,
    field_value: str,
    error_text: str,
) -> None:
    scenario = Scenario()
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_sources=(
            "exchange_state.recent_order_history",
            "exchange_state.recent_algo_order_history",
        ),
    )
    source_evidence = scenario.exchange_state[
        "opening_execution_evidence"
    ]["items"][0]["source_evidence"]
    source_evidence[1][field_name] = field_value

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert error_text in completed.stderr


@pytest.mark.parametrize("future_surface", ["mirror", "proof"])
def test_close_rejects_far_future_exchange_timestamps(
    tmp_path: Path,
    future_surface: str,
) -> None:
    future_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    scenario = Scenario()
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=future_at,
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_observed_at=future_at,
    )
    if future_surface == "mirror":
        scenario.refresh_exchange_mirror_on_close = False
    else:
        scenario.refresh_exchange_evidence_on_close = False

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "0.01",
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    if future_surface == "mirror":
        assert completed.returncode == 0, completed.stderr
        assert payload["accepted"] is False
        assert payload["error_code"] == "EXCHANGE_MIRROR_LAG"
        assert "allowed clock skew" in payload["reason"]
    else:
        assert completed.returncode == 1
        assert payload == {}
        assert "exact CLOSE unproven" in completed.stderr


@pytest.mark.parametrize(
    ("evidence_overrides", "error_text"),
    [
        (
            {"evidence_account_id": "account-b"},
            "exact CLOSE unproven",
        ),
        (
            {"evidence_instrument_id": "BTCUSDT-PERP.BINANCE"},
            "conflicts with CLOSE symbol",
        ),
        (
            {"evidence_sources": ("orders_projection",)},
            "exact CLOSE unproven",
        ),
    ],
)
def test_close_rejects_unbound_exchange_history_proof(
    tmp_path: Path,
    evidence_overrides: dict[str, Any],
    error_text: str,
) -> None:
    scenario = Scenario()
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        **evidence_overrides,
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "0.01",
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    assert completed.returncode == 1
    assert payload == {}
    assert error_text in completed.stderr


@pytest.mark.parametrize("surface", ["orders", "execution_events"])
def test_close_ignores_exact_fill_rows_without_matching_client_order_id(
    tmp_path: Path,
    surface: str,
) -> None:
    scenario = Scenario()
    status = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    status[surface][0]["client_order_id"] = ""
    if surface == "orders":
        status["execution_events"] = []
    else:
        status["orders"][0]["filled_quantity"] = "0"
        status["orders"][0]["average_fill_price"] = "0"
    scenario.operator_statuses[CLOSE_INTENT_ID] = status
    scenario.exchange_state = _exchange_state(
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.07",
                reduce_only=True,
                side="SELL",
                position_side="LONG",
                side_effect_id="close-request-id",
            ),
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "0.01",
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "exchange history CLOSE filled_quantity must be positive" in (
        completed.stderr
    )


@pytest.mark.parametrize("action", ["position", "final-snapshot"])
def test_stale_mirror_cannot_prove_position_or_final_state(
    tmp_path: Path,
    action: str,
) -> None:
    scenario = Scenario()
    scenario.exchange_state = _exchange_state(stale=True)

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            action,
            _request(),
            tmp_path,
            server.url,
            environment_overrides={
                "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS": "0.01",
                "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS": "0",
            },
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["accepted"] is False
    assert payload["error_code"] == "EXCHANGE_MIRROR_STALE"


def test_final_snapshot_keeps_exchange_proof_when_enrichment_is_unavailable(
    tmp_path: Path,
) -> None:
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    scenario = Scenario()
    scenario.exchange_state = _exchange_state(fetched_at=fetched_at)
    scenario.forced_responses[
        ("GET", f"/v1/operator/orders/{OPEN_INTENT_ID}")
    ] = (503, {"detail": "projection unavailable"})
    scenario.forced_responses[
        ("GET", f"/v1/operator/orders/{CLOSE_INTENT_ID}")
    ] = (511, {"detail": "projection dependency unavailable"})

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["target_symbol_flat"] is True
    assert payload["target_symbol_regular_orders_zero"] is True
    assert payload["target_symbol_algo_orders_zero"] is True
    assert payload["non_target_portfolio_baseline_sha256"] == (
        EMPTY_PORTFOLIO_SHA256
    )
    assert payload["source"] == "exchange"
    assert payload["fetched_at"] == fetched_at.isoformat()
    assert payload["enrichment_degraded"] is True
    assert payload["financial_proof_complete"] is False
    assert len(payload["warnings"]) == 2
    assert "HTTP_503" in payload["warnings"][0]
    assert "HTTP_511" in payload["warnings"][1]
    assert payload["gross_pnl_usdt"] == "0"
    assert payload["fees_usdt"] == "0"
    assert payload["net_pnl_usdt"] == "0"


def test_final_snapshot_degrades_malformed_financial_enrichment(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.1",
        average_fill_price="100",
    )
    open_status["execution_events"][0]["payload"]["commission"] = (
        "invalid"
    )
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.1",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["target_symbol_flat"] is True
    assert payload["target_symbol_regular_orders_zero"] is True
    assert payload["target_symbol_algo_orders_zero"] is True
    assert payload["enrichment_degraded"] is True
    assert payload["financial_proof_complete"] is False
    assert len(payload["warnings"]) == 1
    assert "open operator projection degraded" in payload["warnings"][0]
    assert "commission must be numeric" in payload["warnings"][0]


def test_final_snapshot_durable_operator_failure_is_hard(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.forced_responses[
        ("GET", f"/v1/operator/orders/{OPEN_INTENT_ID}")
    ] = (503, {"detail": "intent store unavailable"})

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "durable control-plane failure" in completed.stderr
    assert "intent store unavailable" in completed.stderr


@pytest.mark.parametrize(
    "missing_surface",
    [
        "orders",
        "execution_events",
        "fill_price",
        "commission",
        "intent_side",
        "partial_fill_events",
        "order_fill_quantity",
        "fill_price_mismatch",
    ],
)
def test_final_snapshot_rejects_incomplete_successful_projection(
    tmp_path: Path,
    missing_surface: str,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    close_status = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    if missing_surface == "orders":
        open_status["orders"] = []
    if missing_surface == "execution_events":
        open_status["execution_events"] = []
    if missing_surface == "fill_price":
        open_status["orders"][0]["average_fill_price"] = "0"
        open_status["execution_events"][0]["payload"]["last_px"] = "0"
    if missing_surface == "commission":
        open_status["execution_events"][0]["payload"].pop("commission")
    if missing_surface == "intent_side":
        open_status["intent"]["order_plan"]["side"] = ""
    if missing_surface == "partial_fill_events":
        open_status["execution_events"][0]["payload"]["last_qty"] = (
            "0.01"
        )
    if missing_surface == "order_fill_quantity":
        open_status["orders"][0]["filled_quantity"] = "0"
    if missing_surface == "fill_price_mismatch":
        open_status["execution_events"][0]["payload"]["last_px"] = (
            "101"
        )
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = close_status

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["enrichment_degraded"] is True
    assert payload["financial_proof_complete"] is False
    assert len(payload["warnings"]) == 1
    assert "incomplete financial proof" in payload["warnings"][0]


def test_final_snapshot_requires_commission_for_every_unique_fill(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    first_fill = open_status["execution_events"][0]
    first_fill["payload"]["last_qty"] = "0.03"
    second_fill = json.loads(json.dumps(first_fill))
    second_fill["trade_id"] = f"trade-2-{OPEN_CLIENT_ORDER_ID}"
    second_fill["payload"]["last_qty"] = "0.04"
    second_fill["payload"].pop("commission")
    open_status["execution_events"].append(second_fill)
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert len(payload["warnings"]) == 1
    assert "commission coverage" in payload["warnings"][0]


@pytest.mark.parametrize("empty_commission", [None, ""])
def test_final_snapshot_rejects_empty_commission_value(
    tmp_path: Path,
    empty_commission: str | None,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    open_status["execution_events"][0]["payload"][
        "commission"
    ] = empty_commission
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert len(payload["warnings"]) == 1
    assert "commission coverage" in payload["warnings"][0]


def test_final_snapshot_requires_realized_pnl_for_every_unique_fill(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    first_fill = open_status["execution_events"][0]
    first_fill["payload"]["last_qty"] = "0.03"
    second_fill = json.loads(json.dumps(first_fill))
    second_fill["trade_id"] = f"trade-2-{OPEN_CLIENT_ORDER_ID}"
    second_fill["payload"]["last_qty"] = "0.04"
    second_fill["payload"].pop("realized_pnl")
    open_status["execution_events"].append(second_fill)
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert len(payload["warnings"]) == 1
    assert "realized PnL coverage" in payload["warnings"][0]


def test_final_snapshot_preserves_signed_pnl_and_dedupes_fill_fees(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    duplicate_open_fill = json.loads(
        json.dumps(open_status["execution_events"][0])
    )
    open_status["execution_events"].append(duplicate_open_fill)
    close_status = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="99",
    )
    close_status["execution_events"][0]["payload"][
        "realized_pnl"
    ] = "-0.05"
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = close_status

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is True
    assert payload["open_filled_quantity"] == "0.07"
    assert payload["open_average_fill_price_usdt"] == "100"
    assert payload["gross_pnl_usdt"] == "-0.05"
    assert payload["fees_usdt"] == "0.02"
    assert payload["net_pnl_usdt"] == "-0.07"
    assert payload["cumulative_net_loss_usdt"] == "0.07"


def test_final_snapshot_preserves_complete_signed_zero_pnl(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    scenario.operator_statuses[OPEN_INTENT_ID] = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="99",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is True
    assert payload["gross_pnl_usdt"] == "0"
    assert payload["fees_usdt"] == "0.02"
    assert payload["net_pnl_usdt"] == "-0.02"


@pytest.mark.parametrize(
    ("field_name", "field_value", "warning_fragment"),
    [
        ("quantity", "0.08", "open order quantity"),
        ("price", "101", "open order limit price"),
    ],
)
def test_final_snapshot_binds_projected_open_order_to_signed_plan(
    tmp_path: Path,
    field_name: str,
    field_value: str,
    warning_fragment: str,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    open_status["orders"][0][field_name] = field_value
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert any(
        warning_fragment in warning
        for warning in payload["warnings"]
    )


@pytest.mark.parametrize(
    ("signed_side", "projected_side"),
    [
        ("BUY", "short"),
        ("SELL", "long"),
    ],
)
def test_final_snapshot_rejects_projected_side_conflicting_with_signed_side(
    tmp_path: Path,
    signed_side: str,
    projected_side: str,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    open_status["intent"]["order_plan"]["side"] = projected_side
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(open_side=signed_side),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert any(
        "open side" in warning
        for warning in payload["warnings"]
    )


def test_final_snapshot_requires_usdt_commission_currency(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    open_status["execution_events"][0]["payload"].update(
        {
            "commission": "0.001",
            "currency": "BNB",
        }
    )
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert any(
        "commission currency" in warning
        for warning in payload["warnings"]
    )


def test_final_snapshot_rejects_conflicting_commission_currency_fields(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    open_status["execution_events"][0]["payload"].update(
        {
            "commission": "0.001",
            "commission_currency": "USDT",
            "currency": "BNB",
        }
    )
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert any(
        "commission currency" in warning
        for warning in payload["warnings"]
    )


def test_final_snapshot_accepts_embedded_usdt_commission_currency(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    open_status["execution_events"][0]["payload"].pop("currency")
    open_status["execution_events"][0]["payload"]["commission"] = (
        "0.001 USDT"
    )
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is True


def test_final_snapshot_rejects_whitespace_trade_identity(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    open_status["execution_events"][0]["trade_id"] = "   "
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert any(
        "fill identity" in warning
        for warning in payload["warnings"]
    )


@pytest.mark.parametrize(
    ("conflict", "warning_fragment"),
    [
        ("quantity", "cross-leg quantity reconciliation"),
        ("trade_id", "cross-leg trade identity"),
    ],
)
def test_final_snapshot_rejects_cross_leg_fill_conflict(
    tmp_path: Path,
    conflict: str,
    warning_fragment: str,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    close_status = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )
    if conflict == "quantity":
        close_status["orders"][0]["filled_quantity"] = "0.06"
        close_status["execution_events"][0]["payload"]["last_qty"] = (
            "0.06"
        )
    if conflict == "trade_id":
        close_status["execution_events"][0]["trade_id"] = (
            open_status["execution_events"][0]["trade_id"]
        )
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = close_status

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert any(
        warning_fragment in warning
        for warning in payload["warnings"]
    )


def test_final_snapshot_rejects_conflicting_duplicate_trade(
    tmp_path: Path,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    conflicting_fill = json.loads(
        json.dumps(open_status["execution_events"][0])
    )
    conflicting_fill["payload"]["commission"] = "0.02"
    open_status["execution_events"].append(conflicting_fill)
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert len(payload["warnings"]) == 1
    assert "fill identity" in payload["warnings"][0]


@pytest.mark.parametrize(
    "conflicting_currency_first",
    [False, True],
)
def test_final_snapshot_rejects_zero_commission_duplicate_currency_conflict(
    tmp_path: Path,
    conflicting_currency_first: bool,
) -> None:
    scenario = Scenario()
    open_status = _operator_status(
        OPEN_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100",
    )
    valid_fill = open_status["execution_events"][0]
    valid_fill["payload"].update(
        {
            "commission": "0",
            "currency": "USDT",
        }
    )
    conflicting_fill = json.loads(json.dumps(valid_fill))
    conflicting_fill["payload"]["currency"] = "BNB"
    events = [valid_fill, conflicting_fill]
    if conflicting_currency_first:
        events.reverse()
    open_status["execution_events"] = events
    scenario.operator_statuses[OPEN_INTENT_ID] = open_status
    scenario.operator_statuses[CLOSE_INTENT_ID] = _operator_status(
        CLOSE_CLIENT_ORDER_ID,
        filled_quantity="0.07",
        average_fill_price="100.5",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "final-snapshot",
            _request(),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["financial_proof_complete"] is False
    assert any(
        "fill identity" in warning
        for warning in payload["warnings"]
    )


@pytest.mark.parametrize(
    "status_code",
    [408, 425, 429, 500, 501, 505, 511, 599],
)
def test_http_retryable_statuses_return_soft_error_json(
    tmp_path: Path,
    status_code: int,
) -> None:
    scenario = Scenario()
    scenario.forced_responses[("POST", "/v1/operator/orders")] = (
        status_code,
        {"detail": "temporarily unavailable"},
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "open",
            _request(
                client_order_id=OPEN_CLIENT_ORDER_ID,
                side="BUY",
                order_type="LIMIT",
                time_in_force="IOC",
                quantity="0.07",
                limit_price_usdt="100",
                max_actual_open_notional_usdt="12",
                side_effect_id="open-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 0, completed.stderr
    assert payload["accepted"] is False
    assert payload["action"] == "open"
    assert payload["error_code"] == f"HTTP_{status_code}"
    assert payload["status_code"] == status_code
    assert "temporarily unavailable" in payload["reason"]


@pytest.mark.parametrize(
    "detail",
    [
        "intent store unavailable",
        "journal fsync failed",
        "outbox durable write failed",
        "capacity exhausted",
        "ENOSPC",
    ],
)
def test_http_durable_5xx_is_a_hard_adapter_failure(
    tmp_path: Path,
    detail: str,
) -> None:
    scenario = Scenario()
    scenario.forced_responses[("POST", "/v1/operator/orders")] = (
        503,
        {"detail": detail},
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "open",
            _request(
                client_order_id=OPEN_CLIENT_ORDER_ID,
                side="BUY",
                order_type="LIMIT",
                time_in_force="IOC",
                quantity="0.07",
                limit_price_usdt="100",
                max_actual_open_notional_usdt="12",
                side_effect_id="open-request-id",
            ),
            tmp_path,
            server.url,
        )

    assert completed.returncode == 1
    assert payload == {}
    assert "durable control-plane failure" in completed.stderr
    assert detail in completed.stderr


def _invoke(
    action: str,
    request: dict[str, Any],
    tmp_path: Path,
    server_url: str,
    *,
    environment_overrides: dict[str, str] | None = None,
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
    if environment_overrides is not None:
        environment.update(environment_overrides)
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
    payload = {}
    if completed.stdout.strip():
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
        "open_client_order_id": OPEN_CLIENT_ORDER_ID,
        "exchange_confirmed_open_fill_quantity": "0.07",
        "protected_baseline_quantity": "0",
        "protected_baseline_side": "FLAT",
        "quantity": "0.07",
        "limit_price_usdt": "100",
        "open_side": "BUY",
    }
    payload.update(overrides)
    return payload


def _preflight_portfolio_baseline(
    scenario: Scenario,
    tmp_path: Path,
    server_url: str,
) -> str:
    invocation_path = tmp_path / f"baseline-{len(scenario.requests)}"
    invocation_path.mkdir()
    completed, payload = _invoke(
        "preflight",
        _request(phase="before-open"),
        invocation_path,
        server_url,
    )
    assert completed.returncode == 0, completed.stderr
    return str(payload["non_target_portfolio_baseline_sha256"])


def _identity() -> dict[str, Any]:
    return {
        "account_id": ACCOUNT_ID,
        "rollout_phase": ROLLOUT_PHASE,
        "symbol": SYMBOL,
        "release_id": "release-account-a-test",
        "node_id": NODE_ID,
        "writer_id": WRITER_ID,
        "lease_id": LEASE_ID,
        "fencing_epoch": FENCING_EPOCH,
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


def _node_snapshot() -> dict[str, Any]:
    return {
        "node_id": NODE_ID,
        "account_id": ACCOUNT_ID,
        "writer_id": WRITER_ID,
        "lease_id": LEASE_ID,
        "fencing_epoch": FENCING_EPOCH,
        "trading_state": "HALTED",
        "readiness": True,
        "last_heartbeat_at": _now(),
        "projection_lag_ms": 0,
        "reconciliation_state": "healthy",
        "process_liveness": True,
        "actor_tick_at": _now(),
        "loss_monitor_healthy": True,
        "loss_monitor_at": _now(),
    }


def _operator_status(
    client_order_id: str,
    *,
    filled_quantity: str,
    average_fill_price: str,
) -> dict[str, Any]:
    return {
        "intent": {
            "order_plan": {
                "side": "long",
            }
        },
        "orders": [
            {
                "client_order_id": client_order_id,
                "status": "FILLED",
                "quantity": filled_quantity,
                "filled_quantity": filled_quantity,
                "price": average_fill_price,
                "average_fill_price": average_fill_price,
            }
        ],
        "execution_events": [
            {
                "event_type": "OrderFilled",
                "client_order_id": client_order_id,
                "trade_id": f"trade-{client_order_id}",
                "payload": {
                    "commission": "0.01",
                    "currency": "USDT",
                    "realized_pnl": "0",
                    "last_qty": filled_quantity,
                    "last_px": average_fill_price,
                },
            }
        ],
    }


def _exchange_state(
    *,
    fetched_at: datetime | None = None,
    stale: bool = False,
    position_quantity: str = "0",
    mark_price: str = "100",
    unrealized_pnl: str = "0",
    client_order_id: str = "",
    evidence_state: str = "confirmed_executed",
    evidence_observed_at: datetime | None = None,
    evidence_authoritative: bool = True,
    evidence_account_id: str = ACCOUNT_ID,
    evidence_filled_quantity: str = "",
    evidence_instrument_id: str = f"{SYMBOL}-PERP.BINANCE",
    evidence_sources: tuple[str, ...] = (
        "exchange_state.recent_order_history",
    ),
    available_usdt_balance: str | bool = "100",
) -> dict[str, Any]:
    observed_at = fetched_at
    if observed_at is None:
        observed_at = datetime.now(timezone.utc)
    positions = []
    if position_quantity != "0":
        positions.append(
            {
                "symbol": SYMBOL,
                "position_amt": position_quantity,
                "position_side": "LONG",
                "mark_price": mark_price,
                "unrealized_pnl": unrealized_pnl,
            }
        )
    evidence_items = []
    if client_order_id:
        if not evidence_filled_quantity:
            evidence_filled_quantity = "0.1"
            if client_order_id == CLOSE_CLIENT_ORDER_ID:
                evidence_filled_quantity = "0.07"
        item_observed_at = evidence_observed_at
        if item_observed_at is None:
            item_observed_at = observed_at
        evidence_items.append(
            {
                "account_id": evidence_account_id,
                "client_order_id": client_order_id,
                "state": evidence_state,
                "instrument_id": evidence_instrument_id,
                "filled_quantity": evidence_filled_quantity,
                "sources": list(evidence_sources),
                "source_evidence": [
                    {
                        "account_id": evidence_account_id,
                        "client_order_id": client_order_id,
                        "state": evidence_state,
                        "order_status": "FILLED",
                        "instrument_id": evidence_instrument_id,
                        "venue_order_id": "venue-close",
                        "filled_quantity": evidence_filled_quantity,
                        "source": source,
                        "observed_at": item_observed_at.isoformat(),
                        "reason": "fresh_exchange_order_history_match",
                    }
                    for source in evidence_sources
                ],
                "observed_at": item_observed_at.isoformat(),
            }
        )
    payload = {
        "fetched_at": observed_at.isoformat(),
        "positions": positions,
        "open_orders": [],
        "algo_orders": [],
    }
    if available_usdt_balance is not False:
        payload["account"] = {
            "currency": "USDT",
            "free": available_usdt_balance,
        }
    return {
        "account_id": ACCOUNT_ID,
        "updated_at": observed_at.isoformat(),
        "stale": stale,
        "payload": payload,
        "opening_execution_evidence": {
            "authoritative": evidence_authoritative,
            "reason": "fresh_account_scoped_evidence",
            "items": evidence_items,
        },
    }


def _exchange_state_has_client_order_id(
    exchange_state: dict[str, Any],
    client_order_id: str,
) -> bool:
    evidence = exchange_state.get("opening_execution_evidence")
    if not isinstance(evidence, dict):
        return False
    items = evidence.get("items")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("client_order_id") == client_order_id
        for item in items
    )
