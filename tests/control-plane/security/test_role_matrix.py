from __future__ import annotations

import pytest

from security import (
    DangerousOperationValidationError,
    Operation,
    PermissionDenied,
    Role,
    has_permission,
    require_dangerous_operation,
    require_permission,
)


def _actor(role: Role) -> dict[str, str]:
    return {"actor_id": f"{role.value}-actor", "role": role.value}


def test_role_matrix_covers_five_roles_and_major_operations() -> None:
    expected = {
        Role.VIEWER: {
            Operation.DASHBOARD_READ,
            Operation.REPORTS_READ,
            Operation.RISK_READ,
            Operation.SIGNAL_READ,
        },
        Role.REVIEWER: {
            Operation.DASHBOARD_READ,
            Operation.REPORTS_READ,
            Operation.RISK_READ,
            Operation.SIGNAL_READ,
            Operation.REVIEW_DECISION_WRITE,
        },
        Role.RISK_ADMIN: set(Operation)
        - {
            Operation.EXECUTION_EVENT_WRITE,
            Operation.NODE_HEARTBEAT_WRITE,
            Operation.EXECUTION_ACK_WRITE,
        },
        Role.SYSTEM_OBSERVER: {
            Operation.DASHBOARD_READ,
            Operation.REPORTS_READ,
            Operation.RISK_READ,
            Operation.SIGNAL_READ,
            Operation.AUDIT_READ,
        },
        Role.NAUTILUS_NODE: {
            Operation.EXECUTION_EVENT_WRITE,
            Operation.NODE_HEARTBEAT_WRITE,
            Operation.EXECUTION_ACK_WRITE,
        },
    }

    assert {role.value for role in Role} == {
        "viewer",
        "reviewer",
        "risk_admin",
        "system_observer",
        "nautilus_node",
    }

    for role in Role:
        for operation in Operation:
            assert has_permission(role.value, operation.value) is (operation in expected[role])


def test_system_observer_is_read_only() -> None:
    write_operations = [operation for operation in Operation if not operation.value.endswith("_read")]

    assert write_operations
    for operation in write_operations:
        assert has_permission(Role.SYSTEM_OBSERVER.value, operation.value) is False


def test_nautilus_node_can_only_write_execution_event_heartbeat_and_ack() -> None:
    allowed = {
        Operation.EXECUTION_EVENT_WRITE,
        Operation.NODE_HEARTBEAT_WRITE,
        Operation.EXECUTION_ACK_WRITE,
    }

    for operation in Operation:
        assert has_permission(Role.NAUTILUS_NODE.value, operation.value) is (operation in allowed)


def test_require_permission_works_as_dependency_and_decorator() -> None:
    dependency = require_permission(Operation.KILL_SWITCH)

    assert dependency(_actor(Role.RISK_ADMIN))["role"] == Role.RISK_ADMIN.value
    with pytest.raises(PermissionDenied):
        dependency(_actor(Role.VIEWER))

    @require_permission(Operation.RISK_READ)
    def endpoint(*, actor: dict[str, str]) -> str:
        return actor["actor_id"]

    assert endpoint(actor=_actor(Role.VIEWER)) == "viewer-actor"


def test_dangerous_operation_requires_confirmation_metadata_and_audit() -> None:
    class AuditSink:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def record(self, event: dict) -> None:
            self.events.append(event)

    actor = _actor(Role.RISK_ADMIN)
    sink = AuditSink()

    for bad_request in (
        {"request_id": "", "reason": "manual stop", "confirm": True},
        {"request_id": "req-danger-1", "reason": " ", "confirm": True},
        {"request_id": "req-danger-1", "reason": "manual stop", "confirm": False},
    ):
        with pytest.raises(DangerousOperationValidationError):
            require_dangerous_operation(
                actor=actor,
                operation=Operation.KILL_SWITCH,
                request=bad_request,
                audit_sink=sink,
            )

    decision = require_dangerous_operation(
        actor=actor,
        operation=Operation.KILL_SWITCH,
        request={
            "request_id": "req-danger-1",
            "reason": "manual stop",
            "confirm": True,
            "payload": {"exchange_key": "unit-sensitive-value"},
        },
        audit_sink=sink,
    )

    assert decision.allowed is True
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event["event_type"] == Operation.KILL_SWITCH.value
    assert event["actor"] == "risk_admin-actor"
    assert event["payload"]["request_id"] == "req-danger-1"
    assert event["payload"]["reason"] == "manual stop"
    assert event["payload"]["actor"] == {"actor_id": "risk_admin-actor", "role": "risk_admin"}
    assert event["payload"]["timestamp"]
    assert event["payload"]["payload"]["exchange_key"] == "[REDACTED]"
