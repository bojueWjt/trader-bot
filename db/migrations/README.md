# Hermes to Nautilus PostgreSQL Migrations

This directory owns the canonical PostgreSQL schema for Window A. Use the lightweight psycopg2 runner in `services/control-plane/db/migrate.py`; Alembic is intentionally not used.

## One-Shot Local Verification

Create a temp venv and install the test/runtime dependencies:

```bash
python3 -m venv .venv-a02
.venv-a02/bin/pip install psycopg2-binary pytest
```

Start a temporary Homebrew PostgreSQL 16 cluster:

```bash
TMP_PG_DATA="$(mktemp -d /tmp/pg-a02-data.XXXXXX)"
rm -rf /tmp/pg-a02
mkdir -p /tmp/pg-a02
initdb -D "$TMP_PG_DATA" -A trust -U "$(whoami)"
pg_ctl -D "$TMP_PG_DATA" -l "$TMP_PG_DATA/postgres.log" -o "-p 55432 -k /tmp/pg-a02" -w start
export DATABASE_URL="postgresql://localhost/postgres?host=/tmp/pg-a02&port=55432"
```

Apply migrations:

```bash
.venv-a02/bin/python services/control-plane/db/migrate.py up
```

List tables with `psql`:

```bash
psql "$DATABASE_URL" -c '\dt'
```

Run the DB test suite:

```bash
.venv-a02/bin/python -m pytest tests/control-plane/db -q
```

Roll back all applied migrations:

```bash
.venv-a02/bin/python services/control-plane/db/migrate.py down
psql "$DATABASE_URL" -c '\dt'
```

Stop and clean up the temporary cluster:

```bash
pg_ctl -D "$TMP_PG_DATA" -w stop
rm -rf "$TMP_PG_DATA" /tmp/pg-a02
```

## Migration Runner Behavior

- `up` creates `schema_migrations` if needed and applies pending `NNNN_*.up.sql` files in lexical version order.
- `down` rolls back all applied migrations in reverse version order, then drops `schema_migrations` when no migrations remain.
- Each migration file runs inside a PostgreSQL transaction.
- `DATABASE_URL` is required for both directions.

## Projection Writer Role

The `0001_canonical_schema` migration creates `nautilus_projection_writer` as a separate `NOLOGIN` role. It receives only:

```sql
GRANT INSERT, UPDATE ON execution_events TO nautilus_projection_writer;
GRANT INSERT, UPDATE ON orders_projection TO nautilus_projection_writer;
GRANT INSERT, UPDATE ON positions_projection TO nautilus_projection_writer;
GRANT INSERT, UPDATE ON accounts_projection TO nautilus_projection_writer;
```

It receives no `SELECT` privilege and no grants on raw ingest, Hermes semantic, risk, trade intent, audit, replay, or outbox tables. Deployment can grant this role to the concrete Nautilus projection consumer login role.

## Canonical Invariants

- `raw_messages` has a unique source identity: `(source, channel_id, source_message_id, source_version)`.
- `raw_messages.source_received_at` is insert-only; updates are rejected by trigger.
- Hermes semantic judgments live in `hermes_decisions`, not `raw_messages`.
- Risk approvals live in `risk_decisions`, not `raw_messages` or `hermes_decisions`.
- Approved trade intents require non-null FKs to both Hermes and risk decisions.
- `outbox_events` is designed for same-transaction writes with business data.
- `execution_events.event_id` and intent/command idempotency keys are unique.
- `0006_trade_outcomes` adds the offline `trade_outcomes` ledger for closed-intent outcome metrics.
