# Remote Freqtrade Deployment Status

Checked at `2026-05-31T23:36:19+08:00`.

## Server

- Host: `qctbhy0a32hucub`
- IPv6: `2001:df1:7880:2::18dc`
- OS: Debian GNU/Linux 13
- Docker: installed and active
- SSH hardening: `PasswordAuthentication no`, `PermitRootLogin without-password`, `PubkeyAuthentication yes`

## Running Services

```text
freqtrade                    freqtradeorg/freqtrade:stable      127.0.0.1:8080->8080
docker-freqtrade-dryrun-1    hermes-freqtrade-signal:local      127.0.0.1:18081->8080
docker-frontend-1            node:22-alpine                     127.0.0.1:13000->3000
```

All HTTP ports are bound to `127.0.0.1`.

## Compose

- Compose file: `/root/freqtrade/docker/docker-compose-signal-dashboard.yml`
- Runtime env file: `/root/freqtrade/docker/.env`
- `.env` mode: `0600`
- Services visible through compose: `freqtrade-dryrun`, `frontend`
- `docker compose -f docker-compose-signal-dashboard.yml ps` works from `/root/freqtrade/docker`

## Verification

Audit markers: `remote 49 tests passed`, `dry-run API pong`, `dashboard 200`, `system docs 200`, `watcher importer wrote Signal Store`, `ssh -6 balen@balen.wang ok`.

```bash
curl http://127.0.0.1:18081/api/v1/ping
# {"status":"pong"}

curl -I http://127.0.0.1:13000
# HTTP/1.1 200 OK

curl -I http://127.0.0.1:13000/system/hermes-trader-system.html
# HTTP/1.1 200 OK

docker exec docker-frontend-1 sh -lc "cd /app/apps/dashboard && npm test"
# 1 test file passed, 9 tests passed
```

Window 3 risk/security gate tests ran on the remote host with an isolated host virtualenv at `/root/freqtrade/.w3-venv`:

```bash
PYTHONPATH=/root/freqtrade/apps/api .w3-venv/bin/python -m pytest -o addopts="" --confcutdir=/root/freqtrade/tests/unit tests/unit/test_risk_governor.py tests/unit/test_audit_events.py tests/unit/test_permissions.py -q
# 26 passed

PYTHONPATH=/root/freqtrade/apps/api .w3-venv/bin/python -m pytest -o addopts="" --confcutdir=/root/freqtrade/tests/security tests/security -q
# 4 passed

PYTHONPATH=/root/freqtrade/apps/api .w3-venv/bin/python -m pytest -o addopts="" --confcutdir=/root/freqtrade/tests/e2e_api tests/e2e_api/test_risk_api.py tests/e2e_api/test_release_gates.py -q
# 19 passed
```

Remote post-test health:

```bash
curl -fsS http://127.0.0.1:18081/api/v1/ping
# {"status":"pong"}

curl -fsS -o /dev/null -w "dashboard_http=%{http_code}\n" http://127.0.0.1:13000
# dashboard_http=200
```

Latest live health check:

```bash
ssh -o AddressFamily=inet6 root@2001:df1:7880:2::18dc \
  'curl -fsS http://127.0.0.1:18081/api/v1/ping && curl -fsS -o /dev/null -w "dashboard_http=%{http_code}\n" http://127.0.0.1:13000'
# {"status":"pong"}
# dashboard_http=200
```

Real signal sample dry-run boundary check:

```bash
python3 -m freqtrade.signal_strategy.importer \
  /root/freqtrade/user_data/real-trading-signals-20260531-051540.zip \
  --store-url sqlite:////tmp/codex-real-signal-sample-approved.sqlite3 \
  --pair-whitelist BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,LA/USDT:USDT,TAO/USDT:USDT,ZKP/USDT:USDT,LTC/USDT:USDT \
  --refresh-window \
  --approve-parsed
# {"approved": 0, "needs_review": 6, "rejected": 0, "total": 6}
```

The six packaged real signals import successfully into a temporary SQLite store and all remain under manual review when approval is requested. Review reasons observed: `cmp_current_price_missing`, `tp_price_missing_from_text`, and `short_price_geometry_invalid`.

Host network check:

```text
127.0.0.1:8080   official freqtrade container
127.0.0.1:18081  Hermes SignalStrategy dry-run container
127.0.0.1:13000  Hermes dashboard frontend
0.0.0.0:22       SSH
*:80, *:443      Caddy
```

`freqtrade show-config` inside `docker-freqtrade-dryrun-1` validates the dry-run config and resolves:

- bot: `hermes-signal-dryrun`
- strategy: `SignalStrategy`
- dry_run: `true`
- trading_mode: `futures`
- margin_mode: `isolated`
- pairs: `BTC/USDT:USDT`, `ETH/USDT:USDT`, `SOL/USDT:USDT`
- Freqtrade API listen address inside container: `0.0.0.0:8080`
- Host bind: `127.0.0.1:18081`

## Runtime Data

- Signal store: `/root/freqtrade/user_data/signal_strategy.sqlite3`
- Trade DB: `/root/freqtrade/user_data/tradesv3.signal_strategy.dryrun.sqlite`
- Signal store contains 7 signals after the Telegram watcher importer smoke and 3 reserved entry operations.
- Dry-run trade DB contains 1 open simulated trade with `enter_tag` beginning `sig:codex-smoke`.

Latest Telegram watcher bridge smoke entry:

```text
signal_id: codex-smoke:1780241183518
status: needs_review
pair: BTC/USDT:USDT
importer summary: {"approved":0,"needs_review":1,"rejected":0,"total":1}
```

## Notes

- The dry-run container image lacks `pytest`; Window 3 remote validation uses `/root/freqtrade/.w3-venv`.
- Host package `python3.13-venv` was installed to create the validation virtualenv.
- `freqtrade list-strategies` reports duplicate `SignalStrategy` names because the user strategy wraps `freqtrade.signal_strategy.strategy.SignalStrategy`; runtime resolution still loads `/freqtrade/user_data/strategies/SignalStrategy.py`.
- The official `/opt/freqtrade` stable container is also running on `127.0.0.1:8080` and is separate from the Hermes signal dry-run stack.
- Caddy currently returns a static 404 for `hk-bot.balen.wang`: `Freqtrade API is available through SSH tunnel only.`
- Caddy currently returns a static health response for `hk.balen.wang`.
- Binance HTTPS connectivity from the server works for `testnet.binancefuture.com/fapi/v1/time` and `fapi.binance.com/fapi/v1/time`.
