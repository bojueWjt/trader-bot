CREATE TABLE IF NOT EXISTS trade_outcomes (
    outcome_id uuid PRIMARY KEY,
    intent_id uuid NOT NULL REFERENCES trade_intents(intent_id),
    account_id text NOT NULL,
    instrument_id text,
    side text,
    entry_avg_price numeric,
    exit_avg_price numeric,
    filled_quantity numeric,
    realized_pnl numeric,
    fees numeric,
    initial_risk numeric,
    r_multiple numeric,
    mae numeric,
    mfe numeric,
    mae_price numeric,
    mfe_price numeric,
    holding_seconds bigint,
    first_fill_at timestamptz,
    closed_at timestamptz,
    kline_source text,
    computed_at timestamptz NOT NULL DEFAULT now(),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_trade_outcomes_intent_account UNIQUE (intent_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_trade_outcomes_account_closed_at
    ON trade_outcomes (account_id, closed_at);
