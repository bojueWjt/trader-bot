-- Revert 0019: drop the account equity sample table. Index, PK, and the
-- trader_v3_operator_query SELECT grant are all dropped with the table
-- (there is no grant on a surviving pre-existing table to revoke here,
-- unlike 0018's order_events/reconciliation_* revokes).
DROP TABLE IF EXISTS account_equity_samples;
