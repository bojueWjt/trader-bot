from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi import Header, HTTPException, Request

from app.db.repositories_signal import SignalRepository
from app.security.auth import AuthTokenError, user_from_auth_token
from app.security.roles import validate_actor
from app.security.tokens import static_token_users
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
        return validate_actor(user)
    except ValueError as exc:
        detail = str(exc)
        if detail == "login required":
            raise HTTPException(status_code=401, detail=detail) from exc
        raise HTTPException(status_code=403, detail=detail) from exc


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
    return static_token_users()
