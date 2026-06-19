-- Revert message_processing_runs to the 0001 canonical baseline.

DROP INDEX IF EXISTS idx_message_processing_runs_lease;

ALTER TABLE message_processing_runs
    DROP CONSTRAINT ck_message_processing_runs_status;

-- coerce queue-only statuses so the original CHECK can be re-applied
UPDATE message_processing_runs
   SET status = 'failed'
 WHERE status IN ('processing', 'hermes_timeout', 'hermes_failed', 'outbox_failed');

ALTER TABLE message_processing_runs
    ADD CONSTRAINT ck_message_processing_runs_status
    CHECK (status IN ('started', 'succeeded', 'failed', 'skipped'));

ALTER TABLE message_processing_runs
    DROP COLUMN IF EXISTS lease_expires_at;
