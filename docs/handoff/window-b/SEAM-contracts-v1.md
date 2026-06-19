# Window B ⇄ control-plane seam (contracts-v1)

**Status:** proposal by window B, to be satisfied by window A's `services/control-plane`
and arbitrated/locked by window C (step "锁定 contracts-v1").

Window B owns the **execution side** (Nautilus nodes) and the **dashboard frontend**.
It does **not** own `packages/contracts`, `services/control-plane`, or `db/migrations`
(window A). So window B builds against the interface defined here, backed by
**mocks/contract-tests using the frozen schemas** in
`tests/nautilus/_fixtures/contracts-v1/`. Any field B finds insufficient is raised
as a `contract-change-request`, never a silent divergence (PLAN global constraints).

```
                ┌───────────────────────── control-plane (window A) ─────────────────────────┐
   intents  ───▶│  outbox/stream of ApprovedTradeIntentV1   (B consumes, acks)                │
   events   ◀───│  execution-events sink (ExecutionEventEnvelopeV1, idempotent by event_id)   │
   commands ───▶│  node command channel (HALT/REDUCING/cancel-all/close-all, B acks)          │
   reads    ◀───│  dashboard read API (read models + data-quality envelope §2.2)              │
                └─────────────────────────────────────────────────────────────────────────────┘
        ▲ Nautilus nodes (B)                                   ▲ dashboard frontend (B)
```

All bodies are JSON. All responses carry `schema_version`. The control-plane is the
**only** application query source (PLAN constraint #4): the node and the dashboard
never read Nautilus Cache, Redis, SQLite, or the exchange directly.

---

## 1. Inbound — ApprovedTradeIntentV1 (control-plane → node)

Window B consumes approved intents **per account/node** with a durable cursor so a
node restart resumes from the last safe offset without re-executing (PLAN B.4).

**Interface B codes against** (`ControlPlaneIntentSource`):

- `fetch(account_id, after_cursor, limit) -> { items: [{ cursor, intent: ApprovedTradeIntentV1 }], next_cursor }`
- `ack(account_id, node_id, intent_id, status, detail?) -> 204`
  - `status ∈ { received, accepted, rejected, executed, expired, duplicate, failed }`

**Required control-plane endpoints (proposed):**

| Method | Path | Notes |
|---|---|---|
| `GET` | `/v1/nodes/{node_id}/intents?account_id=&after=&limit=` | monotonic `cursor`; only intents routed to this account/node; long-poll optional via `?wait_ms=` |
| `POST` | `/v1/nodes/{node_id}/intents/{intent_id}/ack` | body `{ account_id, status, detail? }`; idempotent |

**Node-side invariants** (enforced in B, independent of transport):

- validate `schema_version == "1.0"` and full schema (reject → `ack(rejected)`);
- validate routing: `intent.account_id` must equal this node's account, else `ack(rejected, wrong_account)`;
- validate `valid_until` (expired → `ack(expired)`, never execute);
- de-dup by `(intent_id, account_id)` **and** `idempotency_key` before publishing to Nautilus;
- only after a durable local record is the cursor advanced.

> **Open contract question (→ window A / C):** push (SSE / Redis stream) vs. pull
> (cursor) delivery. B implements the pull `ControlPlaneIntentSource` first
> (durable, restart-safe) and keeps the transport behind the interface so an SSE
> adapter can be added without touching the strategy.

## 2. Outbound — ExecutionEventEnvelopeV1 (node → control-plane)

The Projection Actor (B-07) emits one envelope per Nautilus order/position/account
event. Delivery is **at-least-once with idempotent apply** keyed on `event_id`
(PLAN §3.3, B.7).

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/nodes/{node_id}/execution-events` | body `{ events: ExecutionEventEnvelopeV1[] }`; returns `{ acked_event_ids: [...] }`; safe to resend; only acked ids leave the local spool |
| `POST` | `/v1/nodes/{node_id}/heartbeat` | body `{ ts, trading_state, readiness, projection_lag_ms, reconciliation_state, last_event_id }` |

The control-plane MUST upsert by `event_id` so duplicate WS events and reconciliation
replays do not double-apply to `orders/positions/accounts` projections.

## 3. Commands — control-plane → node (B acks)

Operator/kill-switch actions are issued by the control-plane and **must be acked by
every target node** before the dashboard reports success (PLAN B.3, B.8).

| Method | Path | Notes |
|---|---|---|
| `GET` | `/v1/nodes/{node_id}/commands?after=` | pending commands `{ command_id, type, args, issued_at }` |
| `POST` | `/v1/nodes/{node_id}/commands/{command_id}/ack` | body `{ status, result?, error? }` |

`type ∈ { halt, resume, set_reducing, cancel_all, close_all }`.
- `halt` → `TradingState.HALTED`; cancels still allowed.
- `close_all` → `REDUCING` → cancel all working orders → reduce-only close each position → wait for fills → ack with final state.
- All command handling is idempotent on `command_id`.

## 4. Dashboard read API (control-plane → dashboard frontend)

Every read response is wrapped with the **data-quality envelope** (§2.2,
`data_quality_envelope_v1.schema.json`). Empty real state returns empty arrays +
zeroed balances with `data_source`, never fixtures (PLAN constraint #7).

Read models the dashboard renders (B-09 / PLAN B.8):

- `GET /v1/accounts` · `GET /v1/accounts/{id}` (balance/equity/margin)
- `GET /v1/nodes` (status, trading_state, readiness, heartbeat age)
- `GET /v1/orders?account_id=&status=` · `GET /v1/positions?account_id=`
- `GET /v1/trades?account_id=`
- `GET /v1/messages?...` (raw message + media) · `GET /v1/decisions/{id}` (Hermes decision + evidence)
- `GET /v1/risk/decisions` · `GET /v1/risk/state`
- `GET /v1/trace/{intent_id}` (raw_message → decision → risk → intent → orders → events)
- `GET /v1/stream` (SSE) for live projection updates + `stale` banner transitions

Command-issuing dashboard endpoints proxy section 3 and return a request id; the UI
polls/streams until **all** target-node acks arrive (no optimistic success).

## 5. Auth & roles (PLAN A.9 — defined by window A, consumed by B)

- Node → control-plane uses role **`nautilus_node`**: may write execution events,
  heartbeat, command acks **only**. Token injected via secret file/env; never in code,
  DB, or API responses.
- Dashboard users: `viewer | reviewer | risk_admin | system_observer`. Dangerous
  commands require `risk_admin`, request id, reason, second confirmation, audit.
- No `test-*-token` fallback; missing secret = startup failure (mirrors A.9 on the node side too).

## 6. What window B delivers to window C

- `packages/execution-domain` — typed models for the inbound/outbound contracts +
  `ControlPlaneClient` interface (the only seam the node/dashboard use).
- Contract tests asserting B's models ↔ `contracts-v1` fixtures.
- A mock control-plane (test double implementing §1–§4) used by node and dashboard tests.
- This document, listing every endpoint/field B depends on, so window A can satisfy
  it and window C can lock `contracts-v1`.

## 7. Tracking divergences

Open items that window C must resolve are listed in
`docs/handoff/window-b/CONTRACT-CHANGE-REQUESTS.md` (created when the first real gap
is found). As of foundation: (1) intent delivery transport (pull vs push);
(2) command delivery mechanism (poll vs push); (3) exact `payload` summary shape per
Nautilus `event_type` (B will publish its event-mapping catalog in B-07).
