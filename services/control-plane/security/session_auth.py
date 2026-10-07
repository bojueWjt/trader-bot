"""Verify the existing dashboard login issuer's HS256 sessions; never issue one."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time


SESSION_ROLES = frozenset({"risk_admin", "viewer", "reviewer", "system_observer"})


class SessionTokenError(ValueError):
    """No authenticated session identity may be derived from this credential."""


def _decode(segment: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", segment):
        raise SessionTokenError("invalid session token")
    return base64.b64decode(segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True)


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SessionTokenError("invalid session token")
        result[key] = value
    return result


def verify_session_token(token: str, secret: str | None, *, now: int | None = None) -> dict[str, str]:
    """Return only verified sub/role; match bridge auth.py's current wire format."""
    if not isinstance(secret, str) or not secret.strip():
        raise SessionTokenError("session authentication is not configured")
    if not isinstance(token, str) or len(token) > 8192 or token.count(".") != 2:
        raise SessionTokenError("invalid session token")
    header_segment, claims_segment, signature_segment = token.split(".")
    try:
        signature = _decode(signature_segment)
        signing_input = f"{header_segment}.{claims_segment}".encode("ascii")
        expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        if len(signature) != 32 or not hmac.compare_digest(signature, expected):
            raise SessionTokenError("invalid session token")
        header = json.loads(_decode(header_segment), object_pairs_hook=_object)
        claims = json.loads(_decode(claims_segment), object_pairs_hook=_object)
    except (ValueError, UnicodeError) as exc:
        raise SessionTokenError("invalid session token") from exc
    if not isinstance(header, dict) or header != {"alg": "HS256", "typ": "JWT"}:
        raise SessionTokenError("invalid session token")
    if not isinstance(claims, dict):
        raise SessionTokenError("invalid session token")
    issued_at, expires_at = claims.get("iat"), claims.get("exp")
    checked_at = int(time.time()) if now is None else now
    if (
        type(issued_at) is not int or type(expires_at) is not int
        or issued_at < 0 or issued_at > checked_at
        or expires_at <= checked_at or expires_at <= issued_at
    ):
        raise SessionTokenError("session token expired or has invalid timestamps")
    subject, role = claims.get("sub"), claims.get("role")
    if (
        not isinstance(subject, str) or not subject.strip() or len(subject) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in subject)
        or not isinstance(role, str) or role not in SESSION_ROLES
    ):
        raise SessionTokenError("invalid session identity")
    return {"sub": subject, "role": role}
