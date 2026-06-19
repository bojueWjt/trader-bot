# Hong Kong Whole-Host Deployment Runbook

## Prerequisites

- Host OS: Debian 13.
- Docker Engine 26.1.5 and Docker Compose plugin 2.26.1.
- Repository branch `bridge-import` is available from `origin`.
- Secrets directory exists on the host: `/srv/trader-secrets/`.
- Watcher config exists and is readable by the deploy user: `/srv/trader-secrets/telegram-watcher.config.json`.
- Optional alert variables can be set in compose or an env override when Telegram alerts are required:
  - `WATCHER_ALERT_BOT_TOKEN`
  - `WATCHER_ALERT_CHAT_ID`
- Caddy domain is set with `CADDY_DOMAIN`, or defaults to `localhost`.
- The deploy user can run `docker`, `docker compose`, `git`, and `curl`.

Secrets are mounted or injected at runtime. They must not be copied into images or committed.

## Deploy

Run from the repository root on the Hong Kong host:

```bash
scripts/deploy_hk.sh
```

The script:

- Fetches and checks out `bridge-import`.
- Builds compose services with `--pull`.
- Removes old containers only when their names exactly match:
  - `docker-frontend-1`
  - `docker-freqtrade-dryrun-1`
  - `freqtrade`
- Starts the M4-01 compose stack.
- Checks:
  - API: `http://localhost:8000/healthz`
  - Dashboard: `http://localhost:3000`
  - Freqtrade dry-run: `http://localhost:18081/api/v1/ping`
  - Watcher: `http://localhost:9090/api/status`

Only Caddy is intended as the public ingress. Service health ports are bound to `127.0.0.1`.

## Smoke Replay

After deployment, or locally before deployment:

```bash
SIGNAL_STORE_URL=sqlite:////tmp/replay_test.db \
PYTHONPATH=.:apps/api \
.venv/bin/python scripts/replay_smoke.py fixtures/signals/telegram_latest20/messages.latest20.json
```

The smoke script replays messages one by one through the Python importer, verifies four `new_signal` rows are stored, verifies `update`, `noise`, and `position_screenshot` rows are not approved, then replays the fixture again to confirm idempotency.

## Rollback

Stop the compose stack:

```bash
docker compose down
```

Then restore the previous deployment using the prior image tag or container definitions. If the legacy containers were preserved elsewhere, recreate or start the legacy names:

```bash
docker start docker-frontend-1 docker-freqtrade-dryrun-1 freqtrade
```

If those exact containers were removed by the deploy script, recreate them from the previously recorded image tags and port mappings.

## Troubleshooting

- `missing watcher config`: confirm `/srv/trader-secrets/telegram-watcher.config.json` exists and permissions allow the deploy user or Docker daemon to read it.
- Watcher health fails: check `docker compose logs watcher`; confirm `WATCHER_HOST=0.0.0.0` is present in compose and `config.json` contains a valid Telegram session when live connection is expected.
- Importer failures: inspect `/data/importer-audit.jsonl` inside services using the `signal-data` volume. If alert env vars are empty, failures are logged with `console.error`.
- API cannot see signals: confirm `SIGNAL_STORE_URL=sqlite:////data/signal_store.db` and `signal-data:/data` are mounted on both `api` and `watcher`.
- Caddy route issues: check `docker compose logs caddy` and verify `CADDY_DOMAIN` resolves to the host. Watcher UI is routed under `/watcher/`.
- Freqtrade ping fails: confirm the dry-run service is configured to expose its API on container port `8080`; the compose health check expects host loopback `18081`.
