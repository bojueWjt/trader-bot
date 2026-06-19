from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from app.dependencies import require_bearer_user
from app.risk import router as risk_router
from app.services.permissions import has_permission


router = APIRouter()


@router.get("/events")
def list_events(
    authorization: str | None = Header(default=None),
):
    user = require_bearer_user(authorization)
    if not has_permission(user["role"], "audit_read"):
        raise HTTPException(status_code=403, detail="permission denied")
    return {"events": risk_router.audit_log.list_events()}
