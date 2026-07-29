# Order Management Rollback Runbook (OM8-10)

Rollback is an operator procedure. Start by stopping new risk, then choose the smallest rollback that restores safety.

## 1. Immediate Safety

```bash
export CONTROL_PLANE_URL=http://127.0.0.1:8080
export RISK_ADMIN_TOKEN=...
export VIEWER_TOKEN=...
export ACCOUNT_ID=acct-main
export NODE_ID=node-main
export INSTRUMENT_ID=BTCUSDT-PERP.BINANCE
export REQUEST_ID=req-$(date -u +%Y%m%dT%H%M%SZ)-rollback-halt

curl -sS -X POST "$CONTROL_PLANE_URL/v1/commands" \
  -H "Authorization: Bearer $RISK_ADMIN_TOKEN" \
  -H "X-Request-Id: $REQUEST_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"HALT\",
    \"reason\": \"rollback safety halt\",
    \"confirm\": true,
    \"target_nodes\": [\"$NODE_ID\"],
    \"scope\": {\"account_id\": \"$ACCOUNT_ID\", \"instruments\": [\"$INSTRUMENT_ID\"]}
  }"
```

If exposure must be removed, run `CLOSE_ALL` from `docs/runbooks/ORDER_MANAGEMENT_OPERATIONS.md` and verify exchange flat before changing software or schema.

## 2. Settings Rollback

List versions:

```bash
curl -sS "$CONTROL_PLANE_URL/v1/order-management/settings/versions?scope=account&scope_key=$ACCOUNT_ID" \
  -H "Authorization: Bearer $VIEWER_TOKEN"
```

Rollback creates a new settings version; it does not edit history in place.

```bash
export TARGET_VERSION=1
export EXPECTED_VERSION=2
export REQUEST_ID=req-$(date -u +%Y%m%dT%H%M%SZ)-settings-rollback

curl -sS -X POST "$CONTROL_PLANE_URL/v1/order-management/settings/rollback" \
  -H "Authorization: Bearer $RISK_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"scope\": \"account\",
    \"scope_key\": \"$ACCOUNT_ID\",
    \"target_version\": $TARGET_VERSION,
    \"expected_version\": $EXPECTED_VERSION,
    \"reason\": \"rollback to prior approved settings\",
    \"request_id\": \"$REQUEST_ID\",
    \"confirm\": true,
    \"operator_signoff\": \"operator-approved-rollback\"
  }"
```

Verify desired/effective version and node ACK before resuming any scope.

## 3. Migration Rollback: 0005 Down

Migration 0005 is additive and reversible. Its down migration drops only OM8 order-management additions and reverses additive `audit_events` columns; it does not drop baseline `audit_events` or baseline `orders_projection.intent_id`.

Preconditions:

- All nodes HALTED.
- No acceptance or chaos run is active.
- Fresh database backup exists and is recorded in the rollback log.
- Operator has confirmed that dropping 0005 order-management tables is intended.

The generic migrator `services/control-plane/db/migrate.py down` rolls back all applied migrations. For an isolated 0005 rollback, run the 0005 down SQL and remove only schema_migrations version `0005` in the same psql session:

```bash
export DATABASE_URL=postgresql://...
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -c "BEGIN" \
  -f db/migrations/0005_order_management.down.sql \
  -c "DELETE FROM schema_migrations WHERE version = '0005'" \
  -c "COMMIT"
```

Re-apply after fixing the release:

```bash
DATABASE_URL="$DATABASE_URL" /Users/pudu/projects/.venv-integration/bin/python \
  services/control-plane/db/migrate.py up
```

## 4. Revert to Prior Release

1. Keep all nodes HALTED.
2. Stop v3 node/control-plane processes for the affected scope.
3. Restore the prior release artifact or container tag.
4. Keep old production auto-open/importer paths disabled unless a separate signed rollback explicitly authorizes them.
5. If routing must move back, route only after HALT is confirmed and exchange state is reconciled.
6. Confirm snapshot/read-only surfaces match exchange reality.
7. Record a rollback report with request IDs, operator, reason, final exchange orders/positions, migration state, and release artifact IDs.

## 5. Resume After Rollback

Do not issue RESUME until all are true:

- The snapshot is fresh.
- Reconciliation is clean.
- Node command runs for HALT/cancel_all/close_all are terminal.
- Settings desired/effective versions match.
- The operator signs the resume decision.
