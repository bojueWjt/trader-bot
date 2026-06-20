# Window B handoff → Window C

**Program:** hermes-nautilus-migration-v3 · **Window:** B (NautilusTrader execution layer + real Dashboard)
**Branch:** `work/nautilus-dashboard-v3` (base `plan/nautilus-hermes-v3` == initial `237cabc`)
**Status:** all 12 P0 tasks implemented and verified; `B_READY=1` set in
`trader-bot-window-c-staging/GO-SIGNAL.txt`.

## 1. Summary

Window B builds the half-link **approved intent → execution engine → real state on the
dashboard**: NautilusTrader `1.227.0` per-account live nodes, the Binance USDT-M Futures
adapter, the ApprovedTradeIntent CustomData path, the IntentExecutionStrategy, the
RiskEngine + trading-state commands, persistence/reconciliation, the Execution Projection
Actor, two-account isolation, containers, and the de-faked dashboard.

Iron rules upheld: Hermes is the only semantic processor (no semantic regex in B — B never
reads Telegram text); control-plane/PostgreSQL is the only application query source; live is
default-OFF (every node boots `TradingState.HALTED`, `environment=testnet`); fail-closed on
missing secrets / dependencies; full traceability via `intent_id` in client order id + tags.

## 2. Verification environment

- **Nautilus runtime:** `1.227.0` pinned, validated on **hk** (Debian 13, Python 3.12.13) in
  an isolated dir `/srv/hermes-nautilus-b` + image `nautilus-spike:1.227.0` — **never touches
  the live trader stack**. Base image digest pinned:
  `python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9`.
- **Dashboard:** Node 24 local (pudu-mini).
- **Full Python regression (hk container, nautilus 1.227.0):** `pytest tests/nautilus
  tests/execution` → **84 passed / 11 skipped / 0 failed**. The 11 skips are running-node /
  real-Redis / testnet integration tests (see §6).
- **Dashboard (local):** `vitest` 7 passed, `tsc --noEmit && vite build` green, production
  bundle fake-data scan clean.

## 3. Delivered (per task, with evidence)

| Task | Commit | Evidence |
|---|---|---|
| B-FND foundation | `58a0904` | contracts-v1 fixtures, SEAM doc, execution-domain types + ControlPlaneClient Protocols |
| B-00 spike + version pin | `32e35e1` | 1.227.0 imports on hk; Binance USDM capability matrix; hash-locked deps; base digest pinned |
| B-01 per-account node lifecycle | `9245c4d` | hk pytest 4/4; fail-closed creds; `BinanceAccountType.USDT_FUTURES`+`BinanceEnvironment.TESTNET` |
| B-02 intent CustomData + data client | `8ff1d2e` | hk 15 passed; DataType(class) API fixed via hk |
| B-03 strategy open/add | `d6effd2` | hk 10 passed; StrategyConfig(msgspec) + read-only cache fixed via hk |
| B-04 stops/TPs/position mgmt | `d470980` | hk 21 passed; OrderFactory/Strategy order API introspection-confirmed |
| B-05 RiskEngine + commands | `cf6c21c` | hk 12 passed; LiveRiskEngineConfig fields confirmed; close_all = REDUCING→cancel→close→ack |
| B-06 persistence + reconciliation | `81f356a` | hk 15 passed; per-account Redis prefix; reconciliation config |
| B-07 projection actor + spool | `8401a86` | hk 6 passed; deterministic event_id (business-key hash, not event.id); ordered spool |
| B-08 two-account isolation/routing | `7db40e3` | hk 7 passed; `docker compose config` OK; fail-closed routing |
| B-09 dashboard → control-plane API | `8bf28ae` | local vitest 7/7 + build; no prod fake hits |
| B-10 node image + assembly | `496330d` | node assembles to HALTED/testnet from source AND built image; clean build from lockfile; compose OK; no plaintext secrets |
| regression fixes | `9cec593` | full hk suite 84/0 |

The node assembly was proven on hk: `python -m app.run_node --config account-a --dry-run
--build-trading-node` (redis reachable) → `assembled account-a … environment=testnet
trading_state=HALTED`, both from source and from the built production image.

## 4. The A↔B seam (what B needs from window A's control-plane)

Authoritative: `docs/handoff/window-b/SEAM-contracts-v1.md`. B builds against it with
contract-mocks; window A must satisfy these (or window C reconciles). Summary:

- **Inbound** `ApprovedTradeIntentV1`: pull cursor `GET /v1/nodes/{node_id}/intents?account_id=&after=`
  + idempotent `POST /v1/nodes/{node_id}/intents/{intent_id}/ack`.
