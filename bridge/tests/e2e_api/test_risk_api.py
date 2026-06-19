from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.audit.router import router as audit_router
from app.risk.router import reset_runtime_state, router as risk_router
from app.security.router import router as security_router


def build_client():
    reset_runtime_state()
    app = FastAPI()
    app.include_router(risk_router, prefix="/api/risk")
    app.include_router(audit_router, prefix="/api/audit")
    app.include_router(security_router, prefix="/api/security")
    return TestClient(app)


def auth_headers(role="risk_admin"):
    token = "test-risk-admin-token"
    if role == "viewer":
        token = "test-viewer-token"
    if role == "observer":
        token = "test-system-observer-token"
    if role == "trader":
        token = "test-trader-token"
    return {
        "authorization": f"Bearer {token}",
        "x-request-id": "req-1",
    }


def test_risk_overview_requires_login():
    client = build_client()

    response = client.get("/api/risk/overview")

    assert response.status_code == 401


def test_unknown_role_gets_403_for_overview():
    client = build_client()

    response = client.get(
        "/api/risk/overview",
        headers={"authorization": "Bearer unknown-token"},
    )

    assert response.status_code == 403


def test_failed_auth_attempt_is_audited():
    client = build_client()

    client.get("/api/risk/overview", headers={"x-request-id": "req-auth-fail"})
    response = client.get("/api/audit/events", headers=auth_headers())

    events = response.json()["events"]
    event_types = [event["event_type"] for event in events]
    failed_events = [event for event in events if event["event_type"] == "failed_auth_attempt"]
    assert "failed_auth_attempt" in event_types
    assert failed_events[0]["request_id"] == "req-auth-fail"


def test_audit_events_require_risk_admin():
    client = build_client()

    response = client.get("/api/audit/events", headers=auth_headers(role="viewer"))

    assert response.status_code == 403


def test_precheck_invalid_payload_returns_422():
    client = build_client()

    response = client.post(
        "/api/risk/precheck",
        json={"signal": {"side": "long", "leverage": None}, "account": {"equity": 10000}},
        headers=auth_headers(),
    )

    assert response.status_code == 422


def test_kill_switch_requires_request_id():
    client = build_client()

    response = client.post(
        "/api/risk/kill-switch",
        json={"confirm": True, "reason": "manual emergency stop"},
        headers={"authorization": "Bearer test-risk-admin-token"},
    )

    assert response.status_code == 422


def test_kill_switch_api_blocks_new_entries():
    client = build_client()

    kill_response = client.post(
        "/api/risk/kill-switch",
        json={"confirm": True, "reason": "manual emergency stop"},
        headers=auth_headers(),
    )
    precheck_response = client.post(
        "/api/risk/precheck",
        json={
            "signal": {
                "pair": "BTC/USDT:USDT",
                "side": "long",
                "entry_price": 71000,
                "stop_loss": 70400,
                "take_profits": [72000],
                "notional": 1000,
                "liquidation_buffer_pct": 5,
            },
            "account": {"equity": 10000},
        },
        headers=auth_headers(),
    )

    assert kill_response.status_code == 200
    assert precheck_response.json()["decision"] == "blocked"
    assert "kill_switch_enabled" in precheck_response.json()["reason_codes"]
    audit_response = client.get("/api/audit/events", headers=auth_headers())
    event_types = [event["event_type"] for event in audit_response.json()["events"]]
    assert "kill_switch_enabled" in event_types
    assert "risk_blocked" in event_types


def test_risk_explain_blocked_signal_returns_rule_details():
    client = build_client()

    precheck_response = client.post(
        "/api/risk/precheck",
        json={
            "signal": {
                "signal_id": "sig-leverage-blocked",
                "pair": "BTC/USDT:USDT",
                "side": "long",
                "entry_price": 71000,
                "stop_loss": 70400,
                "take_profits": [72000],
                "leverage": 8,
                "notional": 1000,
                "liquidation_buffer_pct": 5,
            },
            "account": {"equity": 10000},
        },
        headers=auth_headers(),
    )

    response = client.get(
        "/api/risk/explain?signal_id=sig-leverage-blocked",
        headers=auth_headers(),
    )
    body = response.json()

    assert precheck_response.status_code == 200
    assert precheck_response.json()["decision"] == "blocked"
    assert response.status_code == 200
    assert body == {
        "status": "blocked",
        "rule_name": "leverage_exceeded",
        "current_value": 8,
        "limit": 5,
        "gap_to_allow": 3,
    }


def test_risk_explain_not_blocked_signal_returns_not_blocked():
    client = build_client()

    precheck_response = client.post(
        "/api/risk/precheck",
        json={
            "signal": {
                "signal_id": "sig-approved",
                "pair": "BTC/USDT:USDT",
                "side": "long",
                "entry_price": 71000,
                "stop_loss": 70400,
                "take_profits": [72000],
                "notional": 1000,
                "liquidation_buffer_pct": 5,
            },
            "account": {"equity": 10000},
        },
        headers=auth_headers(),
    )

    response = client.get("/api/risk/explain?signal_id=sig-approved", headers=auth_headers())

    assert precheck_response.status_code == 200
    assert precheck_response.json()["decision"] == "approved"
    assert response.status_code == 200
    assert response.json() == {"status": "not_blocked"}


