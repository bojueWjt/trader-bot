from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .audit import dangerous_operation_payload, record_audit_event
from .permissions import Operation, require_permission


class DangerousOperationValidationError(ValueError):
    pass


class DangerousAuditSink(Protocol):
    def record(self, event: dict[str, Any]) -> Any:
        ...


@dataclass(frozen=True)
class DangerousOperationRequest:
    request_id: str
    reason: str
    confirm: bool
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DangerousOperationDecision:
    allowed: bool
    operation: str
    request_id: str
    actor_id: str
    actor_role: str


def require_dangerous_operation(
    *,
    actor: Mapping[str, Any],
    operation: Operation | str,
    request: DangerousOperationRequest | Mapping[str, Any],
    audit_sink: DangerousAuditSink | None = None,
    conn: Any | None = None,
    aggregate_id: str = "",
) -> DangerousOperationDecision:
    user = require_permission(operation)(actor)
    operation_value = operation.value if isinstance(operation, Operation) else str(operation)
    dangerous_request = _coerce_request(request)
    _validate_request(dangerous_request)

    audit_payload = dangerous_operation_payload(
        request_id=dangerous_request.request_id,
        reason=dangerous_request.reason,
        actor=user,
        operation=operation_value,
        payload=dangerous_request.payload,
    )
    _write_audit(
        audit_sink=audit_sink,
        conn=conn,
        operation=operation_value,
        aggregate_id=aggregate_id or operation_value,
        actor_id=user["actor_id"],
        audit_payload=audit_payload,
    )
    return DangerousOperationDecision(
        allowed=True,
        operation=operation_value,
        request_id=dangerous_request.request_id,
        actor_id=user["actor_id"],
        actor_role=user["role"],
    )


def _coerce_request(request: DangerousOperationRequest | Mapping[str, Any]) -> DangerousOperationRequest:
    if isinstance(request, DangerousOperationRequest):
        return request
    return DangerousOperationRequest(
        request_id=str(request.get("request_id", "")),
        reason=str(request.get("reason", "")),
        confirm=bool(request.get("confirm", False)),
        payload=request.get("payload", {}),
    )


def _validate_request(request: DangerousOperationRequest) -> None:
    if not request.request_id.strip():
        raise DangerousOperationValidationError("request_id required")
    if not request.reason.strip():
        raise DangerousOperationValidationError("reason required")
    if request.confirm is not True:
        raise DangerousOperationValidationError("confirmation required")


def _write_audit(
    *,
    audit_sink: DangerousAuditSink | None,
    conn: Any | None,
    operation: str,
    aggregate_id: str,
    actor_id: str,
    audit_payload: dict[str, Any],
) -> None:
    if audit_sink is not None:
        audit_sink.record(
            {
                "event_type": operation,
                "aggregate_type": "dangerous_operation",
                "aggregate_id": aggregate_id,
                "actor": actor_id,
                "payload": audit_payload,
            }
        )
        return
    if conn is None:
        raise DangerousOperationValidationError("audit writer required")
    record_audit_event(
        conn,
        event_type=operation,
        aggregate_type="dangerous_operation",
        aggregate_id=aggregate_id,
        actor=actor_id,
        payload=audit_payload,
        trace_id=audit_payload["request_id"],
    )
