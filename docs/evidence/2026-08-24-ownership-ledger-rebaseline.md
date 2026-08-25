# Ownership Ledger Rebaseline Evidence - 2026-08-24

## Mechanism

- Rebaseline markers use `execution_events.event_type =
  'OwnershipRebaseline'`.
- Markers keep `client_order_id` empty and carry
  `ownership_schema_version = 'ownership-rebaseline/v1'`.
- Robot ownership is:
  `latest baseline quantity + B-prefix fills strictly after the marker
  (ts_event, created_at, event_id)`.
- Manual attribution starts from the marker's adjudicated manual quantity
  and adds recognized `aos_` and `stToAg_` fills after the marker.
- RESUME target-symbol flat checks, canary target-symbol flat checks, and
  protection watchdog position discovery use the shared rebased ledger.
- The release builder now carries the ledger module, audit CLI, and
  protection watchdog source.

## GOOGL Production Action

| Field | Evidence |
| --- | --- |
| Account | `account-a` |
| Symbol | `GOOGLUSDT` |
| Event type | `OwnershipRebaseline` |
| Event ID | `ownership-rebaseline:ownership-rebaseline-account-a-googlusdt-20260824` |
| Event time | `2026-08-24 15:44:17.1781 UTC` |
| Baseline robot quantity | `0` |
| Exchange quantity | `LONG 14.57` |
| Manual quantity | `14.57` |
| Raw database B ledger | `+0.89`, 22 fill events |
| User historical reconstruction | `-30.07` |
| Reason | `user-adjudicated manual domain 2026-08-24` |
| Active robot GOOGL orders | `0` |
| Preserved manual order | `stToAg_OTO_631848891_2`, reduce-only SELL stop, quantity `14.57` |

The production transaction inserted one typed execution marker and one
matching `audit_events` row. The marker has an empty `client_order_id`.
The post-write ledger result is:

```json
{
  "account_id": "account-a",
  "symbol": "GOOGLUSDT",
  "baseline_quantity": "0",
  "post_baseline_fill_quantity": "0",
  "robot_owned_quantity": "0",
  "fill_event_count": 0
}
```

The `+0.89` database reconstruction and the user-provided `-30.07`
reconstruction are independent evidence of incomplete mixed-period history.
The adjudicated baseline is the ownership authority from the marker onward.

## Fleet Audit

Full row-level artifacts:

- `docs/evidence/2026-08-24-ownership-ledger-audit-pre.json`
  - SHA-256:
    `c8378dab83573edd80a5a985003431b37a7ff3df3350e47a25a503bed220dbe9`
- `docs/evidence/2026-08-24-ownership-ledger-audit-post.json`
  - SHA-256:
    `15fddeae1e389de70493ec94b67dfce947b73013ce8efffe10565d23d9aa6b9c`

| Account | Audited symbols | Pre mismatches | Post mismatches | Status |
| --- | ---: | ---: | ---: | --- |
| account-a | 27 | 7 | 6 | ACTIVE |
| account-b | 5 | 0 | 0 | ACTIVE |
| account-c | 0 | 0 | 0 | ACTIVE |
| account-d | 0 | 0 | 0 | ACTIVE |

GOOGL changed from a `-2.16` difference to a `0.00` difference. The six
remaining rows retain their existing ledgers pending per-fill adjudication:

| Account | Symbol | Rebased robot | Exchange | Manual attributed | Expected robot | Difference | Manual events | Unknown events |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| account-a | BNBUSDT | -7.02 | 0 | 0 | 0 | -7.02 | 0 | 0 |
| account-a | BTCUSDT | 0.1072 | -0.100 | -0.017 | -0.083 | 0.1902 | 84 | 6 |
| account-a | ETHUSDT | 3.931 | 0 | -3.911 | 3.911 | 0.020 | 109 | 5 |
| account-a | MUUSDT | 7.95 | 0 | -2.87 | 2.87 | 5.08 | 82 | 21 |
| account-a | SNDKUSDT | 0 | 0 | 2.42 | -2.42 | 2.42 | 124 | 22 |
| account-a | SPCXUSDT | 0 | 42.76 | 173.60 | -130.84 | 130.84 | 10 | 36 |

