# Order Management Operations Runbook (OM8-10)

This runbook is an operator procedure. It does not authorize live rollout by itself. Live stages require release-gate approval and explicit operator signoff.

## Required Environment

```bash
export CONTROL_PLANE_URL=http://127.0.0.1:8080
export RISK_ADMIN_TOKEN=...
export VIEWER_TOKEN=...
export NAUTILUS_NODE_TOKEN=...
export ACCOUNT_ID=acct-main
export NODE_ID=node-main
export INSTRUMENT_ID=BTCUSDT-PERP.BINANCE
```

Use a unique request id for every write:

```bash
export REQUEST_ID=req-$(date -u +%Y%m%dT%H%M%SZ)-operator
```

## Observe State

```bash
curl -sS "$CONTROL_PLANE_URL/api/system/snapshot" \
  -H "Authorization: Bearer $VIEWER_TOKEN"
```

Do not proceed to ACTIVE if the snapshot is stale, has missing nodes, or reports reconciliation drift.

## HALT

HALT stops new risk and sets the scoped risk state fail-closed.

```bash
export REQUEST_ID=req-$(date -u +%Y%m%dT%H%M%SZ)-halt
curl -sS -X POST "$CONTROL_PLANE_URL/v1/commands" \
  -H "Authorization: Bearer $RISK_ADMIN_TOKEN" \
  -H "X-Request-Id: $REQUEST_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"HALT\",
    \"reason\": \"operator HALT\",
    \"confirm\": true,
    \"target_nodes\": [\"$NODE_ID\"],
    \"scope\": {\"account_id\": \"$ACCOUNT_ID\", \"instruments\": [\"$INSTRUMENT_ID\"]}
  }"
```

Verify node pickup and terminal ack:

```bash
curl -sS "$CONTROL_PLANE_URL/v1/nodes/$NODE_ID/commands" \
  -H "Authorization: Bearer $NAUTILUS_NODE_TOKEN"
```

The node, not the operator, normally posts command acks:

```bash
curl -sS -X POST "$CONTROL_PLANE_URL/v1/nodes/$NODE_ID/commands/$COMMAND_ID/ack" \
  -H "Authorization: Bearer $NAUTILUS_NODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"status\":\"completed\",\"request_id\":\"$REQUEST_ID\",\"result\":{\"request_id\":\"$REQUEST_ID\"}}"
```

## cancel_all

Use only after checking that cancelling working orders is the intended action.

```bash
export REQUEST_ID=req-$(date -u +%Y%m%dT%H%M%SZ)-cancel-all
curl -sS -X POST "$CONTROL_PLANE_URL/v1/commands" \
  -H "Authorization: Bearer $RISK_ADMIN_TOKEN" \
  -H "X-Request-Id: $REQUEST_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"CANCEL_ALL\",
    \"reason\": \"operator cancel_all\",
    \"confirm\": true,
    \"target_nodes\": [\"$NODE_ID\"],
    \"scope\": {\"account_id\": \"$ACCOUNT_ID\", \"instruments\": [\"$INSTRUMENT_ID\"]}
  }"
```

Acceptance condition: exchange working orders are zero, node command status is terminal, and the control-plane projection matches the exchange.

## close_all

Use for emergency flattening or planned staged exits. The node must submit reduce-only closes and verify exchange flat before reporting completed.

```bash
export REQUEST_ID=req-$(date -u +%Y%m%dT%H%M%SZ)-close-all
curl -sS -X POST "$CONTROL_PLANE_URL/v1/commands" \
  -H "Authorization: Bearer $RISK_ADMIN_TOKEN" \
  -H "X-Request-Id: $REQUEST_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"CLOSE_ALL\",
    \"reason\": \"operator close_all\",
    \"confirm\": true,
    \"target_nodes\": [\"$NODE_ID\"],
    \"scope\": {\"account_id\": \"$ACCOUNT_ID\", \"instruments\": [\"$INSTRUMENT_ID\"]}
  }"
```

Acceptance condition: exchange positions are flat for the scope, working orders are zero, node command status is terminal, and the control-plane projection matches the exchange.

## Staged Rollout

1. Shadow: keep nodes HALTED or non-trading. Run projections, settings publish/ACK, reconciliation, and dashboards against real data. No exchange orders are allowed.
2. Testnet: set settings `general.execution_mode=testnet`; use Binance testnet credentials only. Run `scripts/order_management_acceptance.py` and `scripts/order_management_chaos.py`. All rows must pass before promotion.
3. Live-readonly: connect live read credentials for observation only. Keep trading HALTED. Verify account/order/position projections and reconciliation against live exchange reality.
4. Operator signoff gate: stop here until the human operator signs the live preconditions in the release gate. This runbook does not edit `release-gate.json`.
5. Live-small: only after signoff, enable the smallest scoped account/instrument set. Keep low notional, low leverage, and manual monitoring. Start HALTED, reconcile, then issue a scoped RESUME only if the snapshot is fresh and reconciled.

Scoped RESUME after signoff:

```bash
export REQUEST_ID=req-$(date -u +%Y%m%dT%H%M%SZ)-resume
curl -sS -X POST "$CONTROL_PLANE_URL/v1/commands" \
  -H "Authorization: Bearer $RISK_ADMIN_TOKEN" \
  -H "X-Request-Id: $REQUEST_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"RESUME\",
    \"reason\": \"operator signed live-small staged resume\",
    \"confirm\": true,
    \"target_nodes\": [\"$NODE_ID\"],
    \"scope\": {\"account_id\": \"$ACCOUNT_ID\", \"instruments\": [\"$INSTRUMENT_ID\"]}
  }"
```

## Old Production Path

The old production path is left untouched during v3 rollout:

- Do not modify `bridge/**` as part of this runbook.
- Do not restart or re-enable legacy auto-approval/importer paths.
- Keep old credentials out of PostgreSQL and out of repository files.
- If old dashboards remain available, use them read-only for comparison unless rollback explicitly routes traffic back.
- Rollback must never revive an unsafe legacy auto-open path.
