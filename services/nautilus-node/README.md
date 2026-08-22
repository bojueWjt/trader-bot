# services/nautilus-node

One **NautilusTrader live TradingNode per trading account, one process per container**
(PLAN constraint #9). The node turns `ApprovedTradeIntentV1` (from the control-plane)
into exchange orders via the Binance USDT-M Futures adapter, and projects every
resulting order/position/account event back to the control-plane as
`ExecutionEventEnvelopeV1`.

It never interprets Telegram text, never contains semantic regex, and never reads the
exchange/Redis/SQLite from outside Nautilus. The control-plane (window A) is the only
application query source.

## Package layout (window B owned)

| Path | Responsibility | Task |
|---|---|---|
| `spike/` | compatibility spike harness (version pin, capability matrix) | B-00 |
| `config/` | per-account node config (ids, redis prefix, creds via secret) | B-01, B-06 |
| `runtime/` | TradingNode bootstrap + lifecycle (HALTED→readiness→ACTIVE, auto-halt) | B-01 |
| `data_client/` | control-plane intent source → Nautilus CustomData client | B-02 |
| `strategy/` | `IntentExecutionStrategy` (open/add + stops/TPs/position mgmt) | B-03, B-04 |
| `risk/` | RiskEngine config (notional/rate/precision) | B-05 |
| `commands/` | trading-state + kill-switch/cancel-all/close-all handlers w/ ack | B-05 |
| `persistence/` | Redis-backed cache/bus + reconciliation config | B-06 |
| `projection/` | Projection Actor + local spool (idempotent event delivery) | B-07 |
| `routing/` | account/node routing guards (no cross-account leakage) | B-08 |
| `control_plane/` | concrete `ControlPlaneClient` HTTP impl + in-memory mock | B-02+ |

The typed contracts and the `ControlPlaneClient` Protocol live in
`packages/execution-domain`; this service depends on that package and on
`nautilus_trader`.

## Lifecycle (B-01)

```
boot → HALTED
  → load instruments
  → connect Redis cache/bus           (per-account prefix)
  → startup reconciliation            (confirm robot-owned orders)
  → connect control-plane (intents/events/commands)
  → register node + first heartbeat
  → READY (still HALTED)
  → operator/control-plane → ACTIVE   (never automatic)
auto-HALT on: lost control-plane heartbeat · Redis fencing loss · durable projection egress failure
```

`cancel` is allowed in HALTED. `close_all` = REDUCING → cancel working orders →
reduce-only close each position → wait for fills → ack.

## Default-safe

All nodes start `HALTED` and `sandbox/testnet`. Real live is enabled only after window
C's gates (PLAN constraint #10). No runtime `pip install`; deps come from the lockfile
baked into the image (B-10).

## Dev / test environment

NautilusTrader `1.227.0` requires **Linux + Python 3.12** (locked in B-00). Build and
run in the container (`infra/docker/nautilus`). Pure logic and contract tests run under
`pytest`; intent→order→event flows run against Nautilus' simulated venue (sandbox);
testnet smoke needs Binance USDT-M Futures testnet keys (see `docs/handoff/window-b/STATUS.md`).

## Implementers (Codex)

Work the tasks in `window-b-nautilus-dashboard/TaskList.json` order, honour the path
ownership and iron rules in the bundle PLAN, and build against
`docs/handoff/window-b/SEAM-contracts-v1.md` + the frozen fixtures in
`tests/nautilus/_fixtures/contracts-v1/`. A task is `done` only with reproducible
commands, outputs/report paths, and a commit SHA.
