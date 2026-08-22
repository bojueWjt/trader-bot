# B-06 Recovery Runbook

This node must restart into `HALTED`, acquire a fenced Redis persistence
generation, run exchange-first startup reconciliation, and only become ready
after Redis, instruments, reconciliation, projection, and control-plane
dependencies are healthy.

## Redis Isolation

Each live execution account uses two Redis identities:

- stable lease namespace: `trader-{trader_id}`
- fenced persistence instance: an acquisition-scoped UUID4 candidate generated
  by the process and atomically committed by Redis when the lease is acquired

Example account A identities:

```text
lease namespace:       trader-TRADER-ACCOUNT-A
cache namespace:       trader-TRADER-ACCOUNT-A:7f3a01d2-9b8c-4d6e-a123-4f5b6c7d8e90
message bus namespace: trader-TRADER-ACCOUNT-A:7f3a01d2-9b8c-4d6e-a123-4f5b6c7d8e90:nautilus:account-a:TRADER-ACCOUNT-A:message-bus
```

The stable namespace provides process exclusivity. The monotonically increasing
fencing token orders owners inside the current Redis history. The independently
generated UUID4 keeps persistence generations distinct across counter loss,
backup restore, and Redis replacement. Live cache and message bus configs set
`use_instance_id=true`, and `TradingNodeConfig.instance_id` receives the same
Nautilus `UUID4`. A replacement process writes a new generation. A stale process
can only resume writes in its retired generation.

Testnet keeps `use_instance_id=false` and has no production lease requirement.
Account B must always use a distinct trader ID and configured Redis prefix.

Message-bus streams auto-trim to the latest 1440 minutes by default. Set
`NAUTILUS_MESSAGE_BUS_AUTOTRIM_MINS` to a positive integer to override the
retention window.

## Startup Recovery

1. Load per-account config and secrets.
2. Acquire the stable account lease before constructing `TradingNodeConfig`.
3. Read the returned `fencing_token`, `persistence_instance_id`, and
   `persistence_namespace`; verify all identities against the node config.
4. Build Redis `DatabaseConfig(type="redis")`.
5. Build `CacheConfig` with Redis database, `use_trader_prefix=true`,
   `use_instance_id=true`, and `flush_on_start=false`.
6. Build `MessageBusConfig` with Redis database and account-specific
   `streams_prefix`, `use_instance_id=true`, and an explicit `autotrim_mins`.
7. Pass the same persistence instance through
   `UUID4.from_str(...)` to `TradingNodeConfig.instance_id`.
8. Build `LiveExecEngineConfig` with:
   - `reconciliation=true`
   - `reconciliation_lookback_mins >= 60`
   - non-null open order and position check intervals for continuous reconciliation
9. Start the Nautilus node in `HALTED`.
10. Rebuild cache state from exchange-first startup reconciliation and durable
    execution projections. Retired generations do not act as recovery truth sources.
11. Mark lifecycle `REDIS` ready only after the Redis-backed cache/bus connect.
12. Mark lifecycle `RECONCILIATION` ready only after startup reconciliation completes.

The node must not auto-switch to `ACTIVE`; operator/control-plane resume remains
required after readiness.

## Duplicate Fill Hook

The persistence idempotency key is:

```text
fills:{account_id}:fill:{venue_order_id}:{trade_id}
```

The `source` field is intentionally excluded so the same fill from websocket and a
later reconciliation replay claims the same key. Redis runtime code should use
`SET key 1 NX EX <ttl>` via `RedisIdempotencyStore.claim_fill()`.

## Local Verification

```bash
python3 -m unittest discover -s tests/nautilus/reconciliation -v
python3 -m py_compile $(find services/nautilus-node/persistence services/nautilus-node/config tests/nautilus/reconciliation -name '*.py' ! -name '._*')
```

## Legacy UUID Namespace Cleanup

Each running node owns a fenced namespace lease containing:

