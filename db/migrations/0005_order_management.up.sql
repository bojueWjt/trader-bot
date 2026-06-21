CREATE TABLE IF NOT EXISTS execution_jobs (
    execution_job_id uuid PRIMARY KEY,
    intent_id uuid REFERENCES trade_intents(intent_id),
    account_id text NOT NULL,
    instrument_id text NOT NULL,
    venue_symbol text NOT NULL,
    action text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    request_id text,
    idempotency_key text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    CONSTRAINT uq_execution_jobs_account_idempotency UNIQUE (account_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_execution_jobs_intent_id ON execution_jobs (intent_id);
CREATE INDEX IF NOT EXISTS idx_execution_jobs_status ON execution_jobs (status);
CREATE INDEX IF NOT EXISTS idx_execution_jobs_request_id ON execution_jobs (request_id);

-- orders_projection.intent_id already exists in the baseline schema (0001); add only the new columns.
ALTER TABLE orders_projection
    ADD COLUMN IF NOT EXISTS execution_job_id uuid REFERENCES execution_jobs(execution_job_id),
    ADD COLUMN IF NOT EXISTS venue_symbol text,
    ADD COLUMN IF NOT EXISTS lifecycle_role text;

CREATE INDEX IF NOT EXISTS idx_orders_projection_execution_job_id
    ON orders_projection (execution_job_id);
CREATE INDEX IF NOT EXISTS idx_orders_projection_venue_symbol
    ON orders_projection (account_id, venue_symbol);
CREATE INDEX IF NOT EXISTS idx_orders_projection_lifecycle_role
    ON orders_projection (account_id, lifecycle_role);

CREATE TABLE IF NOT EXISTS order_events (
    order_event_row_id uuid PRIMARY KEY,
    event_id text NOT NULL,
    execution_job_id uuid REFERENCES execution_jobs(execution_job_id),
    account_id text NOT NULL,
    order_projection_id uuid REFERENCES orders_projection(order_projection_id),
    client_order_id text,
    venue_order_id text,
    event_type text NOT NULL,
    lifecycle_role text,
    position_key text,
    ts_event timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_order_events_event_id UNIQUE (event_id)
);

CREATE INDEX IF NOT EXISTS idx_order_events_execution_job_id ON order_events (execution_job_id);
CREATE INDEX IF NOT EXISTS idx_order_events_account_event ON order_events (account_id, ts_event);

CREATE TABLE IF NOT EXISTS order_links (
    order_link_id uuid PRIMARY KEY,
    account_id text NOT NULL,
    parent_order_projection_id uuid REFERENCES orders_projection(order_projection_id),
    child_order_projection_id uuid REFERENCES orders_projection(order_projection_id),
    link_type text NOT NULL,
    lifecycle_role text,
    position_key text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_order_links_pair_type UNIQUE (
        parent_order_projection_id,
        child_order_projection_id,
        link_type
    )
);

CREATE INDEX IF NOT EXISTS idx_order_links_account_position ON order_links (account_id, position_key);

CREATE TABLE IF NOT EXISTS protective_orders_projection (
    protective_order_projection_id uuid PRIMARY KEY,
    account_id text NOT NULL,
    position_key text NOT NULL,
    venue_symbol text NOT NULL,
    lifecycle_role text NOT NULL,
    order_projection_id uuid REFERENCES orders_projection(order_projection_id),
    client_order_id text,
    venue_order_id text,
    status text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    quantity numeric,
    price numeric,
    trigger_price numeric,
    updated_from_event_id text,
    ts_event timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_protective_orders_quantity CHECK (quantity IS NULL OR quantity >= 0),
    CONSTRAINT ck_protective_orders_price CHECK (price IS NULL OR price > 0),
    CONSTRAINT ck_protective_orders_trigger_price CHECK (trigger_price IS NULL OR trigger_price > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_protective_orders_active_role
    ON protective_orders_projection (account_id, position_key, lifecycle_role)
    WHERE active;
CREATE INDEX IF NOT EXISTS idx_protective_orders_order_projection_id
    ON protective_orders_projection (order_projection_id);

CREATE TABLE IF NOT EXISTS risk_reservations (
    risk_reservation_id uuid PRIMARY KEY,
    execution_job_id uuid REFERENCES execution_jobs(execution_job_id),
    account_id text NOT NULL,
    position_key text,
    venue_symbol text NOT NULL,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'reserved',
    notional numeric NOT NULL DEFAULT 0,
    risk_amount numeric NOT NULL DEFAULT 0,
    margin_amount numeric NOT NULL DEFAULT 0,
    expires_at timestamptz,
    released_at timestamptz,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_risk_reservations_account_idempotency UNIQUE (account_id, idempotency_key),
    CONSTRAINT ck_risk_reservations_notional CHECK (notional >= 0),
    CONSTRAINT ck_risk_reservations_risk_amount CHECK (risk_amount >= 0),
    CONSTRAINT ck_risk_reservations_margin_amount CHECK (margin_amount >= 0)
);

CREATE INDEX IF NOT EXISTS idx_risk_reservations_execution_job_id
    ON risk_reservations (execution_job_id);
CREATE INDEX IF NOT EXISTS idx_risk_reservations_status ON risk_reservations (status);

CREATE TABLE IF NOT EXISTS order_management_settings (
    order_management_setting_id uuid PRIMARY KEY,
    scope text NOT NULL,
    scope_key text NOT NULL,
    version integer NOT NULL,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    effective_from timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    reason text NOT NULL,
    request_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_order_management_settings_scope_version UNIQUE (scope, scope_key, version),
    CONSTRAINT ck_order_management_settings_version CHECK (version > 0)
);

CREATE INDEX IF NOT EXISTS idx_order_management_settings_scope
    ON order_management_settings (scope, scope_key);
CREATE INDEX IF NOT EXISTS idx_order_management_settings_request_id
    ON order_management_settings (request_id);

CREATE TABLE IF NOT EXISTS order_management_setting_versions (
    setting_version_id uuid PRIMARY KEY,
    scope text NOT NULL,
    scope_key text NOT NULL,
    version integer NOT NULL,
    previous_version integer,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    changed_by text NOT NULL,
    reason text NOT NULL,
    request_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_order_management_setting_versions_scope_version UNIQUE (scope, scope_key, version),
    CONSTRAINT ck_order_management_setting_versions_version CHECK (version > 0)
);

CREATE OR REPLACE FUNCTION prevent_order_management_setting_versions_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'order_management_setting_versions is immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_order_management_setting_versions_immutable
    ON order_management_setting_versions;
CREATE TRIGGER trg_order_management_setting_versions_immutable
BEFORE UPDATE OR DELETE ON order_management_setting_versions
FOR EACH ROW
EXECUTE FUNCTION prevent_order_management_setting_versions_update();

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    reconciliation_run_id uuid PRIMARY KEY,
    account_id text,
    status text NOT NULL DEFAULT 'requested',
    request_id text,
    reason text,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_reconciliation_runs_time_order CHECK (
        completed_at IS NULL OR completed_at >= started_at
    )
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_status ON reconciliation_runs (status);
CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_request_id ON reconciliation_runs (request_id);

CREATE TABLE IF NOT EXISTS reconciliation_findings (
    reconciliation_finding_id uuid PRIMARY KEY,
    reconciliation_run_id uuid NOT NULL REFERENCES reconciliation_runs(reconciliation_run_id) ON DELETE CASCADE,
    account_id text NOT NULL,
    finding_type text NOT NULL,
    severity text NOT NULL,
    status text NOT NULL DEFAULT 'open',
    position_key text,
    order_projection_id uuid REFERENCES orders_projection(order_projection_id),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_findings_run
    ON reconciliation_findings (reconciliation_run_id);
CREATE INDEX IF NOT EXISTS idx_reconciliation_findings_status
    ON reconciliation_findings (status);

CREATE TABLE IF NOT EXISTS node_command_runs (
    node_command_run_id uuid PRIMARY KEY,
    command_id uuid REFERENCES operator_commands(command_id),
    node_id text NOT NULL,
    command_type text NOT NULL,
    request_id text,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'requested',
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_node_command_runs_node_idempotency UNIQUE (node_id, idempotency_key),
    CONSTRAINT ck_node_command_runs_time_order CHECK (
        completed_at IS NULL OR completed_at >= started_at
    )
);

CREATE INDEX IF NOT EXISTS idx_node_command_runs_request_id ON node_command_runs (request_id);
CREATE INDEX IF NOT EXISTS idx_node_command_runs_status ON node_command_runs (status);

CREATE TABLE IF NOT EXISTS price_feed_status (
    price_feed_status_id uuid PRIMARY KEY,
    account_id text NOT NULL,
    venue_symbol text NOT NULL,
    source text NOT NULL,
    mark_price numeric,
    last_price numeric,
    bid_price numeric,
    ask_price numeric,
    last_event_at timestamptz,
    stale boolean NOT NULL DEFAULT true,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_price_feed_status_identity UNIQUE (account_id, venue_symbol, source),
    CONSTRAINT ck_price_feed_status_mark_price CHECK (mark_price IS NULL OR mark_price > 0),
    CONSTRAINT ck_price_feed_status_last_price CHECK (last_price IS NULL OR last_price > 0),
    CONSTRAINT ck_price_feed_status_bid_price CHECK (bid_price IS NULL OR bid_price > 0),
    CONSTRAINT ck_price_feed_status_ask_price CHECK (ask_price IS NULL OR ask_price > 0)
);

CREATE INDEX IF NOT EXISTS idx_price_feed_status_stale ON price_feed_status (stale);
CREATE INDEX IF NOT EXISTS idx_price_feed_status_updated_at ON price_feed_status (updated_at);

-- audit_events already exists in the baseline schema (0001) with actor/aggregate/trace fields.
-- Enhance it ADDITIVELY with the order-management audit columns (PLAN §13; consumed by OM1-07).
ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS action text,
    ADD COLUMN IF NOT EXISTS target text,
    ADD COLUMN IF NOT EXISTS before_state jsonb,
    ADD COLUMN IF NOT EXISTS after_state jsonb,
    ADD COLUMN IF NOT EXISTS reason text,
    ADD COLUMN IF NOT EXISTS request_id uuid;

CREATE INDEX IF NOT EXISTS idx_audit_events_request_id ON audit_events (request_id);
