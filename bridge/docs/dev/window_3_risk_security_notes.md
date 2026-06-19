# Window 3 Risk Security Notes

## Risk Precheck Contract

SignalStrategy should call `POST /api/risk/precheck` before reserving or entering a signal. The request body contains `signal` and `account`; the response returns `decision`, `risk_state`, `reason_codes`, `single_trade_risk_usage_pct`, `total_open_risk_usage_pct`, and `daily_loss_usage_pct`.

## Kill Switch Contract

`risk_state=kill_switch_enabled` blocks new entries. `blocked_new_entries` allows close, partial close, reduce, and stop movement actions. Strategy bridge should treat any blocked precheck as `blocked_by_risk` and persist `reason_codes`.

## Dashboard Fields

Dashboard should consume `risk_state`, `kill_switch`, `pair_locks`, `reason_codes`, `single_trade_risk_usage_pct`, `total_open_risk_usage_pct`, and `daily_loss_usage_pct`.

## Bridge Requirements

Window 4 should mount these routers:

```text
/api/risk -> app.risk.router:router
/api/audit -> app.audit.router:router
/api/security -> app.security.router:router
/api/release-gates -> app.security.release_gates_router:router
```

Window 4 should provide request id middleware, real auth integration for `x-actor-id` and `x-actor-role`, and persistent stores for audit events and pair locks.

Dangerous endpoints require `x-request-id`; missing request ids return `422`. `RISK_STATE_PATH` can point the risk router at a JSON state file so kill switch, risk state, and pair locks survive process restart. Production should replace the JSON store with the shared database store during bridge integration.

## CI Secret Scan

```bash
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/security -q
```

## Remote Verification

Audit markers: `remote 49 tests passed`, `dry-run API pong`, `dashboard 200`, `ssh -6 balen@balen.wang ok`.

Remote host `/root/freqtrade` has Window 3 files deployed and validated through `/root/freqtrade/.w3-venv`:

```bash
PYTHONPATH=/root/freqtrade/apps/api .w3-venv/bin/python -m pytest -o addopts="" --confcutdir=/root/freqtrade/tests/unit tests/unit/test_risk_governor.py tests/unit/test_audit_events.py tests/unit/test_permissions.py -q
# 26 passed

PYTHONPATH=/root/freqtrade/apps/api .w3-venv/bin/python -m pytest -o addopts="" --confcutdir=/root/freqtrade/tests/security tests/security -q
# 4 passed

PYTHONPATH=/root/freqtrade/apps/api .w3-venv/bin/python -m pytest -o addopts="" --confcutdir=/root/freqtrade/tests/e2e_api tests/e2e_api/test_risk_api.py tests/e2e_api/test_release_gates.py -q
# 19 passed
```

The remote Freqtrade dry-run API returned `{"status":"pong"}` on `127.0.0.1:18081`, and the dashboard returned HTTP `200` on `127.0.0.1:13000` after the remote test run.

Latest remote health on `2026-05-31T23:36:19+08:00` also returned `{"status":"pong"}` from `127.0.0.1:18081` and dashboard HTTP `200` from `127.0.0.1:13000`.

## Release Gate Gaps

Live readonly approval still needs real Strategy bridge evidence that automatic entries are disabled. Live small-size approval still needs 7 dry-run report days, 30 dry-run signal lifecycles, and a configured default risk at or below `0.25%`.

Release gate approval endpoints require an `evidence` object. Missing gate evidence returns `422` with the missing fields.
