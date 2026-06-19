# Remote Hermes And Telegram Watcher Status

Checked at `2026-05-31T23:36:19+08:00`.

## SSH Entry

Audit markers: `ssh -6 balen@balen.wang ok`, `telegram-watcher 15 tests passed`, `Hermes trader cron HEALTH_OK`, `Hermes trader cron FALLBACK_HEALTH_OK`, `watcher importer smoke wrote remote Signal Store`, `dry-run importer signal present`.

- Required command: `ssh -6 balen@balen.wang`
- Current AAAA: `2409:8a55:3333:1ee1:cb3:a3dc:63d3:f147`
- Current status: `ssh -6 balen@balen.wang` succeeds
- Host: `dalongxia.local`

## Hermes Trader

Remote audit results:

- Host: `dalongxia.local`
- Hermes repo: `/Users/balen/.hermes/hermes-agent`
- Branch: `main`
- Commit: `648b89911`
- Version describe: `v2026.4.23-274-g648b89911`
- Worktree: clean
- Upstream state: behind `origin/main` by `2057` commits

Key config files exist:

- `/Users/balen/.hermes/config.yaml`
- `/Users/balen/.hermes/profiles/trader/config.yaml`

Trader profile uses isolated home:

```text
HERMES_HOME=/Users/balen/.hermes/profiles/trader
python -m hermes_cli.main --profile trader gateway run --replace
```

## Trader Skill

- Profile skill path exists: `/Users/balen/.hermes/profiles/trader/skills/openclaw-imports/crypto-trader`
- Contents include `SKILL.md`, `references/`, and `scripts/`
- Local `/Users/balen/Downloads/crypto-trader` and the remote profile skill have matching file hashes
- Remote `/Users/balen/Downloads/crypto-trader` has been restored from the local copy, excluding Python caches

## Hermes Trader Cron

Model/provider checks through the trader profile:

- `gpt-5.5` returned `OK`
- `gemini-2.5-pro` returned `OK`
- `qwen3.6-plus` returned `OK`
- `qwen3-max` returned HTTP `503`
- Trader default model is `qwen3.6-plus`
- Trader fallback model is `cliproxyapi/gemini-2.5-pro`
- `hermes config check` passed under `HERMES_HOME=/Users/balen/.hermes/profiles/trader`

Safe cron health check:

```text
Job ID: db69d33f79fd
Output: HEALTH_OK
Output file: /Users/balen/.hermes/profiles/trader/cron/output/db69d33f79fd/2026-05-31_23-15-28.md
```

Fallback cron health check:

```text
Job ID: fe0175429d04
Output: FALLBACK_HEALTH_OK
```

`hermes cron status` reports:

```text
Gateway is running — cron jobs will fire automatically
PID: 74365
No active jobs
```

## LaunchAgent Status

LaunchAgent files exist:

- `/Users/balen/Library/LaunchAgents/ai.hermes.gateway.plist`
- `/Users/balen/Library/LaunchAgents/ai.hermes.gateway-trader.plist`
- `/Users/balen/Library/LaunchAgents/ai.openclaw.gateway.plist`

Runtime status from `launchctl list`:

```text
ai.hermes.gateway          running, last exit code 1
ai.hermes.gateway-trader   running, last exit code 1
ai.openclaw.gateway        running, last exit code 0
```

The trader LaunchAgent has `HERMES_HOME=/Users/balen/.hermes/profiles/trader` and is running with PID `74365`. `last exit code = 1` reflects prior restart history; current state is running.

## Telegram Watcher

Remote path: `/Users/balen/git/telegram-watcher`

Git audit:

- Branch: `main`
- HEAD: `cc829df Harden Hermes trader watcher bridge`
- Worktree: clean
- Remote: `git@gitee.com:leon-wong/telegram-watcher.git`
- Origin `main` pushed to `cc829df`

Runtime:

