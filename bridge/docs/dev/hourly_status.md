# Hourly Status

## 2026-05-31T21:13:00+08:00

- Checked by: window_4_hourly_checker
- Overall status: not_ready
- Window status: W1 yellow, W2 yellow, W3 yellow, W4 green
- Progress: W1 0/9 done, W2 0/10 done, W3 0/11 done, W4 1/6 done after recording this check
- Blockers: 0 blocked tasks observed
- Change requests: 0 open change requests observed
- Evidence gaps: 0 done tasks without evidence before this check
- Scope drift: W3-owned risk/audit/security files changed around the check timestamp while W3 tasks remain todo; attribution or tasklist evidence is needed
- Bridge mode: not_started; bridge tasks remain todo until windows 1/2/3 are bridge_ready

Evidence commands recorded on W4-CHECK-001:

```bash
jq empty tasklist.json
jq .health_checks[-1] tasklist.json
test -f docs/dev/hourly_status.md
find apps fixtures tests docs -type f -newermt '2026-05-31 21:00:00'
```

## 2026-05-31T21:26:00+08:00

- Checked by: window_4_hourly_checker
- Overall status: partial_ready
- Window status: W1 yellow, W2 yellow, W3 bridge_ready, W4 green
- Progress: W1 0/9 done, W2 0/10 done, W3 11/11 done, W4 1/6 done
- Blockers: 0 blocked tasks observed
- Change requests: 4 open change requests: CR-W3-001, CR-W3-002, CR-W3-003, CR-W3-004
- Evidence gaps: 0 done tasks without evidence
- Bridge mode: not_started; W1 and W2 still need completion before bridge mode

Evidence commands:

```bash
jq empty tasklist.json
jq '.health_checks[-1]' tasklist.json
PYTHONPATH=apps/api .venv/bin/pytest tests/unit -q
PYTHONPATH=apps/api .venv/bin/pytest tests/security -q
PYTHONPATH=apps/api .venv/bin/pytest tests/e2e_api -q
```

## 2026-05-31T22:43:37+08:00

- Checked by: window_4_hourly_checker
- Overall status: testnet_candidate
- Window status: W1 bridge_ready, W2 bridge_ready, W3 bridge_ready, W4 bridge_ready
- Progress: W1 9/9 done, W2 10/10 done, W3 11/11 done, W4 6/6 done
- Blockers: 0 blocked tasks observed
- Change requests: CR-W3-001, CR-W3-002, CR-W3-003, and CR-W3-004 resolved
- Evidence gaps: 0 done tasks without evidence
- Bridge mode: complete; operator testnet gate items remain pending

Evidence commands:

```bash
PYTHONPATH=.:apps/api:user_data/strategies .venv/bin/pytest tests/unit tests/e2e_api tests/security tests/integration -q
PYTHONPATH=.:apps/api:user_data/strategies .venv/bin/pytest tests/integration/test_full_system_smoke.py -q
PYTHONPATH=apps/api .venv/bin/pytest tests/e2e_api/test_main_app.py -q
PYTHONPATH=apps/api .venv/bin/pytest tests/security -q
ssh -6 root@2001:df1:7880:2::18dc 'docker --version && docker compose version'
```
