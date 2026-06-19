from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.permissions import AuthError401, PermissionError403, require_user


DEFAULT_TOKEN_TTL_SECONDS = 3600

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    role: str


class AuthTokenError(Exception):
    pass


@dataclass(frozen=True)
class Credential:
    username: str
    password_value: str
    role: str


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    credential = _find_credential(payload.username)
    if credential is None:
        if _has_configured_credentials():
            raise HTTPException(status_code=401, detail="invalid credentials")
        raise HTTPException(status_code=422, detail="auth credentials not configured")
    if not _password_matches(payload.password, credential.password_value):
        raise HTTPException(status_code=401, detail="invalid credentials")

    user = _validated_user({"actor_id": credential.username, "role": credential.role})
    ttl_seconds = _token_ttl_seconds()
    token = issue_auth_token(user, ttl_seconds)
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=ttl_seconds,
        role=user["role"],
    )


def issue_auth_token(user: dict[str, str], ttl_seconds: int | None = None) -> str:
    secret = _secret_key()
    now = int(time.time())
    ttl = _token_ttl_seconds() if ttl_seconds is None else ttl_seconds
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "sub": user["actor_id"],
        "role": user["role"],
        "iat": now,
        "exp": now + ttl,
    }
    signing_input = ".".join(
        [
            _base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _base64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_base64url_encode(signature)}"


def user_from_auth_token(token: str) -> dict[str, str] | None:
    if token.count(".") != 2:
        return None

    secret = os.environ.get("AUTH_SECRET_KEY")
    if not secret:
        raise AuthTokenError("invalid token")

    header_segment, claims_segment, signature_segment = token.split(".", 2)
    signing_input = f"{header_segment}.{claims_segment}"
    try:
        header = json.loads(_base64url_decode(header_segment))
        claims = json.loads(_base64url_decode(claims_segment))
        signature = _base64url_decode(signature_segment)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthTokenError("invalid token") from exc

    if header.get("alg") != "HS256":
        raise AuthTokenError("invalid token")

    expected = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise AuthTokenError("invalid token")

    exp = claims.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise AuthTokenError("token expired")

    actor_id = claims.get("sub")
    role = claims.get("role")
    if not isinstance(actor_id, str) or not isinstance(role, str):
        raise AuthTokenError("invalid token")

    try:
        return _validated_user({"actor_id": actor_id, "role": role})
    except HTTPException as exc:
        raise AuthTokenError(str(exc.detail)) from exc


def _find_credential(username: str) -> Credential | None:
    credentials = _credentials_from_json_map()
    if username in credentials:
        return credentials[username]

    env_username = os.environ.get("AUTH_USERNAME")
    env_password = os.environ.get("AUTH_PASSWORD")
    if not env_username or not env_password:
        if credentials:
            return None
        return None
    if username != env_username:
        return None
    return Credential(
        username=env_username,
        password_value=env_password,
        role=os.environ.get("AUTH_ROLE", "risk_admin"),
    )


def _has_configured_credentials() -> bool:
    if os.environ.get("AUTH_CREDENTIALS"):
        return True
    return bool(os.environ.get("AUTH_USERNAME") and os.environ.get("AUTH_PASSWORD"))


def _credentials_from_json_map() -> dict[str, Credential]:
    raw_credentials = os.environ.get("AUTH_CREDENTIALS")
    if not raw_credentials:
        return {}

    try:
        parsed = json.loads(raw_credentials)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="AUTH_CREDENTIALS must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="AUTH_CREDENTIALS must be a JSON object")

    credentials: dict[str, Credential] = {}
    for username, value in parsed.items():
        if not isinstance(username, str) or not username:
            raise HTTPException(status_code=422, detail="AUTH_CREDENTIALS usernames must be strings")
        password_value, role = _credential_value(value)
        credentials[username] = Credential(username=username, password_value=password_value, role=role)
    return credentials


def _credential_value(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        return value, os.environ.get("AUTH_ROLE", "risk_admin")
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="AUTH_CREDENTIALS values must be strings or objects")

    password_value = value.get("password_hash") or value.get("password")
    role = value.get("role", os.environ.get("AUTH_ROLE", "risk_admin"))
    if not isinstance(password_value, str) or not password_value:
        raise HTTPException(status_code=422, detail="AUTH_CREDENTIALS password is required")
    if not isinstance(role, str) or not role:
        raise HTTPException(status_code=422, detail="AUTH_CREDENTIALS role is required")
    return password_value, role


def _password_matches(password: str, expected: str) -> bool:
    if expected.startswith("sha256:"):
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, expected.removeprefix("sha256:"))
    if expected.startswith("plain:"):
        expected = expected.removeprefix("plain:")
    return hmac.compare_digest(password, expected)


def _validated_user(user: dict[str, str]) -> dict[str, str]:
    try:
        return require_user(user)
    except AuthError401 as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError403 as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _token_ttl_seconds() -> int:
    raw_ttl = os.environ.get("AUTH_TOKEN_TTL_SECONDS", str(DEFAULT_TOKEN_TTL_SECONDS))
    try:
        return int(raw_ttl)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="AUTH_TOKEN_TTL_SECONDS must be an integer") from exc


def _secret_key() -> str:
    secret = os.environ.get("AUTH_SECRET_KEY")
    if not secret:
        raise HTTPException(status_code=422, detail="AUTH_SECRET_KEY is required")
    return secret


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))
