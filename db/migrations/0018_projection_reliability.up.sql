-- 0018: projection reliability (2026-08-28 execution-state arch migration, WP-A).
-- The node event ingest savepoint used to swallow projection derivation errors
-- with no durable trace (orders_projection silently stopped updating). Give the
-- ingest path a durable failure record and per-projector watermarks.

CREATE TABLE projection_failures (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id text NOT NULL,
    account_id text NOT NULL,
    projector text NOT NULL,
    error text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE INDEX idx_projection_failures_account_created
    ON projection_failures (account_id, created_at);
CREATE INDEX idx_projection_failures_unresolved
    ON projection_failures (account_id, projector)
    WHERE resolved_at IS NULL;

CREATE TABLE projection_watermarks (
    account_id text NOT NULL,
    projector text NOT NULL,
    last_event_id text NOT NULL,
    last_event_ts timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, projector)
);

-- Role privileges (same roles as 0012/0017): the event-ingest writer records
-- failures and advances watermarks; operators mark failures resolved and read
-- both tables; node control reads for diagnostics.
GRANT SELECT, INSERT ON projection_failures
    TO trader_v3_event_ingest;
GRANT SELECT, INSERT, UPDATE ON projection_watermarks
    TO trader_v3_event_ingest;

GRANT SELECT ON projection_failures, projection_watermarks
    TO trader_v3_operator_query;
GRANT UPDATE (resolved_at) ON projection_failures
    TO trader_v3_operator_query;

GRANT SELECT ON projection_failures, projection_watermarks
    TO trader_v3_node_control;

-- P0-1 (batch 1.1): the ingest-path order reducer runs under
-- trader_v3_event_ingest but 0012 never granted it the reducer's full table
-- surface. OrderProjectionReducer.apply_event needs:
--   * order_events: SELECT (event_id dedupe) + INSERT (append-only log);
--   * reconciliation_findings: SELECT + INSERT (record_reconciliation_finding
--     on illegal transitions);
--   * reconciliation_runs: INSERT (record_reconciliation_finding creates a
--     run row when none is supplied).
-- execution_events and the *_projection tables were already granted in 0012.
GRANT SELECT, INSERT ON order_events
    TO trader_v3_event_ingest;
GRANT SELECT, INSERT ON reconciliation_findings
    TO trader_v3_event_ingest;
GRANT INSERT ON reconciliation_runs
    TO trader_v3_event_ingest;
