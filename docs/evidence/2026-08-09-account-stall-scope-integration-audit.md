# Account Stall Scope and Integration Audit

Date: 2026-08-09
Owner: Scope and Integration Auditor
Mode: source-only audit
Production state: `HALTED / NO-GO`
Source baseline: `7368641e98c410b3c6edbe4ab3ea8f72cf5efe1d`

## Decision

Phase A6 should import the smallest runtime closure that can run:

```text
tests/nautilus/runtime/test_account_stall_fault_injection.py
```

The red-capable test must exercise the real shared
`NodeControlPlaneSession`, `IntentPublisherActor`, `CommandPollerActor`, and
production cleanup ordering. Test-local fakes cover control-plane transport,
intent source, lifecycle, message bus, Redis safety, and namespace lease.

Two current-worktree mechanisms are established:

1. The shared-session branch of `CommandPollerActor.on_stop()` shuts down only
   the ACK executor. An in-flight terminal verification can leave the
   terminal future, terminal executor, and
   `operator-commands.poll.terminal_*` thread alive.
2. `BoundedTaskWorker.stop()` returns `False` when its worker remains alive.
   Cleanup callers can ignore that result and release the namespace lease
   while an old writer-capable worker can resume.

Historical synchronous HTTP inside actor callbacks remains lineage evidence.
It is not the current GAP-0 mechanism.

## Minimal Import Closure

### Full-file imports

These untracked files are cohesive and have no repository-local runtime
imports:

| Source file | Required symbols | Reason |
|---|---|---|
| `services/nautilus-node/runtime/control_plane_session.py` | `NodeControlPlaneSession`, `SubmissionResult`, `SessionHealth`, `LaneHealth` | Real heartbeat, command poll/delivery/ACK, intent fetch/delivery, progress clocks, bounded stop |
| `services/nautilus-node/runtime/bounded_task_worker.py` | `BoundedTaskWorker`, `BoundedTaskWorkerSnapshot` | Reproduce and assert propagation of `stop() is False` |

Create this test directly in the Phase A worktree:

| Test file | Required coverage |
|---|---|
| `tests/nautilus/runtime/test_account_stall_fault_injection.py` | Shared session plus real Command/Intent actors; blocked terminal verification; cleanup order; surviving-thread inventory; lease retention; background-worker `False` propagation |

The test must define its own deterministic fakes. It should not import helper
objects from other test modules.

### Tracked mixed-hunk imports

#### `services/nautilus-node/app/nautilus_actors.py`

Import only the symbols and supporting hunks below. The complete file diff is
`+4045/-107` and includes projection, journal recovery, authorization,
terminal behavior, and unrelated actor expansion.

Required actor symbols:

```text
IntentPublisherActor
CommandPollerActor
```

Required shared-session support:

```text
_IntentPublication
_QueueingIntentPublisher
_SessionCommandPublication
_PendingCommandAck
_CommandGenerationContext
DEFAULT_PENDING_INTENT_LIMIT
DEFAULT_PENDING_COMMAND_LIMIT
DEFAULT_PENDING_COMMAND_ACK_LIMIT
DEFAULT_WORKER_SHUTDOWN_WAIT_SECONDS
DEFAULT_CALLBACK_MAX_ITEMS
DEFAULT_CALLBACK_TIME_BUDGET_SECONDS
```

Required terminal-verification support:

```text
_TerminalCommandResult
_TerminalVerificationResult
_CommandJournalEntry
_CommandPersistenceTask
_JsonCommandJournal
_parse_terminal_command_result
_parse_terminal_datetime
_terminal_command_result_payload
_terminal_snapshot_fetched_at
_terminal_snapshot_residual
_scoped_terminal_rows
_canonical_terminal_symbol
_terminal_command_instrument_ids
```

Required actor lifecycle helpers:

```text
_init_actor_base
_submission_was_accepted
_shutdown_executor
_first_attr
_dependency_by_value
```

Required `IntentPublisherActor` methods:

```text
__init__
on_start
on_stop
_on_poll_timer
_drain_pending_intents
_evaluate_session_health
_record_poll_progress
_register_poll_timer
_attach_to_plain_client_publisher
_message_bus
_mark_dependency_ready
_mark_dependency_failed
```

Required `CommandPollerActor` methods:

