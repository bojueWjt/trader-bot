# Live Small Size Gate Runbook

## Gate Goal

Live small-size automation gate permits constrained live entries after dry-run evidence, drills, and reduced default risk are complete.

## Required Commands

```bash
cd /Users/balen/dalongxia/trader
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/unit/test_risk_governor.py -q
PYTHONPATH=/Users/balen/dalongxia/trader/apps/api .venv/bin/pytest tests/e2e_api/test_release_gates.py -q
```

## Manual Confirmation

- Confirm at least 7 consecutive daily dry-run reports.
- Confirm at least 30 dry-run signals completed lifecycle.
- Confirm each blocked signal has reason codes.
- Drill kill switch and daily loss guard.
- Set default single-trade risk to `0.25%` or lower.
- Approve `/api/release-gates/approve-live-small-size` with `risk_admin`, confirmation, and reason.

## Rollback

- Move state to `blocked_new_entries`.
- Allow close, partial close, and stop movement.
- Require human confirmation before any future live entry resumes.

## Disallowed States

- Default single-trade risk above `0.25%`.
- Daily loss guard drill missing.
- Any blocked signal without an explainable reason.
