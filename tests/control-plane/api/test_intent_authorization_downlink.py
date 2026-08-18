from __future__ import annotations

import ast
import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path

import read_api

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
if str(EXECUTION_DOMAIN_ROOT) not in sys.path:
    sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from execution_domain.contracts import ReconciliationState  # noqa: E402
from execution_domain.control_plane import (  # noqa: E402
    Heartbeat,
    TradingState,
)
from execution_domain.http_client import HttpControlPlaneClient  # noqa: E402


AUTHORIZATION = {
    "authorized_by_type": "channel",
    "authorized_by_id": "-1002136478186",
    "source_message_id": "tg-msg-5026",
    "created_by_service": "decision-gateway",
    "parent_intent_id": False,
    "channel_id": "-1002136478186",
}
ATTRIBUTION = {
    "resolution": "intent",
    "owner_channel": "-1002136478186",
    "channel_match": True,
    "would_reject": False,
}
LIVE_OPEN_GATE = {
    "mode": "normal",
    "release_id": "release-a",
    "rollout_phase": "fleet_complete",
    "phase_version": 5,
}


def _assert_authorization_metadata(plan: dict) -> None:
    assert plan["authorization"] == AUTHORIZATION
    assert plan["attribution"] == ATTRIBUTION


def test_open_position_downlink_preserves_authorization_metadata() -> None:
    plan = read_api._execution_order_plan(
        {
            "side": "long",
            "entry": {"type": "limit", "price": 100.0},
            "quantity": "2",
            "stop_loss": 90.0,
            "take_profits": [110.0, 120.0],
            "authorization": AUTHORIZATION,
            "attribution": ATTRIBUTION,
        },
        {"max_notional": 200.0},
        "BTCUSDT",
        action="open_position",
    )

    assert plan["side"] == "buy"
    assert plan["type"] == "limit"
    _assert_authorization_metadata(plan)


def test_open_position_downlink_preserves_live_open_gate() -> None:
    plan = read_api._execution_order_plan(
        {
            "side": "long",
            "entry": {"type": "limit", "price": 100.0},
            "quantity": "2",
            "authorization": AUTHORIZATION,
            "live_open_gate": LIVE_OPEN_GATE,
        },
        {"max_notional": 200.0},
        "BTCUSDT",
        action="open_position",
    )

    assert plan["live_open_gate"] == LIVE_OPEN_GATE


def test_open_position_downlink_preserves_canary_permit_metadata() -> None:
    permit = {
        "permit_id": "a7695a69-af4f-475d-a768-6f51e38067f7",
        "account_id": "account-a",
        "symbol": "BTCUSDT",
        "target_symbol": "BTCUSDT",
        "release_id": "release-a",
        "node_id": "node-a",
        "max_notional_usdt": "12",
        "expires_at": "2026-08-08T12:34:56+00:00",
        "portfolio_baseline_sha256": "4" * 64,
    }
    plan = read_api._execution_order_plan(
        {
            "side": "long",
            "entry": {
                "type": "limit",
                "price": 100.0,
                "time_in_force": "IOC",
            },
            "quantity": "0.12",
            "canary_permit": permit,
            "authorization": AUTHORIZATION,
            "attribution": ATTRIBUTION,
        },
        {"max_notional": 12.0},
        "BTCUSDT",
        action="open_position",
    )

    assert plan["canary_permit"] == permit
    assert plan["type"] == "limit"
    assert plan["time_in_force"] == "IOC"
    assert plan["quantity"] == "0.12"
    assert plan["price"] == 100.0


def test_canary_permit_downlink_includes_timezone_aware_expiry() -> None:
    expires_at = datetime(
        2026,
        8,
        8,
        12,
        34,
        56,
        tzinfo=timezone.utc,
    )
    evidence = read_api._canary_permit_downlink_evidence(
        permit_id="a7695a69-af4f-475d-a768-6f51e38067f7",
        account_id="account-a",
        symbol="BTCUSDT",
        release_id="release-a",
        node_id="node-a",
        max_notional="12",
        max_cumulative_loss="1.5",
        expires_at=expires_at,
        emergency_close_evidence_sha256="3" * 64,
        emergency_close_verified_at=datetime(
            2026,
            8,
            8,
            10,
            tzinfo=timezone.utc,
        ),
        portfolio_baseline_sha256="4" * 64,
    )

    assert evidence["expires_at"] == "2026-08-08T12:34:56+00:00"
    parsed_expiry = datetime.fromisoformat(evidence["expires_at"])
    assert parsed_expiry.tzinfo is not None
    assert parsed_expiry.utcoffset() == expires_at.utcoffset()


def test_canary_permit_downlink_callers_forward_database_expiry() -> None:
    callers = (
        read_api._validate_and_arm_resume,
        read_api._lock_canary_permit,
    )

    for caller in callers:
        calls = _function_calls(
            caller,
            "_canary_permit_downlink_evidence",
        )
        assert len(calls) == 1
        keywords = {
            keyword.arg: keyword.value
            for keyword in calls[0].keywords
        }
        expires_at = keywords["expires_at"]
        assert isinstance(expires_at, ast.Name)
        assert expires_at.id == "expires_at"


def test_management_downlink_preserves_authorization_metadata() -> None:
    plan = read_api._execution_order_plan(
        {
            "stop_loss": 95.0,
            "position_side": "long",
            "authorization": AUTHORIZATION,
            "attribution": ATTRIBUTION,
        },
        {"max_notional": 0.0},
        "BTCUSDT",
        action="move_stop_loss",
    )

    assert plan["stop_price"] == 95.0
    assert plan["position_side"] == "long"
    _assert_authorization_metadata(plan)


