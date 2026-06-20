-- Revert 0004: restore the non-unique index on raw_message_id.
ALTER TABLE hermes_decisions
    DROP CONSTRAINT IF EXISTS uq_hermes_decisions_raw_message_id;
CREATE INDEX idx_hermes_decisions_raw_message_id ON hermes_decisions (raw_message_id);
