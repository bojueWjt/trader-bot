"""Structured audit writes for order-management settings."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg2.extras import Json

from security.audit import redact_payload


def request_id_uuid(request_id: str) -> UUID:
    value = str(request_id).strip()
    try:
        return UUID(value)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"order-management-settings:{value}")


def record_settings_audit(
    conn: Any,
    *,
    actor: Mapping[str, str],
    action: str,
    target: str,
    before_state: Mapping[str, Any],
    after_state: Mapping[str, Any],
    reason: str,
    request_id: str,
) -> None:
    request_uuid = request_id_uuid(request_id)
    payload = redact_payload(
        {
            "actor": dict(actor),
            "action": action,
            "target": target,
            "before_state": dict(before_state),
            "after_state": dict(after_state),
            "reason": reason,
            "request_id": str(request_id),
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_events (
                audit_event_id,
                event_type,
                aggregate_type,
                aggregate_id,
                actor,
                trace_id,
                payload,
                action,
                target,
                before_state,
                after_state,
                reason,
                request_id
            )
            VALUES (%s, %s, 'order_management_settings', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid4()),
                action,
                target,
                actor["actor_id"],
                str(request_uuid),
                Json(payload),
                action,
                target,
                Json(redact_payload(before_state)),
                Json(redact_payload(after_state)),
                reason,
                str(request_uuid),
            ),
        )
