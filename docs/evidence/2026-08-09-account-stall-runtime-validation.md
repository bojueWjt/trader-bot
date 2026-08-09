# Account Stall Runtime Validation Evidence

Date: 2026-08-09
Production state: `HALTED / NO-GO`
Validation conclusion: bounded cleanup/fencing fix `PASS`; production stall
causality, Phase B-S, deployment, and live trading remain unauthorized

## 1. Source Boundary

| Role | Worktree | Branch | Commit |
|---|---|---|---|
| Original evidence | `/Users/balen/projects/trader-bot` | `codex/account-stall-hardening` | `7368641e98c410b3c6edbe4ab3ea8f72cf5efe1d` plus preserved dirty evidence |
| Phase A integration before this evidence update | `/Users/balen/projects/trader-bot-account-stall-phase-a` | `codex/account-stall-phase-a` | `c5eafcb0ba703d145aeacd8c1692e850a09b7957` |
| Runtime validation | `/Users/balen/projects/trader-bot-account-stall-runtime-validation` | `codex/account-stall-runtime-validation` | `ffc14e559afdf7b56bf245d8cadbea1d3013e609` |

The runtime validation branch is a clean, committed overlay used to prove the
current cleanup/fencing behavior. It is not a deployable release candidate.

## 2. Claude Meta-Review Absorption

### M1: historical cause and current mechanism

The validation commit claims closure only for two current, deterministic
cleanup/fencing defects:

- actor executors or futures surviving a bounded stop
- durable I/O workers surviving cleanup while process lease ownership changes

It does not claim that these defects caused the historical production stall.
The historical synchronous callback I/O evidence remains separate. A current
actor tick/progress freeze has not been reproduced, so Phase B-S remains
unauthorized.

### M2: reproducible evidence

This document records the exact source commit, dependency versions, selected
test files, collection hash, commands, results, file hashes, and reviewer
verdicts for the runtime validation subset.

The complete Phase A replay package still requires versioned raw output, JUnit,
inherited environment, and dependency artifact hashes for all four frozen
suites. Phase A A3 therefore remains open.

### M3: runtime resource optionality drift

The validation commit does not modify `node_config.py`,
`release_manifest.py`, or `make_account_stall_release.py`. The shared required
field set, Node/manifest parity matrix, and live fail-closed work remain Phase B
scope.

## 3. Validated Behavior

- `CommandPollerActor` exposes one reusable cleanup worker. Nautilus
  `on_stop()` and app cleanup serialize through the same stop lock.
- Command poll future capture and the stopped-state recheck share one lock,
  closing the replacement-poll shutdown race.
- Executor, future, session, Redis guard, and Redis client references remain
  attached after failed cleanup so a later cleanup can retry them.
- Redis cleanup preserves guard ownership of its client. The client closes only
  after guard stop succeeds or a retry confirms `snapshot.running is False`.
- The namespace lease remains held while any writer-capable worker remains
  alive.
- Strategy durable I/O uses the same worker instance for strategy stop and app
  cleanup.
- The production fatal callback uses `os._exit(75)`. A subprocess test proves
  that `finally` and `atexit` do not run. Process isolation is immediate and
  the Redis lease remains until TTL.
- Injected returning fatal policies continue through app cleanup and retain the
  process lease until all writer cleanup reports success.

## 4. Environment

```text
Darwin 25.5.0 arm64
uv 0.11.26
Python 3.12.13
pytest 9.1.1
nautilus_trader 1.227.0
pydantic 2.13.4
jsonschema 4.26.0
fastapi 0.141.1
httpx 0.28.1
psycopg2-binary 2.9.12
```

All pytest commands used this shell function:

```bash
run_runtime_pytest() {
  uv run --no-project --with pytest \
    --with 'nautilus_trader==1.227.0' \
    --with 'pydantic>=2.7,<3' \
    --with jsonschema --with fastapi --with httpx \
    --with psycopg2-binary \
    python -m pytest "$@"
}
```

## 5. Test Evidence

### Deterministic control-plane session test

```bash
run_runtime_pytest -q \
  tests/nautilus/runtime/test_control_plane_session.py
```

Result: `16 passed, 2 warnings`.

The startup replay test uses event state to prove that `session.start()` has
returned while replay is started and remains incomplete until the actor starts.
It no longer depends on a host-specific `<10ms` wall-clock threshold.

### Broad runtime selection