def test_risk_explain_unknown_signal_returns_not_found():
    client = build_client()

    response = client.get("/api/risk/explain?signal_id=missing-signal", headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {"status": "not_found"}


def test_risk_explain_allows_viewer_and_observer_roles():
    client = build_client()
    client.post(
        "/api/risk/precheck",
        json={
            "signal": {
                "signal_id": "sig-role-readable",
                "pair": "BTC/USDT:USDT",
                "side": "long",
                "entry_price": 71000,
                "stop_loss": 70400,
                "take_profits": [72000],
                "notional": 1000,
                "liquidation_buffer_pct": 5,
            },
            "account": {"equity": 10000},
        },
        headers=auth_headers(),
    )

    viewer_response = client.get(
        "/api/risk/explain?signal_id=sig-role-readable",
        headers=auth_headers(role="viewer"),
    )
    observer_response = client.get(
        "/api/risk/explain?signal_id=sig-role-readable",
        headers=auth_headers(role="observer"),
    )

    assert viewer_response.status_code == 200
    assert viewer_response.json()["status"] == "not_blocked"
    assert observer_response.status_code == 200
    assert observer_response.json()["status"] == "not_blocked"


def test_viewer_gets_403_for_kill_switch():
    client = build_client()

    response = client.post(
        "/api/risk/kill-switch",
        json={"confirm": True, "reason": "test"},
        headers=auth_headers(role="viewer"),
    )

    assert response.status_code == 403


def test_close_all_requires_phrase():
    client = build_client()

    response = client.post(
        "/api/risk/kill-switch/close-all",
        json={"confirm": True, "reason": "emergency", "phrase": "WRONG"},
        headers=auth_headers(),
    )

    assert response.status_code == 422


def test_pair_lock_api_blocks_and_expires():
    client = build_client()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    create_response = client.post(
        "/api/risk/pair-locks",
        json={
            "pair": "ETH/USDT:USDT",
            "reason": "event risk",
            "expires_at": expires_at.isoformat(),
            "confirm": True,
        },
        headers=auth_headers(),
    )
    precheck_response = client.post(
        "/api/risk/precheck",
        json={
            "signal": {
                "pair": "ETH/USDT:USDT",
                "side": "long",
                "entry_price": 3000,
                "stop_loss": 2900,
                "take_profits": [3200],
                "notional": 100,
                "liquidation_buffer_pct": 5,
            },
            "account": {"equity": 10000},
        },
        headers=auth_headers(),
    )

    assert create_response.status_code == 200
    assert precheck_response.json()["decision"] == "blocked"
    assert "pair_locked" in precheck_response.json()["reason_codes"]
    audit_response = client.get("/api/audit/events", headers=auth_headers())
    event_types = [event["event_type"] for event in audit_response.json()["events"]]
    assert "pair_lock_created" in event_types


def test_pair_lock_invalid_expires_at_returns_422():
    client = build_client()

    response = client.post(
        "/api/risk/pair-locks",
        json={
            "pair": "ETH/USDT:USDT",
            "reason": "event risk",
            "expires_at": "not-a-date",
            "confirm": True,
        },
        headers=auth_headers(),
    )

    assert response.status_code == 422


def test_audit_and_security_scan_endpoints():
    client = build_client()
    client.post(
        "/api/risk/kill-switch",
        json={"confirm": True, "reason": "manual emergency stop"},
        headers=auth_headers(),
    )

    audit_response = client.get("/api/audit/events", headers=auth_headers())
    scan_response = client.post(
        "/api/security/scan-report",
        json={"source": "report.md", "text": "password=abc1234"},
        headers=auth_headers(),
    )

    assert audit_response.status_code == 200
    event_types = [event["event_type"] for event in audit_response.json()["events"]]
    assert "kill_switch" in event_types
    assert scan_response.json()["passed"] is False


def test_kill_switch_disable_steps_down_then_resumes_normal():
    client = build_client()

    enable = client.post(
        "/api/risk/kill-switch",
        json={"confirm": True, "reason": "drill enable"},
        headers=auth_headers(),
    )
    assert enable.status_code == 200
    assert enable.json()["risk_state"] == "kill_switch_enabled"

    disable = client.post(
        "/api/risk/kill-switch/disable",
        json={"confirm": True, "reason": "drill recovery step-down"},
        headers=auth_headers(),
    )
    assert disable.status_code == 200
    assert disable.json()["risk_state"] == "blocked_new_entries"

    enable_again = client.post(
        "/api/risk/kill-switch",
        json={"confirm": True, "reason": "drill enable again"},
        headers=auth_headers(),
    )
    assert enable_again.status_code == 200

    resume = client.post(
        "/api/risk/kill-switch/disable",
        json={"confirm": True, "reason": "drill full recovery", "resume_normal": True},
        headers=auth_headers(),
    )
    assert resume.status_code == 200
    assert resume.json()["risk_state"] == "normal"

    audit_response = client.get("/api/audit/events", headers=auth_headers())
    event_types = [event["event_type"] for event in audit_response.json()["events"]]
    assert "kill_switch_disabled" in event_types


def test_kill_switch_disable_requires_risk_admin():
    client = build_client()
    response = client.post(
        "/api/risk/kill-switch/disable",
        json={"confirm": True, "reason": "viewer should fail"},
        headers=auth_headers(role="viewer"),
    )
    assert response.status_code == 403