- `namespace`
- a process-unique `owner`
- immutable `release_id`
- monotonically increasing `fencing_token`
- acquisition-scoped `persistence_instance_id`
- exact `persistence_namespace`
- `refreshed_at_epoch`

Acquire and refresh scripts use Redis `TIME` inside Lua for freshness scores and
cutoffs. Client wall-clock values never decide ownership or takeover.

Acquire the lease during startup from a trusted host or the runtime:

```bash
python3 scripts/redis_namespace_registry.py register \
  trader-TRADER-ACCOUNT-A \
  --owner node-a:runtime-uuid \
  --release-id "$TRADER_RELEASE_ID"
```

Store the full returned record in process memory and deployment evidence. The
independent heartbeat lane must refresh the lease at least every 60 seconds,
inside the default 120-second freshness window:

```bash
python3 scripts/redis_namespace_registry.py refresh \
  trader-TRADER-ACCOUNT-A \
  --owner node-a:runtime-uuid \
  --release-id "$TRADER_RELEASE_ID" \
  --fencing-token 42 \
  --persistence-instance-id 7f3a01d2-9b8c-4d6e-a123-4f5b6c7d8e90 \
  --persistence-namespace \
    trader-TRADER-ACCOUNT-A:7f3a01d2-9b8c-4d6e-a123-4f5b6c7d8e90
```

Audit legacy and retired generation namespaces. Dry-run is the default:

```bash
python3 scripts/redis_namespace_janitor.py \
  --legacy-prefix trader-TRADER-ACCOUNT-A: \
  --active-namespace \
    trader-TRADER-ACCOUNT-A:7f3a01d2-9b8c-4d6e-a123-4f5b6c7d8e90
```

Deletion requires `--apply --safety-manifest <manifest.json>`. The janitor
apply path requires a v2 safety manifest containing both
`lease_namespace` and `persistence_namespace` for every live execution
account. Lease freshness and owner identity are verified through the stable
namespace; deletion protects the exact active generation. The janitor only
selects canonical UUID namespaces inside the explicit trader prefix, requires
every key to exceed the idle threshold, and applies namespace/key batch limits.
Apply requires both generation fields in live lease metadata and verifies their
exact relationship. Legacy lease metadata without those fields remains
inspectable through dry-run and cannot authorize deletion.
A namespace larger than the current key limit is reported in
`partial_namespaces`; later runs continue from its remaining keys.

Every batch rechecks the stable lease metadata and exact active generation in
one Redis Lua operation before `UNLINK`. Redis executes the script atomically,
so a generation becoming active at the deletion boundary wins and the batch
deletes zero keys. `UNLINK` runs in bounded sub-batches; each batch is at most
one second of the configured rate, and a monotonic token bucket controls
subsequent calls. Run one account at a time and preserve the JSON report as the
audit record.

The runtime integration API is `persistence.RedisNamespaceLease`:

```python
lease = RedisNamespaceLease(
    redis_client,
    namespace=namespace,
    owner=process_unique_owner,
    release_id=release_id,
)
record = lease.acquire()

# Independent heartbeat lane, every 60 seconds.
record = lease.refresh()

# Graceful shutdown.
lease.release()
```

A fresh lease held by another owner fails acquisition. A stale lease can be
reacquired with a larger fencing token and a newly committed UUID4 generation.
A fenced process cannot refresh or remove the replacement lease. Its Redis
writes remain confined to the retired generation. Lease loss must HALT the node
and require process restart before trading resumes.

## Redis Capacity Governance

Apply capacity governance in this order:

1. deploy the fenced generation namespace settings
2. register every active namespace
3. dry-run and apply the legacy namespace janitor
4. measure `used_memory` and `used_memory_dataset` again
5. generate and persist the Redis recreate command or mounted config
6. recreate Redis and validate the running values

Generate a validated recreate plan:

```bash
python3 scripts/redis_capacity_config.py generate \
  --maxmemory 2.5gb \
  --current-used-memory 1.5gb \
  --current-dataset-size 1.2gb \
  --redis-cgroup-limit 4gb \
  --host-total-memory 8gb \
  --host-available-memory 6gb \
  --other-services-reserve 1gb \
  --system-reserve 3gb
```