`BNBUSDT` is the strongest stale robot-ledger candidate: exchange quantity,
manual attribution, and unknown event counts are all zero while the B ledger
is `-7.02`. BTC, ETH, and MU have mixed manual/unknown history. SNDK and SPCX
show manual-attribution history inconsistent with current exchange state.
All six remain unchanged.

## Deployment Evidence

- Runtime backup:
  `/srv/trader-v3/backups/ownership-rebaseline-20260824T1544Z`
- Runtime release reported by all nodes: `9401c478c02d`
- Operator-query old PID: `830423`
- Operator-query new PID: `3700434`
- Operator-query current state: `active/running`, `NRestarts=0`
- Operator-query `/openapi.json`: HTTP `200`
- Node ready endpoints `8081` through `8084`: HTTP `200`
- Open production incidents: `0`
- `operator_command` rows after `2026-08-24 15:40:00 UTC`: `0`
- Exchange evidence source: `node_heartbeats` snapshots
- HK operations: `0`
- Node restarts: `0`

Runtime source SHA-256 values:

| Runtime file | SHA-256 |
| --- | --- |
| `packages/execution-domain/execution_domain/ownership_ledger.py` | `d02b9f68ab7dd3f0b5caabbd57e5905c15f9ebdbd04ec6cdb79e67384860cfcd` |
| `services/control-plane/api/read_api.py` | `b51401d7b56785da4c2cabde4744e286934558a4135b3b7f4762ab6b5bef6d33` |
| `services/control-plane/order_management/protection_watchdog.py` | `986d6e5d2a1aefe97a22aff0a335730e7cf55ec1aeaebaf0beacad10e3711ac5` |
| `scripts/ownership_ledger.py` | `6bb695fa71cd83f36f6cf72e266aab376f27f3c6a66ce252d29f38ff9f10f1da` |

The operator-query restart began at `15:46:19 UTC`. Its existing stream
connection held shutdown for the configured 15-second timeout, and systemd
replaced the old process at `15:46:34 UTC`. The new process completed startup
at `15:46:35 UTC`. Node heartbeats advanced across samples at `15:47:40` and
`15:47:46 UTC`; all four nodes stayed ACTIVE with heartbeat ages below 2
seconds.

## Test Evidence

| Suite | Result |
| --- | --- |
| Ownership rebaseline DB tests | `5 passed` |
| Live safety gate API tests | `102 passed` |
| Protection watchdog tests | `3 passed` |
| Migration/schema/operator-query privilege tests | `15 passed` |
| Account-stall release mapping tests | `22 passed` |
| Full deployment gate after final release mapping changes | `715 passed, 2 skipped, 65 subtests passed` |
| Static checks | `compileall` and `git diff --check` passed |

## Changed Files

- `packages/execution-domain/execution_domain/ownership_ledger.py`
- `scripts/ownership_ledger.py`
- `scripts/make_account_stall_release.py`
- `services/control-plane/api/read_api.py`
- `services/control-plane/order_management/protection_watchdog.py`
- `tests/control-plane/db/test_ownership_rebaseline.py`
- `tests/control-plane/api/test_live_safety_gates.py`
- `tests/order_management/projections/test_protection_watchdog.py`
- `tests/deployment/test_make_account_stall_release.py`
- `docs/evidence/2026-08-24-ownership-ledger-audit-pre.json`
- `docs/evidence/2026-08-24-ownership-ledger-audit-post.json`
- `docs/evidence/2026-08-24-ownership-ledger-rebaseline.md`

## Remaining Risks

- The six reported account-a differences require per-fill adjudication.
- The watchdog will treat each nonzero rebased B ledger as robot-owned when
  its updated source is loaded by a future watchdog process.
- Production currently contains a source hotpatch while node release IDs
  remain `9401c478c02d`. The formal release mapping includes every new file
  for the next immutable release.
- Manual attribution quality depends on retained events and recognized
  `aos_`/`stToAg_` prefixes. Unknown historical events remain visible in the
  audit rows.
- Operator-query shutdown can consume its full 15-second timeout while a
  stream connection is open. The node-control heartbeat path remained live
  throughout this restart.