def test_zone_ladder_downlink_preserves_authorization_metadata(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_binance_mark_price",
        lambda _symbol: 102.0,
    )
    plan = read_api._execution_order_plan(
        {
            "side": "long",
            "entry": {
                "type": "zone",
                "price_min": 99.0,
                "price_max": 101.0,
            },
            "stop_loss": 90.0,
            "take_profits": [110.0, 120.0],
            "authorization": AUTHORIZATION,
            "attribution": ATTRIBUTION,
        },
        {"max_notional": 1000.0},
        "BTCUSDT",
        action="open_position",
    )

    assert plan["type"] == "zone_ladder"
    assert len(plan["tranches"]) == 3
    _assert_authorization_metadata(plan)


def test_disable_take_profits_downlink_preserves_tombstone_and_authorization() -> None:
    plan = read_api._execution_order_plan(
        {
            "take_profits": [],
            "disable_take_profits": True,
            "position_side": "long",
            "authorization": AUTHORIZATION,
            "attribution": ATTRIBUTION,
        },
        {"max_notional": 0.0},
        "BTCUSDT",
        action="replace_take_profits",
    )

    assert plan["take_profits"] == []
    assert plan["disable_take_profits"] is True
    assert plan["position_side"] == "long"
    _assert_authorization_metadata(plan)


def test_http_heartbeat_returns_peer_drift_receipt(monkeypatch) -> None:
    client = HttpControlPlaneClient(
        base_url="https://control-plane.invalid",
        token="node-a-token",
        node_id="node-a",
        account_id="account-a",
    )
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *args, **kwargs: {
            "ok": True,
            "release_gate": {
                "status": "pass",
                "release_id": "release-a",
                "rollout_phase": "fleet_complete",
                "reviewed_manifest": {
                    "release_id": "release-a",
                    "image_digest": "sha256:" + ("1" * 64),
                    "config_sha256": "2" * 64,
                    "dependency_lock_sha256": "3" * 64,
                    "schema_epoch": "0010_live_safety",
                },
            },
            "peers": [
                {
                    "node_id": "node-b",
                    "account_id": "account-b",
                    "release_id": "release-b",
                    "image_digest": "sha256:" + ("9" * 64),
                    "config_sha256": "2" * 64,
                    "dependency_lock_sha256": "3" * 64,
                    "schema_epoch": "0010_live_safety",
                    "freshness_age_seconds": 0.75,
                    "fresh": True,
                    "identity_matches": False,
                    "status": "identity_drift",
                }
            ],
        },
    )

    receipt = client.heartbeat(
        "node-a",
        Heartbeat(
            account_id="account-a",
            ts=datetime(2026, 8, 8, tzinfo=timezone.utc),
            trading_state=TradingState.HALTED,
            readiness=True,
            projection_lag_ms=0,
            reconciliation_state=ReconciliationState.HEALTHY,
            release_id="release-a",
            image_digest="sha256:" + ("1" * 64),
            config_sha256="2" * 64,
            dependency_lock_sha256="3" * 64,
            schema_epoch="0010_live_safety",
        ),
    )

    assert receipt.release_gate.status == "pass"
    assert receipt.release_gate.rollout_phase == "fleet_complete"
    assert receipt.peers[0].node_id == "node-b"
    assert receipt.peers[0].identity_matches is False
    assert receipt.peers[0].status == "identity_drift"
    assert receipt.requires_sticky_halt is False


def test_http_heartbeat_allows_explicit_rollout_pending_peer(
    monkeypatch,
) -> None:
    client = HttpControlPlaneClient(
        base_url="https://control-plane.invalid",
        token="node-a-token",
        node_id="node-a",
        account_id="account-a",
    )
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *args, **kwargs: {
            "ok": True,
            "release_gate": {
                "status": "pass",
                "release_id": "release-a",
                "rollout_phase": "account_a_canary",
                "reviewed_manifest": {
                    "release_id": "release-a",
                    "image_digest": "sha256:" + ("1" * 64),
                    "config_sha256": "2" * 64,
                    "dependency_lock_sha256": "3" * 64,
                    "schema_epoch": "0010_live_safety",
                },
            },
            "peers": [
                {
                    "node_id": "node-b",
                    "account_id": "account-b",
                    "release_id": "release-b",
                    "image_digest": "sha256:" + ("9" * 64),
                    "config_sha256": "2" * 64,
                    "dependency_lock_sha256": "3" * 64,
                    "schema_epoch": "0010_live_safety",
                    "freshness_age_seconds": 0.75,
                    "fresh": True,
                    "identity_matches": False,
                    "status": "rollout_pending",
                }
            ],
        },
    )

    receipt = client.heartbeat(
        "node-a",
        Heartbeat(
            account_id="account-a",
            ts=datetime(2026, 8, 8, tzinfo=timezone.utc),
            trading_state=TradingState.HALTED,
            readiness=True,
            projection_lag_ms=0,
            reconciliation_state=ReconciliationState.HEALTHY,
            release_id="release-a",
            image_digest="sha256:" + ("1" * 64),
            config_sha256="2" * 64,
            dependency_lock_sha256="3" * 64,
            schema_epoch="0010_live_safety",
        ),
    )

    assert receipt.peers[0].status == "rollout_pending"
    assert receipt.requires_sticky_halt is False


def _function_calls(function, called_name: str) -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(function))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == called_name
    ]
