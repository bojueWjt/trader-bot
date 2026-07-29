CREATE TABLE IF NOT EXISTS trade_outcome_job_runs (
    job_name text PRIMARY KEY,
    status text NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    outcome_count bigint NOT NULL DEFAULT 0,
    error text,
    CONSTRAINT ck_trade_outcome_job_runs_status
        CHECK (status IN ('running', 'succeeded', 'failed')),
    CONSTRAINT ck_trade_outcome_job_runs_outcome_count
        CHECK (outcome_count >= 0),
    CONSTRAINT ck_trade_outcome_job_runs_time_order
        CHECK (completed_at IS NULL OR completed_at >= started_at)
);