```bash
run_runtime_pytest -q \
  tests/nautilus/runtime/test_account_stall_fault_injection.py \
  tests/nautilus/runtime/test_actor_callback_budget.py \
  tests/nautilus/runtime/test_b10_node_app.py \
  tests/nautilus/runtime/test_bounded_task_worker.py \
  tests/nautilus/runtime/test_command_poller_lease.py \
  tests/nautilus/runtime/test_control_plane_session.py \
  tests/nautilus/runtime/test_run_node_lease_lifecycle.py \
  tests/nautilus/runtime/test_terminal_command_state.py
```

Result: `104 passed, 2 warnings`.

Collect-only result: `104 tests collected`.

SHA-256 of the captured collect-only output:

```text
9f9cee7856edb675951f5987cb6b6b95faad21654ecf3a007a24f4a4f5289dce
```

### Frozen focused selection

The same command without `test_b10_node_app.py` produced:

```text
62 passed, 2 warnings
```

The b10 node assembly file independently produced:

```text
42 passed, 2 warnings
```

### Fault-injection stability loop

```bash
for round in $(seq 1 50); do
  run_runtime_pytest -q \
    tests/nautilus/runtime/test_account_stall_fault_injection.py
done
```

Result: 50 independent pytest processes passed. Every process reported
`4 passed`; no process leaked a named runtime thread.

### Static checks

```bash
python3 -m py_compile \
  services/nautilus-node/app/nautilus_actors.py \
  services/nautilus-node/app/node.py \
  services/nautilus-node/strategy/intent_execution_strategy.py \
  tests/nautilus/runtime/test_account_stall_fault_injection.py \
  tests/nautilus/runtime/test_b10_node_app.py \
  tests/nautilus/runtime/test_control_plane_session.py \
  tests/nautilus/runtime/test_run_node_lease_lifecycle.py
git diff --check
```

Result: `PASS`.

## 6. Source Hashes

| Path | SHA-256 |
|---|---|
| `services/nautilus-node/app/nautilus_actors.py` | `f247e2f60b7e2b6b816c2f1b4db08dcedcc92e917283bef4bc4e226de8ea15d2` |
| `services/nautilus-node/app/node.py` | `f8d1423b40307b5253b682d910daf0431e0c4b379927f984bfad35f9e64d3b4b` |
| `services/nautilus-node/strategy/intent_execution_strategy.py` | `874b5354948ea6e8c739f9aa8bedf11edc2f8dd4a8d5d9fc81e8186524e075f2` |
| `tests/nautilus/runtime/test_account_stall_fault_injection.py` | `b5170af1a2eae17b1ee57a26927689831cbd3d16108934f800ac063afe68f96f` |
| `tests/nautilus/runtime/test_b10_node_app.py` | `63596612e288992a288646fca121b4aca7c2c46ff1092ca9d4e5f44ef7561fc0` |
| `tests/nautilus/runtime/test_control_plane_session.py` | `8e57aaa61f11f8288c69ee5229b835a7d9c74d51cd3cc5bbc370b549145f13ed` |
| `tests/nautilus/runtime/test_run_node_lease_lifecycle.py` | `575f26e8b76969164c84121a779af86793b78ce1fb62150eecdbcd82c6577cda` |

## 7. Review Gate

- Lagrange (`019fe4e2-f737-7d02-b253-28e5d88340a3`): final diff
  `PASS`, P0/P1/P2 all zero. This review includes the deterministic session
  assertion, Redis cleanup authority order, timeout retry snapshot, and cold
  import changes.
- Anscombe (`019fe4e3-12f4-7a91-9817-7ba6556260bf`): final diff `PASS`,
  P0/P1/P2 all zero.

The final review cycle first found and then closed:

- P1: a failed Redis guard stop could be followed by client close
- P2: the real retry shape, `stop() == False` plus
  `snapshot.running == False`, lacked direct coverage
- cold-start collection depended on another test importing execution-domain

## 8. A7 Import Decision

The validation branch cannot be imported into Phase A as one patch. Across the
seven runtime and test files, the two branches differ by:

```text
15543 insertions, 2274 deletions
```

The strategy file alone differs by `6386` insertions and `1997` deletions.
Applying the validation patch directly to Phase A fails at the actor, node, and
strategy hunks; two test files are absent in the Phase A tree.

Commit `ffc14e5` is therefore retained as validated overlay evidence. A7 must
first produce a dependency-closed, hunk-level import manifest for actor,
strategy, node wiring, session, HTTP client, projection spool, and matching
tests. Until that manifest is reviewed, Phase B-S, production deployment, and
real trading remain `NO-GO`.
