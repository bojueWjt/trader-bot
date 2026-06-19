from __future__ import annotations

from app.services.audit_events import AuditLog


class AuthError401(Exception):
    pass


class PermissionError403(Exception):
    pass


class ValidationError422(Exception):
    pass


ROLE_SYSTEM_OBSERVER = "system_observer"

READ_PERMISSIONS = {
    "dashboard_read",
    "reports_read",
    "risk_overview_read",
    "audit_read",
}

TRADER_WRITE_PERMISSIONS = {
    "close_trade",
    "partial_close",
    "move_stoploss",
    "pause_bot",
    "resume_bot",
}

RISK_ADMIN_WRITE_PERMISSIONS = TRADER_WRITE_PERMISSIONS | {
    "kill_switch",
    "close_all",
    "pair_lock",
    "risk_override",
    "live_gate_approval",
}

ROLE_PERMISSIONS = {
    "viewer": {
        "dashboard_read",
        "reports_read",
        "risk_overview_read",
    },
    "trader": {"dashboard_read", "reports_read", "risk_overview_read"} | TRADER_WRITE_PERMISSIONS,
    "risk_admin": READ_PERMISSIONS | RISK_ADMIN_WRITE_PERMISSIONS,
    ROLE_SYSTEM_OBSERVER: READ_PERMISSIONS,
}


def has_permission(role: str, operation: str) -> bool:
    permissions = ROLE_PERMISSIONS.get(role, set())
    return operation in permissions


def require_user(user: dict | None) -> dict:
    if not user:
        raise AuthError401("login required")
    actor_id = user.get("actor_id")
    role = user.get("role")
    if not actor_id or not role:
        raise AuthError401("login required")
    if role not in ROLE_PERMISSIONS:
        raise PermissionError403("unknown role")
    return user


def require_dangerous_operation(
    user: dict | None,
    operation: str,
    payload: dict,
    audit_log: AuditLog | None = None,
    request_id: str = "manual-request",
    correlation_id: str = "",
) -> bool:
    user = require_user(user)
    role = user["role"]
    if not has_permission(role, operation):
        raise PermissionError403("permission denied")
    if not request_id:
        raise ValidationError422("request id required")
    if request_id == "request-missing":
        raise ValidationError422("request id required")
    if not payload.get("confirm"):
        raise ValidationError422("confirmation required")
    reason = payload.get("reason")
    if not reason or not reason.strip():
        raise ValidationError422("reason required")
    if audit_log:
        audit_log.record(
            event_type=operation,
            actor_id=user["actor_id"],
            actor_role=role,
            request_id=request_id,
            correlation_id=correlation_id,
            reason=reason,
            payload=payload,
        )
    return True
