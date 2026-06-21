# Order Management Chaos Report (OM8-06)

Status: PENDING - run against chaos infra.

Harness: `scripts/order_management_chaos.py`

Run offline checks:

```bash
/Users/pudu/projects/.venv-integration/bin/python scripts/order_management_chaos.py --list
/Users/pudu/projects/.venv-integration/bin/python scripts/order_management_chaos.py --dry-run
```

Run live only with operator approval and the target infra ready:

```bash
export DATABASE_URL=postgresql://...
/Users/pudu/projects/.venv-integration/bin/python scripts/order_management_chaos.py --evidence-dir /path/to/chaos-evidence
```

Each live evidence file is `CASE_NAME.json` and must include `passed=true`, `recovery_state`, and assertions for `no_duplicate_order`, `stale_zero_new_risk`, and `reconciled_before_active`. Missing or failed evidence exits non-zero and is not reported as pass.

| # | Case | Status | Required assertions | Event chain | Recovery state | Notes |
|---|---|---|---|---|---|---|
| 1 | node_restart | PENDING - run against chaos infra | no duplicate order; stale => 0 new risk; reconciles before ACTIVE | PENDING | PENDING | Restart Nautilus node. |
| 2 | control_plane_restart | PENDING - run against chaos infra | no duplicate order; stale => 0 new risk; reconciles before ACTIVE | PENDING | PENDING | Restart control-plane API. |
| 3 | postgres_restart | PENDING - run against chaos infra | no duplicate order; stale => 0 new risk; reconciles before ACTIVE | PENDING | PENDING | Restart PostgreSQL. |
| 4 | redis_restart | PENDING - run against chaos infra | no duplicate order; stale => 0 new risk; reconciles before ACTIVE | PENDING | PENDING | Restart Redis. |
| 5 | network_disconnect | PENDING - run against chaos infra | no duplicate order; stale => 0 new risk; reconciles before ACTIVE | PENDING | PENDING | Disconnect node/control-plane network. |
| 6 | ws_reconnect | PENDING - run against chaos infra | no duplicate order; stale => 0 new risk; reconciles before ACTIVE | PENDING | PENDING | Drop and reconnect Binance websocket. |
| 7 | db_fault | PENDING - run against chaos infra | no duplicate order; stale => 0 new risk; reconciles before ACTIVE | PENDING | PENDING | Inject transient DB fault. |
