"""Effective order-management settings resolution."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Mapping

from psycopg2.extras import RealDictCursor

from .schema import default_settings, load_descriptor, validate_settings


_REPO = Path(__file__).resolve().parents[3]
_EXECUTION_DOMAIN = _REPO / "packages" / "execution-domain"
if str(_EXECUTION_DOMAIN) not in sys.path:
    sys.path.insert(0, str(_EXECUTION_DOMAIN))

from execution_domain.identifiers import canonical_account_id, canonical_instrument_key  # noqa: E402


SYSTEM_SCOPE = "system_default"
GLOBAL_SCOPE = "global"
ACCOUNT_SCOPE = "account"
INSTRUMENT_SCOPE = "instrument"
GLOBAL_SCOPE_KEY = "global"
SCOPES = {GLOBAL_SCOPE, ACCOUNT_SCOPE, INSTRUMENT_SCOPE}


def canonical_scope_key(scope: str, scope_key: str | None) -> str:
    scope_value = str(scope).strip().lower()
    if scope_value == GLOBAL_SCOPE:
        return GLOBAL_SCOPE_KEY
    if scope_value == ACCOUNT_SCOPE:
        return canonical_account_id(scope_key or "")
    if scope_value == INSTRUMENT_SCOPE:
        return canonical_instrument_key(scope_key or "")
    raise ValueError(f"unsupported settings scope {scope!r}")


def scope_chain(account_id: str | None = None, instrument_id: str | None = None) -> list[tuple[str, str]]:
    chain = [(GLOBAL_SCOPE, GLOBAL_SCOPE_KEY)]
    if account_id is not None:
        chain.append((ACCOUNT_SCOPE, canonical_scope_key(ACCOUNT_SCOPE, account_id)))
    if instrument_id is not None:
        chain.append((INSTRUMENT_SCOPE, canonical_scope_key(INSTRUMENT_SCOPE, instrument_id)))
    return chain


def target_scope(account_id: str | None = None, instrument_id: str | None = None) -> str:
    if instrument_id is not None:
        return INSTRUMENT_SCOPE
    if account_id is not None:
        return ACCOUNT_SCOPE
    return GLOBAL_SCOPE


def resolve_effective_settings(
    *,
    settings_by_scope: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    conn: Any | None = None,
    account_id: str | None = None,
    instrument_id: str | None = None,
    descriptor: dict[str, Any] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    descriptor = descriptor or load_descriptor()
    layers = dict(settings_by_scope or {})
    if conn is not None:
        layers.update(fetch_latest_settings_by_scope(conn, account_id=account_id, instrument_id=instrument_id))

    resolved_values = default_settings(descriptor)
    sources: dict[tuple[str, str], str] = {}
    for category, fields in resolved_values.items():
        for field_name in fields:
            sources[(category, field_name)] = SYSTEM_SCOPE

    for scope, scope_key in scope_chain(account_id=account_id, instrument_id=instrument_id):
        settings = layers.get((scope, scope_key)) or {}
        _merge_layer(resolved_values, sources, scope, settings)

    current_target = target_scope(account_id=account_id, instrument_id=instrument_id)
    output: dict[str, dict[str, dict[str, Any]]] = {}
    for category, fields in descriptor["categories"].items():
        output_category: dict[str, dict[str, Any]] = {}
        for field_name, field in fields.items():
            value = resolved_values[category][field_name]
            source_scope = sources[(category, field_name)]
            validation = validate_settings({category: {field_name: value}}, descriptor)
            output_category[field_name] = {
                "value": copy.deepcopy(value),
                "source_scope": source_scope,
                "inherited": source_scope != current_target,
                "validation": {
                    "valid": validation["valid"],
                    "errors": validation["errors"],
                },
                "apply_mode": field["apply_mode"],
            }
        output[category] = output_category
    return output


def fetch_latest_settings_by_scope(
    conn: Any,
    *,
    account_id: str | None = None,
    instrument_id: str | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for scope, scope_key in scope_chain(account_id=account_id, instrument_id=instrument_id):
            cur.execute(
                """
                SELECT settings
                FROM order_management_settings
                WHERE scope = %s AND scope_key = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (scope, scope_key),
            )
            row = cur.fetchone()
            if row:
                rows[(scope, scope_key)] = dict(row["settings"] or {})
    return rows


def extract_values(resolved: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for category, fields in resolved.items():
        values[category] = {field_name: field["value"] for field_name, field in fields.items()}
    return values


def _merge_layer(
    resolved_values: dict[str, dict[str, Any]],
    sources: dict[tuple[str, str], str],
    scope: str,
    settings: Mapping[str, Any],
) -> None:
    for category, fields in settings.items():
        if category not in resolved_values or not isinstance(fields, Mapping):
            continue
        for field_name, value in fields.items():
            if value is None or field_name not in resolved_values[category]:
                continue
            resolved_values[category][field_name] = copy.deepcopy(value)
            sources[(category, field_name)] = scope
