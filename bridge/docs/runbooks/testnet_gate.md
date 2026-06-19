# Testnet Gate Runbook

## Gate Goal

Testnet gate confirms the system can receive signals, apply risk controls, expose operational APIs, and keep Freqtrade reachable only from local/private interfaces.

## Required Commands

```bash
cd /Users/balen/dalongxia/trader
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/unit/test_risk_governor.py -q
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/unit/test_audit_events.py -q
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/unit/test_permissions.py -q
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/security -q
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/e2e_api/test_risk_api.py -q
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/e2e_api/test_release_gates.py -q
```

## Manual Confirmation

- Trigger `/api/risk/kill-switch` with `risk_admin`, `confirm=true`, and a non-empty reason.
- Run `/api/risk/precheck` against a valid entry signal and confirm `decision=blocked` with `kill_switch_enabled`.
- Confirm Freqtrade API bind address is `127.0.0.1` or private network only.
- Confirm exchange keys are testnet-scoped and withdrawal permission is disabled at the exchange account level.

## Rollback

- Keep live entry automation disabled.
- Disable any newly added webhook source.
- Restart Hermes trader only after kill switch state and audit output are reviewed.

## Disallowed States

- Freqtrade API bound to `0.0.0.0`.
- Exchange key with live trading permission.
- Missing request id in dangerous operation logs.
- Kill switch drill missing audit event.
