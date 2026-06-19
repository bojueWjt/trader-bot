# Live Readonly Gate Runbook

## Gate Goal

Live readonly gate allows live bot visibility, live snapshots, and reports while preserving manual control over entries.

## Required Commands

```bash
cd /Users/balen/dalongxia/trader
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/security -q
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/e2e_api/test_release_gates.py -q
```

## Manual Confirmation

- Confirm live automatic entry path is disabled in Strategy bridge settings.
- Confirm Dashboard presents live bot state as readonly.
- Generate a live snapshot report and scan it with `/api/security/scan-report`.
- Run recovery drill: stop bot, keep risk state, restart bot, verify readonly state remains.
- Approve `/api/release-gates/approve-live-readonly` with `risk_admin`, confirmation, and reason.

## Rollback

- Set `APP_ENV=live_readonly`.
- Keep kill switch available.
- Revoke live snapshot publishing if audit scan reports a secret.

## Disallowed States

- Automatic entries enabled.
- Secret present in audit or report output.
- Live bot restart clears risk state without human approval.
