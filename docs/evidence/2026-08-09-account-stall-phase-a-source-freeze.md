# Account Stall Phase A Source Freeze

Date: 2026-08-09
Production state: `HALTED / NO-GO`

## Worktrees

| Role | Path | Branch | HEAD |
|---|---|---|---|
| Evidence source | `/Users/balen/projects/trader-bot` | `codex/account-stall-hardening` | `7368641e98c410b3c6edbe4ab3ea8f72cf5efe1d` |
| Phase A integration | `/Users/balen/projects/trader-bot-account-stall-phase-a` | `codex/account-stall-phase-a` | `7368641e98c410b3c6edbe4ab3ea8f72cf5efe1d` |

The evidence source contains the uncommitted implementation accumulated before
the meta-review. The integration worktree started clean from the same HEAD.
Files move into the integration worktree only through an explicit in-scope
manifest and hash verification.

## Frozen Source Snapshot

The source snapshot was taken after the meta-review documents were added. It
contains 169 directory-collapsed status entries. The earlier test-baseline
document records 168 entries from the immediately preceding snapshot; the
additional entry is the new evidence directory entry.

| Artifact | SHA-256 | Meaning |
|---|---|---|
| `/tmp/account-stall-phase-a-status.z` | `5f7c58290fac0a6337e6390ea8b81f64ad347a76d22b2f273da4a01eeca198a0` | NUL-delimited `git status --porcelain=v1` |
| `/tmp/account-stall-phase-a-status.tsv` | `2f5e62b313956fb7dbad6ae5a665e033cf95de17a06d121f84d6a17eab7a4941` | Sorted human-readable status inventory |
| normalized `git status --porcelain=v1 --untracked-files=normal` | `843168a314f8e03790aed3ef836cc332468d2bdb1cba52c700f4fadfd5d484b1` | Sorted command output using Git's directory-collapsed display |
| `docs/evidence/inventory/2026-08-09-account-stall-source-status-all.txt` | `56788ad7e1c57af4cc8e806dff7b36b1703db0d27d7781b56ec775bf24a690fd` | 373 sorted status entries using `--untracked-files=all` |
| `/tmp/account-stall-phase-a-tracked.patch` | `19ac81ad6160e15caf553be1a6af54f31be8e75f5678c96dffa34bbfcda2c106` | Binary-safe tracked diff from HEAD |
| `/tmp/account-stall-phase-a-domain-sha256.txt` | `81a7389547d8e327067ec7bcfce360025537ae4f3172f464315173592fdcada9` | Sorted hashes under runtime, scripts, and focused tests |

The snapshot contains 61 tracked modifications and 108 untracked
directory-collapsed status entries at freeze time.

## Source Authority

- `services/` is the canonical application implementation.
- `packages/` contains shared importable domain modules.
- `scripts/` owns release and deployment tooling.
- `.live-mirror/` is production-byte evidence and a drift comparator.
- `infra/systemd/*.conf` contains concrete host resource values.
- Tests use temporary roots and explicit test mode.
- `scripts/hk-deploy-20260803.sh` currently hardcodes
  `T=/srv/trader-v3`; that production root is outside local test mutation
  scope.

## Import Rules

1. Import one domain batch at a time.
2. Record every imported path and source SHA-256.
3. Audit tracked mixed-purpose files at hunk granularity.
4. Exclude Attention, Hermes, channel strategy, and unrelated order-management
   changes.
5. Run the batch-specific collection hash and red/green command before the next
   import.
6. Preserve the original evidence worktree without cleanup or reset.

## Agent Team

| Agent | Responsibility | Write permission |
|---|---|---|
| Runtime Diagnostician | Current executor/queue/shutdown stall mechanisms and red-capable seam | Read-only |
| Runtime Resource Contract Auditor | Node/manifest optionality drift and parity matrix | Read-only |
| Scope and Integration Auditor | In-scope dependency closure and import order | Read-only |
| Historical and Evidence Auditor | Git lineage, Redis evidence, mirror drift, test reproducibility | Read-only |
| Planner/Integrator | Clean worktree, imports, implementation, verification | Integration worktree only |

Production access, restart, data mutation, deployment, and trading remain
frozen throughout Phase A.

## Runtime Validation Overlay

A separate clean validation worktree now records the bounded cleanup/fencing
fix:

| Worktree | Branch | Commit |
|---|---|---|
| `/Users/balen/projects/trader-bot-account-stall-runtime-validation` | `codex/account-stall-runtime-validation` | `ffc14e559afdf7b56bf245d8cadbea1d3013e609` |

The overlay passed the 104-test runtime selection and 50 independent
fault-injection processes. Its source and test dependency closure differs from
the Phase A branch by 15,543 insertions and 2,274 deletions across the seven
validated files. It remains evidence for A7 planning and is not a release
source.

Exact commands, hashes, semantics, and reviewer results are recorded in
`docs/evidence/2026-08-09-account-stall-runtime-validation.md`.

## Local Deployment Entry Boundary

Phase A excludes the complete `scripts/hk-deploy-20260803.sh` entrypoint from
local execution.

The script hardcodes `/srv/trader-v3` and performs host-wide mutations through
`systemctl`, Docker, PostgreSQL, control-plane commands, backups, and in-place
file replacement. A local `TRADER_ROOT` override would leave the remaining
host mutation interfaces active and would not provide a safe test seam.

Local tests may inspect the script as text or execute separately extracted
pure helpers with temporary roots. They must not invoke the complete script.
The immutable deployment path introduced in later phases must accept an
explicit validated `release_root`; the legacy script remains production
evidence and a rollback reference.

This decision satisfies the Phase A test-root isolation gate without changing
the historical production script.
