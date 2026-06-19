from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from app.dependencies import require_bearer_user
from app.risk.router import audit_log
from app.services.permissions import (
    PermissionError403,
    ValidationError422,
    require_dangerous_operation,
)
from app.services.release_gates import ReleaseGateEvaluator


router = APIRouter()
evaluator = ReleaseGateEvaluator()
approval_state = {
    "live_readonly": False,
    "live_small_size": False,
}
approval_evidence = {
    "live_readonly": {},
    "live_small_size": {},
}


def reset_runtime_state() -> None:
    approval_state["live_readonly"] = False
    approval_state["live_small_size"] = False
    approval_evidence["live_readonly"] = {}
    approval_evidence["live_small_size"] = {}


def _user(authorization: str | None) -> dict:
    return require_bearer_user(authorization)


def _approve(user: dict, body: dict, request_id: str | None, gate: str) -> dict:
    try:
        require_dangerous_operation(
            user=user,
            operation="live_gate_approval",
            payload=body,
            audit_log=audit_log,
            request_id=request_id,
            correlation_id=gate,
        )
    except PermissionError403 as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValidationError422 as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    evidence = body.get("evidence", {})
    status = evaluator.evaluate(evidence)[gate]
    if not status["passed"]:
        audit_log.record(
            event_type=f"{gate}_gate_blocked",
            actor_id=user["actor_id"],
            actor_role=user["role"],
            request_id=request_id,
            correlation_id=gate,
            reason=body["reason"],
            payload={"missing": status["missing"]},
            result="blocked",
        )
        raise HTTPException(status_code=422, detail=status)
    approval_state[gate] = True
    approval_evidence[gate] = dict(evidence)
    audit_log.record(
        event_type=f"{gate}_gate_approved",
        actor_id=user["actor_id"],
        actor_role=user["role"],
        request_id=request_id,
        correlation_id=gate,
        reason=body["reason"],
        payload=evidence,
    )
    return {"approved": True, "gate": gate}


@router.get("/status")
def status(
    authorization: str | None = Header(default=None),
):
    _user(authorization)
    evidence = {}
    evidence.update(approval_evidence["live_readonly"])
    evidence.update(approval_evidence["live_small_size"])
    return evaluator.evaluate(evidence)


@router.post("/approve-live-readonly")
def approve_live_readonly(
    body: dict,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    user = _user(authorization)
    return _approve(user, body, x_request_id, "live_readonly")


@router.post("/approve-live-small-size")
def approve_live_small_size(
    body: dict,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    user = _user(authorization)
    return _approve(user, body, x_request_id, "live_small_size")
