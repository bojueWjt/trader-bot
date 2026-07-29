-- 0004: enforce one Hermes decision per raw message (dedup backstop).
-- PLAN: a retry must not create multiple executable decisions for one message.
-- The app-level claim/lease already prevents this in the happy path; this is the
-- DB-level guarantee so a logic/lease bug or re-queued message hard-fails instead of
-- producing a duplicate decision (which could become a duplicate intent/order, G7).
-- Replaces the non-unique index with a UNIQUE constraint on raw_message_id.
DROP INDEX IF EXISTS idx_hermes_decisions_raw_message_id;
ALTER TABLE hermes_decisions
    ADD CONSTRAINT uq_hermes_decisions_raw_message_id UNIQUE (raw_message_id);
