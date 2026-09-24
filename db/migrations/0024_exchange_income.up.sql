-- Read-only Binance USDT-M income history. Authority for daily REALIZED_PNL /
-- COMMISSION / FUNDING_FEE. Not Nautilus Position.realized_pnl.
-- Binance tranId is unique per account incomeType.
CREATE TABLE IF NOT EXISTS exchange_income (
    account_id text NOT NULL,
    tran_id text NOT NULL,
    income_type text NOT NULL,
    symbol text NOT NULL DEFAULT '',
    income numeric NOT NULL,
    asset text NOT NULL,
    info text,
    trade_id text,
    income_time timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, income_type, tran_id)
);

CREATE INDEX IF NOT EXISTS idx_exchange_income_account_time
    ON exchange_income (account_id, income_time);

CREATE INDEX IF NOT EXISTS idx_exchange_income_type_time
    ON exchange_income (account_id, income_type, income_time);

-- Half-open [window_start, window_end). complete=true only after pagination
-- finished on a short last page for every chunk covering the window.
CREATE TABLE IF NOT EXISTS exchange_income_coverage (
    account_id text NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    income_type text NOT NULL DEFAULT '*',
    complete boolean NOT NULL,
    row_count bigint NOT NULL DEFAULT 0,
    collected_at timestamptz NOT NULL DEFAULT now(),
    error text,
    PRIMARY KEY (account_id, window_start, window_end, income_type),
    CONSTRAINT ck_exchange_income_coverage_window
        CHECK (window_end > window_start),
    CONSTRAINT ck_exchange_income_coverage_row_count
        CHECK (row_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_exchange_income_coverage_account_window
    ON exchange_income_coverage (account_id, window_start, window_end);

GRANT SELECT ON exchange_income, exchange_income_coverage
    TO trader_v3_operator_query;
GRANT SELECT ON exchange_income, exchange_income_coverage
    TO trader_v3_node_control;
GRANT SELECT ON exchange_income, exchange_income_coverage
    TO trader_v3_event_ingest;
