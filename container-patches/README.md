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

### Optional minimum opening leverage (USDT-M)

Set `BINANCE_MIN_OPEN_LEVERAGE=50` only in account-c/account-d's recreate
environment. Missing or empty values disable the feature; invalid or non-positive
integers log an error at startup and disable it without preventing startup.
The variable is read once per client construction. This patch is self-contained
in the mounted futures execution file; no additional repository modules are needed.

Before the common `_submit_order_inner` path (shared by single orders and order
lists), opening BUY/LONG or SELL/SHORT orders ensure the minimum. One-way orders
(`None` or the adapter's `BOTH`) use `not is_reduce_only`. Closing orders and all
SL/TP/trailing algo types skip this check. The common submission event sequence
remains unchanged.

The client's MarginAccount leverage cache avoids requests when leverage already
meets the minimum. Otherwise a signed GET `/fapi/v1/leverageBracket?symbol=...`
uses the existing HTTP client's signing, timestamp, configured `recvWindow`,
and global rate limiter. The first bracket's `initialLeverage` caps the target:
`min(configured minimum, exchange cap)`. Leverage only increases. Caps are cached
per symbol, and a per-symbol asyncio lock prevents duplicate concurrent changes.
Successful changes update MarginAccount; `ACCOUNT_CONFIG_UPDATE` events with
`ac` also update it, including external leverage changes. Unknown instruments
are ignored; events without `ac` keep their existing log-only behavior.

Bracket or leverage-change failures log a warning and let the original order
proceed. Further checks for that symbol skip HTTP for five minutes in the same
process. This is best effort: opening can proceed below the minimum on failure.

Behavior verification requires Python 3.12+ and `nautilus_trader==1.227.0` with
its normal dependencies. Keep the repository layout (`container-patches/` and
`tests/nautilus/runtime/`) when copying tests into the image. No network, API keys,
pytest, or pytest-asyncio are required for the new test:

```sh
python -m unittest discover -s tests/nautilus/runtime -p test_futures_min_leverage.py -v
```

Without Nautilus, behavior tests skip; the AST/source checks still run. With
pytest available, run both requested files together:

```sh
python -m pytest tests/nautilus/runtime/test_binance_adapter_config.py tests/nautilus/runtime/test_futures_min_leverage.py -q
```
