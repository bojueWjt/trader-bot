# Window A — Progress & Evidence Ledger

- Branch: `work/hermes-data-v3` (base `plan/nautilus-hermes-v3`, off `main`)
- Canonical task spec: `window-a-hermes-data/TaskList.json` (A-00..A-11)
- Rule (不可伪造完成): a task is `done` only with reproducible commands, outputs, and a commit SHA.
- Window C release signal: flip `A_READY=0→1` in `trader-bot-window-c-staging/GO-SIGNAL.txt` **only after all P0 done + A-11 handoff + tree committed**.

| Task | Status | Commit | Notes |
|---|---|---|---|
| A-00 | ✅ done | `00b8bc0` | safety baseline + no-semantic-regex gate |
| A-01 | ✅ done | `b299e60` | contracts-v1 (4 schemas + tests + B export) |
| A-02 | ✅ done | `c979a46` | Postgres canonical schema + migration (pg@16) |
| A-03 | 🔄 in_progress | — | watcher→collector + ingress |
| A-04 | 🔄 in_progress | — | transactional outbox + worker queue |
| A-07 | 🔄 in_progress | — | auth / permission / audit |
| A-10 | ✅ done | `6330830` | legacy SQLite read-only import |
| A-03 | ✅ done | `0374153` | watcher→collector + Postgres ingress |
| A-04 | ✅ done | `a29be3e` | transactional outbox + worker queue |
| A-07 | ✅ done | `198cb0d` | auth/permission/audit (222 bridge + 10 sec tests) |
| A-05 | ✅ done* | `2655ca1` | Hermes worker — code + 11 tests; *real smoke BLOCKED |
| fix | ✅ done | `134a49e` | 0002 migration: A-02/A-04 queue schema mismatch |
| A-06 | 🔄 in_progress | — | decision gateway + risk governor (←A-05) |
| A-08 | ⬜ ready | — | kill switch + node commands (←A-07) |
| A-09 | ⬜ ready | — | real projection snapshot/API (←A-07) |
| A-11 | ⬜ pending | — | ←all |

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

## A-02 — PostgreSQL canonical schema 与 migration — DONE
Commit: `c979a46`

Acceptance evidence (real Homebrew pg@16, throwaway cluster):
- `db/migrations/0001_canonical_schema.{up,down}.sql`: 18 canonical tables separating raw_messages → hermes_decisions → risk_decisions → trade_intents (+ execution_*, *_projection, outbox_events, audit_events, replay_*, context_snapshots, node_heartbeats, risk_state, message_processing_runs, media_assets).
- Guardrails: `raw_messages` UNIQUE(source,channel_id,source_message_id,source_version); `source_received_at` insert-only via trigger; `trade_intents` CHECK requires hermes+risk decision FKs + approved_at when approved; execution `event_id` + intent `idempotency_key` unique; `nautilus_projection_writer` role with GRANTs restricting writes to projection tables.
- `services/control-plane/db`: `migrate.py` runner, `connection.py`, `enums.py` (aligned to contracts-v1), `repository.py` (same-tx raw_message+outbox).
- Tests: `pytest tests/control-plane/db` = **5 passed** (fresh-migration creates tables/constraints/indexes/projection role; tx rollback; unique-key concurrent-duplicate rejected; same-tx raw+outbox; down-migration removes objects). Independent: `migrate up` → 19 tables (+schema_migrations) + projection role; `migrate down` clean.

Fix applied at acceptance: `migrate.py` opened `with psycopg2.connect() as conn:` (transaction CM) then nested `with conn:` per migration → `psycopg2 cannot re-enter recursively`. Changed `main()` to open the connection without the outer transaction CM (try/finally close); the per-migration `with conn:` blocks now own their transactions. Re-verified green.

Residual: object storage for `media_assets` bytes is out of scope here (A-03 wires ingestion); legacy SQLite history import is A-10.

## A-10 — 旧 SQLite 历史导入工具（只读历史）— DONE (P1)
Commit: `6330830`

Acceptance evidence (real pg@16):
- `scripts/migrate_legacy_sqlite.py`: idempotent import of legacy `signals`/`signal_events`/`signal_operations` into separate `legacy_*` history tables (`services/control-plane/migration/legacy_ddl.sql`), with row-count + sha256 reconciliation output. Recursive sensitive-field scrubber drops `api_key`/`secret`/`token`/`password`/`credential` before insert.
- Tests: `pytest tests/control-plane/migration` = **3 passed** — fixture import row-counts; double-run idempotency + stable checksums; secret-exclusion (payloads contain no sensitive substrings).
- Does **not** touch `db/migrations` or canonical execution tables; legacy data is read-only history.

