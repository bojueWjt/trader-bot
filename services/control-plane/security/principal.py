"""Server-derived control-plane principal (G3-T1 slice 1).

Kind comes only from the presented Bearer credential. Scope and account_id
never substitute for kind. Viewer is not an operator.
"""

from __future__ import annotations

import os
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from security.permissions import AuthRequired, PermissionDenied, TOKEN_ENV_VARS
from security.session_auth import SessionTokenError, verify_session_token

UNASSIGNED_ACCOUNT_ID = "unassigned"

SIGNAL_TOKEN_ENV: dict[str, str] = {
    "SIGNAL_TOKEN_ACCOUNT_A": "account-a",
    "SIGNAL_TOKEN_ACCOUNT_B": "account-b",
    "SIGNAL_TOKEN_ACCOUNT_C": "account-c",
    "SIGNAL_TOKEN_ACCOUNT_D": "account-d",
}

READER_ROLE_BY_ENV: dict[str, str] = {
    "RISK_ADMIN_TOKEN": "risk_admin",
    "VIEWER_TOKEN": "viewer",
    "REVIEWER_TOKEN": "reviewer",
    "SYSTEM_OBSERVER_TOKEN": "system_observer",
}


class PrincipalKind(str, Enum):
    OPERATOR = "operator"
    SIGNAL = "signal"
    READER = "reader"


class TokenCatalogError(PermissionError):
    """Configured token catalog is unusable (missing or colliding)."""


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    actor_id: str
    role: str
    scope: str
    account_id: str | None
    session_subject: str | None = None


def signal_account_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Map configured signal token values to bound account ids (omit unset)."""
    source = os.environ if env is None else env
    bound: dict[str, str] = {}
    for env_name, account_id in SIGNAL_TOKEN_ENV.items():
        value = str(source.get(env_name, "") or "").strip()
        if value:
            bound[value] = account_id
    return bound


def configured_token_values(env: Mapping[str, str] | None = None) -> list[str]:
    source = os.environ if env is None else env
    names = list(TOKEN_ENV_VARS) + list(SIGNAL_TOKEN_ENV)
    values: list[str] = []
    seen_names: set[str] = set()
    for name in names:
        if name in seen_names:
            continue
        seen_names.add(name)
        value = str(source.get(name, "") or "").strip()
        if value:
            values.append(value)
    node_auth = str(source.get("NAUTILUS_NODE_AUTH_JSON", "") or "").strip()
    if node_auth:
        try:
            bindings = json.loads(node_auth)
            if not isinstance(bindings, dict):
                raise ValueError("node bindings must be an object")
            for binding in bindings.values():
                value = str(binding.get("token", "") or "").strip()
                if value:
                    values.append(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise TokenCatalogError("invalid node token catalog") from exc
    return values


def assert_token_catalog_unique(env: Mapping[str, str] | None = None) -> None:
    values = configured_token_values(env)
    if len(values) != len(set(values)):
        raise TokenCatalogError("security token values must be unique")


def resolve_principal(
    authorization: str | None,
    *,
    env: Mapping[str, str] | None = None,
    reader_tokens: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> Principal:
    """Derive Principal from server credentials or a verified login. Ignore body."""
    del body
    source = os.environ if env is None else env
    assert_token_catalog_unique(source)
    if not authorization or not str(authorization).startswith("Bearer "):
        raise AuthRequired("bearer token required")
    presented = str(authorization)[len("Bearer ") :].strip()
    if not presented:
        raise AuthRequired("bearer token required")

    readers = dict(reader_tokens) if reader_tokens is not None else _reader_token_map(source)
    signals = signal_account_tokens(source)
    role = readers.get(presented)
    if role is not None:
        return _principal_for_reader_role(role)

    account_id = signals.get(presented)
    if account_id is not None:
        return Principal(
            kind=PrincipalKind.SIGNAL,
            actor_id=f"signal:{account_id}",
            role="signal_agent",
            scope="account",
            account_id=account_id,
        )
    if presented.count(".") == 2:
        try:
            session = verify_session_token(presented, source.get("AUTH_SECRET_KEY"))
        except SessionTokenError as exc:
            raise AuthRequired(str(exc)) from exc
        role = session["role"]
        return Principal(
            kind=PrincipalKind.OPERATOR if role == "risk_admin" else PrincipalKind.READER,
            actor_id=f"user:{session['sub']}",
            role=role,
            scope="global",
            account_id=None,
            session_subject=session["sub"],
        )
    if not readers and not signals:
        raise TokenCatalogError("reader auth not configured")
    raise PermissionDenied("forbidden")


def _reader_token_map(env: Mapping[str, str]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for env_name, role in READER_ROLE_BY_ENV.items():
        value = str(env.get(env_name, "") or "").strip()
        if value:
            tokens[value] = role
    return tokens


def _principal_for_reader_role(role: str) -> Principal:
    if role == "risk_admin":
        return Principal(
            kind=PrincipalKind.OPERATOR,
            actor_id="risk_admin",
            role="risk_admin",
            scope="global",
            account_id=None,
        )
    return Principal(
        kind=PrincipalKind.READER,
        actor_id=role,
        role=role,
        scope="global",
        account_id=None,
    )


def can_read_trace(principal: Principal) -> bool:
    """Trace/incidents: global operator and legacy readers only. Signal denied."""
    if principal.kind is PrincipalKind.SIGNAL:
        return False
    return principal.kind in {PrincipalKind.OPERATOR, PrincipalKind.READER} and principal.role in {
        "risk_admin",
        "viewer",
        "reviewer",
        "system_observer",
    }


def can_write_operator_orders(principal: Principal) -> bool:
    """Interactive operator write: kind=operator and risk_admin. Signal is not operator."""
    return (
        principal.kind is PrincipalKind.OPERATOR
        and principal.role == "risk_admin"
        and principal.scope == "global"
    )


def assert_account_authorized(principal: Principal, account_id: str) -> None:
    target = str(account_id or "").strip()
    if not target or target == UNASSIGNED_ACCOUNT_ID:
        raise PermissionDenied("unassigned cannot live-dispatch")
    if principal.kind is PrincipalKind.SIGNAL:
        if principal.account_id != target:
            raise PermissionDenied("account token cannot operate other accounts")
        return
    if principal.kind is PrincipalKind.OPERATOR and principal.scope == "global":
        return
    raise PermissionDenied("permission denied")