```text
__init__
on_start
on_stop
_on_poll_timer
_ensure_executors
session_send_heartbeat
session_poll_commands
session_apply_command
session_ack_command
_drain_session_commands
_apply_session_command
_evaluate_session_health
_subscribe_terminal_command_results
_on_terminal_command_result
_drain_terminal_command_results
_accept_terminal_command_result
_submit_terminal_verification
_verify_terminal_command_after_persistence
_verify_terminal_command
_harvest_terminal_verification
_finish_terminal_command
_ensure_command_persistence_worker
_stop_command_persistence_worker
_submit_command_persistence
_wait_for_command_persistence
_fatal_runtime
_record_actor_tick
```

The test may seed `_pending_terminal_results` directly with a
`_TerminalCommandResult`. This keeps command replay, receipt restoration, and
the complete durable journal migration outside A6 while preserving the real
terminal executor and verification path.

#### `services/nautilus-node/app/node.py`

Import only:

```text
import time
_stop_control_plane_session
_stop_background_workers
_stop_redis_runtime_safety
```

Behavior required by the test:

- session stop uses a one-second absolute deadline;
- session drain failure raises;
- every background worker `stop()` result is checked;
- `False` becomes a cleanup error;
- Redis safety guard/client cleanup errors propagate.

The complete `node.py` diff is `+1658/-71` and would pull in Redis
construction, reconciliation, live canary, reporters, strategy wiring,
runtime resources, and release identity.

#### `services/nautilus-node/app/run_node.py`

Import only:

```text
from typing import Any
node helper imports:
  _stop_background_workers
  _stop_control_plane_session
  _stop_redis_runtime_safety
_cleanup_runtime
_call_cleanup
```

The test must call `_cleanup_runtime()` to preserve production ordering:

```text
control-plane session
trading node / actors
background workers
Redis runtime safety
namespace lease close
```

The `main()` startup and CLI changes remain outside A6.

## Existing HEAD Dependencies

The following files already exist at `7368641` and require no dirty hunk for
this test:

```text
packages/execution-domain/execution_domain/control_plane.py
services/nautilus-node/app/health_server.py
services/nautilus-node/config/node_config.py
services/nautilus-node/persistence/nautilus_config.py
services/nautilus-node/projection/actor.py
services/nautilus-node/projection/event_mapper.py
services/nautilus-node/projection/spool.py
services/nautilus-node/risk/config.py
services/nautilus-node/routing/multi_account.py
services/nautilus-node/runtime/health.py
```

`CommandType`, `CommandAckStatus`, `NodeCommand`, and `TradingState` required
by the actor already exist in the HEAD execution-domain contract. Test-local
objects can satisfy the remaining lifecycle and transport protocols.

## Import Order

1. Import `runtime/control_plane_session.py`.
2. Import `runtime/bounded_task_worker.py`.
3. Import the `IntentPublisherActor` shared-session hunks and dependencies.
4. Import the `CommandPollerActor` shared-session, terminal verification, and
   shutdown hunks.
5. Import the three cleanup helpers from `app/node.py`.
6. Import `_cleanup_runtime` and `_call_cleanup` from `app/run_node.py`.
7. Add `test_account_stall_fault_injection.py`.
8. Run the focused test repeatedly and record the red observation.

## Required Red Tests

```text
test_shared_session_cleanup_fences_inflight_terminal_verification_before_lease_release
test_cleanup_retains_lease_when_bounded_worker_stop_returns_false
```

The first test must observe:

- actor tick, heartbeat, command poll/ACK, and intent progress advancing;
- `snapshot(force_refresh=True)` blocked by an `Event`;
- `_cleanup_runtime()` using the production order;
- terminal future and named thread state at the shutdown deadline;
- lease state before and after provider release.

The second test must use a real `BoundedTaskWorker` with an uncooperative
handler, place it in `runtime.background_workers`, and assert that cleanup
raises while the lease remains open.

Suggested command:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --with pytest \
  --with 'nautilus_trader==1.227.0' \
  python -m pytest -p no:cacheprovider -q \
  tests/nautilus/runtime/test_account_stall_fault_injection.py
```

Repeatability gate:

```bash
for i in $(seq 1 50); do
  PYTHONDONTWRITEBYTECODE=1 uv run --no-project --with pytest \
    --with 'nautilus_trader==1.227.0' \
    python -m pytest -p no:cacheprovider -q \
    tests/nautilus/runtime/test_account_stall_fault_injection.py || exit 1
