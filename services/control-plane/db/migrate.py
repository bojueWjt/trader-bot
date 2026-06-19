#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg2


REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
MIGRATION_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.(?P<direction>up|down)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_path: Path
    down_path: Path


def _database_url() -> str:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    return database_url


def _discover_migrations() -> list[Migration]:
    found: dict[tuple[str, str], dict[str, Path]] = {}
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = MIGRATION_RE.match(path.name)
        if not match:
            continue
        key = (match.group("version"), match.group("name"))
        found.setdefault(key, {})[match.group("direction")] = path

    migrations: list[Migration] = []
    for (version, name), paths in sorted(found.items()):
        if "up" not in paths or "down" not in paths:
            raise SystemExit(f"Migration {version}_{name} must have both up and down files")
        migrations.append(Migration(version, name, paths["up"], paths["down"]))
    return migrations


def _ensure_tracking_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                name text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )


def _tracking_table_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        return bool(cur.fetchone()[0])


def _applied_versions(conn) -> set[str]:
    if not _tracking_table_exists(conn):
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def migrate_up(conn, migrations: list[Migration]) -> None:
    with conn:
        _ensure_tracking_table(conn)

    applied = _applied_versions(conn)
    pending = [migration for migration in migrations if migration.version not in applied]
    if not pending:
        print("No pending migrations.")
        return

    for migration in pending:
        sql = migration.up_path.read_text()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (migration.version, migration.name),
                )
        print(f"Applied {migration.version}_{migration.name}")


def migrate_down(conn, migrations: list[Migration]) -> None:
    if not _tracking_table_exists(conn):
        print("No schema_migrations table; nothing to roll back.")
        return

    migrations_by_version = {migration.version: migration for migration in migrations}
    with conn.cursor() as cur:
        cur.execute("SELECT version, name FROM schema_migrations ORDER BY version DESC")
        applied = cur.fetchall()

    if not applied:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS schema_migrations")
        print("No applied migrations.")
        return

    for version, name in applied:
        migration = migrations_by_version.get(version)
        if migration is None:
            raise SystemExit(f"Applied migration {version}_{name} has no local down file")
        sql = migration.down_path.read_text()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
        print(f"Rolled back {migration.version}_{migration.name}")

    with conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS schema_migrations")
    print("Dropped schema_migrations")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or roll back PostgreSQL migrations.")
    parser.add_argument("direction", choices=["up", "down"], help="Migration direction")
    args = parser.parse_args(argv)

    migrations = _discover_migrations()
    if not migrations:
        raise SystemExit(f"No migrations found in {MIGRATIONS_DIR}")

    # Do NOT use `with conn:` here: it opens a transaction context on the
    # connection, and migrate_up/migrate_down nest their own per-migration
    # `with conn:` blocks (psycopg2 forbids re-entering recursively).
    conn = psycopg2.connect(_database_url())
    try:
        if args.direction == "up":
            migrate_up(conn, migrations)
        else:
            migrate_down(conn, migrations)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
