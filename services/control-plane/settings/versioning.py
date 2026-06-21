"""Version history helpers for order-management settings."""

from __future__ import annotations

import copy
from typing import Any, Mapping
from uuid import uuid4

from psycopg2.extras import Json, RealDictCursor


def get_current_settings(conn: Any, scope: str, scope_key: str) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT scope, scope_key, version, settings, created_by, reason, request_id, created_at
            FROM order_management_settings
            WHERE scope=%s AND scope_key=%s
            ORDER BY version DESC
            LIMIT 1
            """,
            (scope, scope_key),
        )
        row = cur.fetchone()
    if not row:
        return {"scope": scope, "scope_key": scope_key, "version": 0, "settings": {}}
    result = dict(row)
    result["settings"] = dict(result.get("settings") or {})
    return result


def insert_settings_version(
    conn: Any,
    *,
    scope: str,
    scope_key: str,
    version: int,
    previous_version: int | None,
    settings: Mapping[str, Any],
    changed_by: str,
    reason: str,
    request_id: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO order_management_settings (
                order_management_setting_id,
                scope,
                scope_key,
                version,
                settings,
                created_by,
                reason,
                request_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid4()),
                scope,
                scope_key,
                version,
                Json(settings),
                changed_by,
                reason,
                request_id,
            ),
        )
        cur.execute(
            """
            INSERT INTO order_management_setting_versions (
                setting_version_id,
                scope,
                scope_key,
                version,
                previous_version,
                settings,
                changed_by,
                reason,
                request_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid4()),
                scope,
                scope_key,
                version,
                previous_version,
                Json(settings),
                changed_by,
                reason,
                request_id,
            ),
        )


def list_versions(conn: Any, scope: str, scope_key: str) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT setting_version_id::text, scope, scope_key, version, previous_version,
                   settings, changed_by, reason, request_id, created_at
            FROM order_management_setting_versions
            WHERE scope=%s AND scope_key=%s
            ORDER BY version DESC
            """,
            (scope, scope_key),
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        row["settings"] = dict(row.get("settings") or {})
    return rows


def get_version(conn: Any, scope: str, scope_key: str, version: int) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT setting_version_id::text, scope, scope_key, version, previous_version,
                   settings, changed_by, reason, request_id, created_at
            FROM order_management_setting_versions
            WHERE scope=%s AND scope_key=%s AND version=%s
            """,
            (scope, scope_key, version),
        )
        row = cur.fetchone()
    if not row:
        raise KeyError(f"settings version not found: {scope}:{scope_key}:{version}")
    result = dict(row)
    result["settings"] = dict(result.get("settings") or {})
    return result


def diff_settings(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    paths = sorted(_flatten(before).keys() | _flatten(after).keys())
    flat_before = _flatten(before)
    flat_after = _flatten(after)
    for path in paths:
        before_value = flat_before.get(path)
        after_value = flat_after.get(path)
        if before_value != after_value:
            changes.append(
                {
                    "path": path,
                    "before": copy.deepcopy(before_value),
                    "after": copy.deepcopy(after_value),
                }
            )
    return changes


def _flatten(settings: Mapping[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for category, fields in (settings or {}).items():
        if not isinstance(fields, Mapping):
            continue
        for field_name, value in fields.items():
            flattened[f"{category}.{field_name}"] = value
    return flattened