done
```

The initial test commit is expected to be red for the shared-session terminal
executor leak. The bounded-worker cleanup test should pass only when the
`False` result reaches `_cleanup_runtime()` and retains the lease.

## Explicit Exclusions

Do not import these files for A6:

```text
services/nautilus-node/app/node.py                         whole file
services/nautilus-node/app/nautilus_actors.py              whole file
services/nautilus-node/app/run_node.py                      whole file
services/nautilus-node/runtime/lifecycle.py
services/nautilus-node/runtime/reconciliation.py
services/nautilus-node/runtime/exchange_cancel_adapter.py
services/nautilus-node/runtime/intent_execution_inbox.py
services/nautilus-node/runtime/live_canary_execution.py
services/nautilus-node/runtime/redis_safety.py
services/nautilus-node/strategy/intent_execution_strategy.py
services/nautilus-node/data_client/approved_intent_client.py
services/nautilus-node/persistence/redis_namespace_lease.py
services/nautilus-node/persistence/redis_resp_client.py
services/nautilus-node/persistence/__init__.py              dirty hunk
services/nautilus-node/persistence/nautilus_config.py       dirty hunk
services/nautilus-node/config/node_config.py                dirty hunk
packages/execution-domain/execution_domain/control_plane.py dirty hunk
packages/execution-domain/execution_domain/http_client.py   dirty hunk
scripts/**
infra/**
db/migrations/**
.live-mirror/**
Attention/Hermes/channel files
release/deployment tests
```

Actual Redis is not part of this deterministic red test. A fake lease guard
provides `close()` and ownership state. A fake Redis safety guard/client
provides stop and close state. This directly tests cleanup fencing without
network or data mutation.

## Runtime Resource Owner Boundary

The resource contract currently has three implementation owners:

| Owner | Responsibility |
|---|---|
| `services/nautilus-node/config/node_config.py` | Application runtime parsing, compatibility defaults, dataclass materialization |
| `scripts/release_manifest.py` | Release-side required/exact field validation, canonical normalization, and hashes |
| `scripts/make_account_stall_release.py` | Host systemd/Docker resource production and projection |

The immediate drift is between `node_config.py` and `release_manifest.py`:

- Node compatibility mode requires three top-level groups and defaults four.
- Node strict mode requires seven groups but accepts selected unknown nested
  fields.
- Manifest validation requires the exact seven-group and nested-field sets.

This is Phase B contract work. None of the three owner files should enter A6.
Deployment test parsers are fixtures and verifiers, not contract owners.

## Suggested Atomic Commits

```text
docs(account-stall): freeze A2 scope integration audit
feat(account-stall): import shared session and bounded worker primitives
feat(account-stall): import command and intent actor session hunks
feat(account-stall): import cleanup fencing helpers
test(account-stall): reproduce shared-session cleanup leak
fix(account-stall): drain shared-session actor executors before lease release
fix(account-stall): propagate bounded worker stop failure
```

The first four runtime commits should preserve the source behavior. The test
commit records the deterministic red. Each mechanism receives its own fix
commit and focused green evidence.

## Risks and Gates

1. `CommandPollerActor` is the largest import risk. A whole-file copy would
   silently import command journal, reconciliation, release, and ordinary
   command changes.
2. Directly seeding pending terminal state can weaken the test. The test must
   still use the real terminal executor, real
   `_verify_terminal_command()`, and real actor `on_stop()`.
3. Importing `run_node.py` without the three matching `node.py` helpers breaks
   module import.
4. A fake session or fake actor would miss the confirmed leak. Only transport,
   lifecycle, Redis safety, and lease may be faked.
5. Thread assertions require `Event` barriers and the actor's real thread-name
   prefix. Sleeps alone are insufficient.
6. Every test exit path must release blocked providers and workers in
   `finally`, then assert zero surviving named threads.
7. Cleanup success requires all writer-capable workers stopped before lease
   close. Any `False`, exception, incomplete future, or surviving thread keeps
   the lease held.
8. A6 remains local and deterministic. SSH, production reads, Redis or
   PostgreSQL access, deployment, restart, and trading remain frozen.

## A2 Result

A2 is complete. The approved A6 import surface is two cohesive untracked
runtime files, three tracked files imported at symbol/hunk granularity, and one
new focused test file. Runtime resource parity remains isolated for Phase B.

