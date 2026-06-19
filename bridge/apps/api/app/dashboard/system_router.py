from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException

from app.contracts.dashboard import SystemSnapshotResponse
from app.dashboard.snapshot import SystemSnapshotAdapter
from app.services.permissions import AuthError401, PermissionError403, has_permission, require_user


router = APIRouter()
adapter = SystemSnapshotAdapter()


@router.get("/snapshot")
def snapshot(authorization: str | None = Header(default=None)) -> SystemSnapshotResponse:
    user = _read_user(authorization)
    if not has_permission(user["role"], "dashboard_read"):
        raise HTTPException(status_code=403, detail="permission denied")
    return adapter.snapshot()


def _read_user(authorization: str | None) -> dict[str, str]:
    if not authorization:
        raise HTTPException(status_code=401, detail="login required")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="bearer token required")
    token = authorization[len(prefix) :].strip()
    user = _token_users().get(token)
    if not user:
        raise HTTPException(status_code=403, detail="unknown token")
    try:
        return require_user(user)
    except AuthError401 as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except PermissionError403 as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _token_users() -> dict[str, dict[str, str]]:
    risk_admin_token = os.environ.get("RISK_ADMIN_API_TOKEN", "test-risk-admin-token")
    viewer_token = os.environ.get("VIEWER_API_TOKEN", "test-viewer-token")
    trader_token = os.environ.get("TRADER_API_TOKEN", "test-trader-token")
    observer_token = os.environ.get("SYSTEM_OBSERVER_API_TOKEN", "test-system-observer-token")
    return {
        risk_admin_token: {"actor_id": "risk-admin", "role": "risk_admin"},
        viewer_token: {"actor_id": "viewer", "role": "viewer"},
        trader_token: {"actor_id": "trader", "role": "trader"},
        observer_token: {"actor_id": "system-observer", "role": "system_observer"},
    }
