"""M1e read routers: GET /v1/intents/{intent_id}/trace and GET /v1/incidents."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from psycopg2.extras import RealDictCursor

router = APIRouter()

_TRACE_LAYERS = (
    "raw_message",
    "hermes_decision",
    "risk_decision",
    "intent",
    "execution_events",
    "node_acks",
)
_INCIDENT_STATUSES = frozenset({"open", "closed"})


def _iso(value):
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


def _as_uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a uuid") from exc


def _is_operator_hermes_stub(hermes: dict | None, raw: dict | None) -> bool:
    """Operator orders persist a synthetic hermes row for FKs; the layer is absent."""
    if not hermes:
        return True
    prompt = str(hermes.get("prompt_version") or "")
    model = str(hermes.get("model_version") or "")
    source = str((raw or {}).get("source") or "")
    return (
        source == "operator"
        or prompt.startswith("operator")
        or model.startswith("operator")
        or "operator" in model
    )


def _media_refs(rows) -> list[dict]:
    refs = []
    for row in rows:
        refs.append(
            {
                "mime": row.get("mime"),
                "object_key": row.get("object_key"),
            }
        )
    return refs


def _raw_message_layer(row: dict | None, media) -> dict | None:
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "channel_id": row.get("channel_id"),
        "source": row.get("source"),
        "message_text": row.get("message_text"),
        "source_received_at": _iso(row.get("source_received_at")),
        "media": _media_refs(media),
    }


def _hermes_layer(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "decision_id": str(row["decision_id"]),
        "action": row.get("action"),
        "message_type": row.get("message_type"),
        "evidence": row.get("evidence"),
        "instrument_symbol": row.get("instrument_symbol"),
        "side": row.get("side"),
        "confidence": row.get("confidence"),
    }


def _risk_layer(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "risk_decision_id": str(row["risk_decision_id"]),
        "account_id": row.get("account_id"),
        "instrument_id": row.get("instrument_id"),
        "status": row.get("status"),
        "reason": row.get("reason"),
    }


def _intent_layer(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "intent_id": str(row["intent_id"]),
        "account_id": row.get("account_id"),
        "instrument_id": row.get("instrument_id"),
        "action": row.get("action"),
        "status": row.get("status"),
    }


def _execution_event_layer(row: dict) -> dict:
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    event = {
        "event_type": row.get("event_type"),
        "ts_event": _iso(row.get("ts_event")),
        "payload": payload,
    }
    client_order_id = row.get("client_order_id")
    if client_order_id:
        event["client_order_id"] = client_order_id
    reason = payload.get("reason") or payload.get("error")
    if reason:
        event["reason"] = reason
    return event


def _ack_layer(row: dict) -> dict:
    ack = {
        "node_id": row.get("node_id"),
        "status": row.get("status"),
        "ack_at": _iso(row.get("ack_at")),
    }
    if row.get("detail") is not None:
        ack["detail"] = row.get("detail")
    return ack


@router.get("/v1/intents/{intent_id}/trace")
def v1_intent_trace(intent_id: str, authorization: str | None = Header(default=None)):
    import read_api as ra

    ra.require_reader(authorization)
    intent_id = _as_uuid(intent_id, field="intent_id")
    conn = ra._read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = ra._envelope(cur)
            cur.execute(
                """
                SELECT intent_id::text AS intent_id, account_id, instrument_id,
                       action::text AS action, status::text AS status,
                       hermes_decision_id::text AS hermes_decision_id,
                       risk_decision_id::text AS risk_decision_id
                FROM trade_intents
                WHERE intent_id = %s
                """,
                (intent_id,),
            )
            intent = cur.fetchone()
            if intent is None:
                raise HTTPException(status_code=404, detail="intent not found")
            intent = dict(intent)

            hermes = None
            if intent.get("hermes_decision_id"):
                cur.execute(
                    """
                    SELECT decision_id::text AS decision_id,
                           action::text AS action,
                           message_type::text AS message_type,
                           evidence, instrument_symbol, side::text AS side,
                           confidence, prompt_version, model_version,
                           raw_message_id::text AS raw_message_id
                    FROM hermes_decisions
                    WHERE decision_id = %s
                    """,
                    (intent["hermes_decision_id"],),
                )
                row = cur.fetchone()
                hermes = dict(row) if row else None

            risk = None
            if intent.get("risk_decision_id"):
                cur.execute(
                    """
                    SELECT risk_decision_id::text AS risk_decision_id,
                           account_id, instrument_id, status::text AS status, reason
                    FROM risk_decisions
                    WHERE risk_decision_id = %s
                    """,
                    (intent["risk_decision_id"],),
                )
                row = cur.fetchone()
                risk = dict(row) if row else None

            raw = None
            media: list[dict] = []
            raw_id = (hermes or {}).get("raw_message_id")
            if raw_id:
                cur.execute(
                    """
                    SELECT id::text AS id, source, channel_id, message_text,
                           source_received_at
                    FROM raw_messages
                    WHERE id = %s
                    """,
                    (raw_id,),
                )
                row = cur.fetchone()
                raw = dict(row) if row else None
                cur.execute(
                    """
                    SELECT mime, object_key
                    FROM media_assets
                    WHERE raw_message_id = %s
                    ORDER BY created_at, asset_id
                    """,
                    (raw_id,),
                )
                media = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT event_type, client_order_id, payload, ts_event
                FROM execution_events
                WHERE intent_id = %s
                ORDER BY ts_event, event_id
                """,
                (intent_id,),
            )
            events = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT na.node_id, na.status::text AS status, na.detail, na.ack_at
                FROM command_node_acks na
                JOIN operator_commands oc ON oc.command_id = na.command_id
                WHERE oc.scope->>'intent_id' = %s
                ORDER BY na.ack_at NULLS LAST, na.node_id
                """,
                (intent_id,),
            )
            acks = [dict(r) for r in cur.fetchall()]
            if not acks:
                cur.execute(
                    """
                    SELECT
                        CASE
                            WHEN actor LIKE 'node:%%' THEN substring(actor FROM 6)
                            ELSE actor
                        END AS node_id,
                        COALESCE(payload->>'status', replace(event_type, 'intent_ack.', ''))
                            AS status,
                        payload->>'detail' AS detail,
                        created_at AS ack_at
                    FROM audit_events
                    WHERE (intent_id = %s OR aggregate_id = %s)
                      AND event_type LIKE 'intent_ack.%%'
                    ORDER BY created_at, audit_event_id
                    """,
                    (intent_id, intent_id),
                )
                acks = [dict(r) for r in cur.fetchall()]

        hermes_out = None if _is_operator_hermes_stub(hermes, raw) else _hermes_layer(hermes)
        data = {
            "raw_message": _raw_message_layer(raw, media),
            "hermes_decision": hermes_out,
            "risk_decision": _risk_layer(risk),
            "intent": _intent_layer(intent),
            "execution_events": [_execution_event_layer(row) for row in events] or None,
            "node_acks": [_ack_layer(row) for row in acks] or None,
        }
        for key in _TRACE_LAYERS:
            data.setdefault(key, None)
        return jsonable_encoder({**env, "data": data})
    finally:
        conn.close()


@router.get("/v1/incidents")
def v1_incidents(
    status: str = Query(default="open"),
    authorization: str | None = Header(default=None),
):
    import read_api as ra

    ra.require_reader(authorization)
    wanted = (status or "open").strip().lower()
    if wanted not in _INCIDENT_STATUSES:
        raise HTTPException(status_code=400, detail="status must be open or closed")
    conn = ra._read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = ra._envelope(cur)
            cur.execute(
                """
                SELECT
                    pi.incident_id::text AS id,
                    COALESCE(nh.node_id, pi.account_id) AS node,
                    pi.summary,
                    pi.opened_at
                FROM production_incidents pi
                LEFT JOIN LATERAL (
                    SELECT node_id
                    FROM node_heartbeats
                    WHERE account_id = pi.account_id
                    ORDER BY last_seen_at DESC NULLS LAST, node_id
                    LIMIT 1
                ) nh ON true
                WHERE pi.status = %s
                ORDER BY pi.opened_at DESC, pi.incident_id
                """,
                (wanted,),
            )
            rows = [
                {
                    "id": row["id"],
                    "node": row["node"],
                    "summary": row["summary"],
                    "opened_at": _iso(row["opened_at"]),
                }
                for row in cur.fetchall()
            ]
        return jsonable_encoder({**env, "data": rows})
    finally:
        conn.close()
