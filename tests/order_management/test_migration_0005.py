from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATE_PATH = ROOT / "services" / "control-plane" / "db" / "migrate.py"
UP = ROOT / "db" / "migrations" / "0005_order_management.up.sql"
DOWN = ROOT / "db" / "migrations" / "0005_order_management.down.sql"

REQUIRED_TABLES = [
    "execution_jobs",
    "order_events",
    "order_links",
    "protective_orders_projection",
    "risk_reservations",
    "order_management_settings",
    "order_management_setting_versions",
    "reconciliation_runs",
    "reconciliation_findings",
    "node_command_runs",
    "price_feed_status",
]
# audit_events is a BASELINE table (0001); 0005 enhances it additively rather than creating it.


def _migrate_module():
    spec = importlib.util.spec_from_file_location("control_plane_migrate", MIGRATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_migration_files_match_migrator_naming():
    migrate = _migrate_module()

    assert UP.exists()
    assert DOWN.exists()
    assert migrate.MIGRATION_RE.match(UP.name)
    assert migrate.MIGRATION_RE.match(DOWN.name)


def test_up_migration_contains_required_tables_and_constraints():
    sql = UP.read_text(encoding="utf-8")

    for table in REQUIRED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql

    assert "ALTER TABLE orders_projection" in sql
    # intent_id is baseline (0001) — only the new columns are added.
    for column in ("execution_job_id", "venue_symbol", "lifecycle_role"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql
    # audit_events (baseline) is enhanced additively, not created.
    assert "ALTER TABLE audit_events" in sql
    for column in ("action", "target", "before_state", "after_state", "reason", "request_id"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql

    assert "CONSTRAINT uq_execution_jobs_account_idempotency UNIQUE (account_id, idempotency_key)" in sql
    assert "CONSTRAINT uq_order_events_event_id UNIQUE (event_id)" in sql
    assert "CONSTRAINT uq_order_management_settings_scope_version UNIQUE (scope, scope_key, version)" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_protective_orders_active_role" in sql
    assert "WHERE active" in sql
    assert "numeric" in sql.lower()
    assert "timestamptz" in sql.lower()


def test_down_migration_reverses_0005_additions_without_dropping_baseline_intent_id():
    sql = DOWN.read_text(encoding="utf-8")

    for table in REQUIRED_TABLES:
        assert f"DROP TABLE IF EXISTS {table}" in sql

    assert "DROP COLUMN IF EXISTS execution_job_id" in sql
    assert "DROP COLUMN IF EXISTS venue_symbol" in sql
    assert "DROP COLUMN IF EXISTS lifecycle_role" in sql
    # baseline column/table must NOT be dropped by the 0005 down
    assert "DROP COLUMN IF EXISTS intent_id" not in sql
    assert "DROP TABLE IF EXISTS audit_events" not in sql
    # but the additive audit columns ARE reversed
    for column in ("action", "target", "before_state", "after_state", "reason", "request_id"):
        assert f"DROP COLUMN IF EXISTS {column}" in sql
