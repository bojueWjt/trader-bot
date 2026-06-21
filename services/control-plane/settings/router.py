"""FastAPI router for order-management settings."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

from db.connection import connect

from .permissions import assert_can_read_settings
from .schema import validate_settings
from .service import SettingsService, SettingsServiceError


router = APIRouter(prefix="/v1/order-management/settings", tags=["order-management-settings"])

TOKEN_ROLES = {
    "VIEWER_TOKEN": "viewer",
    "SYSTEM_OBSERVER_TOKEN": "viewer",
    "OPERATOR_TOKEN": "operator",
    "RISK_ADMIN_TOKEN": "risk_admin",
}


def require_settings_actor(authorization: str | None, *, operation: str) -> dict[str, str]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    tokens: dict[str, str] = {}
    for env_name, role in TOKEN_ROLES.items():
        token = os.environ.get(env_name, "").strip()
        if token:
            tokens[token] = role
    if not tokens:
        raise HTTPException(status_code=503, detail="settings auth not configured")
    token_value = authorization[len("Bearer "):].strip()
    role = tokens.get(token_value)
    if role is None:
        raise HTTPException(status_code=403, detail="forbidden")
    actor = {"actor_id": f"{role}-actor", "role": role}
    try:
        assert_can_read_settings(actor, operation=operation if operation in {"read", "validate", "export"} else "read")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return actor


@router.get("")
def get_settings(
    scope: str = "global",
    scope_key: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_settings_actor(authorization, operation="read")
    return _with_service(lambda service: service.get_settings(scope, scope_key))


@router.get("/effective")
def get_effective_settings(
    account_id: str | None = None,
    instrument_id: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_settings_actor(authorization, operation="read")
    return _with_service(lambda service: service.get_effective(account_id=account_id, instrument_id=instrument_id))


@router.post("/validate")
def validate_settings_endpoint(
    body: dict[str, Any] = Body(default={}),
    authorization: str | None = Header(default=None),
):
    require_settings_actor(authorization, operation="validate")
    settings = body.get("settings") or {}
    validation = validate_settings(settings)
    return {"valid": validation["valid"], "errors": validation["errors"], "normalized": validation["normalized"]}


@router.patch("")
def patch_settings(
    body: dict[str, Any] = Body(default={}),
    authorization: str | None = Header(default=None),
):
    actor = require_settings_actor(authorization, operation="read")
    return _with_service(
        lambda service: service.patch_settings(
            scope=body.get("scope", "global"),
            scope_key=body.get("scope_key"),
            patch=body.get("settings") or {},
            expected_version=body.get("expected_version"),
            reason=body.get("reason", ""),
            request_id=body.get("request_id", ""),
            actor=actor,
            confirm=body.get("confirm"),
            operator_signoff=body.get("operator_signoff"),
        )
    )


@router.get("/versions")
def list_setting_versions(
    scope: str = "global",
    scope_key: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_settings_actor(authorization, operation="read")
    return _with_service(lambda service: service.list_versions(scope, scope_key))


@router.get("/versions/diff")
def diff_setting_versions(
    from_version: int,
    to_version: int,
    scope: str = "global",
    scope_key: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_settings_actor(authorization, operation="read")
    return _with_service(lambda service: service.diff_versions(scope, scope_key, from_version, to_version))


@router.get("/versions/{version}")
def get_setting_version(
    version: int,
    scope: str = "global",
    scope_key: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_settings_actor(authorization, operation="read")
    return _with_service(lambda service: service.get_version(scope, scope_key, version))


@router.post("/rollback")
def rollback_settings(
    body: dict[str, Any] = Body(default={}),
    authorization: str | None = Header(default=None),
):
    actor = require_settings_actor(authorization, operation="read")
    return _with_service(
        lambda service: service.rollback_settings(
            scope=body.get("scope", "global"),
            scope_key=body.get("scope_key"),
            target_version=body.get("target_version"),
            expected_version=body.get("expected_version"),
            reason=body.get("reason", ""),
            request_id=body.get("request_id", ""),
            actor=actor,
            confirm=body.get("confirm"),
            operator_signoff=body.get("operator_signoff"),
        )
    )


@router.get("/runtime-status")
def runtime_status(
    scope: str = "global",
    scope_key: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_settings_actor(authorization, operation="read")
    return _with_service(lambda service: service.runtime_status(scope=scope, scope_key=scope_key))


@router.post("/export")
def export_settings_endpoint(
    body: dict[str, Any] = Body(default={}),
    authorization: str | None = Header(default=None),
):
    require_settings_actor(authorization, operation="export")
    return _with_service(
        lambda service: service.export_settings(
            scope=body.get("scope", "global"),
            scope_key=body.get("scope_key"),
            environment=body.get("environment", os.environ.get("APP_ENV", "testnet")),
        )
    )


@router.post("/import/validate")
def validate_import_endpoint(
    body: dict[str, Any] = Body(default={}),
    authorization: str | None = Header(default=None),
):
    require_settings_actor(authorization, operation="validate")
    return _with_service(
        lambda service: service.validate_import(
            body.get("payload") or body,
            current_environment=body.get("current_environment", os.environ.get("APP_ENV", "testnet")),
            scope=body.get("scope", "global"),
            scope_key=body.get("scope_key"),
        )
    )


@router.post("/import")
def import_settings_endpoint(
    body: dict[str, Any] = Body(default={}),
    authorization: str | None = Header(default=None),
):
    actor = require_settings_actor(authorization, operation="read")
    return _with_service(
        lambda service: service.import_settings(
            body.get("payload") or {},
            current_environment=body.get("current_environment", os.environ.get("APP_ENV", "testnet")),
            scope=body.get("scope", "global"),
            scope_key=body.get("scope_key"),
            expected_version=body.get("expected_version"),
            reason=body.get("reason", ""),
            request_id=body.get("request_id", ""),
            actor=actor,
            confirm=body.get("confirm"),
            operator_signoff=body.get("operator_signoff"),
        )
    )


def _with_service(callback):
    conn = connect()
    try:
        return callback(SettingsService(conn))
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "permission_denied", "message": str(exc), "errors": []},
        ) from exc
    except SettingsServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    finally:
        conn.close()
