-- Read-only mirror of real exchange state (positions, open orders, algo/conditional
-- orders). Populated by services/control-plane/tools/exchange_state_recorder.py.
-- Exists because orders_projection drifts when nodes miss events, and Binance
-- migrated conditional orders (STOP_MARKET/TAKE_PROFIT) to the algo-order system
-- (2025-12-09), invisible to /fapi/v1/openOrders and to the projections.
CREATE TABLE IF NOT EXISTS exchange_state_mirror (
  account_id text PRIMARY KEY,
  payload jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
