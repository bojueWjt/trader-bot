# Trade Outcomes Systemd Timer

The HK host has no cron package, unit, or process. Install the repository-owned
systemd units after migration `0009_trade_outcome_job_runs` and the updated
`trade_outcomes.py` are deployed.

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
- a fresh `completed_at`
- `/var/log/trader-v3/trade-outcomes-latest.json` updated by the systemd run

Remove `/etc/cron.d/trader-v3-trade-outcomes` after the timer is verified so a
future cron installation cannot create a duplicate schedule.
