from __future__ import annotations

from collections.abc import Mapping
from typing import Any


ROLE_VIEWER = "viewer"
ROLE_REVIEWER = "reviewer"
ROLE_RISK_ADMIN = "risk_admin"
ROLE_SYSTEM_OBSERVER = "system_observer"
ROLE_NAUTILUS_NODE = "nautilus_node"

ROLE_VALUES = frozenset(
    {
        ROLE_VIEWER,
        ROLE_REVIEWER,
        ROLE_RISK_ADMIN,
        ROLE_SYSTEM_OBSERVER,
        ROLE_NAUTILUS_NODE,
    }
)


def validate_actor(user: Mapping[str, Any] | None) -> dict[str, str]:
    if not user:
        raise ValueError("login required")
    actor_id = user.get("actor_id")
    role = user.get("role")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("login required")
    if not isinstance(role, str) or role not in ROLE_VALUES:
        raise ValueError("unknown role")
    return {"actor_id": actor_id, "role": role}
