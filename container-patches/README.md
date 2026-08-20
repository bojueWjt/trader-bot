# container-patches

Host files bind-mounted over the node container image on hk
(`/srv/trader-v3/container-patches/` → mounts in
`recreate-trader-v3-node-{a,b}.sh`). Source of truth for the mounted
strategy/projection files is `services/nautilus-node/` — copies here must stay
in sync.

## binance_execution.py

Patched copy of nautilus_trader 1.227.0
`adapters/binance/execution.py` (image path
`/usr/local/lib/python3.12/site-packages/nautilus_trader/adapters/binance/execution.py`).

Three upstream behaviors required local hardening:

Two bugs made every node restart forget all pre-restart resting
orders (2026-07-10 incident, see
`docs/incidents/2026-07-10-node-order-amnesia.md`):

1. `_parse_order_status_reports` applied the reconciliation lookback window
   (default 60 min) to ALL orders — an OPEN resting order older than the
   window was silently dropped from startup reconciliation.
2. The `open_only=False` path discarded the `openOrders` snapshot and relied
   on per-symbol `allOrders`, which omits orders created beyond its ~7-day
   retention even when still open.

Patch: open orders (NEW / PARTIALLY_FILLED) bypass the time filter, and the
openOrders snapshot is merged into the report set (dedupe on symbol+orderId).

The 2026-08-13 projection stall investigation also found that a targeted
reconciliation command still expanded to every active symbol in the Nautilus
cache. Targeted order and fill queries now normalize the requested Nautilus
symbol through `BinanceSymbol` and keep the HTTP request set to that one Binance
symbol. `runtime/nautilus_reconciliation_scope.py` issues continuous and startup
reconciliation commands once per release-owned instrument.

Mount line added to both recreate scripts:

```
-v /srv/trader-v3/container-patches/binance_execution.py:/usr/local/lib/python3.12/site-packages/nautilus_trader/adapters/binance/execution.py:ro
```

(The recreate scripts themselves are not committed — they embed account API
keys.)

## binance_futures_execution.py

Patched copy of Nautilus Trader 1.227.0
`adapters/binance/futures/execution.py`. Its account bootstrap request used a
hard-coded 5000 ms receive window. The patch uses the configured
`recv_window_ms`, matching the common execution client and signed order paths.

Mount it read-only at:

```
/usr/local/lib/python3.12/site-packages/nautilus_trader/adapters/binance/futures/execution.py
```
