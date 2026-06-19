# Window B Contract Change Requests

Date: 2026-06-19

## B09-CCR-001: Dashboard Command Issue Endpoint

SEAM §3 defines node command polling and ack endpoints, and §4 says dashboard command-issuing endpoints proxy §3 and return a request id. The frontend needs the concrete dashboard-facing endpoint.

Window B currently assumes:

- `POST /v1/commands`
- Request body: `{ "type": string, "args": object }`
- Response body includes `command_id`, `target_nodes`, `acks`, and `status`

Required resolution: lock the exact endpoint and response shape, including ack status vocabulary and timeout representation.

## B09-CCR-002: Manual Single-Position Actions

SEAM §3 command types include `halt`, `resume`, `set_reducing`, `cancel_all`, and `close_all`. The dashboard has manual single-position close, partial close, and stop-loss movement controls that must also wait for per-node ack.

Window B currently assumes:

- Single-position close uses `type="close_all"` with `args.scope="position"`.
- Partial close uses `type="close_all"` with `args.scope="position_partial"` and `args.amount`.
- Stop-loss movement uses `type="move_stop_loss"`.

Required resolution: either add explicit command types for these actions or define the canonical scoped args under existing command types.

## B09-CCR-003: Daily Report Read Endpoints

SEAM §4 lists dashboard read models but does not lock daily report endpoints. The current dashboard has report views and needs real control-plane data without seeded rows.

Window B currently assumes:

- `GET /v1/reports/daily/{date}`
- `GET /v1/reports/daily/{date}/markdown`
- `GET /v1/reports/daily/{date}/versions`
- `POST /v1/reports/daily/{date}/telegram-preview`
- `POST /v1/reports/daily/snapshot`

Required resolution: either lock these endpoints or remove the report views from the v1 dashboard scope.
