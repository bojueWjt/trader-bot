# Deployment Notes

## Local configuration

`config.json` is a local-only runtime file. It is ignored by Git and should stay on each deployment host as a private file.

The file has been removed from the Git index with `git rm --cached`, so future commits should not carry Telegram session-style configuration.

## Credential rotation

Rotate every credential category that may have been present in the tracked history:

- Telegram session strings or session files
- Telegram API ID and API hash credentials
- Webhook or shared-secret values used by the watcher

Store replacement values only in local runtime configuration, PM2 environment variables, or the host secret manager.

## PM2 environment

`ecosystem.config.js` contains non-secret templates for SignalStrategy importer settings:

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
