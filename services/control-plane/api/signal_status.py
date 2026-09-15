"""Read-only signal coverage, including inputs with no downstream evidence."""


def load_signal_rows(cur, *, since, limit):
    cur.execute(
        """
        SELECT r.id::text AS raw_message_id, r.channel_id, r.source_message_id,
               r.source_version AS edit_version, r.message_text, r.ingested_at,
               r.raw_payload->>'source_ts' AS source_ts,
               r.raw_payload->>'receive_ts' AS receive_ts,
               t.task_id::text AS task_id, t.account_id, t.processing_purpose,
               t.status AS task_status, t.disposition_reason,
               t.shadow_result, t.created_at AS queued_at,
               COALESCE(t.operator_intent_id, i.intent_id)::text AS intent_id,
               COALESCE(si.status, i.status)::text AS intent_status,
               COALESCE(si.valid_until, i.valid_until) AS valid_until,
               d.action::text AS decision_action, rd.reason AS risk_reason,
               rd.status::text AS risk_status,
               (SELECT jsonb_agg(jsonb_build_object(
                    'status', o.status, 'filled_quantity', o.filled_quantity,
                    'client_order_id', o.client_order_id, 'lifecycle_role', o.lifecycle_role,
                    'payload', o.payload, 'order_type', o.order_type))
                FROM orders_projection o
                WHERE o.intent_id=COALESCE(t.operator_intent_id, i.intent_id)) AS orders,
               (SELECT e.payload->>'reason' FROM execution_events e
                WHERE e.intent_id=COALESCE(t.operator_intent_id, i.intent_id)
                  AND e.event_type IN ('OrderDenied','OrderRejected')
                ORDER BY e.ts_event DESC LIMIT 1) AS denial_reason
        FROM raw_messages r
        LEFT JOIN signal_dispatch_tasks t ON t.raw_message_id=r.id
        LEFT JOIN hermes_decisions d ON d.raw_message_id=r.id AND t.task_id IS NULL
        LEFT JOIN risk_decisions rd ON rd.hermes_decision_id=d.decision_id
        LEFT JOIN trade_intents i ON i.risk_decision_id=rd.risk_decision_id
        LEFT JOIN trade_intents si ON si.intent_id=t.operator_intent_id
        WHERE r.source <> 'operator' AND r.ingested_at >= %s
        ORDER BY r.ingested_at DESC, r.id, t.account_id
        LIMIT %s
        """,
        (since, limit + 1),
    )
    return [dict(row) for row in cur.fetchall()]


def signal_disposition(row, *, operation_status):
    """Every input gets a reason; missing evidence is pending, never a fill."""
    intent_status = row.get("intent_status")
    if row.get("intent_id"):
        status = operation_status(
            intent_status=intent_status or "pending", orders=row.get("orders") or [],
            denial_reason=row.get("denial_reason") or "",
        )
        if status == "filled":
            return "filled", "exchange_fill_recorded"
        if status in {"rejected", "denied", "failed", "expired", "cancelled", "canceled"}:
            return "rejected", row.get("denial_reason") or status
        return "pending", f"operation_{status}"
    task_status = row.get("task_status")
    reason = row.get("disposition_reason")
    result = row.get("shadow_result")
    operator_detail = result.get("operator_detail") if isinstance(result, dict) else None
    if isinstance(operator_detail, dict) and operator_detail.get("detail"):
        reason = f"{reason or task_status}: {str(operator_detail['detail'])[:500]}"
    if task_status in {"failed", "expired"}:
        return "rejected", reason or task_status
    if task_status == "skipped":
        return "skipped", reason or "decision_did_not_request_a_trade"
    if task_status == "shadow_dispatched":
        return "skipped", "shadow_only_no_live_submission"
    if task_status:
        return "pending", reason or f"signal_{task_status}"
    if row.get("risk_status") in {"rejected", "expired"}:
        return "rejected", row.get("risk_reason") or row["risk_status"]
    if row.get("decision_action") in {"hold", "ignore", "needs_review"}:
        return "skipped", f"decision_{row['decision_action']}"
    return "pending", "downstream_evidence_missing_requires_review"
