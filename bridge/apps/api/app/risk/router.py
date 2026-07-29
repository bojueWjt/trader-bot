from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException

from app.dependencies import require_bearer_user
from app.services.audit_events import AuditLog
from app.services.permissions import (
    AuthError401,
    PermissionError403,
    ValidationError422,
    require_dangerous_operation,
    require_user,
)
from app.services.risk_governor import RiskGovernor
from app.services.risk_governor import JsonRiskStateStore


router = APIRouter()
audit_log = AuditLog()
state_path = os.environ.get("RISK_STATE_PATH")
state_store = JsonRiskStateStore(state_path) if state_path else None
governor = RiskGovernor(state_store=state_store)


def reset_runtime_state() -> None:
    global audit_log, governor

    audit_log = AuditLog()
    governor = RiskGovernor(state_store=state_store)


def _user(authorization: str | None, x_request_id: str | None = None) -> dict:
    try:
        observer_token = os.environ.get("SYSTEM_OBSERVER_API_TOKEN", "").strip()
        if observer_token and authorization == f"Bearer {observer_token}":
            return require_user({"actor_id": "system-observer", "role": "system_observer"})
        return require_bearer_user(authorization)
    except HTTPException as exc:
        audit_log.record(
            event_type="failed_auth_attempt",
            actor_id="anonymous",
            actor_role="unknown",
            request_id=x_request_id or "auth-failed",
            correlation_id="",
            reason=str(exc.detail),
            payload={},
            result="failed",
        )
        raise


def _guard(user: dict, operation: str, payload: dict, request_id: str, correlation_id: str = "") -> None:
    try:
        require_dangerous_operation(
            user=user,
            operation=operation,
            payload=payload,
            audit_log=audit_log,
            request_id=request_id,
            correlation_id=correlation_id,
        )
    except AuthError401 as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except PermissionError403 as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValidationError422 as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/overview")
def overview(
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    _user(authorization, x_request_id)
    return governor.overview()


@router.get("/explain")
def explain(
    signal_id: str,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    _user(authorization, x_request_id)
    return governor.explain(signal_id)


def _validate_precheck_payload(signal: dict, account: dict) -> None:
    if not isinstance(signal, dict):
        raise HTTPException(status_code=422, detail="signal must be an object")
    if not isinstance(account, dict):
        raise HTTPException(status_code=422, detail="account must be an object")
    numeric_signal_fields = ["entry_price", "stop_loss", "leverage", "notional", "liquidation_buffer_pct"]
    for field in numeric_signal_fields:
        if field not in signal:
            continue
        value = signal[field]
        if not isinstance(value, (int, float)):
            raise HTTPException(status_code=422, detail=f"{field} must be numeric")
    if "take_profits" in signal:
        take_profits = signal["take_profits"]
        if not isinstance(take_profits, list):
            raise HTTPException(status_code=422, detail="take_profits must be a list")
        for value in take_profits:
            if not isinstance(value, (int, float)):
                raise HTTPException(status_code=422, detail="take_profits values must be numeric")
    numeric_account_fields = ["equity", "open_risk_pct", "daily_realized_loss_pct"]
    for field in numeric_account_fields:
        if field not in account:
            continue
        value = account[field]
        if not isinstance(value, (int, float)):
            raise HTTPException(status_code=422, detail=f"{field} must be numeric")


@router.post("/precheck")
def precheck(
    body: dict,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default="precheck-request"),
):
    user = _user(authorization, x_request_id)
    signal = body.get("signal", {})
    account = body.get("account", {})
    _validate_precheck_payload(signal, account)
    try:
        result = governor.precheck(signal, account)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result["decision"] == "blocked":
        audit_log.record(
            event_type="risk_blocked",
            actor_id=user["actor_id"],
            actor_role=user["role"],
            request_id=x_request_id or "precheck-request",
            correlation_id=signal.get("signal_id", signal.get("pair", "")),
            reason=",".join(result["reason_codes"]),
            payload={"signal": signal, "result": result},
            result="blocked",
        )
    return result


@router.post("/kill-switch")
def kill_switch(
    body: dict,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    user = _user(authorization, x_request_id)
    _guard(user, "kill_switch", body, x_request_id)
    state = governor.enable_kill_switch(actor_id=user["actor_id"], reason=body["reason"])
    audit_log.record(
        event_type="kill_switch_enabled",
        actor_id=user["actor_id"],
        actor_role=user["role"],
        request_id=x_request_id,
        correlation_id="",
        reason=body["reason"],
        payload={"kill_switch": state},
    )
    return {"kill_switch": state, "risk_state": governor.get_risk_state()}


@router.post("/kill-switch/disable")
def kill_switch_disable(
    body: dict,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    user = _user(authorization, x_request_id)
    _guard(user, "kill_switch", body, x_request_id)
    state = governor.disable_kill_switch()
    # cautious step-down: entries stay blocked until normal is resumed explicitly
    if body.get("resume_normal") is True:
        governor.set_risk_state("normal")
    audit_log.record(
        event_type="kill_switch_disabled",
        actor_id=user["actor_id"],
        actor_role=user["role"],
        request_id=x_request_id,
        correlation_id="",
        reason=body["reason"],
        payload={"kill_switch": state, "resume_normal": bool(body.get("resume_normal"))},
    )
    return {"kill_switch": state, "risk_state": governor.get_risk_state()}


@router.post("/kill-switch/close-all")
def close_all(
    body: dict,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    user = _user(authorization, x_request_id)
    if body.get("phrase") != "CLOSE ALL":
        raise HTTPException(status_code=422, detail="CLOSE ALL phrase required")
    _guard(user, "close_all", body, x_request_id)
    return {"accepted": True, "action": "close_all"}


@router.get("/pair-locks")
def list_pair_locks(
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    _user(authorization, x_request_id)
    return {"pair_locks": governor.pair_lock_store.list_active()}


@router.post("/pair-locks")
def create_pair_lock(
    body: dict,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    user = _user(authorization, x_request_id)
    _guard(user, "pair_lock", body, x_request_id, body.get("pair", ""))
    pair = body.get("pair")
    expires_at = body.get("expires_at")
    if not pair or not expires_at:
        raise HTTPException(status_code=422, detail="pair and expires_at required")
    try:
        parsed_expires_at = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="expires_at must be ISO8601") from exc
    lock = governor.pair_lock_store.create(
        pair=pair,
        reason=body["reason"],
        actor_id=user["actor_id"],
        expires_at=parsed_expires_at,
    )
    audit_log.record(
        event_type="pair_lock_created",
        actor_id=user["actor_id"],
        actor_role=user["role"],
        request_id=x_request_id,
        correlation_id=pair,
        reason=body["reason"],
        payload=lock,
    )
    return lock


@router.delete("/pair-locks/{lock_id}")
def release_pair_lock(
    lock_id: str,
    body: dict,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    user = _user(authorization, x_request_id)
    _guard(user, "pair_lock", body, x_request_id, lock_id)
    released = governor.pair_lock_store.release(lock_id)
    if released:
        audit_log.record(
            event_type="pair_lock_released",
            actor_id=user["actor_id"],
            actor_role=user["role"],
            request_id=x_request_id,
            correlation_id=lock_id,
            reason=body["reason"],
            payload={"lock_id": lock_id},
        )
    return {"released": released}


@router.get("/audit")
def risk_audit(
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    user = _user(authorization, x_request_id)
    from app.services.permissions import has_permission

    if not has_permission(user["role"], "audit_read"):
        raise HTTPException(status_code=403, detail="permission denied")
    return {"events": audit_log.list_events()}
