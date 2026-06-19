# Window 1 Signal Core Notes

## Status

Window 1 signal core is ready for bridge review after W1 review approvals are recorded.

## Contracts

### Signal Store

- `SignalStore.upsert_signal(signal)` stores parsed signals by unique `signal_id`.
- `SignalStore.list_approved(since_minutes, current_time)` returns approved signals within the lookback window.
- `SignalStore.reserve_signal(signal_id, operation_type="entry")` is idempotent and moves approved entry signals to `reserved`.
- `SignalStore.transition_signal(signal_id, target_status, actor)` validates lifecycle transitions.
- `SignalStore.list_events(signal_id)` returns lifecycle events for audit.

### SignalStrategy

- Strategy path: `user_data/strategies/SignalStrategy.py`.
- Support package path: `user_data/strategies/signal_strategy_support/`.
- Required store injection: `config["signal_strategy"]["store"]`.
- `bot_loop_start(current_time)` loads approved signals from the store.
- `populate_entry_trend(dataframe, metadata)` reserves the signal, marks it `sent_to_freqtrade`, then emits `enter_long` or `enter_short`.
- `custom_entry_price` supports CMP, explicit primary price, midpoint, and proposed-rate fallback.
- `custom_stake_amount` sizes stake with fixed risk: `equity * risk_pct / stop_distance * entry_price / leverage`.
- `leverage` caps selected leverage by signal max, strategy max, and exchange max.
- `custom_stoploss` uses absolute signal stop loss through Freqtrade `stoploss_from_absolute`.
- `custom_exit` emits take-profit exits once per trade, signal, and TP level.
- `adjust_trade_position` emits DCA once per trade and DCA price.
- `order_filled` writes `entered` status and `trade_id` back to `SignalStore`.

## Router And API Notes

- Window 1 does not mount API routers directly.
- Bridge should expose store/import endpoints through the shared API app after bridge mode starts.
- Recommended bridge router responsibilities:
  - ingest parsed Telegram watcher signals,
  - list approved/current signals for dashboard,
  - receive Freqtrade order/status callbacks,
  - expose lifecycle/audit events.

## Environment Notes

- Freqtrade strategy needs access to a signal store object or future `HERMES_SIGNAL_STORE_URL` adapter.
- Existing remote bridge hook already references `HERMES_SIGNAL_STORE_URL`.
- `SIGNAL_IMPORTER_REMOTE_HOST`, `SIGNAL_IMPORTER_REMOTE_CWD`, and `SIGNAL_IMPORTER_SSH_IPV6=1` remain bridge-level wiring.

## Migration Notes

- Current store is in-memory and test-focused.
- Bridge should add durable persistence before production use.
- Lifecycle states already match `tasklist.json` shared signal status machine.
- The current tests use fake DataFrame objects locally because this workspace venv lacks pandas; real Freqtrade runtime should use pandas DataFrames.

## Verification

- `PYTHONPATH=apps/api .venv/bin/pytest tests/unit/test_signal_parser.py -q`
- `PYTHONPATH=apps/api .venv/bin/pytest tests/unit/test_signal_store.py -q`
- `PYTHONPATH=.:apps/api:user_data/strategies .venv/bin/pytest tests/unit/test_signalstrategy_loading.py tests/unit/test_signalstrategy_core.py tests/unit/test_signalstrategy_exits.py -q`
- `PYTHONPATH=.:apps/api:user_data/strategies .venv/bin/pytest tests/integration/test_signal_strategy_dryrun.py -q`
- `PYTHONPATH=.:apps/api:user_data/strategies .venv/bin/pytest tests/unit/test_signal_parser.py tests/unit/test_signal_store.py tests/unit/test_signalstrategy_loading.py tests/unit/test_signalstrategy_core.py tests/unit/test_signalstrategy_exits.py tests/integration/test_signal_strategy_dryrun.py -q`
