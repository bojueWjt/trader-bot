# wac local test backend (wac-100)

A one-command local stack for the tablet device test, step 2 (signal page data, scroll
performance, media memory). It starts:

- the watcher (`bridge/services/telegram-watcher/server.js`) on `127.0.0.1:9100`, and
- the control-plane app that hosts the watcher gateway (`read_api:app`, role
  `watcher-gateway` since WGW-1.0.4) on `127.0.0.1:<port>` (default `18731`).

All data is synthetic, all tokens are fresh random test values, and both servers bind
loopback only. The app reaches the gateway through `adb reverse`.

## Commands

```bash
W=scripts/dev/wac_local_backend/wac_local_backend.sh
$W up     [--port 18731] [--role watcher-gateway] [--python PY] [--node NODE] [--node-modules DIR]
$W status [--port 18731]
$W verify [--port 18731]      # smoke checks through the gateway, exits non-zero on any failure
$W token  [--port 18731] [--role viewer|risk_admin|reviewer|system_observer] [--reveal]
$W down   [--port 18731]      # stops only our processes, deletes the state dir
```

`up` refuses to start if `127.0.0.1:<port>` or `127.0.0.1:9100` already has a listener; it
never stops or reuses an existing service. The watcher port 9100 is hard-coded in
`server.js`, so it cannot be moved without a code change.

State lives in `$TMPDIR/wac-local-backend-<port>` (mode 0700). Run `status`, `verify`,
`token` and `down` from a shell with the same `TMPDIR`, or pass `--state-dir` to all of them.

## Connecting the test package

1. `$W up`
2. Fill in the test package's Trading settings form (the watcher API reuses it):
   - baseUrl: `http://127.0.0.1:18731` (no `/m`, no trailing slash)
   - operator token: the `VIEWER_TOKEN` from `<state dir>/secrets/test.env`
     (`RISK_ADMIN_TOKEN` only when testing watcher writes). `$W token --reveal`
     prints it on your terminal; nothing writes it to a log.
3. Run adb yourself (the script never runs adb):
   `adb -s <serial> reverse tcp:18731 tcp:18731`, and afterwards
   `adb -s <serial> reverse --remove tcp:18731`.

The app must be a build that allows cleartext HTTP to `127.0.0.1`. The current
`build.gradle` sets `usesCleartextTraffic` to `false`, which blocks `http://127.0.0.1`.
Only `/v1/watcher/*` is backed here. The other `/v1` endpoints (accounts, positions)
need PostgreSQL and return 503 `projection store unavailable`. Watcher status reports
`needs_login` because there is deliberately no Telegram session.

## Seeded data

| Table / dir | Content |
|---|---|
| `telegram_messages` | 2100 rows in 3 channels: 1800 in `[now-23h, now-60s]`, 300 in `[now-72h, now-25h]`. Includes long text (10-20 paragraphs), CJK, Cyrillic, Japanese, emoji including ZWJ sequences and flags, one long unbroken line, and albums (3-4 photo messages in the same channel and second). About a quarter of rows share their `created_at` second with another row, so page boundaries fall inside tie groups and exercise the `(created_at, id)` cursor. |
| `briefings` | 300 rows (240 in the 24h window), with tied timestamps. |
| `active_orders` | 24 synthetic rows. `binance_*` ids are blank. |
| `media/` | PNG files. Filenames match the gateway pattern `<epoch_ms>-<msg_id>.png`. |

Media tiers:

| Tier | Size | Expected behaviour |
|---|---|---|
| small (about 120 files, some album members) | < 1 MiB. Includes solid 1920x1080 images that are tiny on disk and large once decoded | 200. Thumbnail loads |
| large (12) | about 2 MiB | 200. Over the app thumbnail cap (1 MiB), under the detail cap (8 MiB) |
| huge (6) | about 6 MiB and about 9 MiB | 200 from the gateway. The 9 MiB files exceed the app detail cap (8 MiB) |
| over_limit (2) | about 21 MiB | 503 `media_too_large` before headers, because the contract `max_file_bytes` is 20 MiB |
| missing (4 rows) | referenced, not on disk | 404. The message text must still render |

The newest in-window rows carry one file from each non-small tier plus a missing
reference, so all of them show up on the first page.

The in-window rows stay inside the 24h window for about an hour after `up`. Re-run
`down` and `up` to refresh them.

## Safety properties

- Child processes get an environment built from scratch. The caller's environment
  (tokens, `DATABASE_URL`, alert bot tokens, proxies) is not inherited, and no real
  env or secret file is read.
- The watcher runs from a copy of its code inside the state dir, so its `config.json` is
  new and empty: no apiId, no session, no Telegram connection. As a second guard,
  Telegram traffic is pointed at the closed loopback port 9. The price monitor and
  signal importer are off, and no alert bot token is set, so nothing is sent anywhere.
- The gateway's `DATABASE_URL` is unset and `WATCHER_CONFIG_SNAPSHOT_ENABLED=0`.
- Tokens are stored in a 0600 file inside the 0700 state dir. `up` and `verify` fail if
  any token value shows up in `logs/*.log`.
- `down` signals a process only when its recorded start time matches and its command
  line contains the state dir. It then removes the state dir, which it identifies by a
  marker file. Symlinks inside the state dir (the `node_modules` link) are removed,
  not followed.

## Future `watcher-gateway` role

`--role` is passed through as `CONTROL_PLANE_APP_ROLE`, and `--port` picks the gateway
port. Until `app_roles.AppRole` has a `watcher-gateway` member, `up --role watcher-gateway`
fails at import time and cleans up after itself. After that, the same command should work
unchanged. If the role moves to its own ASGI app, pass `--app module:attr` as well.
