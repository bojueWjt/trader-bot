# Window A → Window C Handoff

> Program: hermes-nautilus-migration v3 · Window A (hermes-data) · Branch `work/hermes-data-v3`
> Base `plan/nautilus-hermes-v3` (← `main`). All P0/P1 tasks implemented and verified on real Homebrew PostgreSQL 16.

## 1. Status

| Task | Status | Commit |
|---|---|---|
| A-00 safety baseline + no-semantic-regex gate | ✅ done | `00b8bc0` |
| A-01 contracts-v1 | ✅ done | `b299e60` |
| A-02 PostgreSQL canonical schema + migration | ✅ done | `c979a46` |
| (fix) 0002 queue lease/statuses (A-02/A-04 mismatch) | ✅ done | `134a49e` |
| A-03 watcher→collector + ingress | ✅ done | `0374153` |
| A-04 transactional outbox + worker queue | ✅ done | `a29be3e` |
| A-05 Hermes multimodal worker | ✅ code done · 🚧 real smoke BLOCKED | `2655ca1` |
| A-06 decision gateway + risk governor | ✅ done | `98bc4b2` |
| A-07 unified auth / permission / audit | ✅ done | `198cb0d` |
| A-08 kill switch + node command state | ✅ done | `2c2f3da` |
| A-09 real projection SystemSnapshotV1 + fake-data removal | ✅ done | `72a803d` |
| A-10 legacy SQLite read-only import (P1) | ✅ done | `6330830` |
| A-11 regression + handoff | ✅ this doc | — |

## 2. Verification (reproducible)

PostgreSQL-backed tests use a throwaway pg@16 cluster (UTC). Python deps in a venv
(system Python is PEP-668 locked): `python3 -m venv .venv && .venv/bin/pip install psycopg2-binary pytest 'jsonschema[format]' fastapi httpx`.

```
PATH=/opt/homebrew/opt/postgresql@16/bin:$PATH \
  .venv/bin/python -m pytest tests/contracts tests/ingress tests/control-plane tests/hermes -q
# => 92 passed

python3 scripts/check_no_semantic_regex.py
# => Scanned 147 files / 0 violations

cd bridge && .venv/bin/python -m pytest tests/   # => 222 passed
```

Per-area: contracts 5 · ingress 10 · control-plane/db 5 · outbox 3 · security 10 · risk 28 · commands 8 · api 9 · migration 3 · hermes 11 · bridge 222. **Total 314 passing, gate clean.**

## 3. Deliverables

- **contracts-v1**: `packages/contracts/v1/*.json` (HermesDecisionV1, ApprovedTradeIntentV1, ExecutionEventEnvelopeV1, SystemSnapshotV1) + examples + `.snapshot.json` backward-incompat detector + `version.json`/`README.md` (Window B export).
- **Migrations / rollback**: `db/migrations/000{1,2,3}_*.{up,down}.sql` applied by `services/control-plane/db/migrate.py up|down` (reads `DATABASE_URL`). Runbook `db/migrations/README.md`.
- **ERD**: `docs/handoff/window-a/ERD.md`.
- **OpenAPI**: `docs/handoff/window-a/openapi.json` (control-plane read API: `GET /api/system/snapshot`).
- **Schema version manifest**: `packages/contracts/version.json` (`contracts-v1`).
- **No-semantic-regex report**: `docs/handoff/window-a/no-semantic-regex-report.txt` + CI job in `bridge/.github/workflows/security.yml`.
- **Baseline + backup**: `docs/handoff/window-a/baseline-inventory.md`, `backup-manifest.json`.
- **Real ingestion smoke**: covered by `tests/ingress` (new/edit/reply/duplicate/media on real pg).
- **Evidence ledger**: `docs/handoff/window-a/PROGRESS.md` (per-task acceptance + commit SHAs).

## 4. Architecture delivered (half-link: message → approved intent)

