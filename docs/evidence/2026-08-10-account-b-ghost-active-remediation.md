# Account-B Ghost ACTIVE Remediation

- Date: 2026-08-10
- Production target: HK `/srv/trader-v3`
- Account: `account-b`
- New opening trade: none
- Risk-reducing exchange mutation: one stale regular order canceled

## Finding

At `2026-08-10T03:40:21Z`, the node and control-plane state disagreed:

| Surface | Observed state |
|---|---|
| Docker container | `trader-v3-node-b`, exited with code 137 at `2026-08-08T07:26:57.960124379Z` |
| Docker OOM flag | `false` |
| Docker restart policy | `unless-stopped` |
| Docker daemon decision | restart canceled with `hasBeenManuallyStopped=true` |
| Last database heartbeat | `2026-08-06T12:40:27.955870Z` |
| Last reported trading state | `ACTIVE` |
| Last reported readiness | `true` |
| Process/loss health | absent |
| Runtime initial state | `HALTED` |

The exit-137 event came through a manual stop/kill path. Docker suppressed the
restart because the container had been manually stopped. The database and
`/v1/nodes` continued presenting the last reported ACTIVE/readiness values
without a current operational-state field.

The decision gateway also used the fleet-wide maximum heartbeat timestamp for
projection freshness. A fresh heartbeat from `account-a` could therefore mask a
stale `account-b` heartbeat after `account-b` received an explicit ACTIVE risk
state.

## Exchange Exposure

A fresh signed read found:

- Non-zero positions: `0`
- Regular orders: `1`
- Algo orders: `0`

The remaining regular order was:

| Field | Value |
|---|---|
| Symbol | `SPCXUSDT` |
| Position side | `SHORT` |
| Side/type | `SELL LIMIT` |
| Quantity/price | `15.54 @ 193` |
| Reduce-only | `false` |
| Status | `NEW` |
| Created | `2026-06-30T16:11:30Z` |
| Venue order ID | `620212860` |

No position existed for the order. Its opening-risk direction, age, dead node,
and absent protection relationship made exact cancellation the smallest
risk-reducing action.

## Production Remediation

At `2026-08-10T03:43:56Z`, the risk-admin API recorded a completed HALT command:

- Command ID: `538e24a6-8e2a-4773-bd2f-9bd68db10295`
- Account: `account-b`
- Instruments: `BTCUSDT`, `ETHUSDT`, `SOLUSDT`
- Result: three `risk_state` rows set to `HALTED`
- Audit event: durable `operator_command` event written

The exact stale order was then canceled through a signed exchange request:

- Cancel result: `CANCELED`
- Executed quantity: `0.00`
- No other order identity was mutated

The post-cancel exchange read and the recorder mirror both converged to:

- Non-zero positions: `0`
- Regular orders: `0`
- Algo orders: `0`

The mirror persisted the zero state at `2026-08-10T03:44:41.963219Z`.

## Code Remediation

The following commits prevent recurrence:

| Commit | Effect |
|---|---|
| `9115f84` | Projection freshness is scoped to the target account, so another account cannot mask a stale node |
| `5474040` | Actor cleanup failures retain the namespace lease until retry succeeds |
| `9ef98fb` | `/v1/nodes` exposes reported and effective availability separately; canary polling consumes stale/ineligible fields |
| `a0164e0` | Introduces the shared runtime-resource contract; follow-up consumer/dependency closure remains in progress after reviewer findings |

The effective availability policy is:

- Heartbeat stale: `operational_state=OFFLINE`,
  `effective_readiness=false`, `admission_eligible=false`.
- Fresh ACTIVE with readiness missing: telemetry remains soft and admission can
  continue.
- Fresh ACTIVE with readiness explicitly false: admission is ineligible.
- `trading_state`, `status`, and `readiness` remain the last reported values for
  audit and canary completion correctness.

## Verification

- Gateway risk suite: `33 passed`
- Control-plane API suite: `87 passed`
- Account-A HTTP adapter suite: `206 passed`
- Actor cleanup focused suite: `157 passed`
- Actor cleanup repeat loop: `150 passed`
- Runtime-resource contract: `65 passed`
- Node GAP-1 suite: `60 passed`
- Nautilus runtime suite: `310 passed`
- Deployment suite after runtime contract: `607 passed`
- Independent Codex reviews:
  - account-scoped freshness: `P0=0 P1=0 P2=0`
  - actor cleanup fencing: `P0=0 P1=0 P2=0`
  - effective availability and canary consumer: `P0=0 P1=0 P2=0`

The runtime-resource batch produced green tests but failed its independent
architecture review with two P1 findings: several validated resource fields did
not yet drive runtime consumers, and the imported release-manifest surface
referenced missing Phase C/D artifacts. That batch remains outside production
until the follow-up commit closes both findings.

## Production Read-API Rollout

Commit `9ef98fb` was deployed at `2026-08-10T04:15:45Z` under the shared
account-stall operation lock.

- Previous production SHA matched the exact pre-change repository byte stream:
  `1247ce472505addb71ce5323462824165ce9cb2694c185cc184ad3e3187ae894`.
