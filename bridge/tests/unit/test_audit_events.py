from app.services.audit_events import AuditLog, create_audit_event, redact_payload


def test_audit_event_has_required_fields():
    event = create_audit_event(
        event_type="kill_switch_enabled",
        actor_id="user-1",
        actor_role="risk_admin",
        request_id="req-1",
        correlation_id="sig-1",
        reason="manual emergency stop",
        payload={"pair": "BTC/USDT:USDT"},
    )

    assert event["event_id"]
    assert event["event_type"] == "kill_switch_enabled"
    assert event["actor_id"] == "user-1"
    assert event["actor_role"] == "risk_admin"
    assert event["occurred_at"]
    assert event["request_id"] == "req-1"
    assert event["correlation_id"] == "sig-1"
    assert event["reason"] == "manual emergency stop"
    assert event["payload_redacted"] == {"pair": "BTC/USDT:USDT"}
    assert event["result"] == "success"


def test_payload_is_redacted():
    payload = {
        "password": "abc",
        "jwt_token": "token",
        "headers": {"Authorization": "Bearer eyJabc.def.ghi"},
        "message": "password=hunter2 token=secret-token",
        "nested": {"api_secret": "secret", "safe": "value"},
    }

    redacted = redact_payload(payload)

    assert redacted["password"] == "[REDACTED]"
    assert redacted["jwt_token"] == "[REDACTED]"
    assert redacted["headers"]["Authorization"] == "[REDACTED]"
    assert "hunter2" not in redacted["message"]
    assert "secret-token" not in redacted["message"]
    assert redacted["nested"]["api_secret"] == "[REDACTED]"
    assert redacted["nested"]["safe"] == "value"


def test_audit_log_records_events():
    log = AuditLog()

    event = log.record(
        event_type="pair_lock_created",
        actor_id="risk-admin",
        actor_role="risk_admin",
        request_id="req-2",
        correlation_id="BTC/USDT:USDT",
        reason="volatility",
        payload={"exchange_key": "live-key"},
    )

    assert log.list_events()[0]["event_id"] == event["event_id"]
    assert log.list_events()[0]["payload_redacted"]["exchange_key"] == "[REDACTED]"
