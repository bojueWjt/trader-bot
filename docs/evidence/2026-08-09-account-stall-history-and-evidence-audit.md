# Account Stall History and Evidence Audit

Date: 2026-08-09
Mode: read-only agent audit
Production state: `HALTED / NO-GO`

## Historical Callback Lineage

Git provides commit-level evidence for the historical synchronous callback
path:

| Commit | UTC | Change |
|---|---|---|
| `9245c4d` | 2026-06-19 20:50:22 | Added synchronous `HttpControlPlaneClient._request_json`, including `urlopen` and response reads |
| `8ff1d2e` | 2026-06-19 21:08:21 | Added `ApprovedIntentDataClient.poll_once` with serial fetch, ACK, and cursor persistence |
| `496330d` | 2026-06-20 08:24:57 | Added `IntentPublisherActor` timer callback calling `poll_once` directly |
| `58e80cb` | 2026-06-20 14:09:34 | Added the two-second `CommandPollerActor` with synchronous poll, apply, and ACK |
| `6096c00` | 2026-06-21 02:10:26 | Added heartbeat to the same serial `poll_once` failure domain |
| `3ec3722` | 2026-07-11 05:49:35 | Preserved the synchronous callback path in the recorded production hotfix lineage |

Current offload code is visible in the 2026-08-09 uncommitted source worktree:

- `services/nautilus-node/app/nautilus_actors.py`
- `services/nautilus-node/runtime/control_plane_session.py`
- `services/nautilus-node/runtime/bounded_task_worker.py`
- `services/nautilus-node/runtime/intent_execution_inbox.py`

Git pickaxe searches find no commit introducing
`NodeControlPlaneSession`, `_heartbeat_executor`, or `BoundedTaskWorker`.
Filesystem birth times provide machine metadata and do not establish code
lineage. Phase A must import the offload code in domain batches with source
hashes and independent commits.

## Redis Lineage

Redis risk uses two commits:

1. `81f356a9ff1c...` introduced persistent Redis cache/message bus settings,
   trader/account prefixes, instance identity, `flush_on_start=False`, and no
   message-stream retention.
2. `496330d` connected those settings to `TradingNodeConfig` while leaving
   `TradingNodeConfig.instance_id` unset, allowing Nautilus to generate a new
   UUID4 for each process.

Evidence strength:

- Configuration origin and runtime activation: strong Git evidence.
- Random namespaces and orphan-key growth: committed incident evidence.
- Memory, swap, AOF, and I/O pressure increasing timeout probability: strong
  contributing inference.
- Redis uniquely triggering a specific actor timeout: unproven.

The incident wording therefore uses synchronous callback I/O as the historical
direct blocking mechanism and Redis unbounded growth as a strong contributing
factor.

## Source Authority

The previous `.live-mirror/README.md` declared mirror `read_api.py` to be the
deployment source and instructed direct copy/restart. The Phase A decision
retires that workflow:

- `services/` is canonical application source.
- `.live-mirror/` is frozen production-byte evidence and a drift comparator.
- Release tooling must reject mirror bytes as build or deployment source.

Current uncommitted drift includes:

| File pair | State |
|---|---|
| Hermes feeder | Source and mirror bytes equal |
| Order lifecycle monitor | Source and mirror bytes equal |
| Exchange recorder | Source and mirror bytes equal |
| Control-plane `read_api.py` | Diverged; mirror and canonical source differ substantially |

The full current `read_api.py` comparison reports 1,408 additions and 151
deletions between mirror and canonical source. Phase A treats the mirror bytes
as historical evidence and imports only canonical source behavior.

## Missing Original Production Evidence

The incident currently summarizes evidence that has not been versioned in raw,
sanitized form:

- A/B tracebacks and Docker logs
- container IDs and image digests
- host/container file SHA-256 and mountinfo
- deleted-inode output
- Redis `INFO`, `CONFIG GET`, keyspace scans, memory/swap/I/O/AOF timeline
- PostgreSQL query text, result rows, server version, and collection UTC
- original `operator_commands` rows

These gaps limit provenance strength. Production access remains frozen during
Phase A, so the plan records them as required future read-only evidence rather
than silently treating the narrative as primary data.

## Test Reproducibility Audit

The four versioned node-id inventories are sorted, unique, and hash-valid:

| Suite | Count | SHA-256 |
|---|---:|---|
| Runtime/Nautilus | 538 | `ba63a2910ff00cd7f0db6e548b40f21c112e245993bd300217ac3e752ce29b7a` |
| Release | 77 | `5a325a1096a6379e73aa445ce5a0b27864fac275dd03652bbe0ad34b4eb5cf9f` |
| Deployment | 72 | `2a33079315e1520c59d83651b375d30df08edfc6dec6915dc10f43acfaa4117e` |
| Fence/rollout | 16 | `5ce8c43f005275304392788bd001953643c90fa5687b5ea18c3d6bd82a76e5cd` |

Runtime and release overlap by 20 node IDs. Other suite pairs have no overlap.

The inventories prove selection scope. Replaying the same result still
requires:

- a versioned source overlay including untracked files
- a sanitized inherited-environment manifest
- pinned dependency artifacts and hashes
- raw collect and run stdout/stderr
- exit status and JUnit XML
- warning and skip-reason reports
- a recorded subtest plugin/version
- full-pipeline `LC_ALL=C` and `set -o pipefail`

Phase A must close these items before using test counts to authorize Phase B.
