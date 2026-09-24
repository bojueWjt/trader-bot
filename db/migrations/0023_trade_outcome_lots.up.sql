-- One closed entry lot can exist per intent; FIFO exits must not collapse
-- old and new lots onto UNIQUE (intent_id, account_id). Unknown/manual lots
-- have no trade_intents row.
ALTER TABLE trade_outcomes
    ALTER COLUMN intent_id DROP NOT NULL;

ALTER TABLE trade_outcomes
    DROP CONSTRAINT IF EXISTS uq_trade_outcomes_intent_account;

ALTER TABLE trade_outcomes
    ADD COLUMN IF NOT EXISTS lot_key text,
    ADD COLUMN IF NOT EXISTS position_side text,
    ADD COLUMN IF NOT EXISTS attribution text NOT NULL DEFAULT 'unknown';

UPDATE trade_outcomes
   SET lot_key = intent_id::text
 WHERE lot_key IS NULL
   AND intent_id IS NOT NULL;

UPDATE trade_outcomes
   SET lot_key = outcome_id::text
 WHERE lot_key IS NULL;

UPDATE trade_outcomes
   SET position_side = side
 WHERE position_side IS NULL
   AND side IS NOT NULL;

UPDATE trade_outcomes
   SET attribution = 'robot'
 WHERE attribution = 'unknown';

ALTER TABLE trade_outcomes
    ALTER COLUMN lot_key SET NOT NULL;

ALTER TABLE trade_outcomes
    ADD CONSTRAINT uq_trade_outcomes_account_lot UNIQUE (account_id, lot_key);

ALTER TABLE trade_outcomes
    ADD CONSTRAINT ck_trade_outcomes_attribution
        CHECK (attribution IN ('robot', 'manual', 'unknown'));

CREATE INDEX IF NOT EXISTS idx_trade_outcomes_account_lot_key
    ON trade_outcomes (account_id, lot_key);

GRANT SELECT ON trade_outcomes TO trader_v3_operator_query;
GRANT SELECT ON trade_outcomes TO trader_v3_node_control;
GRANT SELECT ON trade_outcomes TO trader_v3_event_ingest;
