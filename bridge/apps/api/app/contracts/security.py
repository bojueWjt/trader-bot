from __future__ import annotations

from typing import Literal, TypedDict


Role = Literal["viewer", "trader", "risk_admin"]


class Actor(TypedDict):
    actor_id: str
    role: Role
