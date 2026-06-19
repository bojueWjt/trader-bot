from __future__ import annotations

from dataclasses import dataclass
import os
from uuid import uuid4

from fastapi import Header, HTTPException, Request

from app.db.repositories_signal import SignalRepository
from app.security.auth import AuthTokenError, user_from_auth_token
from app.services.permissions import AuthError401, PermissionError403, require_user
from app.services.signal_store import SignalStore
from app.settings import get_settings


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    role: str
    request_id: str


def request_id_from_headers(x_request_id: str | None = Header(default=None)) -> str:
    if x_request_id:
        return x_request_id
    return str(uuid4())


def require_bearer_user(authorization: str | None) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="login required")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="bearer token required")
    token = authorization[len(prefix) :].strip()
    try:
        user = user_from_auth_token(token)
    except AuthTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if user is None:
        token_users = _token_users()
        user = token_users.get(token)
    if not user:
        raise HTTPException(status_code=403, detail="unknown token")
    try:
        return require_user(user)
    except AuthError401 as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except PermissionError403 as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def actor_context(
    request: Request,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> ActorContext:
    request_id = x_request_id
    if not request_id:
        request_id = getattr(request.state, "request_id", "")
    if not request_id:
        request_id = str(uuid4())
    user = require_bearer_user(authorization)
    return ActorContext(
        actor_id=user["actor_id"],
        role=user["role"],
        request_id=request_id,
    )


def signal_store(request: Request) -> SignalStore:
    store = getattr(request.app.state, "signal_store", None)
    if store is not None:
        return store
    settings = get_settings()
    return SignalStore(repository=SignalRepository(settings.signal_store_url))


def _token_users() -> dict[str, dict[str, str]]:
    risk_admin_token = os.environ.get("RISK_ADMIN_API_TOKEN") or "test-risk-admin-token"
    viewer_token = os.environ.get("VIEWER_API_TOKEN") or "test-viewer-token"
    trader_token = os.environ.get("TRADER_API_TOKEN") or "test-trader-token"
    observer_token = os.environ.get("SYSTEM_OBSERVER_API_TOKEN") or "test-system-observer-token"
    return {
        risk_admin_token: {"actor_id": "risk-admin", "role": "risk_admin"},
        viewer_token: {"actor_id": "viewer", "role": "viewer"},
        trader_token: {"actor_id": "trader", "role": "trader"},
        observer_token: {"actor_id": "system-observer", "role": "system_observer"},
    }
