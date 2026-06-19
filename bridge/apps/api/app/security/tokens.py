from __future__ import annotations

import os
from collections.abc import Mapping


ROLE_TOKEN_ENV: dict[str, tuple[str, ...]] = {
    "risk_admin": ("RISK_ADMIN_TOKEN", "RISK_ADMIN_API_TOKEN"),
    "viewer": ("VIEWER_TOKEN", "VIEWER_API_TOKEN"),
    "reviewer": ("REVIEWER_TOKEN", "REVIEWER_API_TOKEN"),
    "system_observer": ("SYSTEM_OBSERVER_TOKEN", "SYSTEM_OBSERVER_API_TOKEN"),
    "nautilus_node": ("NAUTILUS_NODE_TOKEN", "NAUTILUS_NODE_API_TOKEN"),
}
AUTH_SECRET_ENV = "AUTH_SECRET_KEY"
REQUIRED_TOKEN_ENV_VARS = tuple(names[0] for names in ROLE_TOKEN_ENV.values())
REQUIRED_SECURITY_ENV_VARS = (AUTH_SECRET_ENV,) + REQUIRED_TOKEN_ENV_VARS


def validate_security_environment(env: Mapping[str, str] | None = None) -> None:
    source = os.environ if env is None else env
    missing = [AUTH_SECRET_ENV] if not source.get(AUTH_SECRET_ENV, "").strip() else []
    tokens: list[str] = []

    for env_names in ROLE_TOKEN_ENV.values():
        token = _first_env_value(source, env_names)
        if not token:
            missing.append(env_names[0])
        else:
            tokens.append(token)

    if missing:
        raise ValueError(f"missing required security environment variables: {', '.join(missing)}")
    if len(set(tokens)) != len(tokens):
        raise ValueError("security token values must be unique")


def static_token_users(env: Mapping[str, str] | None = None) -> dict[str, dict[str, str]]:
    source = os.environ if env is None else env
    validate_security_environment(source)

    users: dict[str, dict[str, str]] = {}
    for role, env_names in ROLE_TOKEN_ENV.items():
        token = _first_env_value(source, env_names)
        users[token] = {"actor_id": role.replace("_", "-"), "role": role}
    return users


def _first_env_value(source: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = source.get(name, "").strip()
        if value:
            return value
    return ""
