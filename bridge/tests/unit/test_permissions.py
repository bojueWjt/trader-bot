import pytest

from app.services.audit_events import AuditLog
from app.services.permissions import (
    PermissionError403,
    READ_PERMISSIONS,
    RISK_ADMIN_WRITE_PERMISSIONS,
    ROLE_SYSTEM_OBSERVER,
    ValidationError422,
    has_permission,
    require_user,
    require_dangerous_operation,
)


@pytest.mark.parametrize("operation", sorted(READ_PERMISSIONS))
def test_system_observer_can_read_all_read_permission_categories(operation):
    assert has_permission(ROLE_SYSTEM_OBSERVER, operation) is True


@pytest.mark.parametrize("operation", sorted(RISK_ADMIN_WRITE_PERMISSIONS))
def test_system_observer_forbidden_for_write_permission_categories(operation):
    with pytest.raises(PermissionError403):
        require_dangerous_operation(
            user={"actor_id": "observer-1", "role": ROLE_SYSTEM_OBSERVER},
            operation=operation,
            payload={"confirm": True, "reason": "read-only observer"},
            request_id="req-observer-denied",
        )


def test_system_observer_is_a_known_role():
    user = {"actor_id": "observer-1", "role": ROLE_SYSTEM_OBSERVER}

    assert require_user(user) == user


def test_viewer_forbidden_for_dangerous_operations():
    with pytest.raises(PermissionError403):
        require_dangerous_operation(
            user={"actor_id": "viewer-1", "role": "viewer"},
            operation="close_trade",
            payload={"confirm": True, "reason": "bad fill"},
        )


def test_unknown_role_is_forbidden():
    with pytest.raises(PermissionError403):
        require_user({"actor_id": "unknown-1", "role": "operator"})


def test_trader_cannot_override_risk():
    with pytest.raises(PermissionError403):
        require_dangerous_operation(
            user={"actor_id": "trader-1", "role": "trader"},
            operation="risk_override",
            payload={"confirm": True, "reason": "manual approval"},
        )


def test_missing_confirmation_returns_422():
    with pytest.raises(ValidationError422):
        require_dangerous_operation(
            user={"actor_id": "admin-1", "role": "risk_admin"},
            operation="kill_switch",
            payload={"reason": "emergency"},
        )


def test_missing_request_id_returns_422():
    with pytest.raises(ValidationError422):
        require_dangerous_operation(
            user={"actor_id": "admin-1", "role": "risk_admin"},
            operation="kill_switch",
            payload={"confirm": True, "reason": "emergency"},
            request_id="",
        )


def test_reason_required():
    with pytest.raises(ValidationError422):
        require_dangerous_operation(
            user={"actor_id": "admin-1", "role": "risk_admin"},
            operation="kill_switch",
            payload={"confirm": True, "reason": " "},
        )


def test_risk_admin_can_kill_switch_and_audit_is_written():
    log = AuditLog()

    result = require_dangerous_operation(
        user={"actor_id": "admin-1", "role": "risk_admin"},
        operation="kill_switch",
        payload={"confirm": True, "reason": "emergency"},
        audit_log=log,
        request_id="req-1",
    )

    assert result is True
    assert log.list_events()[0]["event_type"] == "kill_switch"
