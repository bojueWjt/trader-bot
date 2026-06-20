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

| Task | Title | Status | Evidence / Notes |
|---|---|---|---|
| B-FND | Foundation (scaffold, contracts, seam, execution-domain) | **done** | `58a0904` |
| B-00 | Nautilus/Binance compat spike + pin | **done*** | `32e35e1` — 1.227.0 validated on hk (Py3.12.13), capability matrix, deps hash-locked, base digest pinned. *testnet order/cancel/conditional smoke = needs_review (Binance testnet keys deferred) |
| B-01 | Per-account TradingNode lifecycle | **done** | `9245c4d` — hk container: pytest 4/4, fail-closed creds, BinanceAccountType.USDT_FUTURES + Environment.TESTNET |
| B-02 | ApprovedTradeIntent CustomData + Data Client | **done** | `8ff1d2e` — hk pytest 15 passed; DataType API bug found+fixed |
| B-03 | IntentExecutionStrategy open/add | **done** | hk pytest 10 passed/2 skipped; StrategyConfig + read-only-cache API bugs found+fixed |
| B-04 | Stops/TPs/position updates | **done** | `d470980` — hk 21 passed; OrderFactory/Strategy order API introspection-confirmed |
| B-05 | RiskEngine + trading-state commands | **done** | `cf6c21c` — hk 12 passed; LiveRiskEngineConfig fields confirmed; close_all sequence |
| B-06 | Persistent cache/bus + reconciliation | **done** | `81f356a` — hk pytest 15 passed; per-account Redis prefix + reconciliation config |
| B-07 | Projection Actor + spool | **done** | `8401a86` — hk 6 passed; deterministic event_id; ordered spool |
| B-08 | Two-account isolation + routing | **done** | `7db40e3` — hk 7 passed; `docker compose config` OK |
| B-09 | Dashboard → real control-plane API | **done** | `8bf28ae` — local vitest 7/7, build green, no prod fake hits (FRONTEND fakes only) |
| B-10 | Containers/secrets/health/repro build | **done** | `496330d` — node assembles to HALTED/testnet from source + built image; clean lockfile build; compose OK; no plaintext secrets |
| B-11 | Regression + handoff | **done** | `9cec593` regression (hk 84 passed/11 skipped/0 failed); `WINDOW_B_HANDOFF.md` @ repo root; `GO-SIGNAL B_READY=1` |

**Window B complete:** all 12 P0 tasks done & hk-verified. Residual = testnet-keyed acceptance
(needs_review) + 11 deferred running-node/real-Redis integration tests (window C). See
`WINDOW_B_HANDOFF.md`.

**hk runtime test loop:** isolated `/srv/hermes-nautilus-b/repo` + `nautilus-spike:1.227.0` image (nautilus + pytest; `pip install pydantic` for verification). Node tests run there per-task; window-C handoff via `trader-bot-window-c-staging/GO-SIGNAL.txt` (`B_READY=1`) only after B-11.

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