```
Telegram → services/telegram-watcher (pure collector) → services/ingress
  → raw_messages + media_assets + outbox(queued_for_hermes)        [one tx]
  → services/hermes-worker (claim, real multimodal, validate vs contracts-v1, fail-closed)
  → hermes_decisions
  → services/control-plane/decision_gateway + risk (deterministic governor)
  → risk_decisions; on approval trade_intents(approved) + outbox(trade_intent.approved)  [one tx]
PostgreSQL is the only application query/audit source; services/control-plane/api serves the
real SystemSnapshotV1 (data-quality: stale/missing_nodes/reconciliation_state). Kill switch via
services/control-plane/commands (per-node ack) sets risk_state mode read by the governor.
```

## 5. Global release gates (PLAN §"全局放行条件") — status for Window C

1. No production `raw → regex parser → approved` path — ✅ gate + A-00/A-03 (147 files, 0 violations).
2. Hermes unavailable → 0 new-risk commands — ✅ A-00 test + A-05 fail-closed (`hermes_unavailable`).
3. No FakeAdapter/fixture fallback in production dashboard — ✅ A-09 (gated to APP_ENV=test; empty real state in prod).
4. Dashboard/Hermes snapshot read only control-plane API → PostgreSQL projection — ✅ A-09 snapshot; bridge dashboard de-faked.
5. Each node can enter HALTED/REDUCING with ack — ✅ A-08 (completes only on full ack; partial/timeout).
6. Two-account intent/order/Redis/client-order isolation — ⚠️ PARTIAL: gateway routes a single account (target_account_id/default); full multi-account routing is a follow-up.
7. Replay of duplicate/edited messages + reconnect/reconciliation → no duplicate orders/fills — ⚠️ partial: ingress dedup + worker outbox-consume + gateway idempotency_key proven; full WS/reconciliation replay is Window B/C (Nautilus).
8. Real image/text replay acceptance — 🚧 **BLOCKED** (see §6).
9. Binance testnet full coverage — out of Window A scope (Nautilus = Window B/C).
10. Security: no test-token fallback / no anon watcher write / no plaintext exchange keys / no default live creds — ✅ A-07 (10 sec tests; secret scan).

## 6. Residual risks / BLOCKED (must reach Window C)

- 🚧 **A-05 real multimodal smoke**: `services/hermes-worker/smoke_replay.py` requires `HERMES_API_URL/HERMES_API_KEY/HERMES_MODEL` + ≥10 real image/text messages. Currently reports `BLOCKED` (never faked). Worker code + fail-closed paths are tested with a mock client (11 tests). **Needs a real Hermes endpoint + real data to clear gate #8.**
- **SystemSnapshotV1.account** is a frozen empty-object placeholder; account identity can't ride the snapshot without a contract-change-request.
- **Bridge dashboard production path** serves a real *empty* state; wiring it to the control-plane `/api/system/snapshot` (real numbers) is Window B's consumption step.
- **Media bytes** use a local-dir object-store abstraction; production object storage (S3/MinIO) must be wired.
- **Multi-account routing** (gate #6) is minimal in the gateway.
- `default live disabled`: compose-level hardening was recorded as infra-window blockers in `baseline-inventory.md` (docker-compose intentionally untouched in Window A).

## 7. Window C operating notes

- Merge order per `GO-SIGNAL.txt`: A then B; run full tests; then C-01..C-12.
- Migrate: `DATABASE_URL=... python services/control-plane/db/migrate.py up` (0001→0003); rollback `... down`.
- All new code under `packages/`, `services/`, `db/`, `tests/`; bridge changes limited to `apps/api` (api/security/dashboard) + `tests` + `.github`. `engine/`, `apps/dashboard`, `services/nautilus-node`, `packages/nautilus-adapter`, compose were NOT modified by Window A.
- Codex was quota-blocked mid-run; A-05/A-06/A-08/A-09 + the 0002/0003 integration fixes were implemented directly and verified on real pg@16.
