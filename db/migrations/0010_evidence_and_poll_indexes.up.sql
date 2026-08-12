CREATE INDEX IF NOT EXISTS idx_execution_events_targeted_opening_evidence
ON execution_events (
    account_id,
    client_order_id,
    ts_event DESC,
    created_at DESC
)
WHERE client_order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trade_intents_pending_opening_symbols
ON trade_intents (
    account_id,
    instrument_id,
    updated_at DESC,
    intent_id
)
WHERE status IN ('approved', 'expired')
  AND action IN ('open_position', 'add_position');

CREATE INDEX IF NOT EXISTS idx_command_node_acks_pending_poll
ON command_node_acks (
    node_id,
    created_at,
    command_id
)
WHERE status='pending';