- New production read-api SHA:
  `4226533812c96b267c929246752f4a7036cccfed21a9e7b7a862c3ab2d039026`.
- Backup:
  `/srv/trader-v3/backups/account-b-ghost-active-20260810T041545Z`.
- Immutable future tool bundle:
  `/srv/trader-v3/account-a-canary/releases/account-a-canary-tools-9ef98fb-20260810T041545Z`.
- Bundle manifest SHA:
  `7a0eccfa75f45ac6cc3a1b0d9d5736ccd6f622f899981c499ef948415a737d95`.
- Bundle remains `live_authorized=false`.

Post-deploy `/v1/nodes` returned:

| Account | Reported state | Operational state | Heartbeat stale | Effective readiness | Admission eligible |
|---|---|---|---:|---:|---:|
| `account-a` | `HALTED` | `ONLINE` | false | true | false |
| `account-b` | `ACTIVE` | `OFFLINE` | true | false | false |

The top-level envelope now lists `nautilus-node-account-b` in
`missing_nodes`. The exchange mirror remained at zero positions, zero regular
orders, and zero algo orders; all three account-b risk-state rows remained
HALTED.

The initial control-plane restart exposed a separate graceful-shutdown defect:
systemd waited 90 seconds, then sent SIGKILL to the uvicorn process tree. The
follow-up rollout below replaced that behavior with a bounded Uvicorn shutdown
deadline and production-verified restart acceptance.

No new real opening trade was performed during this remediation.

## Control-Plane Restart Closure

The follow-up used a strict two-file deployment:

- `services/control-plane/api/read_api.py`
- `infra/systemd/trader-v3-controlplane.service`

The final production bytes are:

| Artifact | SHA-256 |
|---|---|
| Read API | `953a429f3290e64409784e1fc9ce5e69f1fbff2eb94646ff313fdae7769ee943` |
| systemd unit | `9a010de0d2486b37669ec8b293102aff7230086961ecb1aeec2478ef36d448c9` |
| Deployment script | `e9e7ba7f01bfd80bc092ba741ed37348cf0d89b76bfbd58881f22fbe70a55d96` |

The captured command transcript is
[`raw/2026-08-10-control-plane-two-file-rollout.txt`](raw/2026-08-10-control-plane-two-file-rollout.txt).

The rollout exposed and closed two deployment-script gaps:

1. The bootstrap restart returned before the proxy and direct SSE contracts
   recovered. Commit `ad08332` added a bounded readiness window.
2. An explicit helper failure could exit without entering the `ERR` trap.
   Commit `07abd32` routed mutation-time `die` calls and the trap through one
   idempotent failure handler, and applied the readiness window after both
   restarts.

Production attempts:

| UTC | Result | Evidence |
|---|---|---|
| `2026-08-10T09:45:22Z` | Bootstrap completed in `0.235s`; immediate probe observed the startup `502` window | Backup `/srv/trader-v3/backups/control-plane-read-api-20260810T094522Z` |
| `2026-08-10T09:59:18Z` | Bootstrap completed in `0.194s`; SSE validation completed in `10.242s`; final immediate probe observed `502` | Backup `/srv/trader-v3/backups/control-plane-read-api-20260810T095918Z` |
| `2026-08-10T10:07:59Z` | Final attempt started; final process startup completed at `2026-08-10T10:08:16Z` | Backup `/srv/trader-v3/backups/control-plane-read-api-20260810T100759Z` |

Final acceptance:

- Bootstrap restart: `0.169s`.
- Authenticated Caddy SSE was active before the validation restart.
- SSE validation restart: `10.153s`.
- Both readiness windows converged to proxy `200` and direct anonymous `401`.
- Uvicorn canceled one running SSE task at its configured 10-second
  application shutdown deadline.
- systemd completed the restart without stop timeout, SIGKILL, or
  `status=9/KILL`.
- Service state: `active`, `enabled`; final PID `3738391`; automatic restart
  count `0`.
- Account-A readiness: `ready=true`, `trading_state=HALTED`,
  `halt_reason=operator_command`.
- Account-A mirror at `2026-08-10T10:08:45.156259Z`:
  `3` positions, `20` regular orders, `33` algo orders.
- Host observation: about `4.9 GiB` available memory, `820 MiB / 8 GiB` swap
  used, Redis `690.26 MiB / 2 GiB`, `30,879` keys.

Verification:

- Focused two-file deployment suite: `23 passed`.
- Full deployment suite: `675 passed, 20 subtests passed`.
- Bash syntax, Python compilation, and `git diff --check`: passed.
- Independent Codex reviewer: PASS after each production-discovered delta.

This rollout script targets only the isolated control-plane read API and its
systemd unit. It contains no order, position, risk-state, or node-container
mutation command. The observed account-a mirror counts were `3 / 20 / 33`
before and after the final rollout. The node runtime rollout and real
small-trade canary remain deferred; the next window must recapture and freeze
the full target-symbol and non-target portfolio signature.
