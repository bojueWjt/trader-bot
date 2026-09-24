# Exchange income collector

Read-only Binance USDT-M `/fapi/v1/income` into `exchange_income` /
`exchange_income_coverage`. Keys come from node container env/secret mounts
via `exchange_state_recorder.container_keys` and are never logged.

Default window matches `report_data.completed_utc_window`: last completed UTC
day `[today 00:00 - 1d, today 00:00)`. Daily report of "today so far" needs
`--start/--end` on both collector and report; incomplete today is 数据缺失.

```bash
# completed yesterday (default)
python3 services/control-plane/tools/exchange_income_collector.py --db-url "$DATABASE_URL" --once

# explicit half-open window, end <= now
python3 services/control-plane/tools/exchange_income_collector.py \
  --db-url "$DATABASE_URL" --once \
  --start 2026-09-23T00:00:00+00:00 --end 2026-09-23T19:00:00+00:00

python3 hermes-profile/scripts/report_data.py \
  --start 2026-09-23T00:00:00+00:00 --end 2026-09-23T19:00:00+00:00
```

Install the oneshot/timer next to other control-plane units. Do not run this
collector in development sessions that lack production keys.
