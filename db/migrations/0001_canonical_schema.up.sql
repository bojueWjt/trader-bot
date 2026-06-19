DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'message_type_v1') THEN
        CREATE TYPE message_type_v1 AS ENUM (
            'new_signal',
            'position_update',
            'close_update',
            'analysis',
            'noise',
            'ambiguous'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'hermes_action_v1') THEN
        CREATE TYPE hermes_action_v1 AS ENUM (
            'open_position',
            'add_position',
            'partial_close',
            'close_position',
            'move_stop_loss',
            'move_stop_to_entry',
            'replace_take_profits',
            'hold',
            'ignore',
            'needs_review'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'approved_trade_action_v1') THEN
        CREATE TYPE approved_trade_action_v1 AS ENUM (
            'open_position',
            'add_position',
            'partial_close',
            'close_position',
            'move_stop_loss',
            'move_stop_to_entry',
            'replace_take_profits'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'account_scope_v1') THEN
        CREATE TYPE account_scope_v1 AS ENUM ('unassigned', 'single', 'all');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'position_side_v1') THEN
        CREATE TYPE position_side_v1 AS ENUM ('long', 'short');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'entry_type_v1') THEN
        CREATE TYPE entry_type_v1 AS ENUM ('market', 'limit', 'zone', 'none');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'reconciliation_state_v1') THEN
        CREATE TYPE reconciliation_state_v1 AS ENUM ('healthy', 'degraded', 'failed');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'outbox_status') THEN
        CREATE TYPE outbox_status AS ENUM ('pending', 'published', 'failed');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'trade_intent_status') THEN
        CREATE TYPE trade_intent_status AS ENUM ('draft', 'approved', 'rejected', 'cancelled', 'expired');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'risk_decision_status') THEN
        CREATE TYPE risk_decision_status AS ENUM ('approved', 'rejected', 'needs_review');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'execution_command_status') THEN
        CREATE TYPE execution_command_status AS ENUM ('pending', 'dispatched', 'acknowledged', 'failed', 'cancelled');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'replay_status') THEN
        CREATE TYPE replay_status AS ENUM ('pending', 'running', 'completed', 'failed', 'cancelled');
    END IF;
END $$;

CREATE TABLE raw_messages (
    id uuid PRIMARY KEY,
    source text NOT NULL,
    channel_id text NOT NULL,
    source_message_id text NOT NULL,
    source_version text NOT NULL,
    source_received_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    author_id text,
    content_hash text NOT NULL,
    message_text text,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_raw_messages_source_identity UNIQUE (source, channel_id, source_message_id, source_version)
);

COMMENT ON TABLE raw_messages IS 'Canonical immutable ingest record for source messages. Raw message content is stored separately from Hermes semantics and risk approvals.';
COMMENT ON COLUMN raw_messages.source_received_at IS 'Insert-only source timestamp. Updates are rejected by trg_raw_messages_source_received_at_insert_only.';

CREATE INDEX idx_raw_messages_channel_received ON raw_messages (channel_id, source_received_at);
CREATE INDEX idx_raw_messages_content_hash ON raw_messages (content_hash);

CREATE OR REPLACE FUNCTION prevent_raw_messages_source_received_at_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.source_received_at IS DISTINCT FROM OLD.source_received_at THEN
        RAISE EXCEPTION 'raw_messages.source_received_at is insert-only';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_raw_messages_source_received_at_insert_only
BEFORE UPDATE OF source_received_at ON raw_messages
FOR EACH ROW
EXECUTE FUNCTION prevent_raw_messages_source_received_at_update();

CREATE TABLE media_assets (
    asset_id uuid PRIMARY KEY,
    raw_message_id uuid NOT NULL REFERENCES raw_messages(id) ON DELETE CASCADE,
    sha256 text NOT NULL,
    object_key text,
    mime text,
    width integer CHECK (width IS NULL OR width > 0),
    height integer CHECK (height IS NULL OR height > 0),
    download_status text NOT NULL DEFAULT 'pending',
    downloaded_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_media_assets_download_status CHECK (download_status IN ('pending', 'downloaded', 'failed', 'skipped')),
    CONSTRAINT uq_media_assets_raw_sha256 UNIQUE (raw_message_id, sha256)
);

