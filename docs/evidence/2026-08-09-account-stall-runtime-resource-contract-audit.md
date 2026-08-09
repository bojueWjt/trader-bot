# Account Stall Runtime Resource Contract Audit

Date: 2026-08-09
Mode: read-only agent audit
Production state: `HALTED / NO-GO`

## Findings

### P0: Live policy is fail-open

`services/nautilus-node/config/node_config.py` enables strict runtime resources
only when all three conditions match:

- `binance.environment == "live"`
- manifest schema environment variable equals release v3
- release purpose environment variable equals account-stall hardening

Missing or different environment variables, including emergency rollback,
select the compatibility path and allow defaults.

Evidence:

- `services/nautilus-node/config/node_config.py:216`
- `services/nautilus-node/config/node_config.py:891`

### P1: Top-level optionality differs

The v1 root fields are:

```text
schema_version
redis
command_journal
control_plane_session
strategy_durable_io
terminal_exchange
reporter_workers
```

Manifest validation requires the exact seven-field set. Node compatibility mode
requires the first three groups and allows the remaining four groups to
materialize from defaults. Node strict mode requires all seven.

Evidence:

- `services/nautilus-node/config/node_config.py:511`
- `services/nautilus-node/config/node_config.py:545`
- `services/nautilus-node/config/node_config.py:866`
- `scripts/release_manifest.py:986`

### P1: Strict nested-field parity is incomplete

Node strict mode still accepts:

- unknown fields in `redis`
- unknown fields in `command_journal`
- partial and unknown fields in legacy `control_plane.session`

Manifest validation uses exact nested field sets. JSON inputs therefore receive
different verdicts even when both consumers claim strict v1 validation.

### P1: Host resources form a second drifting contract

Application runtime resources and host systemd/Docker resources are separate
contracts. The host producer, manifest verifier, and deployment test parser
currently preserve different semantics for:

- the four-artifact exact set
- consumer entrypoint, owner units, and destinations
- `docker_host_config` placement
- control-plane systemd semantic validation
- `RestartSec` and Docker projection

Evidence:

- `scripts/make_account_stall_release.py:183`
- `scripts/make_account_stall_release.py:468`
- `scripts/make_account_stall_release.py:557`
- `scripts/release_manifest.py:1913`
- `scripts/release_manifest.py:1980`
- `tests/deployment/test_account_stall_systemd_resources.py:112`

## Compatibility Matrix

| Scope | Node compatibility | Node strict | Manifest |
|---|---|---|---|
| Root | Missing document materializes defaults; four groups may be absent | Seven groups required | Seven groups exact-set |
| Redis memory ratios | Warning/degraded/critical may default | Required | Required |
| Redis unknown fields | Accepted | Accepted | Rejected |
| Command journal unknown fields | Accepted | Accepted | Rejected |
| Control-plane session group | May be absent or partial | Group and 12 fields required | Group and 12 fields exact-set |
| Legacy session | Partial/default and unknown fields accepted | Partial/default and unknown fields accepted before value comparison | Exact-set when present |
| Durable I/O, terminal, reporters | Missing groups and fields default | Exact-set | Exact-set |

For complete canonical strict input, Node dataclass values and manifest
normalized values are equivalent. Type, numeric-range, and cross-field checks
also align for:

- positive integer and number semantics
- ratio boundaries
- warning/degraded/critical ordering
- total stream capacity
- retry base/max ordering

The Python-level accepted input type still differs: Node accepts `Mapping`,
while manifest accepts `dict`. JSON file input removes this difference.

## Normalization and Hash

Manifest normalizes numbers and computes SHA-256 from canonical JSON with sorted
keys and compact separators. Node produces dataclasses and has no runtime
resource hash.

Evidence:

- `scripts/release_manifest.py:426`
- `scripts/release_manifest.py:1284`
- `scripts/release_manifest.py:1531`
- `scripts/release_manifest.py:3790`

## Phase B Interface

Recommended importable module:

```text
packages/runtime-resource-contract/
  pyproject.toml
  runtime_resource_contract/
    __init__.py
    contract.py
```

Minimal public interface:

```text
RuntimeResourcePolicy = LIVE_STRICT | COMPAT
validate_runtime_resources(
    raw,
    *,
    policy,
    legacy_control_plane_session=ABSENT,
) -> canonical dict
RuntimeResourceContractError(path, code, detail)
SCHEMA_VERSION
GROUP_SPECS
DEFAULTS
```

Node maps the shared error into `NodeConfigError` and converts the canonical
dict into existing dataclasses. Manifest maps it into
`ReleaseManifestError`. Phase B keeps canonical bytes and hash in the manifest
adapter; Phase C can move them into the shared module.

Host systemd/Docker resources remain a separate Phase C module and schema
version.

## Required Parity Tests

The Node and manifest adapters must receive the same parameterized cases:

1. Complete canonical v1, key reordering, and integer number spellings.
2. Missing and unknown root fields.
3. Every missing nested field.
4. Unknown fields in every group.
5. `bool`, string, `None`, list, and float-as-integer type mutations.
6. Zero, negative, NaN, infinity, and ratio boundaries.
7. Stream-capacity, ratio-order, and retry-order cross constraints.
8. Legacy session absent, complete, partial, mismatched, and unknown-field
   cases.
9. Live under every release environment-variable combination.
10. Sandbox/testnet compatibility defaults.
11. Emergency rollback strictness.
12. Canonical normalization and hash equivalence.

## Migration Order

1. Freeze one canonical strict fixture, normalized JSON, and hash golden.
2. Add the shared pure module and contract tests without consumer changes.
3. Connect Node and manifest adapters in one atomic change.
4. Update every image, bundle, transition, release, and attestation inventory
   that imports the shared package.
5. Pass the parity matrix.
6. Materialize all live and rollback configurations.
7. Bind every live environment directly to strict policy.
8. Replace handwritten release and rollout fixtures with one factory.
9. Handle host resources independently in Phase C.

## Authorization Recommendation

Phase B may address application runtime-resource parity and live fail-closed
after Phase A closes GAP-0. Host systemd/Docker convergence remains Phase C.
