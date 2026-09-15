REVOKE UPDATE (operator_intent_id, status, updated_at) ON signal_dispatch_tasks
    FROM trader_v3_operator_query;
DROP TRIGGER IF EXISTS trg_signal_execution_request_immutable ON signal_dispatch_tasks;
DROP FUNCTION IF EXISTS protect_signal_execution_request();
ALTER TABLE signal_dispatch_tasks
    DROP COLUMN operator_intent_id,
    DROP COLUMN execution_context,
    DROP COLUMN execution_request,
    DROP COLUMN processing_purpose;
ALTER TABLE signal_dispatch_tasks DROP CONSTRAINT ck_signal_dispatch_status;
ALTER TABLE signal_dispatch_tasks ADD CONSTRAINT ck_signal_dispatch_status CHECK (
    status IN ('pending', 'leased', 'shadow_dispatched', 'expired', 'failed', 'skipped')
);
DROP INDEX IF EXISTS uq_message_processing_runs_active_signal_account;
DROP INDEX uq_message_processing_runs_active_purpose;
CREATE UNIQUE INDEX uq_message_processing_runs_active_purpose
    ON message_processing_runs (raw_message_id, processing_purpose)
    WHERE status IN ('started', 'processing');
ALTER TABLE message_processing_runs DROP CONSTRAINT ck_signal_run_account;
ALTER TABLE message_processing_runs DROP CONSTRAINT ck_message_processing_runs_processing_purpose;
ALTER TABLE message_processing_runs ADD CONSTRAINT ck_message_processing_runs_processing_purpose
    CHECK (processing_purpose IN ('legacy', 'shadow'));
