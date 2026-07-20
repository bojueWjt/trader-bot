-- repository.upsert_order_projection (patched 2026-07-07) writes trigger_price and
-- reduce_only, but the columns were never added; every order upsert failed with
-- UndefinedColumn inside the ingest SAVEPOINT and orders_projection silently
-- stopped updating on 2026-07-10 (incident: node-a HALTED + 15 orders missing
-- from projection, diagnosed 2026-07-18).
ALTER TABLE orders_projection ADD COLUMN IF NOT EXISTS trigger_price numeric;
ALTER TABLE orders_projection ADD COLUMN IF NOT EXISTS reduce_only boolean;
