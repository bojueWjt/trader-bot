"""Import/export helpers for order-management settings."""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from .permissions import requires_live_risk_confirmation
from .schema import load_descriptor, validate_settings


SECRET_NAME_RE = re.compile(r"(secret|token|api[_-]?key|password|credential)", re.IGNORECASE)


def sanitize_export(settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    descriptor = load_descriptor()
    cleaned: dict[str, dict[str, Any]] = {}
    for category, fields in (settings or {}).items():
        if not isinstance(fields, Mapping):
            continue
        known_fields = descriptor["categories"].get(category, {})
        for field_name, value in fields.items():
            field = known_fields.get(field_name)
            if SECRET_NAME_RE.search(str(field_name)):
                continue
            if field is None or field.get("secret", False):
                continue
            cleaned.setdefault(category, {})[field_name] = copy.deepcopy(value)
    return cleaned


def export_payload(
    *,
    scope: str,
    scope_key: str,
    version: int,
    settings: Mapping[str, Any],
    environment: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment,
        "scope": scope,
        "scope_key": scope_key,
        "version": version,
        "settings": sanitize_export(settings),
    }


def validate_import_payload(
    payload: Mapping[str, Any],
    *,
    current_environment: str,
    before_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    settings = payload.get("settings") if isinstance(payload, Mapping) else None
    validation = validate_settings(settings if isinstance(settings, dict) else {})
    warnings: list[dict[str, str]] = []

    payload_environment = str(payload.get("environment") or "").strip()
    if payload_environment and payload_environment != current_environment:
        warnings.append(
            {
                "code": "environment_mismatch",
                "message": f"import environment {payload_environment} differs from {current_environment}",
            }
        )
    if isinstance(settings, Mapping) and requires_live_risk_confirmation(before_settings or {}, settings):
        warnings.append(
            {
                "code": "risk_relaxation",
                "message": "import relaxes live risk settings and requires confirmation",
            }
        )
    return {
        "valid": validation["valid"],
        "errors": validation["errors"],
        "warnings": warnings,
        "settings": validation["normalized"] if validation["valid"] else {},
    }
