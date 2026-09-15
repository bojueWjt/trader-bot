-- G2: per-account durable signal tasks + Hermes claim attempt/claim_token.
-- Hermes claim_token is NOT the node writer fence
-- (read_api._require_node_writer: x_redis_fencing_epoch /
--  x_runtime_generation / x_lease_fencing_token). Do not reuse attempt as
-- runtime_generation. Stable source identity is
-- (source_platform, channel_id, source_message_id, edit_version, account_id).

ALTER TABLE message_processing_runs
    ADD COLUMN IF NOT EXISTS attempt integer NOT NULL DEFAULT 0;

ALTER TABLE message_processing_runs
    ADD COLUMN IF NOT EXISTS claim_token uuid;

ALTER TABLE message_processing_runs
    ADD COLUMN IF NOT EXISTS account_id text;

ALTER TABLE message_processing_runs
    ADD COLUMN IF NOT EXISTS processing_purpose text NOT NULL DEFAULT 'legacy';

ALTER TABLE message_processing_runs
    DROP CONSTRAINT IF EXISTS ck_message_processing_runs_processing_purpose;

ALTER TABLE message_processing_runs
    ADD CONSTRAINT ck_message_processing_runs_processing_purpose
    CHECK (processing_purpose IN ('legacy', 'shadow'));

ALTER TABLE message_processing_runs
    DROP CONSTRAINT IF EXISTS ck_message_processing_runs_attempt;

ALTER TABLE message_processing_runs
    ADD CONSTRAINT ck_message_processing_runs_attempt
    CHECK (attempt >= 0);

CREATE TABLE signal_dispatch_tasks (
    task_id uuid PRIMARY KEY,
    identity_key text NOT NULL,
    source_platform text NOT NULL,
    channel_id text NOT NULL,
    source_message_id text NOT NULL,
    edit_version text NOT NULL,
    account_id text NOT NULL,
    action text NOT NULL DEFAULT 'evaluate',
    raw_message_id uuid NOT NULL REFERENCES raw_messages(id),
    related_task_id uuid REFERENCES signal_dispatch_tasks(task_id),
    current_processing_run_id uuid REFERENCES message_processing_runs(processing_run_id),
    status text NOT NULL,
    attempt integer NOT NULL DEFAULT 0,
    claim_token uuid,
    worker_id text,
    lease_expires_at timestamptz,
    disposition text,
    disposition_reason text,
    shadow_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_signal_dispatch_identity UNIQUE (
        source_platform,
        channel_id,
        source_message_id,
        edit_version,
        account_id
    ),
    CONSTRAINT uq_signal_dispatch_identity_key UNIQUE (identity_key),
    CONSTRAINT ck_signal_dispatch_status CHECK (
        status IN (
            'pending',
            'leased',
            'shadow_dispatched',
            'expired',
            'failed',
            'skipped'
        )
    ),
    CONSTRAINT ck_signal_dispatch_attempt CHECK (attempt >= 0)
);

COMMENT ON TABLE signal_dispatch_tasks IS
    'G2 per-account signal work. Source identity is (source_platform, channel, message, edit_version, account). Each claim writes message_processing_runs (processing_run_id, attempt, claim_token). claim_token is Hermes-layer only; node writer fence stays on read_api._require_node_writer. Shadow dispatch does not submit live orders.';

COMMENT ON COLUMN message_processing_runs.claim_token IS
    'Single-use Hermes claim token for this attempt. Not x_lease_fencing_token / redis epoch / runtime_generation.';

COMMENT ON COLUMN message_processing_runs.attempt IS
    'Hermes claim attempt counter. Not node runtime_generation.';

COMMENT ON COLUMN message_processing_runs.processing_purpose IS
    'legacy = outbox live writer; shadow = G2 signal queue. Active-run uniqueness is per purpose so shadow cannot block legacy of the same raw_message.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_message_processing_runs_active_purpose
    ON message_processing_runs (raw_message_id, processing_purpose)
    WHERE status IN ('started', 'processing');

CREATE INDEX idx_signal_dispatch_tasks_claim
    ON signal_dispatch_tasks (account_id, status, lease_expires_at, created_at);

CREATE INDEX idx_signal_dispatch_tasks_raw_message
    ON signal_dispatch_tasks (raw_message_id);

CREATE INDEX idx_signal_dispatch_tasks_processing_run
    ON signal_dispatch_tasks (current_processing_run_id);

GRANT SELECT ON signal_dispatch_tasks
    TO trader_v3_node_control,
       trader_v3_operator_query;

GRANT SELECT, INSERT, UPDATE ON signal_dispatch_tasks
    TO trader_v3_event_ingest;
