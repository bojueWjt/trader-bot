# Window A A-00 Safety Baseline Inventory

Date: 2026-06-19

## Deployment Baseline

- `bridge/docker-compose.yml` was not modified in this task.
- Freqtrade defaults to dry-run in compose:
  - `freqtrade-dryrun` command uses `${FREQTRADE_CONFIG:-config_dry_run.json}`.
  - `freqtrade-b` command uses `${FREQTRADE_CONFIG_B:-config_dry_run.json}`.
  - `bridge/user_data/config_dry_run.json` has `"dry_run": true` and `db_url` set to `sqlite:////data/tradesv3.dryrun.sqlite`.
- The watcher compose service already has `HERMES_TRADER_CRON_ENABLED: "0"`.
- The watcher compose service writes collected signals to `SIGNAL_STORE_URL=sqlite:////data/signal_store.db` and importer audit to `/data/importer-audit.jsonl`.

## Frozen Surfaces

- `bridge/services/telegram-watcher/lib/signal-importer.js`
  - `isSignalImporterEnabled()` now defaults to disabled when `SIGNAL_IMPORTER_ENABLED` is unset or empty.
  - `buildImporterArgs()` no longer includes `--approve-parsed` or `--refresh-window`.
  - `SIGNAL_IMPORTER_MODULE` no longer defaults to the freqtrade semantic importer module.
  - `importSignalToFreqtrade()` is a no-op unless both explicit enablement and required dependencies are present.
- `bridge/services/telegram-watcher/lib/watched-entry-routing.js`
  - Default watched-message handling only runs collection and audit persistence (`pushMessage`, `saveTelegramMessage`).
  - The old raw-message to semantic-importer call path was removed from routing.
  - Hermes trader forwarding now requires `HERMES_TRADER_CRON_ENABLED` to be explicitly enabled.
- `bridge/services/telegram-watcher/server.js`
  - The production watcher no longer imports `./lib/signal-importer`.
  - The watcher still collects watched Telegram entries and saves them through `saveTelegramMessage`.
  - `forwardToTrader` is only wired as an explicitly gated `traderCronForwarder`.
- `bridge/services/telegram-watcher/ecosystem.config.js`
  - PM2 defaults are disabled for the semantic importer module and Hermes trader cron.
- `bridge/services/telegram-watcher/scripts/configure-remote-importer.sh`
  - The helper now exports disabled defaults for the importer and Hermes trader cron.

## Live-Disabled Evidence

- Compose default Freqtrade config remains `config_dry_run.json`.
- `bridge/user_data/config_dry_run.json` has `"dry_run": true`.
- Compose watcher environment already has `HERMES_TRADER_CRON_ENABLED: "0"`.
- PM2 watcher config now defaults `HERMES_TRADER_CRON_ENABLED` to `'0'`.
- The static gate report at `docs/handoff/window-a/no-semantic-regex-report.txt` shows `0 violations`.

## Infrastructure Window Blockers

The following hardening belongs to the infrastructure window, not A-00:

- Add explicit compose environment defaults for `SIGNAL_IMPORTER_ENABLED: "0"` and `SIGNAL_IMPORTER_MODULE: ""` so runtime intent is visible in deployment manifests.
- Add deployment policy that blocks `FREQTRADE_CONFIG=config_live.json` and `FREQTRADE_CONFIG_B=config_live_b.json` unless the Nautilus migration approval gate is complete.
- Document and test restore procedures for the `signal-data` Docker volume before any live migration.
- Ensure production secrets and live exchange keys stay outside repo-managed compose files.
