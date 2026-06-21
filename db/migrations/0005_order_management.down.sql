DROP INDEX IF EXISTS idx_price_feed_status_updated_at;
DROP INDEX IF EXISTS idx_price_feed_status_stale;
DROP INDEX IF EXISTS idx_node_command_runs_status;
DROP INDEX IF EXISTS idx_node_command_runs_request_id;
DROP INDEX IF EXISTS idx_reconciliation_findings_status;
DROP INDEX IF EXISTS idx_reconciliation_findings_run;
DROP INDEX IF EXISTS idx_reconciliation_runs_request_id;
DROP INDEX IF EXISTS idx_reconciliation_runs_status;
DROP INDEX IF EXISTS idx_order_management_settings_request_id;
DROP INDEX IF EXISTS idx_order_management_settings_scope;
DROP INDEX IF EXISTS idx_risk_reservations_status;
DROP INDEX IF EXISTS idx_risk_reservations_execution_job_id;
DROP INDEX IF EXISTS idx_protective_orders_order_projection_id;
DROP INDEX IF EXISTS uq_protective_orders_active_role;
DROP INDEX IF EXISTS idx_order_links_account_position;
DROP INDEX IF EXISTS idx_order_events_account_event;
DROP INDEX IF EXISTS idx_order_events_execution_job_id;
DROP INDEX IF EXISTS idx_orders_projection_lifecycle_role;
DROP INDEX IF EXISTS idx_orders_projection_venue_symbol;
DROP INDEX IF EXISTS idx_orders_projection_execution_job_id;
DROP INDEX IF EXISTS idx_execution_jobs_request_id;
DROP INDEX IF EXISTS idx_execution_jobs_status;
DROP INDEX IF EXISTS idx_execution_jobs_intent_id;

-- NOTE: orders_projection.intent_id is a BASELINE column (0001) — do NOT drop it here.
ALTER TABLE orders_projection
    DROP COLUMN IF EXISTS execution_job_id,
    DROP COLUMN IF EXISTS venue_symbol,
    DROP COLUMN IF EXISTS lifecycle_role;

DROP TRIGGER IF EXISTS trg_order_management_setting_versions_immutable
    ON order_management_setting_versions;

DROP TABLE IF EXISTS reconciliation_findings;
DROP TABLE IF EXISTS reconciliation_runs;
DROP TABLE IF EXISTS price_feed_status;
DROP TABLE IF EXISTS node_command_runs;
DROP TABLE IF EXISTS risk_reservations;
DROP TABLE IF EXISTS protective_orders_projection;
DROP TABLE IF EXISTS order_links;
DROP TABLE IF EXISTS order_events;
DROP TABLE IF EXISTS execution_jobs;
DROP TABLE IF EXISTS order_management_setting_versions;
DROP TABLE IF EXISTS order_management_settings;

-- audit_events is a BASELINE table (0001) — do NOT drop it; only reverse the additive columns.
DROP INDEX IF EXISTS idx_audit_events_request_id;
ALTER TABLE audit_events
    DROP COLUMN IF EXISTS action,
    DROP COLUMN IF EXISTS target,
    DROP COLUMN IF EXISTS before_state,
    DROP COLUMN IF EXISTS after_state,
    DROP COLUMN IF EXISTS reason,
    DROP COLUMN IF EXISTS request_id;

DROP FUNCTION IF EXISTS prevent_order_management_setting_versions_update();
