-- Revert 0018: drop the projection reliability tables. Grants and indexes on
-- them are dropped with the tables; the P0-1 ingest grants live on pre-existing
-- tables and must be revoked explicitly.
REVOKE SELECT, INSERT ON order_events
    FROM trader_v3_event_ingest;
REVOKE SELECT, INSERT ON reconciliation_findings
    FROM trader_v3_event_ingest;
REVOKE INSERT ON reconciliation_runs
    FROM trader_v3_event_ingest;

DROP TABLE IF EXISTS projection_watermarks;
DROP TABLE IF EXISTS projection_failures;
