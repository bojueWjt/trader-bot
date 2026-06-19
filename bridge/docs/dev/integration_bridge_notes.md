# Integration Bridge Notes

## 2026-05-31T21:20:00+08:00 Read-Only Findings

Bridge mode is not active. Window 1, 2, and 3 tasks are still todo in `tasklist.json`.

### Hermes And Telegram Watcher

- `ssh -o AddressFamily=inet6 balen@balen.wang` resolves to a reachable Darwin host with the expected Hermes and Telegram watcher paths.
- Hermes gateway, Hermes trader gateway, and OpenClaw gateway LaunchAgents are loaded and running.
- PM2 service `telegram-watcher` is online.
- `telegram-watcher` has a dirty working tree; modified files include `ecosystem.config.js`, `package.json`, `price-monitor.js`, and `server.js`.
- Telegram watcher already calls `importSignalToFreqtrade(entry)` before `forwardToTrader(entry)`.
- Existing bridge hook: `/Users/balen/git/telegram-watcher/lib/signal-importer.js`.
- Current importer defaults point to `/Users/balen/git/freqtrade`, which is absent on the Hermes host.
- Current importer default leaves `SIGNAL_IMPORTER_ENABLED=0`.

Recommended watcher bridge path:

1. Point `SIGNAL_IMPORTER_REMOTE_HOST` at the Freqtrade server.
2. Point `SIGNAL_IMPORTER_REMOTE_CWD` at the deployed repo path containing `freqtrade.signal_strategy.importer`.
3. Set `SIGNAL_IMPORTER_SSH_IPV6=1`.
4. Set `HERMES_SIGNAL_STORE_URL` to the SignalStrategy SQLite store.
5. Enable `SIGNAL_IMPORTER_ENABLED=1`.
6. Restart PM2 `telegram-watcher` after env validation.

### Freqtrade Server

- Server IPv6 `2001:df1:7880:2::18dc` is reachable over SSH.
- Host: Debian 13, amd64, 4 vCPU, 7.8 GiB RAM, 77 GiB free disk.
- Docker and Docker Compose are installed and active.
- Existing compose stacks:
  - `/opt/freqtrade/docker-compose.yml`
  - `/root/freqtrade/docker/docker-compose-signal-dashboard.yml`
- Existing loopback services:
  - `freqtrade`: `127.0.0.1:8080->8080`
  - `docker-freqtrade-dryrun-1`: `127.0.0.1:18081->8080`
  - `docker-frontend-1`: `127.0.0.1:13000->3000`
- Both Freqtrade APIs responded to `/api/v1/ping` with `{"status":"pong"}`.
- `docker-freqtrade-dryrun-1` runs `SignalStrategy` in dry-run mode and binds its API only to loopback.

Recommended deployment guardrails:

1. Preserve existing `/opt/freqtrade` and `/root/freqtrade` services.
2. Use `/srv/freqtrade-prod` for a new production-grade deployment if needed.
3. Avoid occupied ports `8080`, `18081`, and `13000`.
4. Use `127.0.0.1:18082:8080` for any additional Freqtrade API.
5. Keep external access behind SSH tunnel, VPN, or explicit Caddy policy.

### Local Freqtrade Design

- Upstream compose already uses `127.0.0.1:8080:8080`.
- Docker config should set `api_server.listen_ip_address` to `0.0.0.0` inside the container while Docker publishes only loopback.
- Custom strategy path: `user_data/strategies/SignalStrategy.py`.
- Signal support package path: `user_data/strategies/signal_strategy_support/`.
- The sample zip `/Users/balen/Downloads/real-trading-signals-20260531-051540.zip` contains 6 real signals and can seed parser/importer tests.

### Current Blocking State

- `tasklist.json` is not bridge-ready.
- Windows 1, 2, and 3 need to complete their tasks and attach evidence.
- Bridge work should begin after handoff tasks `W1-HANDOFF-001`, `W2-HANDOFF-001`, and `W3-HANDOFF-001` are done.
