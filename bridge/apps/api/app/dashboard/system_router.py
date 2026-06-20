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
    # Secrets fail closed: tokens come ONLY from env, never a hardcoded default.
    # An unset token simply cannot authenticate (no known-default backdoor).
    # Canonical roles only (no `trader`); reviewer is the approval role.
    specs = [
        ("RISK_ADMIN_API_TOKEN", "risk-admin", "risk_admin"),
        ("VIEWER_API_TOKEN", "viewer", "viewer"),
        ("REVIEWER_API_TOKEN", "reviewer", "reviewer"),
        ("SYSTEM_OBSERVER_API_TOKEN", "system-observer", "system_observer"),
    ]
    users: dict[str, dict[str, str]] = {}
    for env_name, actor_id, role in specs:
        token = os.environ.get(env_name, "").strip()
        if token:
            users[token] = {"actor_id": actor_id, "role": role}
    return users