The JSON output includes the equivalent persistent forms:

```text
docker run --memory <cgroup-bytes> --memory-swap <cgroup-bytes> ...
redis-server --maxmemory <bytes> --maxmemory-policy noeviction
```

```text
maxmemory <bytes>
maxmemory-policy noeviction
```

Use the command arguments when recreating a `docker run` container, or mount the
generated config and start Redis with that file. The gate validates Redis
`used_memory`, `used_memory_dataset`, the target `maxmemory`, the finite Redis
cgroup limit, host total and available memory, other-service reserve, and system
reserve together. Container headroom defaults to 20%. System reserve defaults to
3 GiB and has a 1 GiB hard safety floor. The generated Docker memory arguments
set `--memory-swap` equal to `--memory`, keeping Redis out of host swap.

The host-total check reserves the full Redis container budget plus the declared
other-service and system budgets. The host-available check reserves Redis growth
from current `used_memory` to `maxmemory`, container overhead, other services,
and the system budget. Missing budgets, an unlimited cgroup, a limit at or below
current Redis use, insufficient operating headroom, or insufficient host
capacity fail closed. Measure after namespace cleanup before choosing the limit.

For the default 20% Redis operating headroom and 20% container headroom, choose
a target that satisfies every bound below:

```text
maxmemory >= ceil(used_memory * 1.20)
maxmemory <= floor(redis_cgroup_limit / 1.20)
maxmemory <= floor((host_total - other_services_reserve - system_reserve) / 1.20)
maxmemory <= floor((host_available + used_memory - other_services_reserve - system_reserve) / 1.20)
```

Use the smallest upper bound, then rerun `generate` as the authoritative gate.
Set `other_services_reserve` from the measured peak working set of PostgreSQL,
both trading nodes, and host agents with deployment headroom. Keep
`system_reserve` at 3 GiB or higher in production.

Validate the recreated Redis process with explicit host budgets:

```bash
REDIS_URL='redis://127.0.0.1:6379/0' \
python3 scripts/redis_capacity_config.py inspect \
  --expected-maxmemory 2.5gb \
  --redis-cgroup-limit 4gb \
  --host-total-memory 8gb \
  --host-available-memory 6gb \
  --other-services-reserve 1gb \
  --system-reserve 3gb
```

When running the inspector inside the Redis container and on the target host,
`--probe-local-cgroup` and `--probe-host-memory` may replace their corresponding
explicit values. These probes only read finite cgroup limits and
`/proc/meminfo`; an unlimited or unavailable budget fails closed. Output omits
the Redis URL and credentials.

## 待 hk 容器执行

Run in the Linux/Python 3.12 container with `nautilus_trader==1.227.0`, Redis, and
Binance USDT-M Futures testnet credentials:

```bash
python3 - <<'PY'
import os
import sys
from pathlib import Path

repo = Path.cwd()
sys.path.insert(0, str(repo / "services" / "nautilus-node"))

from config.node_config import load_node_config
from persistence.nautilus_config import (
    build_cache_config,
    build_live_exec_engine_config,
    build_message_bus_config,
)

config = load_node_config("services/nautilus-node/config/examples/account-a.sandbox.json")
print(build_cache_config(config))
print(build_message_bus_config(config))
print(build_live_exec_engine_config(config))
PY
```

Then run the host-only restart scenario:

```bash
python3 -m unittest discover -s tests/nautilus/reconciliation -p 'test_host_reconciliation_pending.py' -v
```

Replace the skipped placeholders with target-host assertions once a real Nautilus
node, Redis, and testnet account are available:

- open a test position/order
- stop the node without flushing Redis
- restart with the same account config
- assert open orders/positions are recovered from Cache plus startup reconciliation
- let continuous reconciliation replay recent fills
- assert `RedisIdempotencyStore.claim_fill()` rejects the replayed fill key
