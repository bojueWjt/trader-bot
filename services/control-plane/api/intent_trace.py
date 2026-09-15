"""Intent trace and incidents read aggregation (G3-T1 slice 1).

SELECT-only. Missing layers are None. Hermes presence follows the linked
raw_messages.source relation, not prompt_version string matching.
"""

from __future__ import annotations

from typing import Any

from psycopg2.extras import RealDictCursor


TRACE_KEYS = (
    "raw_message",
    "hermes_decision",
    "risk_decision",
    "intent",
    "execution_events",
    "node_acks",
)


def _iso(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _incident_table_columns(cur) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='production_incidents'
        """
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def _unique_heartbeat_node(cur, account_id: str) -> str | None:
    cur.execute(
        """
        SELECT node_id
        FROM node_heartbeats
        WHERE account_id=%s
        """,
        (account_id,),
    )
    rows = cur.fetchall()
    if len(rows) != 1:
        return None
    node_id = rows[0]["node_id"]
    return str(node_id) if node_id else None


def load_intent_trace(conn, intent_id: str) -> dict[str, Any] | None:
    """Aggregate six trace layers for one intent. None if the intent is absent."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT intent_id::text AS intent_id,
                   account_id,
                   instrument_id,
                   action::text AS action,
                   status::text AS status,
                   order_plan,
                   hermes_decision_id::text AS hermes_decision_id,
                   risk_decision_id::text AS risk_decision_id
            FROM trade_intents
            WHERE intent_id=%s
            """,
            (intent_id,),
        )
        intent_row = cur.fetchone()
        if intent_row is None:
            return None

        hermes_row = None
        raw_row = None
        if intent_row["hermes_decision_id"]:
            cur.execute(
                """
                SELECT decision_id::text AS decision_id,
                       action::text AS action,
                       message_type,
                       evidence,
                       prompt_version,
                       model_version,
                       raw_message_id::text AS raw_message_id,
                       created_at
                FROM hermes_decisions
                WHERE decision_id=%s
                """,
                (intent_row["hermes_decision_id"],),
            )
            hermes_row = cur.fetchone()

        if hermes_row and hermes_row.get("raw_message_id"):
            cur.execute(
                """
                SELECT id::text AS id,
                       source,
                       channel_id,
                       message_text,
                       source_received_at
                FROM raw_messages
                WHERE id=%s
                """,
                (hermes_row["raw_message_id"],),
            )
            raw_row = cur.fetchone()

        # The operator's synthetic authorization row is not the signal's model
        # result. Follow the persisted task binding to show its original input.
        signal_result = None
        plan = _mapping(intent_row.get("order_plan"))
        signal_execution = _mapping(plan.get("signal_execution"))
        if signal_execution.get("task_id"):
            cur.execute(
                """
                SELECT t.shadow_result, t.status, t.disposition_reason,
                       t.current_processing_run_id::text AS processing_run_id,
                       r.id::text AS id, r.source, r.channel_id,
                       r.message_text, r.source_received_at
                FROM signal_dispatch_tasks t
                JOIN raw_messages r ON r.id = t.raw_message_id
                WHERE t.task_id=%s AND t.account_id=%s AND t.operator_intent_id=%s
                """,
                (signal_execution["task_id"], intent_row["account_id"], intent_id),
            )
            task = cur.fetchone()
            if task:
                raw_row = task
                signal_result = _mapping(task.get("shadow_result"))
                semantic = _mapping(signal_result.get("semantic"))
                decision = _mapping(semantic.get("decision"))
                classification = _mapping(decision.get("classification"))
                hermes_row = None
                if decision.get("decision_id"):
                    hermes_row = {
                        "decision_id": decision["decision_id"],
                        "action": classification.get("action"),
                        "message_type": classification.get("message_type"),
                        "evidence": decision.get("evidence"),
                    }

        media: list[dict[str, Any]] = []
        if raw_row:
            cur.execute(
                """
                SELECT mime, object_key
                FROM media_assets
                WHERE raw_message_id=%s
                ORDER BY object_key
                """,
                (raw_row["id"],),
            )
            media = [
                {"mime": row["mime"], "object_key": row["object_key"]}
                for row in cur.fetchall()
            ]

        risk_row = None
        if intent_row["risk_decision_id"]:
            cur.execute(
                """
                SELECT risk_decision_id::text AS risk_decision_id,
                       status::text AS status,
                       account_id,
                       instrument_id,
                       reason
                FROM risk_decisions
                WHERE risk_decision_id=%s
                """,
                (intent_row["risk_decision_id"],),
            )
            risk_row = cur.fetchone()

        cur.execute(
            """
            SELECT event_type, client_order_id, ts_event, payload
            FROM execution_events
            WHERE intent_id=%s
            ORDER BY ts_event
            """,
            (intent_id,),
        )
        event_rows = cur.fetchall()

        cur.execute(
            """
            SELECT a.node_id, a.status, a.ack_at, a.detail
            FROM command_node_acks AS a
            JOIN operator_commands AS c ON c.command_id = a.command_id
            WHERE c.scope->>'intent_id' = %s
            ORDER BY a.ack_at NULLS LAST, a.node_id
            """,
            (intent_id,),
        )
        ack_rows = cur.fetchall()

    raw_message = None
    if raw_row is not None:
        raw_message = {
            "id": raw_row["id"],
            "channel_id": raw_row["channel_id"],
            "message_text": raw_row["message_text"],
            "source_received_at": _iso(raw_row["source_received_at"]),
            "media": media,
        }

    hermes_decision = None
    raw_source = str((raw_row or {}).get("source") or "")
    if hermes_row is not None and raw_source != "operator":
        hermes_decision = {
            "decision_id": hermes_row["decision_id"],
            "action": hermes_row["action"],
            "message_type": hermes_row["message_type"],
            "evidence": hermes_row.get("evidence"),
        }

    risk_decision = None
    if risk_row is not None:
        risk_decision = {
            "risk_decision_id": risk_row["risk_decision_id"],
            "status": risk_row["status"],
            "account_id": risk_row["account_id"],
            "instrument_id": risk_row["instrument_id"],
            "reason": risk_row.get("reason"),
        }

    intent = {
        "intent_id": intent_row["intent_id"],
        "account_id": intent_row["account_id"],
        "action": intent_row["action"],
        "instrument_id": intent_row["instrument_id"],
        "status": intent_row["status"],
    }
    if signal_result is not None:
        intent["signal_execution"] = signal_execution
        intent["signal_stages"] = signal_result.get("stages")

    execution_events = None
    if event_rows:
        execution_events = [
            {
                "event_type": row["event_type"],
                "client_order_id": row["client_order_id"],
                "ts_event": _iso(row["ts_event"]),
                "payload": _mapping(row.get("payload")),
            }
            for row in event_rows
        ]

    node_acks = None
    if ack_rows:
        node_acks = [
            {
                "node_id": row["node_id"],
                "status": row["status"],
                "ack_at": _iso(row["ack_at"]),
                "detail": row.get("detail"),
            }
            for row in ack_rows
        ]

    return {
        "raw_message": raw_message,
        "hermes_decision": hermes_decision,
        "risk_decision": risk_decision,
        "intent": intent,
        "execution_events": execution_events,
        "node_acks": node_acks,
    }


def list_open_incidents(conn) -> list[dict[str, Any]]:
    """Readonly open-incident list. Node uses incident column, else unique heartbeat."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        columns = _incident_table_columns(cur)
        has_own_node = "node_id" in columns or "node" in columns
        node_select = "NULL::text AS own_node"
        if "node_id" in columns:
            node_select = "node_id::text AS own_node"
        elif "node" in columns:
            node_select = "node::text AS own_node"
        cur.execute(
            f"""
            SELECT incident_id::text AS id,
                   summary,
                   opened_at,
                   account_id,
                   {node_select}
            FROM production_incidents
            WHERE status='open'
            ORDER BY opened_at, incident_id
            """
        )
        rows = [dict(row) for row in cur.fetchall()]
        incidents: list[dict[str, Any]] = []
        for row in rows:
            node = str(row.get("own_node") or "").strip() or None
            if node is None and not has_own_node:
                node = _unique_heartbeat_node(cur, str(row.get("account_id") or ""))
            incidents.append(
                {
                    "id": row["id"],
                    "node": node,
                    "summary": row["summary"],
                    "opened_at": _iso(row["opened_at"]),
                }
            )
        return incidents
