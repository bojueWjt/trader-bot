from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / "scripts" / "account_a_live_trade_http_adapter.py"
RISK_TOKEN = "risk-admin-test-token"
NODE_TOKEN = "account-a-node-test-token"
NODE_ID = "nautilus-node-account-a"
ACCOUNT_ID = "account-a"
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
        self.exchange_states: list[dict[str, Any]] = []
        self.node_snapshot = _node_snapshot()
        self.refresh_exchange_mirror_on_close = True
        self.refresh_exchange_evidence_on_close = True

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
        forced = self.forced_responses.get((method, path))
        if forced is not None:
            return forced
        if method == "POST" and path == "/v1/commands":
            assert headers["authorization"] == f"Bearer {RISK_TOKEN}"
            assert isinstance(body, dict)
            self.node_state = str(body["type"])
            if self.node_state == "RESUME":
                self.node_state = "ACTIVE"
            return 200, {"command_id": "command-1", "status": "pending"}
        if method == "POST" and path == "/v1/operator/orders":
            assert headers["authorization"] == f"Bearer {RISK_TOKEN}"
            assert isinstance(body, dict)
            if body["action"] == "partial_close":
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
            return 200, {
                "intent_id": body["intent_id"],
                "status": "approved",
                "account_id": body["account_id"],
                "instrument_id": body["symbol"],
                "action": body["action"],
            }
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
            exchange_state = self.exchange_state
            if self.exchange_states:
                exchange_state = self.exchange_states[0]
                if len(self.exchange_states) > 1:
                    self.exchange_states.pop(0)
            return 200, exchange_state
        if method == "GET" and path == "/v1/nodes":
            assert headers["authorization"] == f"Bearer {RISK_TOKEN}"
            node_snapshot = dict(self.node_snapshot)
            node_snapshot["trading_state"] = self.node_state
            return 200, {
                "nodes": [node_snapshot]
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
                quantity="0.1",
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
    operator_request = scenario.requests[0]
    assert operator_request["path"] == "/v1/operator/orders"
    assert operator_request["headers"]["x-request-id"] == "open-request-id"
    assert operator_request["body"]["entry"] == {
        "type": "limit",
        "price": "100",
        "time_in_force": "IOC",
    }
    assert operator_request["body"]["notional_usdt"] == "10"


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
    assert payload["loss_monitor_at"] == fetched_at.isoformat()
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


def test_observe_surfaces_frozen_node_loss_monitor_as_unhealthy(
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
    assert payload["loss_monitor_healthy"] is False
    assert payload["loss_monitor_at"] == frozen_at.isoformat()
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
        _exchange_state(fetched_at=pre_dispatch),
        _exchange_state(
            fetched_at=post_dispatch,
            position_quantity="0.1",
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
        filled_quantity="0.1",
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
                quantity="0.1",
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
    operator_request = scenario.requests[0]
    assert operator_request["body"]["action"] == "partial_close"
    assert operator_request["body"]["quantity"] == "0.1"
    assert operator_request["body"]["position_side"] == "long"


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
                quantity="0.1",
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
    assert "exchange history evidence has no CLOSE fill" in (
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
        evidence_filled_quantity="0.1",
        evidence_sources=("exchange_state.recent_order_history",),
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.1",
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
    assert payload["filled_quantity"] == "0.1"
    assert payload["close_proof_source"] == "exchange_history"
    assert payload["enrichment_degraded"] is True
    assert "HTTP_503" in payload["warnings"][0]


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
        evidence_filled_quantity="0.1",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.1",
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
        filled_quantity="0.1",
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
                quantity="0.1",
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
        filled_quantity="0.1",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.1",
    )

    with FakeControlPlane(scenario) as server:
        completed, payload = _invoke(
            "close",
            _request(
                intent_id=CLOSE_INTENT_ID,
                open_intent_id=OPEN_INTENT_ID,
                client_order_id=CLOSE_CLIENT_ORDER_ID,
                quantity="0.1",
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
        filled_quantity="0.1",
        average_fill_price="100.5",
    )
    scenario.exchange_state = _exchange_state(
        client_order_id=CLOSE_CLIENT_ORDER_ID,
        evidence_filled_quantity="0.1",
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
                quantity="0.1",
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
        filled_quantity="0.1",
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
                quantity="0.1",
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
        filled_quantity="0.1",
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
                quantity="0.1",
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
        filled_quantity="0.1",
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
                quantity="0.1",
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
        filled_quantity="0.1",
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
                quantity="0.1",
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
    assert "exchange history evidence has no CLOSE fill" in (
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
    assert "asset amount must be numeric" in payload["warnings"][0]


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
                quantity="0.1",
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


def _node_snapshot() -> dict[str, Any]:
    return {
        "node_id": NODE_ID,
        "account_id": ACCOUNT_ID,
        "trading_state": "HALTED",
        "readiness": True,
        "last_heartbeat_at": _now(),
        "projection_lag_ms": 0,
        "reconciliation_state": "healthy",
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
                "filled_quantity": filled_quantity,
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
    evidence_filled_quantity: str = "0.1",
    evidence_instrument_id: str = f"{SYMBOL}-PERP.BINANCE",
    evidence_sources: tuple[str, ...] = (
        "exchange_state.recent_order_history",
    ),
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
    return {
        "account_id": ACCOUNT_ID,
        "updated_at": observed_at.isoformat(),
        "stale": stale,
        "payload": {
            "fetched_at": observed_at.isoformat(),
            "positions": positions,
            "open_orders": [],
            "algo_orders": [],
        },
        "opening_execution_evidence": {
            "authoritative": evidence_authoritative,
            "reason": "fresh_account_scoped_evidence",
            "items": evidence_items,
        },
    }
