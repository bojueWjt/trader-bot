DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM hermes_decisions
        WHERE action::text = 'cancel_order'
    ) OR EXISTS (
        SELECT 1
        FROM trade_intents
        WHERE action::text = 'cancel_order'
    ) THEN
        RAISE EXCEPTION
            'cancel_order contract rollback requires zero persisted cancel_order rows';
    END IF;
END
$$;

DROP INDEX IF EXISTS idx_trade_intents_pending_opening_symbols;

ALTER TABLE hermes_decisions
    ALTER COLUMN action TYPE text
    USING action::text;

ALTER TABLE trade_intents
    ALTER COLUMN action TYPE text
    USING action::text;

DROP TYPE approved_trade_action_v1;
DROP TYPE hermes_action_v1;

CREATE TYPE hermes_action_v1 AS ENUM (
    'open_position',
    'add_position',
    'partial_close',
    'close_position',
    'move_stop_loss',
    'move_stop_to_entry',
    'replace_take_profits',
    'hold',
    'ignore',
    'needs_review'
);

CREATE TYPE approved_trade_action_v1 AS ENUM (
    'open_position',
    'add_position',
    'partial_close',
    'close_position',
    'move_stop_loss',
    'move_stop_to_entry',
    'replace_take_profits'
);

ALTER TABLE hermes_decisions
    ALTER COLUMN action TYPE hermes_action_v1
    USING action::hermes_action_v1;

ALTER TABLE trade_intents
    ALTER COLUMN action TYPE approved_trade_action_v1
    USING action::approved_trade_action_v1;

CREATE INDEX IF NOT EXISTS idx_trade_intents_pending_opening_symbols
ON trade_intents (
    account_id,
    instrument_id,
    updated_at DESC,
    intent_id
)
WHERE status IN ('approved', 'expired')
  AND action IN ('open_position', 'add_position');
