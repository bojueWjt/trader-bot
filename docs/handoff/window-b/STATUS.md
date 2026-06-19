# Window B — execution status

Authoritative ledger: `window-b-nautilus-dashboard/TaskList.json` (in the program
bundle). This file is the human-readable mirror + environment/blocker notes.
A task is `done` only with reproducible commands, outputs/report paths, and a commit
SHA (PLAN "不可伪造完成").

- **Branch:** `work/nautilus-dashboard-v3`
- **Worktree:** `/Users/pudu/projects/trader-bot-nautilus-dashboard` (isolated from
  window A, which holds `/Users/pudu/projects/trader-bot` on `work/hermes-data-v3`)
- **Base:** shared `237cabc` == `plan/nautilus-hermes-v3` (window A created `plan` off
  `main`; B rebases onto `plan` if A lands a bootstrap commit there)
- **Nautilus baseline:** `1.227.0` (to be locked in B-00 against Linux/Py3.12)

## Task board

| Task | Title | Status | Notes |
|---|---|---|---|
| B-FND | Foundation (scaffold, contracts, seam, execution-domain) | in_progress | this commit |
| B-00 | Nautilus/Binance compat spike + pin | todo | **runtime-gated**: Linux+Docker+Py3.12 + Binance USDT-M Futures **testnet keys** |
| B-01 | Per-account TradingNode lifecycle | todo | code authorable now; runtime tests gated |
| B-02 | ApprovedTradeIntent CustomData + Data Client | todo | builds against §1 seam + frozen schema |
| B-03 | IntentExecutionStrategy open/add | todo | sandbox (simulated venue) testable; testnet for B-11 |
| B-04 | Stops/TPs/position updates | todo | depends B-03 |
| B-05 | RiskEngine + trading-state commands | todo | depends B-01,B-03 |
| B-06 | Persistent cache/bus + reconciliation | todo | depends B-01 |
| B-07 | Projection Actor + spool | todo | depends B-01,B-03,B-04 |
| B-08 | Two-account isolation + routing | todo | depends B-01,B-02,B-06,B-07 |
| B-09 | Dashboard → real control-plane API | todo | **locally buildable** (Node 24); FRONTEND fakes only |
| B-10 | Containers/secrets/health/repro build | todo | depends B-00,B-01,B-08,B-09 |
| B-11 | Regression + handoff | todo | depends all; testnet evidence runtime-gated |

## Environment dependencies (external, must be provisioned)

1. **Nautilus runtime host** — pudu-mini has no Docker and Python 3.14;
   NautilusTrader 1.227.0 needs Linux + Python 3.12. The node build/test/spike must
   run on a Linux+Docker host (isolated compose; `hk` runs the production trader
   stack so use a separate project/ports, or a dedicated VPS).
2. **Binance USDT-M Futures testnet API keys** — gate B-00 connection smoke and the
   B-11 testnet evidence. Until provided, those acceptance items are `needs_review`/
   `blocked`, never faked.

The dashboard track (B-09) is fully buildable/testable locally and is prioritised as
the first verifiable deliverable.

## Build/verify map

| Surface | Where it runs | Tooling |
|---|---|---|
| Dashboard (B-09) | local (pudu-mini) + CI | Node 24, vite, vitest, playwright |
| execution-domain contract tests | Linux/Py3.12 container | uv + pytest + jsonschema/pydantic |
| Nautilus node/strategy/projection | Linux/Py3.12 container | uv + pytest + Nautilus backtest/sandbox |
| testnet spike + two-node compose | Linux+Docker host w/ testnet keys | docker compose |
