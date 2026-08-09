# Account Stall Current Runtime Diagnosis

Date: 2026-08-09
Mode: read-only agent diagnosis plus in-memory harness
Production state: `HALTED / NO-GO`

## Current Topology

`run_node` starts one shared `NodeControlPlaneSession` before the Nautilus node.
The shared session owns independent bounded lanes for:

- heartbeat
- command poll
- command delivery
- command ACK
- intent fetch
- intent delivery
- execution events

Command and intent delivery return to the Nautilus actor mailbox. Strategies
also own separate single-thread `BoundedTaskWorker` and
`TerminalExchangeWorker` instances.

Key evidence:

- `services/nautilus-node/app/run_node.py:79`
- `services/nautilus-node/app/run_node.py:82`
- `services/nautilus-node/app/node.py:313`
- `services/nautilus-node/app/node.py:319`
- `services/nautilus-node/app/node.py:333`
- `services/nautilus-node/runtime/control_plane_session.py:436`
- `services/nautilus-node/runtime/control_plane_session.py:678`
- `services/nautilus-node/app/nautilus_actors.py:524`
- `services/nautilus-node/app/nautilus_actors.py:531`
- `services/nautilus-node/app/nautilus_actors.py:2657`
- `services/nautilus-node/app/nautilus_actors.py:2672`

The diagnostic agent ran the existing focused runtime selection:

```text
71 passed, 2 subtests passed in 2.84s
```

## Proven Current Residual Mechanism

### Shared-session shutdown leaks terminal executors

The shared-session stop branch closes only the ACK executor. Terminal
verification can initialize heartbeat, command, ACK, and terminal executors.
When terminal evidence collection remains in flight, actor stop returns while
the terminal future and terminal executor thread remain alive.

An in-memory harness observed:

```text
actor on_stop duration: 0.005 ms
terminal future: incomplete
remaining thread: operator-commands.poll.terminal_0
```

Evidence:

- `services/nautilus-node/app/nautilus_actors.py:2173`
- `services/nautilus-node/app/nautilus_actors.py:2285`
- `services/nautilus-node/app/nautilus_actors.py:2908`

This is a deterministic current-worktree shutdown defect. Its stall window
begins when cleanup starts while terminal verification remains in flight.

### Durable worker stop failure is ignored

`BoundedTaskWorker` reports a handler that survives shutdown by returning
`False`. `IntentExecutionStrategy` currently ignores that return value.
`TerminalExchangeWorker` propagates drain failure as an error, so the two
worker types have inconsistent cleanup semantics.

Evidence:

- `services/nautilus-node/runtime/bounded_task_worker.py:106`
- `services/nautilus-node/runtime/bounded_task_worker.py:149`
- `services/nautilus-node/runtime/bounded_task_worker.py:198`
- `services/nautilus-node/strategy/intent_execution_strategy.py:2526`
- `services/nautilus-node/runtime/exchange_cancel_adapter.py:398`

The dangerous sequence is:

```text
worker remains alive
cleanup treats stop as successful
namespace lease or other fencing resource is released
old worker can resume after ownership moved
```

## Ranked Remaining Mechanisms

| Rank | Mechanism | Falsifiable prediction |
|---|---|---|
| 1 | Terminal verification and heartbeat share one evidence provider lock that spans external HTTP | Heartbeat `in_flight_age` crosses deadline, session emits one fatal reason, exit code 70 follows |
| 2 | Host or VM pause stops actor, every session lane, and both in-process watchdogs | External supervisor sees live PID while all internal clocks stop and jump together after resume |
| 3 | Shared-session terminal executor leak during cleanup | Cleanup returns with incomplete future or surviving `operator-commands.poll.*` thread |
| 4 | Ignored durable worker stop failure | Cleanup releases lease while durable worker remains alive |
| 5 | Native GIL hold or interpreter deadlock | External probe progresses while every Python progress clock stops |

The top lock-contention hypothesis is supported by:

- `services/nautilus-node/app/nautilus_actors.py:2590`
- `services/nautilus-node/app/nautilus_actors.py:2938`
- `services/nautilus-node/runtime/exchange_cancel_adapter.py:1035`
- `services/nautilus-node/runtime/control_plane_session.py:983`
- `services/nautilus-node/runtime/control_plane_session.py:1080`
- `services/nautilus-node/app/node.py:408`

## Existing Coverage

- Lane isolation and hard-deadline fatal behavior:
  `tests/nautilus/runtime/test_control_plane_session.py`
- Actor mailbox thread and callback budget:
  `tests/nautilus/runtime/test_actor_callback_budget.py`
- Worker timeout and stop visibility:
  `tests/nautilus/runtime/test_bounded_task_worker.py`
- Terminal queue, deadline, and drain:
  `tests/execution/manage/test_exchange_cancel_adapter.py`

Current gaps:

- real shared session with command and intent actors
- session-mode terminal shutdown
- propagation of durable `stop() == False`
- uncooperative transport
- subprocess SIGSTOP and GIL fault injection

Existing actor-stop tests exercise the legacy no-session branch. Node assembly
and cleanup tests use fake session/node/worker objects.

## First Red-Capable Test

File:

```text
tests/nautilus/runtime/test_account_stall_fault_injection.py
```

Test:

```text
test_shared_session_cleanup_fences_inflight_terminal_verification_before_lease_release
```

Scenario:

1. Start a real `NodeControlPlaneSession`, Command/Intent actors, and a 5 ms
   actor loop.
2. Wait for actor tick, heartbeat, command poll/ACK, and intent progress to
   advance twice.
3. Inject a pending terminal result and block the evidence provider inside
   `snapshot(force_refresh=True)`.
4. Execute production cleanup order: session stop, actor stop, background
   workers, Redis safety, lease close.
5. Sample the terminal future, executor references, named threads, five
   progress clocks, and lease ownership.
6. Release the provider and observe recovery.

Green thresholds:

- actor callback under 10 ms
- actor tick age under 2 seconds during injected I/O
- session stop at most 1 second
- actor stop at most 0.5 seconds
- cleanup propagates shutdown failure or completes every related future
- zero `operator-commands.poll.*` threads after cleanup
- lease remains held while any worker survives

Current expected red:

- terminal future remains incomplete
- terminal/heartbeat/command executor references remain attached
- terminal executor thread remains alive
- actor stop does not propagate the incomplete shutdown to cleanup

## GAP-0 Decision

Two current-worktree cleanup/fencing defects have a deterministic in-memory
reproduction: shared-session terminal cleanup can leave an in-flight executor
alive, and durable worker shutdown failure can be ignored.

GAP-0 closure, direct causality with the production account stall, and Phase
B-S authorization remain pending until the versioned fault-injection test and
A7 review are complete. The verified defects may receive a narrowly scoped
fix; that fix does not establish that the production stall path is closed.
