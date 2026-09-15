-- 0019: account equity samples (2026-09-05, backend-api contract §8, G0
-- decision). Feeds the app's 30-minute total-account-equity waveform.
--
-- Not an accounting table (D9): it has no relation to trade_outcomes,
-- orders_projection, positions_projection, execution_events, or
-- exchange_state_mirror, and nothing here replays into the trading path.
-- exchange_state_recorder upserts one row per account per 30-minute bucket
-- after every successful exchange_state_mirror write, keeping only the
-- latest reading observed in that bucket. There is no backfill: the series
-- starts accumulating from the first deploy of this migration.
CREATE TABLE account_equity_samples (
    account_id text NOT NULL,
    bucket_at timestamptz NOT NULL,
    equity numeric NOT NULL,
    available numeric NOT NULL,
    margin numeric,
    sampled_at timestamptz NOT NULL,
    PRIMARY KEY (account_id, bucket_at)
);

CREATE INDEX idx_account_equity_samples_bucket_at
    ON account_equity_samples (bucket_at);

-- Read side only: GET /v1/accounts?history_hours=.. runs under
-- trader_v3_operator_query, same role granted SELECT on every other
-- projection/mirror table (0012/0018). exchange_state_mirror itself has no
-- dedicated writer grant either (see 0007); the recorder's maintenance
-- credential is out of scope for the app role model.
GRANT SELECT ON account_equity_samples
    TO trader_v3_operator_query;
-- /v1/accounts is mounted on every control-plane role; grant the read to all
-- three so no role can hit a silent InsufficientPrivilege (2026-09-02 outbox lesson).
GRANT SELECT ON account_equity_samples
    TO trader_v3_node_control;
GRANT SELECT ON account_equity_samples
    TO trader_v3_event_ingest;
