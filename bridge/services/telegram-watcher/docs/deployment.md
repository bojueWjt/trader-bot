# Deployment Notes

## Local configuration

`config.json` is a local-only runtime file. It is ignored by Git and should stay on each deployment host as a private file.

The file has been removed from the Git index with `git rm --cached`, so future commits should not carry Telegram session-style configuration.

## Trading account migration

Watcher startup migrates `account_configs` in place:

- Existing rows receive `account_type=main` and an empty `parent_account_id`.
- Existing rows without a valid `risk_capital_multiplier` remain disabled until
  an operator configures a positive multiplier.
- Existing `channel_routing.target_account_id` values remain unchanged.
- A subaccount stores its own API key and secret, sets `account_type=subaccount`, and references an existing main account through `parent_account_id`.
- Main accounts and subaccounts are both execution accounts and can be selected as channel routing targets.

The Web console supports creating and editing both account types and their risk capital multiplier. Its initialization helper calculates `target_effective_equity / initial_actual_equity` and stores only the resulting multiplier. Runtime sizing reads current `totalMarginBalance` for every new position and calculates `effective_equity = current_actual_equity * risk_capital_multiplier`, so profit and loss change the effective equity while the configured multiplier remains stable. This is the inverse-Martingale sizing basis: profitable accounts receive a larger position budget and losing accounts receive a smaller position budget. A target such as `9000` is an initialization input or test example; runtime sizing always derives effective equity from the current actual equity and the saved multiplier. The CLI accepts `add-account --type subaccount --parent-account <main-account-id> --capital-multiplier <value>`.

## Binance API proxy

`binance_trade.py` accepts a proxy from the explicit `--proxy <url>` option or the `BINANCE_PROXY` environment variable. The command-line value has priority. When both sources are empty, the Binance SDK connects directly.

Every supplied proxy must be an unauthenticated `http://` or `https://` URL. Values containing usernames or passwords, non-HTTP schemes, and sentinel strings such as `none` are rejected. To use a direct connection, omit `--proxy` and leave `BINANCE_PROXY` unset.

## Credential rotation

Rotate every credential category that may have been present in the tracked history:

- Telegram session strings or session files
- Telegram API ID and API hash credentials
- Webhook or shared-secret values used by the watcher

Store replacement values only in local runtime configuration, PM2 environment variables, or the host secret manager.

## PM2 environment

`ecosystem.config.js` contains non-secret templates for SignalStrategy importer settings:

- `TRADER_TRADING_DB_PATH` is the canonical production SQLite path for trading configuration (`account_configs`, `channel_routing`, risk multipliers). Watcher, Hermes feeder, and `v3_trade.py` must read the same file.
- `WATCHER_TRADING_DB` and `TRADING_DB_PATH` remain supported legacy aliases. When more than one of these variables is set, all non-empty values must be identical; otherwise startup/import fails closed.
- `HERMES_SIGNAL_STORE_URL`
- `SIGNAL_IMPORTER_PYTHON`
- `SIGNAL_IMPORTER_MODULE`
- `SIGNAL_IMPORTER_CWD`
- `SIGNAL_IMPORTER_REMOTE_HOST`
- `SIGNAL_IMPORTER_REMOTE_CWD`
- `SIGNAL_IMPORTER_SSH_IPV6`
- `SIGNAL_PAIR_WHITELIST`
- `SIGNAL_IMPORTER_ENABLED`
- `SIGNAL_IMPORTER_MAX_IN_FLIGHT`

The watcher is configured with `WATCHER_HOST=127.0.0.1` so deployments can bind the service to loopback when the application reads that environment variable.

## Remote Freqtrade importer

When Freqtrade runs on a separate host, keep the watcher on the Hermes host and set:

- `SIGNAL_IMPORTER_REMOTE_HOST` to the SSH target for the Freqtrade host
- `SIGNAL_IMPORTER_REMOTE_CWD` to the Freqtrade checkout directory on that host
- `HERMES_SIGNAL_STORE_URL` to the SQLite URL as seen by the Freqtrade host
- `SIGNAL_IMPORTER_SSH_IPV6=1` when the SSH target requires IPv6

The watcher sends the normalized Telegram signal through stdin to:

```text
python3 -m freqtrade.signal_strategy.importer --stdin --store-url ...
```

SSH key login must be configured first. Keep password-based server credentials out of PM2 config, shell history, and repository files.
