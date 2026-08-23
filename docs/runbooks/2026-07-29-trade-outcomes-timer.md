# Trade Outcomes Systemd Timer

The HK host has no cron package, unit, or process. Install the repository-owned
systemd units after migration `0009_trade_outcome_job_runs` and the updated
`trade_outcomes.py` are deployed.

The timer runs hourly at minute 30 UTC (`OnCalendar=*-*-* *:30:00 UTC`). A
once-daily 00:30 UTC schedule left a blind zone: trades that closed after the
job and before the ~13:32 UTC daily report never appeared in any trailing-24h
window. The job is an idempotent upsert.

```bash
sudo install -m 0644 infra/systemd/trader-v3-trade-outcomes.service \
  /etc/systemd/system/trader-v3-trade-outcomes.service
sudo install -m 0644 infra/systemd/trader-v3-trade-outcomes.timer \
  /etc/systemd/system/trader-v3-trade-outcomes.timer
sudo systemctl daemon-reload
sudo systemctl enable --now trader-v3-trade-outcomes.timer
```

Run one foreground systemd job before relying on the timer:

```bash
sudo systemctl start trader-v3-trade-outcomes.service
systemctl --no-pager --full status trader-v3-trade-outcomes.service
systemctl list-timers trader-v3-trade-outcomes.timer --no-pager
tail -n 100 /var/log/trader-v3/trade-outcomes.log
```

Acceptance requires:

- `trade_outcome_job_runs.job_name='trade_outcomes'`
- `status='succeeded'`
- a fresh `completed_at` that covers the report window end (hourly :30 UTC)
- `/var/log/trader-v3/trade-outcomes-latest.json` updated by the systemd run
- JSON result includes `dropped_fill_count` and `deleted_stale_count`

The job rebuilds robot-owned episodes only (`client_order_id` matching
`^B[0-9a-f]{32}[0-9]{2}$`) and deletes `trade_outcomes` rows the new
caliber no longer emits. After deploying this change, run one foreground
job so mixed/manual residual ghosts are removed before the next daily
report.

Remove `/etc/cron.d/trader-v3-trade-outcomes` after the timer is verified so a
future cron installation cannot create a duplicate schedule.
