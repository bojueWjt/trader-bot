"""Bounded, read-only queries for the portable interactive Hermes CLI."""

from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg2.extras import RealDictCursor

router = APIRouter()


@router.get("/v1/query/{resource}")
def operator_query(
    resource: Literal["channels", "channel-route", "messages", "orders", "intents", "intent", "fills", "outcomes", "report"],
    channel: str = "", symbol: str = "", status: str = "", prefix: str = "",
    limit: int = 100, hours: int = 24, days: int = 7,
    authorization: str | None = Header(default=None),
):
    import read_api as api

    api.require_reader(authorization)
    if resource == "channel-route":
        return api._load_channel_risk_route(channel)
    limit = max(1, min(limit, 500))
    hours = max(1, min(hours, 24 * 14))
    days = max(1, min(days, 90))
    conn = api._read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            envelope = api._envelope(cur)
            if resource == "channels":
                cur.execute("SELECT channel_id,count(*) AS messages,max(source_received_at) AS last_message_at FROM raw_messages GROUP BY channel_id ORDER BY last_message_at DESC")
            elif resource == "messages":
                cur.execute(
                    """SELECT r.id::text AS raw_message_id,r.source_message_id,r.source_version,
                    r.source_received_at,r.message_text,
                    (SELECT jsonb_agg(jsonb_build_object('mime',m.mime,'object_key',m.object_key,
                      'status',m.download_status)) FROM media_assets m WHERE m.raw_message_id=r.id) AS media
                    FROM raw_messages r WHERE r.channel_id=%s
                    ORDER BY r.source_received_at DESC,r.id LIMIT %s""", (channel, limit + 1),
                )
            elif resource == "orders":
                cur.execute("SELECT account_id,updated_at,EXTRACT(EPOCH FROM now()-updated_at) AS age_seconds,payload FROM exchange_state_mirror ORDER BY account_id")
            elif resource == "intents":
                cur.execute(
                    """SELECT i.intent_id::text,i.account_id,i.instrument_id,i.action::text,i.status::text,
                    i.created_at,i.valid_until,r.reason FROM trade_intents i
                    JOIN risk_decisions r ON r.risk_decision_id=i.risk_decision_id
                    WHERE (%s='' OR i.status::text=%s) AND (%s='' OR split_part(i.instrument_id,'-',1)=%s)
                    ORDER BY i.created_at DESC,i.intent_id LIMIT %s""",
                    (status,status,symbol,symbol.upper(),limit + 1),
                )
            elif resource == "intent":
                import re

                if not re.fullmatch(r"[0-9a-f-]{4,36}", prefix.lower()):
                    raise HTTPException(status_code=400, detail="intent id requires at least four hex characters")
                cur.execute("SELECT intent_id::text FROM trade_intents WHERE intent_id::text LIKE %s ORDER BY created_at DESC LIMIT 2", (prefix.lower() + "%",))
                matches = cur.fetchall()
                if len(matches) != 1:
                    raise HTTPException(status_code=404 if not matches else 409, detail="intent prefix must match exactly one intent")
                intent_id = matches[0]["intent_id"]
                trace = api.load_intent_trace(conn, intent_id)
                cur.execute("SELECT event_type,actor,payload,created_at FROM audit_events WHERE intent_id=%s OR aggregate_id=%s ORDER BY created_at", (intent_id,intent_id))
                return jsonable_encoder({**envelope,"data":trace,"audit_events":cur.fetchall()})
            elif resource == "fills":
                cur.execute(
                    """SELECT account_id,intent_id::text,client_order_id,ts_event,payload
                    FROM execution_events WHERE event_type='OrderFilled'
                    AND ts_event>now()-(%s * interval '1 hour') ORDER BY ts_event DESC LIMIT %s""",
                    (hours,limit + 1),
                )
            elif resource == "outcomes":
                cur.execute(
                    """SELECT instrument_id,account_id,side,entry_avg_price,exit_avg_price,filled_quantity,
                    realized_pnl,fees,r_multiple,holding_seconds,closed_at FROM trade_outcomes
                    WHERE closed_at>now()-(%s * interval '1 day') ORDER BY closed_at DESC LIMIT %s""",
                    (days,limit + 1),
                )
            else:
                cur.execute("SELECT account_id,status,last_seen_at,node_id FROM node_heartbeats ORDER BY node_id")
                nodes = [dict(row) for row in cur.fetchall()]
                cur.execute("SELECT status::text,count(*) AS count FROM trade_intents WHERE created_at>now()-(%s*interval '1 hour') GROUP BY status", (hours,))
                intents = [dict(row) for row in cur.fetchall()]
                cur.execute("SELECT account_id,sum(realized_pnl) AS realized_pnl,count(*) AS closed FROM trade_outcomes WHERE closed_at>now()-(%s*interval '1 hour') GROUP BY account_id", (hours,))
                outcomes = [dict(row) for row in cur.fetchall()]
                cur.execute("SELECT account_id,count(*) AS fills FROM execution_events WHERE event_type='OrderFilled' AND ts_event>now()-(%s*interval '1 hour') GROUP BY account_id", (hours,))
                fills = [dict(row) for row in cur.fetchall()]
                return jsonable_encoder({**envelope,"window_hours":hours,"nodes":nodes,
                    "intents_by_status":intents,"closed_outcomes_by_account":outcomes,"fills_by_account":fills})
            rows = [dict(row) for row in cur.fetchall()]
            bounded = resource not in {"channels", "orders"}
            return jsonable_encoder({**envelope,resource:rows[:limit] if bounded else rows,
                                     "truncated":bounded and len(rows)>limit})
    finally:
        conn.close()
