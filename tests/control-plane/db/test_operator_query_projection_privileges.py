from __future__ import annotations

from pathlib import Path

import psycopg2
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
UP = (
    REPO_ROOT
    / "db"
    / "migrations"
    / "0017_operator_query_projection_reads.up.sql"
)
DOWN = (
    REPO_ROOT
    / "db"
    / "migrations"
    / "0017_operator_query_projection_reads.down.sql"
)
ROLE = "trader_v3_operator_query"
TABLES = (
    "accounts_projection",
    "execution_events",
    "orders_projection",
)
UPDATE_PROBE_COLUMNS = {
    "accounts_projection": "updated_at",
    "execution_events": "created_at",
    "orders_projection": "updated_at",
}


def _assert_select_only(cur) -> None:
    for table_name in TABLES:
        cur.execute(
            """
            SELECT has_table_privilege(%s, %s, 'SELECT'),
                   has_table_privilege(%s, %s, 'UPDATE')
            """,
            (ROLE, table_name, ROLE, table_name),
        )
        assert cur.fetchone() == (True, False)
        cur.execute(
            """
            SELECT bool_or(
                has_column_privilege(%s, %s, column_name, 'UPDATE')
            )
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name=%s
            """,
            (ROLE, table_name, table_name),
        )
        assert cur.fetchone() == (False,)


def test_migration_replay_revokes_table_and_column_update_grants(
    migrated_db: str,
) -> None:
    up_sql = UP.read_text(encoding="utf-8")
    down_sql = DOWN.read_text(encoding="utf-8")
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        for table_name in TABLES:
            update_column = UPDATE_PROBE_COLUMNS[table_name]
            cur.execute(
                f"GRANT UPDATE ON {table_name} TO {ROLE}"
            )
            cur.execute(
                f"GRANT UPDATE ({update_column}) ON {table_name} TO {ROLE}"
            )

        cur.execute(up_sql)
        cur.execute(up_sql)
        _assert_select_only(cur)

        cur.execute(down_sql)
        cur.execute(down_sql)
        _assert_select_only(cur)


@pytest.mark.parametrize("table_name", TABLES)
def test_operator_query_can_read_and_cannot_update_projection_tables(
    migrated_db: str,
    table_name: str,
) -> None:
    update_column = UPDATE_PROBE_COLUMNS[table_name]
    with psycopg2.connect(migrated_db, user=ROLE) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table_name}")
            assert cur.fetchone()[0] >= 0
            cur.execute("SAVEPOINT update_probe")
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(
                    f"UPDATE {table_name} "
                    f"SET {update_column}={update_column} WHERE false"
                )
            cur.execute("ROLLBACK TO SAVEPOINT update_probe")
