from app.services.release_gates import ReleaseGateEvaluator
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security.release_gates_router import reset_runtime_state, router


def auth_headers(role="risk_admin", request_id=""):
    token = "test-risk-admin-token"
    if role == "viewer":
        token = "test-viewer-token"
    headers = {"authorization": f"Bearer {token}"}
    if request_id:
        headers["x-request-id"] = request_id
    return headers


def test_release_gate_status_computes_all_levels():
    evaluator = ReleaseGateEvaluator()
    evidence = {
        "unit_tests_passed": True,
        "security_tests_passed": True,
        "strategy_loads": True,
        "dashboard_fake_data": True,
        "fallback_report_generated": True,
        "integration_tests_passed": True,
        "e2e_tests_passed": True,
        "kill_switch_manual_acceptance": True,
        "freqtrade_api_local_only": True,
        "exchange_key_testnet_only": True,
        "live_auto_entries_disabled": True,
        "dashboard_live_readonly": True,
        "live_snapshot_report": True,
        "audit_logs_secret_free": True,
        "recovery_drill_done": True,
        "dry_run_report_days": 7,
        "dry_run_signal_count": 30,
        "blocked_signals_have_reasons": True,
        "kill_switch_drill_passed": True,
        "daily_loss_guard_drill_passed": True,
        "default_single_trade_risk_pct": 0.25,
    }

    status = evaluator.evaluate(evidence)

    assert status["dry_run"]["passed"] is True
    assert status["testnet"]["passed"] is True
    assert status["live_readonly"]["passed"] is True
    assert status["live_small_size"]["passed"] is True


def test_live_small_size_blocks_above_quarter_percent_risk():
    evaluator = ReleaseGateEvaluator()

    status = evaluator.evaluate({"default_single_trade_risk_pct": 1.0})

    assert status["live_small_size"]["passed"] is False
    assert "default_single_trade_risk_pct" in status["live_small_size"]["missing"]


def test_release_gate_status_api():
    reset_runtime_state()
    app = FastAPI()
    app.include_router(router, prefix="/api/release-gates")
    client = TestClient(app)

    response = client.get(
        "/api/release-gates/status",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert "dry_run" in response.json()


def test_live_gate_approval_requires_risk_admin():
    reset_runtime_state()
    app = FastAPI()
    app.include_router(router, prefix="/api/release-gates")
    client = TestClient(app)

    response = client.post(
        "/api/release-gates/approve-live-readonly",
        json={"confirm": True, "reason": "read only drill complete"},
        headers=auth_headers(role="viewer"),
    )

    assert response.status_code == 403


def test_live_small_size_approval_requires_gate_evidence():
    reset_runtime_state()
    app = FastAPI()
    app.include_router(router, prefix="/api/release-gates")
    client = TestClient(app)

    response = client.post(
        "/api/release-gates/approve-live-small-size",
        json={"confirm": True, "reason": "missing evidence"},
        headers=auth_headers(request_id="req-1"),
    )

    assert response.status_code == 422
    assert "dry_run_report_days" in response.json()["detail"]["missing"]


def test_live_readonly_approval_requires_request_id():
    reset_runtime_state()
    app = FastAPI()
    app.include_router(router, prefix="/api/release-gates")
    client = TestClient(app)

    response = client.post(
        "/api/release-gates/approve-live-readonly",
        json={
            "confirm": True,
            "reason": "read only drill complete",
            "evidence": {
                "live_auto_entries_disabled": True,
                "dashboard_live_readonly": True,
                "live_snapshot_report": True,
                "audit_logs_secret_free": True,
                "recovery_drill_done": True,
            },
        },
        headers=auth_headers(),
    )

    assert response.status_code == 422


def test_live_readonly_status_uses_approved_evidence():
    reset_runtime_state()
    app = FastAPI()
    app.include_router(router, prefix="/api/release-gates")
    client = TestClient(app)
    evidence = {
        "live_auto_entries_disabled": True,
        "dashboard_live_readonly": True,
        "live_snapshot_report": True,
        "audit_logs_secret_free": True,
        "recovery_drill_done": True,
    }

    approve_response = client.post(
        "/api/release-gates/approve-live-readonly",
        json={
            "confirm": True,
            "reason": "read only drill complete",
            "evidence": evidence,
        },
        headers=auth_headers(request_id="req-2"),
    )
    status_response = client.get(
        "/api/release-gates/status",
        headers=auth_headers(),
    )

    assert approve_response.status_code == 200
    assert status_response.json()["live_readonly"]["passed"] is True
