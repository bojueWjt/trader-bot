# Hermes Report Service Deployment

This service renders self-contained HTML reports into `/srv/trader-v3/reports`.
Caddy serves those files at `https://hk.balen.wang/reports/*`.

## Install

```bash
cd /srv/trader-v3
python3 -m venv .venv-report
.venv-report/bin/pip install -r services/report/requirements.txt
mkdir -p /srv/trader-v3/reports/assets
chown -R trader:trader /srv/trader-v3/reports
chmod 750 /srv/trader-v3/reports
```

The existing `/srv/trader-v3/.env.v3` must provide `DATABASE_URL`. Optional
report settings:

```bash
REPORT_HOST=127.0.0.1
REPORT_PORT=8090
REPORT_DIR=/srv/trader-v3/reports
PUBLIC_BASE=https://hk.balen.wang
REPORT_TOKEN=<long random token>
REPORT_OUTCOME_FRESHNESS_HOURS=36
```

`python-multipart` is required for `POST /reports/assets`.
`REPORT_OUTCOME_FRESHNESS_HOURS` controls the maximum age of the latest
successful `trade_outcomes` job run. Report publication returns HTTP 503 when
the watermark is missing or stale.

## systemd

```bash
cp services/report/trader-v3-report.service /etc/systemd/system/trader-v3-report.service
systemctl daemon-reload
systemctl enable --now trader-v3-report.service
systemctl status trader-v3-report.service
curl http://127.0.0.1:8090/healthz
```

## Caddy

Add this handle inside the existing `hk.balen.wang` site block:

```caddyfile
handle /reports/* {
    root * /srv/trader-v3
    file_server
}
```

With `REPORT_DIR=/srv/trader-v3/reports`, a file named
`/srv/trader-v3/reports/2026-07-12-daily-ab12cd34.html` becomes
`https://hk.balen.wang/reports/2026-07-12-daily-ab12cd34.html`.

Reload Caddy after validation:

```bash
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

## Access Control And URL Risk

The service binds to `127.0.0.1` by default. Set `REPORT_TOKEN` to require a
matching `Authorization: Bearer <token>` header for report creation and asset
upload.

Report URLs include `secrets.token_hex(4)`, so casual guessing is unlikely, but
the files are public once a URL is shared. Do not put exchange API secrets,
private keys, raw credentials, or private chat exports in a report. Increase the
token length in `report_service.py` if reports need stronger unguessability.
