-- A-04 queue requires a visibility lease and worker lifecycle statuses on the
-- canonical message_processing_runs table. These were originally applied only in
-- A-04's test conftest, which made the queue pass tests but break against the real
-- canonical schema in production. This migration makes the canonical schema match
-- what services/hermes-worker/queue/claims.py needs.

ALTER TABLE message_processing_runs
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;

ALTER TABLE message_processing_runs
    DROP CONSTRAINT ck_message_processing_runs_status;

ALTER TABLE message_processing_runs
    ADD CONSTRAINT ck_message_processing_runs_status
    CHECK (
        status IN (
            'started',
            'processing',
            'succeeded',
            'failed',
            'skipped',
            'hermes_timeout',
            'hermes_failed',
            'outbox_failed'
        )
    );

-- supports the claim path: find/expire active runs by (status, lease_expires_at)
CREATE INDEX IF NOT EXISTS idx_message_processing_runs_lease
    ON message_processing_runs (status, lease_expires_at);
