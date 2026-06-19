# Window A — Progress & Evidence Ledger

- Branch: `work/hermes-data-v3` (base `plan/nautilus-hermes-v3`, off `main`)
- Canonical task spec: `window-a-hermes-data/TaskList.json` (A-00..A-11)
- Rule (不可伪造完成): a task is `done` only with reproducible commands, outputs, and a commit SHA.
- Window C release signal: flip `A_READY=0→1` in `trader-bot-window-c-staging/GO-SIGNAL.txt` **only after all P0 done + A-11 handoff + tree committed**.

| Task | Status | Commit | Notes |
|---|---|---|---|
| A-00 | ✅ done | `00b8bc0` | safety baseline + no-semantic-regex gate |
| A-01 | ✅ done | `b299e60` | contracts-v1 (4 schemas + tests + B export) |
| A-02 | 🔄 in_progress | — | Postgres canonical schema (local pg@16) |
| A-03..A-11 | ⬜ pending | — | pipelined per DAG |

## A-00 — 建立安全基线并冻结旧自动开仓路径 — DONE
Commit: `00b8bc0cf2a6500f7d0296875f1c13e187458e07`

Acceptance evidence:
- no-semantic-regex gate: `python3 scripts/check_no_semantic_regex.py` → `Scanned 84 files / 0 violations`, rc=0
- production banned-flag grep clean: no `--approve-parsed` / `--refresh-window` under `bridge/services` or `scripts` (excl. tests)
- `server.js` no longer imports/calls the semantic importer; `SIGNAL_IMPORTER_ENABLED` defaults OFF; `importSignalToFreqtrade` is a no-op without an explicit module
- `watched-entry-routing.js`: collection + audit only; trader-cron forward requires explicit `HERMES_TRADER_CRON_ENABLED`
- watcher tests: `node --test __tests__/*.test.js` → 11/11 pass (incl. "default disabled importer and Hermes path add zero risk commands")
- CI: `bridge/.github/workflows/security.yml` adds a `no-semantic-regex` job running the gate on PR/push

Deliverables: `scripts/check_no_semantic_regex.py`, `docs/handoff/window-a/{baseline-inventory.md, backup-manifest.json, no-semantic-regex-report.txt}`

Residual risk: compose-level hardening (explicit `SIGNAL_IMPORTER_ENABLED=0` / `SIGNAL_IMPORTER_MODULE=""` in compose, and a deploy policy blocking live freqtrade configs) is deferred to the infrastructure window as blockers — see `baseline-inventory.md` §Infrastructure Window Blockers. `docker-compose.yml` intentionally untouched.

## A-01 — 冻结并实现跨窗口 contracts-v1 — DONE
Commit: `b299e60`

Acceptance evidence:
- `packages/contracts/v1/`: 4 JSON Schemas (HermesDecisionV1, ApprovedTradeIntentV1, ExecutionEventEnvelopeV1, SystemSnapshotV1), draft 2020-12, `additionalProperties:false`, `schema_version` const "1.0" — matches PLAN §3.
- 8 valid + 10 invalid examples; `version.json`, `dist/index.json`, `README.md` (Window B consumes `v1/*.json` directly, language-neutral).
- Tests: clean venv from `tests/contracts/requirements.txt` → `pytest tests/contracts` = **5 passed**; stdlib-fallback validator path = 5/5; backward-incompat detector proven to fire (removed enum → caught).
- Fix applied at acceptance: `requirements.txt` pinned `jsonschema[format]` so `format: date-time` is actually enforced (plain `jsonschema` let an invalid-datetime example slip through).

Residual: cross-validator robustness relies on consumers using **format-aware** validation (e.g. ajv-formats in Window B); schemas use `format: date-time`/`uuid` rather than regex patterns. Noted for Window C.
Consumption note: Window B's worktree gets these on Window C's A-merge; for parallel dev the frozen definitions in PLAN §3 are identical.