- **Outbound** `ExecutionEventEnvelopeV1`: `POST /v1/nodes/{node_id}/execution-events`
  (idempotent on `event_id`) + `POST /v1/nodes/{node_id}/heartbeat`.
- **Commands**: `GET /v1/nodes/{node_id}/commands` + `POST …/{command_id}/ack`
  (`halt|resume|set_reducing|cancel_all|close_all`).
- **Dashboard reads**: `GET /v1/{accounts,nodes,orders,positions,trades,messages,decisions,
  risk/state,risk/decisions,trace/{intent_id},stream}` — each wrapped in the data-quality
  envelope (§2.2). Auth role `nautilus_node` (write events/heartbeat/ack only).

Frozen schemas mirrored in `tests/nautilus/_fixtures/contracts-v1/`. B's typed view +
control-plane client interface live in `packages/execution-domain/` (merge-safe; reconcile to
import from window A's `packages/contracts` at integration).

## 5. Open contract items (for window C to lock)

See `docs/handoff/window-b/CONTRACT-CHANGE-REQUESTS.md`. Key:
1. Intent delivery transport (pull cursor vs SSE/Redis-stream push).
2. Node command delivery (poll vs push) and exact ack detail strings.
3. Dashboard-facing command endpoint + daily report endpoints (B-09 assumed `POST /v1/commands`
   and `/v1/reports/daily/{date}` — confirm/lock).
4. Per-`event_type` `payload` summary shape (B-07 event-mapping catalog as starting point).

## 6. Residual risks / deferred (NOT faked — explicitly skipped/needs_review)

1. **Binance testnet keys not yet provided** → B-00 testnet order/cancel/conditional smoke and
   all testnet acceptance are **`needs_review`** until keys exist. Per operator decision
   (2026-06-19) window B proceeded without keys.
2. **11 skipped integration tests** need a running node + real Redis + (some) testnet: full
   simulated-venue order submission, node-restart recovery, real reconciliation, real on_event
   wiring, two-node live isolation. These are window-C / testnet-gated.
3. **Projection event capture wiring**: `ExecutionProjectionActor` registers via
   `node.trader.add_actor` and forwards on_event; the node-wide MessageBus execution-event
   subscription (`_subscription_targets`/topic names) is marked `TODO(host-verify)` and must be
   validated against a running node in window C.
4. **Restart-persistence**: `instance_id` is omitted from `TradingNodeConfig` (nautilus
   auto-generates UUID4); per-account isolation + restart-stable Redis keys rely on
   `trader_id` prefix (`CacheConfig.use_trader_prefix`). Confirm cache-key stability across
   restart with the real node (window C restart test).
5. **IntentPublisherActor timer / publish_data** API (`Actor.clock.set_timer`, `publish_data`)
   carries `TODO(host-verify)`; assembly succeeds, runtime poll/publish to verify in window C.

## 7. Window-C integration steps

1. Merge window A (contracts/control-plane/db) then window B onto `integration/nautilus-v3`;
   reconcile `packages/execution-domain` to import window A's `packages/contracts`; lock
   `contracts-v1`.
2. Stand up control-plane + PostgreSQL + Redis; wire `infra/compose/multi-account.sandbox.yml`
   to the real control-plane service (replace the `${NAUTILUS_NODE_IMAGE}` tag with the built
   node image; provide secrets via files).
3. Resolve the open contract items (§5) and the `TODO(host-verify)` runtime items (§6.3–6.5)
   against a running node.
4. With Binance USDT-M Futures **testnet** keys: run the deferred integration suite + B-00
   testnet smoke + the C.8 testnet acceptance matrix.
5. Keep all nodes `HALTED`/testnet; release gate stays `testnet_only` until C's gates pass.

## 8. Reproduce

```bash
# Python (hk Linux+Docker+Py3.12 container, nautilus 1.227.0):
#   sync services/nautilus-node packages tests infra to the host, then:
docker run --rm -v "$PWD":/workspace -w /workspace nautilus-spike:1.227.0 sh -c \
  'pip install -q pydantic>=2; export PYTHONPATH=services/nautilus-node:packages/execution-domain:packages/nautilus-adapter RUN_HK_NAUTILUS_TESTS=1; python -m pytest tests/nautilus tests/execution -q'
# Built node image + assembly smoke (redis reachable on the same docker network):
docker build -f infra/docker/nautilus/Dockerfile -t trader-bot/nautilus-node:local .
docker run --rm --network <net-with-redis> -e BINANCE_ACCOUNT_A_API_KEY=… … \
  trader-bot/nautilus-node:local --config /cfg/account-a.sandbox.json --dry-run --build-trading-node
# Dashboard (Node 24):
cd bridge/apps/dashboard && npm ci && npm run test && npm run build
```
