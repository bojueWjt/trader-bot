REVOKE SELECT, INSERT, UPDATE ON signal_dispatch_tasks
    FROM trader_v3_event_ingest;

REVOKE SELECT ON signal_dispatch_tasks
    FROM trader_v3_node_control,
         trader_v3_operator_query;

DROP TABLE IF EXISTS signal_dispatch_tasks;

DROP INDEX IF EXISTS uq_message_processing_runs_active_purpose;

ALTER TABLE message_processing_runs
    DROP CONSTRAINT IF EXISTS ck_message_processing_runs_attempt;

ALTER TABLE message_processing_runs
    DROP CONSTRAINT IF EXISTS ck_message_processing_runs_processing_purpose;

ALTER TABLE message_processing_runs
    DROP COLUMN IF EXISTS attempt;

ALTER TABLE message_processing_runs
    DROP COLUMN IF EXISTS claim_token;

ALTER TABLE message_processing_runs
    DROP COLUMN IF EXISTS account_id;

ALTER TABLE message_processing_runs
    DROP COLUMN IF EXISTS processing_purpose;
