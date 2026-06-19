# B-09 Fake Data Removal Report

Date: 2026-06-19

## Deleted or Removed

- Deleted production seeded data file: `bridge/apps/dashboard/src/data/mockData.ts`.
- Removed all production imports of `mockData` and `getMock*` helpers.
- Removed REST fallback branches that returned seeded dashboard, risk, and report objects.
- Removed fixed reports-page rows and seeded report status values.
- Replaced legacy page-data endpoints under `/api/dashboard`, `/api/freqtrade`, `/api/signals`, `/api/risk`, and `/api/reports` with `/v1/*` control-plane calls.
- Replaced dangerous-action boolean success handling with command ack result handling. The UI now shows success only when all `target_nodes` have acknowledged.

## Data Quality Rendering

All read pages now carry and render the data-quality envelope:

- `data_source`
- `snapshot_id`
- `generated_at`
- `projection_lag_ms`
- `stale`
- `missing_nodes`
- `reconciliation_state`

When `stale=true`, the UI renders a visible alert banner and lists missing nodes.

## Tests Added or Rewritten

- `control-plane dashboard contracts`: verifies `/v1/accounts`, `/v1/nodes`, `/v1/orders`, `/v1/positions`, `/v1/trades`, `/v1/messages`, and `/v1/risk/state` are used and validates required fields from the frozen `data_quality_envelope_v1` schema.
- `real empty state`: verifies empty arrays plus zero balances render as an explicit control-plane empty state and do not show seeded symbols.
- `stale UI`: verifies stale banner, missing nodes, and degraded reconciliation state are visible.
- `partial ack`: verifies a dangerous command with only one of two node acks does not show success and instead lists the pending node.

## Grep Evidence

Command:

```bash
grep -RinE "mockData|fixture|fake|dummy" src | grep -vE "__tests__|test|mock-control-plane" || echo "no prod fake hits"
```

Output:

```text
no prod fake hits
```

Additional production build/source check:

```bash
grep -RinE "mockData|fixture|fake|dummy|hardcoded" dist src | grep -vE "src/__tests__|src/test|mock-control-plane" || echo "no prod fake hits in dist/src"
```

Output:

```text
no prod fake hits in dist/src
```

## Verification

```text
npm ci || npm install
added 174 packages in 2s
```

```text
npm run test
Test Files  2 passed (2)
Tests       7 passed (7)
```

```text
npm run build
tsc --noEmit && vite build
1676 modules transformed.
✓ built in 621ms
```

## Control-Plane Assumptions

- Read endpoints return the `data_quality_envelope_v1` fields at top level with read-model arrays such as `accounts`, `nodes`, `orders`, `positions`, `trades`, `messages`, `decisions`, and `pair_locks`.
- Dashboard command issue endpoint is `POST /v1/commands`, returning `command_id`, `target_nodes`, `acks`, and `status`.
- Ack status values treated as complete are `acked`, `accepted`, `completed`, and `succeeded`; failed statuses are `failed`, `rejected`, and `error`.
- Daily report endpoints are assumed under `/v1/reports/daily/{date}` and related subpaths.

Contract gaps are recorded in `docs/handoff/window-b/CONTRACT-CHANGE-REQUESTS.md`.
