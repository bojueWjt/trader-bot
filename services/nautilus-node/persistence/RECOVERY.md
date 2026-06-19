# B-06 Recovery Runbook

This node must restart into `HALTED`, reconnect Redis-backed Nautilus Cache and
MessageBus, run startup reconciliation, and only become ready after Redis,
instruments, reconciliation, projection, and control-plane dependencies are healthy.

## Redis Isolation

Each account uses a separate key space derived from:

- configured `redis.key_prefix`
- `trader_id`
- `instance_id`
- persistence component name (`cache`, `message-bus`, or `idempotency`)

Example account A cache prefix:

```text
nautilus:account-a:node-a:TRADER-ACCOUNT-A:INSTANCE-ACCOUNT-A:cache
```

Account B must never share the same prefix. If `trader_id`, `instance_id`, or
`redis.key_prefix` overlap, startup config validation fails.

## Startup Recovery

1. Load per-account config and secrets.
2. Build Redis `DatabaseConfig(type="redis")`.
3. Build `CacheConfig` with Redis database, `use_trader_prefix=true`,
   `use_instance_id=true`, and `flush_on_start=false`.
4. Build `MessageBusConfig` with Redis database and account-specific
   `streams_prefix`.
5. Build `LiveExecEngineConfig` with:
   - `reconciliation=true`
   - `reconciliation_lookback_mins >= 60`
   - non-null open order and position check intervals for continuous reconciliation
6. Start the Nautilus node in `HALTED`.
7. Mark lifecycle `REDIS` ready only after the Redis-backed cache/bus connect.
8. Mark lifecycle `RECONCILIATION` ready only after startup reconciliation completes.

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
