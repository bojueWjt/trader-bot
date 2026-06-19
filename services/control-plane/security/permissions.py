from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from enum import Enum
from functools import wraps
from typing import Any, TypeVar


class AuthRequired(PermissionError):
    pass


class PermissionDenied(PermissionError):
    pass


class Role(str, Enum):
    VIEWER = "viewer"
    REVIEWER = "reviewer"
    RISK_ADMIN = "risk_admin"
    SYSTEM_OBSERVER = "system_observer"
    NAUTILUS_NODE = "nautilus_node"


class Operation(str, Enum):
    DASHBOARD_READ = "dashboard_read"
    REPORTS_READ = "reports_read"
    RISK_READ = "risk_read"
    SIGNAL_READ = "signal_read"
    AUDIT_READ = "audit_read"
    REVIEW_DECISION_WRITE = "review_decision_write"
    KILL_SWITCH = "kill_switch"
    CLOSE_ALL = "close_all"
    PAIR_LOCK_WRITE = "pair_lock_write"
    RISK_OVERRIDE_WRITE = "risk_override_write"
    LIVE_GATE_APPROVAL = "live_gate_approval"
    EXECUTION_EVENT_WRITE = "execution_event_write"
    NODE_HEARTBEAT_WRITE = "node_heartbeat_write"
    EXECUTION_ACK_WRITE = "execution_ack_write"


TOKEN_ENV_VARS = (
    "RISK_ADMIN_TOKEN",
    "VIEWER_TOKEN",
    "REVIEWER_TOKEN",
    "SYSTEM_OBSERVER_TOKEN",
    "NAUTILUS_NODE_TOKEN",
)
REQUIRED_SECRET_ENV_VARS = ("CONTROL_PLANE_AUTH_SECRET",) + TOKEN_ENV_VARS

ROLE_PERMISSIONS: dict[Role, frozenset[Operation]] = {
    Role.VIEWER: frozenset(
        {
            Operation.DASHBOARD_READ,
            Operation.REPORTS_READ,
            Operation.RISK_READ,
            Operation.SIGNAL_READ,
        }
    ),
    Role.REVIEWER: frozenset(
        {
            Operation.DASHBOARD_READ,
            Operation.REPORTS_READ,
            Operation.RISK_READ,
            Operation.SIGNAL_READ,
            Operation.REVIEW_DECISION_WRITE,
        }
    ),
    Role.RISK_ADMIN: frozenset(
        set(Operation)
        - {
            Operation.EXECUTION_EVENT_WRITE,
            Operation.NODE_HEARTBEAT_WRITE,
            Operation.EXECUTION_ACK_WRITE,
        }
    ),
    Role.SYSTEM_OBSERVER: frozenset(
        {
            Operation.DASHBOARD_READ,
            Operation.REPORTS_READ,
            Operation.RISK_READ,
            Operation.SIGNAL_READ,
            Operation.AUDIT_READ,
        }
    ),
    Role.NAUTILUS_NODE: frozenset(
        {
            Operation.EXECUTION_EVENT_WRITE,
            Operation.NODE_HEARTBEAT_WRITE,
            Operation.EXECUTION_ACK_WRITE,
        }
    ),
}

F = TypeVar("F", bound=Callable[..., Any])


def validate_required_secrets(env: Mapping[str, str] | None = None) -> None:
    source = os.environ if env is None else env
    missing = [name for name in REQUIRED_SECRET_ENV_VARS if not source.get(name, "").strip()]
    if missing:
        raise ValueError(f"missing required security environment variables: {', '.join(missing)}")

    token_values = [source[name].strip() for name in TOKEN_ENV_VARS]
    if len(set(token_values)) != len(token_values):
        raise ValueError("security token values must be unique")


def require_user(actor: Mapping[str, Any] | None) -> dict[str, str]:
    if not actor:
        raise AuthRequired("login required")

    actor_id = actor.get("actor_id")
    role = actor.get("role")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise AuthRequired("login required")
    if not isinstance(role, str) or not role.strip():
        raise AuthRequired("login required")

    try:
        Role(role)
    except ValueError as exc:
        raise PermissionDenied("unknown role") from exc

    return {"actor_id": actor_id, "role": role}


def has_permission(role: Role | str, operation: Operation | str) -> bool:
    try:
        role_value = _role(role)
        operation_value = _operation(operation)
    except ValueError:
        return False
    return operation_value in ROLE_PERMISSIONS[role_value]


def require_permission(operation: Operation | str) -> Callable[[Any], Any]:
    operation_value = _operation(operation)

    def guard(target: Any) -> Any:
        if callable(target) and not isinstance(target, Mapping):
            return _decorate(target, operation_value)
        return _require_actor_permission(target, operation_value)

    return guard


def _decorate(function: F, operation: Operation) -> F:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        actor = kwargs.get("actor")
        if actor is None:
            actor = _first_actor_arg(args)
        _require_actor_permission(actor, operation)
        return function(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def _first_actor_arg(args: tuple[Any, ...]) -> Mapping[str, Any] | None:
    for arg in args:
        if isinstance(arg, Mapping) and "actor_id" in arg and "role" in arg:
            return arg
    return None


def _require_actor_permission(actor: Mapping[str, Any] | None, operation: Operation) -> dict[str, str]:
    user = require_user(actor)
    if not has_permission(user["role"], operation):
        raise PermissionDenied("permission denied")
    return user


def _role(value: Role | str) -> Role:
    if isinstance(value, Role):
        return value
    return Role(str(value))


def _operation(value: Operation | str) -> Operation:
    if isinstance(value, Operation):
        return value
    return Operation(str(value))