- PM2 service: `telegram-watcher`
- Status: `online`
- Restart count during audit: `9`
- Current PID during audit: `73293`
- PM2 script: `/Users/balen/git/telegram-watcher/server.js`
- `messages.json`: 500 messages, about `343K`
- `media/`: about `1.1G`, 401 files
- API status: `configured=true`, `loggedIn=true`, `connected=true`
- Watch groups: `-1002198013097`, `-1002228497993`, `-1002328068747`
- Tests: `npm test` and `node --test __tests__/*.test.js` both passed `15` tests
- Packaged real sample `/Users/balen/Downloads/real-trading-signals-20260531-051540.zip` was copied to `/root/freqtrade/user_data/real-trading-signals-20260531-051540.zip` for isolated dry-run importer validation
- Real sample importer validation against `/tmp/codex-real-signal-sample-approved.sqlite3` returned `{"approved": 0, "needs_review": 6, "rejected": 0, "total": 6}`

## Freqtrade Importer Bridge

Effective PM2 environment after reload:

```text
HERMES_SIGNAL_STORE_URL=sqlite:////root/freqtrade/user_data/signal_strategy.sqlite3
SIGNAL_IMPORTER_ENABLED=1
SIGNAL_IMPORTER_MAX_IN_FLIGHT=1
SIGNAL_IMPORTER_REMOTE_HOST=root@149.104.30.223
SIGNAL_IMPORTER_REMOTE_CWD=/root/freqtrade
SIGNAL_IMPORTER_SSH_IPV6=0
SIGNAL_PAIR_WHITELIST=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,LA/USDT:USDT,TAO/USDT:USDT,ZKP/USDT:USDT,LTC/USDT:USDT
```

Reload command:

```bash
export PATH=/opt/homebrew/Cellar/node/25.6.1/bin:/opt/homebrew/lib/node_modules/pm2/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
cd /Users/balen/git/telegram-watcher
REMOTE_HOST=root@149.104.30.223 REMOTE_DIR=/root/freqtrade SIGNAL_IMPORTER_SSH_IPV6=0 SIGNAL_STORE_URL=sqlite:////root/freqtrade/user_data/signal_strategy.sqlite3 ./scripts/configure-remote-importer.sh
```

Connectivity evidence:

```text
Hermes -> Freqtrade IPv4 SSH: root@149.104.30.223 ok
Hermes -> Freqtrade IPv6 TCP port 22: open
Hermes -> Freqtrade IPv6 SSH handshake: server closes preauth
```

Watcher importer module smoke:

```text
[signal-importer] imported: {"approved":0,"needs_review":1,"rejected":0,"total":1}
in_flight=0
```

Remote Signal Store evidence:

```text
signals 7
latest signal_id: codex-smoke:1780241183518
latest status: needs_review
latest pair: BTC/USDT:USDT
```

Hermes trigger points:

- `server.js` forwards Telegram messages through `triggerHermesCron(...)`
- `price-monitor.js` triggers trader cron on price alerts
- `lib/hermes-cron.js` uses `hermes cron create` and `hermes cron run --accept-hooks`
- Default workdir: `/Users/balen/.openclaw/workspace-trader`

Observed risk:

- Older `price-monitor` logs include TLS connection failures to Binance endpoints
- Current `curl` and Node HTTPS checks to `testnet.binancefuture.com/fapi/v1/time` and `fapi.binance.com/fapi/v1/time` return HTTP `200`
- Telegram watcher had earlier `Not connected` and `TIMEOUT` logs; current API status is connected and PM2 logs show current updates being received
- `qwen3-max` still returns provider HTTP `503`; trader default and fallback now use healthy models

## Next Actions

1. Run the operator-approved kill-switch drill and testnet key review for the target deployment.
2. Run an operator-approved live Telegram message dry-run through Telegram watcher and Hermes trader.
3. Keep `qwen3-max` out of trader runtime until the provider stops returning HTTP `503`.