Residual: only the three legacy signal tables are mapped (`MAPPING.md`); legacy freqtrade orders/trades import (if needed) is a follow-up — not required for Window A acceptance.

## A-03 — watcher 改为纯采集器 — DONE
Commit: `0374153`
- `services/ingress` writes raw_messages + queued_for_hermes outbox in one transaction; idempotent on duplicate update; edits → new source_version; reply relationship preserved; media failure rolls back raw+outbox.
- `services/telegram-watcher` is a pure collector (collector / ingress_client / media_store); content-hash (sha256) media → media_assets; no semantic parser, no trade intent, no exchange keys.
- Tests: `pytest tests/ingress` = **10 passed** (real pg@16) incl. `test_new_services_do_not_depend_on_semantic_or_execution_paths`, watcher-restart idempotency, media-sha.
- Fix at acceptance: conftest runs throwaway cluster in UTC (timestamptz::text machine-TZ-independent).

## A-04 — transactional outbox 与可靠队列状态机 — DONE
Commit: `a29be3e`
- `services/control-plane/outbox/publisher.py`: at-least-once + idempotent sink; `FOR UPDATE SKIP LOCKED`; published rows skip re-delivery.
- `services/hermes-worker/queue/claims.py`: visibility-lease claim (SKIP LOCKED on outbox + no-active-run lock), lease-expiry takeover → `hermes_timeout`, lease-guarded completion (zombie workers can't finish), explicit failure states.
- Tests: `pytest tests/control-plane/outbox` = **3 passed** (real pg@16): crash-between-commit-and-publish, duplicate-delivery idempotency, lease expiry + takeover. Concurrency-safe by construction.

## A-07 — 统一鉴权、权限和审计 — DONE
Commit: `198cb0d`
- `bridge/apps/api/app/security/tokens.py`: `static_token_users()` → `validate_security_environment()` requires `AUTH_SECRET_KEY` + 5 role tokens from env and raises on missing/duplicate (**fail-closed**); all `test-*-token` defaults removed.
- `dependencies.py` delegates to `static_token_users()` + `validate_actor` (roles viewer/reviewer/risk_admin/system_observer/nautilus_node).
- `services/control-plane/security`: permission matrix, audit writer (`audit_events`), dangerous-ops gating (request_id + reason + confirm).
- Tests: `pytest tests/control-plane/security` = **10 passed** (role matrix, startup fail-closed, secret scan, watcher anon-write); full bridge `pytest tests/` = **222 passed** (no regression). No plaintext exchange keys in source.

## A-05 — Hermes 真实多模态 Worker — IN PROGRESS (Claude直接实现)
Codex run `task-mqlfg9ew-xs94ub` failed at 27s with `"You've hit your usage limit. Try again later."` (Codex/GPT backend quota, not a code defect — no files written). Implemented directly by Claude.

Commit: `2655ca1`
- `services/hermes-worker/hermes_client.py`: `HermesClient` contract + OpenAI-compatible `RealHermesClient` (stdlib HTTP, temperature 0) + Unavailable/Timeout/Response errors; **no regex/OCR fallback** (fail-closed when unconfigured).
- `prompt.py`: pinned `hermes-trader-v1` prompt (strict HermesDecisionV1 JSON; needs_review on ambiguity/missing image; updates ≠ open_position).
- `worker.py`: claim → load raw+images+context+SystemSnapshotV1 → Hermes → **validate vs contracts-v1** → atomically persist context_snapshot + hermes_decision + audit, complete run, **consume outbox (no duplicate decision)**. Fail-closed taxonomy: media_failed / context_stale / hermes_timeout / hermes_unavailable / invalid_decision_schema / hermes_failed. Worker owns identity + pinned model block; the model supplies only classification/intent/evidence.
- Tests: `pytest tests/hermes` = **11 passed** (real pg@16) — text/image/mixed happy paths, all fail-closed cases, no-duplicate-decision, no-semantic-regex source check.

🚧 **BLOCKED (Window C / infra)**: the real multimodal call + 10 real image/text message smoke. `smoke_replay.py` reports `BLOCKED` (not faked) until `HERMES_API_URL/KEY/MODEL` + ≥10 real messages are provided. Also: the live SystemSnapshotV1 provider is wired by A-09; A-05 uses an injected provider.

## Cross-task integration fix — DONE
Commit: `134a49e` — `db/migrations/0002` adds `message_processing_runs.lease_expires_at` + queue statuses (hermes_timeout/…) that A-04's `claims.py` needs but A-02's `0001` lacked (A-04 had hidden this with a test-only `ALTER`). Caught by independent verification; A-04 now passes against the real migration chain.
