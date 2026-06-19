# Testnet Readiness Report

## Status

Local bridge readiness passes. Overall maturity is `testnet_candidate` until the operator completes the manual testnet gate items.

## Evidence

- W1 unit tests: `34 passed`.
- W1 GS-002 dry-run integration: `1 passed`.
- W1 full unit and integration: `35 passed`.
- API E2E, security, and integration: `30 passed, 1 warning`.
- Full local suite: `90 passed, 1 warning`.
- Main app bridge mount smoke: `2 passed, 1 warning`.
- Security tests: `4 passed`.
- Full-system GS-002 smoke: `1 passed, 1 warning`.
- Bearer-token auth replaced risk/audit/security/release-gate test actor headers.
- Remote Freqtrade server supports Docker and Docker Compose:
  - `Docker version 26.1.5+dfsg1`
  - `Docker Compose version 2.26.1-4`

## Bridge Coverage

- `/api/signals/status` mounted.
- `/api/dashboard/overview` mounted.
- `/api/reports/daily/{date}/markdown` mounted with deterministic fallback report.
- `/api/risk`, `/api/audit`, `/api/security`, and `/api/release-gates` mounted.
- Request id middleware echoes provided `x-request-id` and generates one when absent.
- CI security workflow runs `PYTHONPATH=apps/api pytest tests/security -q`.
- Compose defines postgres, redis, api, frontend, freqtrade-dryrun, and worker.
- Freqtrade API is bound to loopback in compose: `127.0.0.1:18082:8080`.

## Testnet Gate

- Automated unit/security/integration/E2E: pass.
- SignalStrategy loads and full GS-002 smoke: pass.
- Dashboard fake data and deterministic fallback report: pass.
- Freqtrade API local-only binding in bridge compose: pass.
- Kill-switch manual acceptance: pending operator drill.
- Exchange API key restricted to testnet: pending credential review on target deployment.

## Live Readonly Gap

Live readonly remains gated by deployment hardening:

- Durable Signal Store migration and retention policy.
- Production auth provider and token rotation beyond bridge Bearer-token scaffolding.
- Operational runbook for Hermes watcher importer environment.
- Remote compose deployment review before switching live services.

## Live Small-Size Gap

Live small-size automation remains gated by operator acceptance:

- Manual kill-switch drill.
- Exchange key custody and secret rotation process.
- Small-size risk limits approved by operator.
- Production monitoring and alert routing.
- Backout procedure verified on the remote host.