CREATE INDEX idx_media_assets_raw_message_id ON media_assets (raw_message_id);
CREATE INDEX idx_media_assets_sha256 ON media_assets (sha256);

CREATE TABLE message_processing_runs (
    processing_run_id uuid PRIMARY KEY,
    raw_message_id uuid NOT NULL REFERENCES raw_messages(id),
    worker_id text,
    status text NOT NULL,
    model_version text,
    prompt_version text,
    context_version text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_message_processing_runs_status CHECK (status IN ('started', 'succeeded', 'failed', 'skipped')),
    CONSTRAINT ck_message_processing_runs_time_order CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX idx_message_processing_runs_raw_message_id ON message_processing_runs (raw_message_id);
CREATE INDEX idx_message_processing_runs_status ON message_processing_runs (status);

CREATE TABLE context_snapshots (
    context_snapshot_id uuid PRIMARY KEY,
    raw_message_id uuid REFERENCES raw_messages(id),
    snapshot_type text NOT NULL,
    context_version text NOT NULL,
    snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_context_snapshots_raw_message_id ON context_snapshots (raw_message_id);

CREATE TABLE hermes_decisions (
    decision_id uuid PRIMARY KEY,
    raw_message_id uuid NOT NULL REFERENCES raw_messages(id),
    processing_run_id uuid NOT NULL REFERENCES message_processing_runs(processing_run_id),
    context_snapshot_id uuid NOT NULL REFERENCES context_snapshots(context_snapshot_id),
    schema_version text NOT NULL DEFAULT '1.0',
    message_type message_type_v1 NOT NULL,
    action hermes_action_v1 NOT NULL,
    ambiguous boolean NOT NULL,
    ambiguity_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    account_scope account_scope_v1 NOT NULL,
    target_account_id text,
    target_position_id text,
    instrument_symbol text,
    side position_side_v1,
    entry_type entry_type_v1 NOT NULL,
    entry_price numeric,
    entry_price_min numeric,
    entry_price_max numeric,
    stop_loss numeric,
    take_profits jsonb NOT NULL DEFAULT '[]'::jsonb,
    leverage numeric,
    valid_until timestamptz,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    model_provider text NOT NULL DEFAULT 'hermes',
    model_version text NOT NULL,
    prompt_version text NOT NULL,
    context_version text NOT NULL,
    temperature numeric NOT NULL,
    confidence numeric,
    created_at timestamptz NOT NULL,
    CONSTRAINT ck_hermes_decisions_schema_version CHECK (schema_version = '1.0'),
    CONSTRAINT ck_hermes_decisions_model_provider CHECK (model_provider = 'hermes'),
    CONSTRAINT ck_hermes_decisions_confidence CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    CONSTRAINT ck_hermes_decisions_take_profits_array CHECK (jsonb_typeof(take_profits) = 'array'),
    CONSTRAINT ck_hermes_decisions_evidence_array CHECK (jsonb_typeof(evidence) = 'array')
);

COMMENT ON TABLE hermes_decisions IS 'Semantic judgment from Hermes. Kept separate from raw_messages and risk approval decisions.';

CREATE INDEX idx_hermes_decisions_raw_message_id ON hermes_decisions (raw_message_id);
CREATE INDEX idx_hermes_decisions_processing_run_id ON hermes_decisions (processing_run_id);
CREATE INDEX idx_hermes_decisions_context_snapshot_id ON hermes_decisions (context_snapshot_id);
CREATE INDEX idx_hermes_decisions_message_action ON hermes_decisions (message_type, action);

CREATE TABLE risk_decisions (
    risk_decision_id uuid PRIMARY KEY,
    hermes_decision_id uuid NOT NULL REFERENCES hermes_decisions(decision_id),
    status risk_decision_status NOT NULL,
    account_id text NOT NULL,
    instrument_id text,
    risk_budget jsonb NOT NULL DEFAULT '{}'::jsonb,
    checks jsonb NOT NULL DEFAULT '[]'::jsonb,
    reason text,
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_risk_decisions_checks_array CHECK (jsonb_typeof(checks) = 'array')
);

COMMENT ON TABLE risk_decisions IS 'Risk approval or rejection for a Hermes decision. Approval is never stored on raw_messages.';

CREATE INDEX idx_risk_decisions_hermes_decision_id ON risk_decisions (hermes_decision_id);
CREATE INDEX idx_risk_decisions_account_id ON risk_decisions (account_id);
CREATE INDEX idx_risk_decisions_status ON risk_decisions (status);

CREATE TABLE trade_intents (
    intent_id uuid PRIMARY KEY,
    hermes_decision_id uuid NOT NULL REFERENCES hermes_decisions(decision_id),
    risk_decision_id uuid NOT NULL REFERENCES risk_decisions(risk_decision_id),
    schema_version text NOT NULL DEFAULT '1.0',
    account_id text NOT NULL,
    instrument_id text NOT NULL,
    action approved_trade_action_v1 NOT NULL,
    status trade_intent_status NOT NULL DEFAULT 'draft',
    order_plan jsonb NOT NULL DEFAULT '{}'::jsonb,
    risk_budget jsonb NOT NULL DEFAULT '{}'::jsonb,
    target_position_id text,
    valid_until timestamptz NOT NULL,
    idempotency_key text NOT NULL,
    approved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_trade_intents_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT ck_trade_intents_schema_version CHECK (schema_version = '1.0'),
    CONSTRAINT ck_trade_intents_idempotency_key_hex CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_trade_intents_approved_chain CHECK (
        status <> 'approved'
        OR (
            hermes_decision_id IS NOT NULL
            AND risk_decision_id IS NOT NULL
            AND approved_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_trade_intents_time_order CHECK (updated_at >= created_at)
);

COMMENT ON TABLE trade_intents IS 'Approved or pending trade intent. Approved rows require both Hermes and risk decision FKs.';

CREATE INDEX idx_trade_intents_hermes_decision_id ON trade_intents (hermes_decision_id);
CREATE INDEX idx_trade_intents_risk_decision_id ON trade_intents (risk_decision_id);
CREATE INDEX idx_trade_intents_account_id ON trade_intents (account_id);
CREATE INDEX idx_trade_intents_status ON trade_intents (status);

CREATE TABLE execution_commands (
    command_id uuid PRIMARY KEY,
    intent_id uuid NOT NULL REFERENCES trade_intents(intent_id),
    command_type text NOT NULL,
    status execution_command_status NOT NULL DEFAULT 'pending',
    idempotency_key text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    acknowledged_at timestamptz,
    error text,
    CONSTRAINT uq_execution_commands_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX idx_execution_commands_intent_id ON execution_commands (intent_id);
CREATE INDEX idx_execution_commands_status ON execution_commands (status);

CREATE TABLE execution_events (
    execution_event_row_id uuid PRIMARY KEY,
    event_id text NOT NULL,
    schema_version text NOT NULL DEFAULT '1.0',
    node_id text NOT NULL,
    account_id text NOT NULL,
    intent_id uuid REFERENCES trade_intents(intent_id),
    client_order_id text,
    venue_order_id text,
    trade_id text,
    event_type text NOT NULL,
    ts_event timestamptz NOT NULL,
    ts_ingest timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_execution_events_event_id UNIQUE (event_id),
    CONSTRAINT ck_execution_events_schema_version CHECK (schema_version = '1.0')
);

CREATE INDEX idx_execution_events_account_id ON execution_events (account_id);
CREATE INDEX idx_execution_events_intent_id ON execution_events (intent_id);
CREATE INDEX idx_execution_events_ts_event ON execution_events (ts_event);

CREATE TABLE orders_projection (
    order_projection_id uuid PRIMARY KEY,
    account_id text NOT NULL,
    instrument_id text NOT NULL,
    intent_id uuid REFERENCES trade_intents(intent_id),
    client_order_id text,
    venue_order_id text,
    status text NOT NULL,
    side position_side_v1,
    order_type text,
    quantity numeric,
    filled_quantity numeric NOT NULL DEFAULT 0,
    price numeric,
    average_fill_price numeric,
    updated_from_event_id text REFERENCES execution_events(event_id),
    ts_event timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_orders_projection_client_order UNIQUE (account_id, client_order_id),
    CONSTRAINT ck_orders_projection_quantity CHECK (quantity IS NULL OR quantity >= 0),
    CONSTRAINT ck_orders_projection_filled_quantity CHECK (filled_quantity >= 0)
);

CREATE INDEX idx_orders_projection_account_id ON orders_projection (account_id);
CREATE INDEX idx_orders_projection_intent_id ON orders_projection (intent_id);
CREATE INDEX idx_orders_projection_venue_order_id ON orders_projection (venue_order_id);

CREATE TABLE positions_projection (
    account_id text NOT NULL,
    position_id text NOT NULL,
    instrument_id text NOT NULL,
    side position_side_v1 NOT NULL,
    quantity numeric NOT NULL,
    avg_entry_price numeric,
    mark_price numeric,
    unrealized_pnl numeric,
    status text NOT NULL,
    updated_from_event_id text REFERENCES execution_events(event_id),
    ts_event timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (account_id, position_id),
    CONSTRAINT ck_positions_projection_quantity CHECK (quantity >= 0)
);

CREATE INDEX idx_positions_projection_account_id ON positions_projection (account_id);
CREATE INDEX idx_positions_projection_instrument_id ON positions_projection (instrument_id);

CREATE TABLE accounts_projection (
    account_id text PRIMARY KEY,
    currency text NOT NULL,
    equity numeric NOT NULL,
    margin numeric NOT NULL,
    available_balance numeric,
    reconciliation_state reconciliation_state_v1 NOT NULL DEFAULT 'healthy',
    last_execution_event_at timestamptz,
    projection_lag_ms bigint NOT NULL DEFAULT 0,
    updated_from_event_id text REFERENCES execution_events(event_id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_accounts_projection_equity CHECK (equity >= 0),
    CONSTRAINT ck_accounts_projection_margin CHECK (margin >= 0),
    CONSTRAINT ck_accounts_projection_available_balance CHECK (available_balance IS NULL OR available_balance >= 0),
    CONSTRAINT ck_accounts_projection_lag CHECK (projection_lag_ms >= 0)
);

CREATE INDEX idx_accounts_projection_account_id ON accounts_projection (account_id);
CREATE INDEX idx_accounts_projection_reconciliation_state ON accounts_projection (reconciliation_state);

COMMENT ON TABLE execution_events IS 'Write-only to the nautilus_projection_writer role; application reads projections as PostgreSQL source of truth.';
COMMENT ON TABLE orders_projection IS 'Execution projection table. Only the Nautilus node/projection consumer role may write.';
COMMENT ON TABLE positions_projection IS 'Execution projection table. Only the Nautilus node/projection consumer role may write.';
COMMENT ON TABLE accounts_projection IS 'Execution projection table. Only the Nautilus node/projection consumer role may write.';

CREATE TABLE risk_state (
    risk_state_id uuid PRIMARY KEY,
    account_id text NOT NULL,
    instrument_id text NOT NULL,
    exposure_notional numeric NOT NULL DEFAULT 0,
    open_risk_fraction numeric NOT NULL DEFAULT 0,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_from_event_id text REFERENCES execution_events(event_id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_risk_state_account_instrument UNIQUE (account_id, instrument_id),
    CONSTRAINT ck_risk_state_exposure CHECK (exposure_notional >= 0),
    CONSTRAINT ck_risk_state_open_risk_fraction CHECK (open_risk_fraction >= 0)
);

CREATE INDEX idx_risk_state_account_id ON risk_state (account_id);

CREATE TABLE node_heartbeats (
    node_id text PRIMARY KEY,
    account_id text,
    status text NOT NULL,
    version text,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_node_heartbeats_capabilities_array CHECK (jsonb_typeof(capabilities) = 'array')
);

CREATE INDEX idx_node_heartbeats_last_seen_at ON node_heartbeats (last_seen_at);

CREATE TABLE audit_events (
    audit_event_id uuid PRIMARY KEY,
    event_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    actor text,
    trace_id uuid,
    raw_message_id uuid REFERENCES raw_messages(id),
    hermes_decision_id uuid REFERENCES hermes_decisions(decision_id),
    risk_decision_id uuid REFERENCES risk_decisions(risk_decision_id),
    intent_id uuid REFERENCES trade_intents(intent_id),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_events_aggregate ON audit_events (aggregate_type, aggregate_id);
CREATE INDEX idx_audit_events_trace_id ON audit_events (trace_id);
CREATE INDEX idx_audit_events_created_at ON audit_events (created_at);

CREATE TABLE outbox_events (
    outbox_event_id uuid PRIMARY KEY,
    status outbox_status NOT NULL DEFAULT 'pending',
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    trace_id uuid,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    failed_at timestamptz,
    error text,
    CONSTRAINT ck_outbox_events_attempts CHECK (attempts >= 0)
);

COMMENT ON TABLE outbox_events IS 'Transactional outbox. Business writes and outbox rows must be committed in the same transaction.';

CREATE INDEX idx_outbox_events_status ON outbox_events (status);
CREATE INDEX idx_outbox_events_aggregate ON outbox_events (aggregate_type, aggregate_id);
CREATE INDEX idx_outbox_events_created_at ON outbox_events (created_at);

CREATE TABLE replay_runs (
    replay_run_id uuid PRIMARY KEY,
    requested_by text NOT NULL,
    reason text NOT NULL,
    source_range jsonb NOT NULL DEFAULT '{}'::jsonb,
    status replay_status NOT NULL DEFAULT 'pending',
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_replay_runs_time_order CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
);

CREATE INDEX idx_replay_runs_status ON replay_runs (status);

CREATE TABLE replay_results (
    replay_result_id uuid PRIMARY KEY,
    replay_run_id uuid NOT NULL REFERENCES replay_runs(replay_run_id) ON DELETE CASCADE,
    raw_message_id uuid REFERENCES raw_messages(id),
    hermes_decision_id uuid REFERENCES hermes_decisions(decision_id),
    risk_decision_id uuid REFERENCES risk_decisions(risk_decision_id),
    intent_id uuid REFERENCES trade_intents(intent_id),
    result_status text NOT NULL,
    diff jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_replay_results_status CHECK (result_status IN ('matched', 'changed', 'failed', 'skipped'))
);

CREATE INDEX idx_replay_results_replay_run_id ON replay_results (replay_run_id);
CREATE INDEX idx_replay_results_raw_message_id ON replay_results (raw_message_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nautilus_projection_writer') THEN
        CREATE ROLE nautilus_projection_writer NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO nautilus_projection_writer;
GRANT USAGE ON TYPE position_side_v1, reconciliation_state_v1 TO nautilus_projection_writer;
GRANT INSERT, UPDATE ON execution_events TO nautilus_projection_writer;
GRANT INSERT, UPDATE ON orders_projection TO nautilus_projection_writer;
GRANT INSERT, UPDATE ON positions_projection TO nautilus_projection_writer;
GRANT INSERT, UPDATE ON accounts_projection TO nautilus_projection_writer;
