"""settings.changed outbox publisher."""

from __future__ import annotations

from typing import Any, Mapping

from order_management.outbox import enqueue_order_management_event


def publish_settings_changed(
    conn: Any,
    *,
    scope: str,
    scope_key: str,
    version: int,
    settings: Mapping[str, Any],
    request_id: str,
) -> str:
    aggregate_id = f"{scope}:{scope_key}"
    return enqueue_order_management_event(
        conn,
        aggregate_type="order_management_settings",
        aggregate_id=aggregate_id,
        event_type="settings.changed",
        idempotency_key=request_id,
        request_id=request_id,
        payload={
            "scope": scope,
            "scope_key": scope_key,
            "version": version,
            "settings": dict(settings),
        },
    )
