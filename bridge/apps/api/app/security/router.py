from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from app.dependencies import require_bearer_user
from app.services.secret_scanner import scan_text_for_secrets


router = APIRouter()


@router.post("/scan-report")
def scan_report(
    body: dict,
    authorization: str | None = Header(default=None),
):
    require_bearer_user(authorization)
    return scan_text_for_secrets(body.get("text", ""), body.get("source", "report"))
