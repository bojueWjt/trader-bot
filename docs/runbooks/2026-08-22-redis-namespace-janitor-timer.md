# Redis Namespace Janitor Systemd Timer

The scheduled janitor reads fresh A-D lease metadata directly from Redis. Every
run protects each account's current `persistence_namespace` and only unlinks
idle UUID generations that are absent from the live lease set.

Create `/etc/trader-v3/redis-janitor.env` with the production `REDIS_URL`, then
install and enable the repository-owned units:

```bash
sudo install -m 0640 /dev/null /etc/trader-v3/redis-janitor.env
sudoedit /etc/trader-v3/redis-janitor.env
sudo install -m 0644 \
  infra/systemd/trader-v3-redis-namespace-janitor.service \
  /etc/systemd/system/trader-v3-redis-namespace-janitor.service
sudo install -m 0644 \
  infra/systemd/trader-v3-redis-namespace-janitor.timer \
  /etc/systemd/system/trader-v3-redis-namespace-janitor.timer
sudo systemctl daemon-reload
sudo systemctl enable --now trader-v3-redis-namespace-janitor.timer
```

Run and inspect one sweep before relying on the timer:

```bash
sudo systemctl start trader-v3-redis-namespace-janitor.service
systemctl --no-pager --full status \
  trader-v3-redis-namespace-janitor.service
systemctl list-timers trader-v3-redis-namespace-janitor.timer --no-pager
tail -n 100 /var/log/trader-v3/redis-janitor.log
```

The command fails closed before deletion when any account lease is missing,
stale, or lacks generation metadata. The Lua unlink boundary repeats the live
lease check atomically, and each completed batch validates the four live
generations again.
