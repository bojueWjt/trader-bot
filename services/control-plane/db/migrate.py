#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json


REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
MIGRATION_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.(?P<direction>up|down)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_path: Path
    down_path: Path


def _database_url(database_env_file: Path | None = None) -> str:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url and database_env_file is not None:
        database_url = _database_url_from_env_file(database_env_file)
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    return database_url


def _database_url_from_env_file(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"database environment file is unreadable: {path}") from exc
    values = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "DATABASE_URL":
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values.append(value)
    if len(values) != 1 or not values[0]:
        raise SystemExit(
            "database environment file must contain exactly one DATABASE_URL"
        )
    return values[0]


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


def _record_applied_migration(cur, migration: Migration) -> None:
    cur.execute(
        "SELECT name FROM schema_migrations WHERE version = %s",
        (migration.version,),
    )
    registered = cur.fetchone()
    if registered is None:
        cur.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
            (migration.version, migration.name),
        )
        return
    registered_name = str(registered[0])
    if registered_name != migration.name:
        raise RuntimeError(
            f"migration {migration.version} registered as "
            f"{registered_name}, expected {migration.name}"
        )


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
                _record_applied_migration(cur, migration)
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


def acquire_maintenance_fence(
    conn,
    *,
    fence_id: UUID,
    operation: str,
    actor: str,
    owner_token: str,
    lease_seconds: int,
    heartbeat_max_age_seconds: int,
) -> dict[str, object]:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fence_id::text,
                       lease_version,
                       expires_at,
                       account_evidence
                FROM acquire_control_plane_maintenance_fence(
                    %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    str(fence_id),
                    operation,
                    actor,
                    owner_token,
                    lease_seconds,
                    heartbeat_max_age_seconds,
                ),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("maintenance fence acquisition returned no row")
    return _maintenance_fence_receipt(row)


def verify_maintenance_fence(
    conn,
    *,
    fence_id: UUID,
    owner_token: str,
    stage: str,
    lease_seconds: int,
    heartbeat_max_age_seconds: int,
) -> dict[str, object]:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fence_id::text,
                       lease_version,
                       expires_at,
                       account_evidence
                FROM verify_control_plane_maintenance_fence(
                    %s, %s, %s, %s, %s
                )
                """,
                (
                    str(fence_id),
                    owner_token,
                    stage,
                    lease_seconds,
                    heartbeat_max_age_seconds,
                ),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("maintenance fence verification returned no row")
    return _maintenance_fence_receipt(row)


def verify_frozen_maintenance_fence(
    conn,
    *,
    fence_id: UUID,
    owner_token: str,
    stage: str,
    lease_seconds: int,
    account_evidence_sha256: str,
) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-f]{64}", owner_token) is None:
        raise RuntimeError("maintenance owner token is invalid")
    if re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", stage) is None:
        raise RuntimeError("maintenance stage is invalid")
    if lease_seconds < 15 or lease_seconds > 300:
        raise RuntimeError(
            "maintenance lease must be between 15 and 300 seconds"
        )
    if re.fullmatch(r"[0-9a-f]{64}", account_evidence_sha256) is None:
        raise RuntimeError("maintenance fence evidence hash is invalid")

    owner_token_sha256 = hashlib.sha256(
        owner_token.encode("utf-8")
    ).hexdigest()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtext(
                        'trader-v3-control-plane-maintenance-fence'
                    )
                )
                """
            )
            cur.execute(
                """
                SELECT operation,
                       actor,
                       owner_token_sha256,
                       status,
                       lease_version,
                       account_evidence,
                       expires_at > clock_timestamp()
                FROM control_plane_maintenance_fences
                WHERE fence_id=%s
                FOR UPDATE
                """,
                (str(fence_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("maintenance fence is unavailable")
            (
                operation,
                actor,
                current_owner_token_sha256,
                status,
                lease_version,
                account_evidence,
                lease_active,
            ) = row
            if status != "active" or not lease_active:
                raise RuntimeError(
                    "maintenance fence is inactive or expired"
                )
            if current_owner_token_sha256 != owner_token_sha256:
                raise RuntimeError(
                    "maintenance fence owner token mismatch"
                )

            current_evidence_sha256 = _canonical_json_sha256(
                account_evidence
            )
            if current_evidence_sha256 != account_evidence_sha256:
                raise RuntimeError(
                    "maintenance fence evidence hash mismatch"
                )
            if not isinstance(account_evidence, list):
                raise RuntimeError(
                    "maintenance fence account evidence is invalid"
                )
            evidence_epochs = set()
            for item in account_evidence:
                if not isinstance(item, dict):
                    raise RuntimeError(
                        "maintenance fence account evidence is invalid"
                    )
                epoch = str(item.get("redis_fencing_epoch") or "").strip()
                if not epoch:
                    raise RuntimeError(
                        "maintenance fence account evidence lacks "
                        "Redis fencing epoch"
                    )
                evidence_epochs.add(epoch)
            if len(evidence_epochs) != 1:
                raise RuntimeError(
                    "maintenance fence account evidence has mixed "
                    "Redis fencing epochs"
                )

            cur.execute(
                """
                SELECT redis_fencing_epoch::text
                FROM redis_fencing_epochs
                WHERE domain='trader-v3'
                  AND status='active'
                FOR SHARE
                """
            )
            active_epoch_rows = cur.fetchall()
            if len(active_epoch_rows) != 1:
                raise RuntimeError(
                    "active trader-v3 Redis fencing epoch is unavailable"
                )
            active_redis_fencing_epoch = str(active_epoch_rows[0][0])
            if evidence_epochs != {active_redis_fencing_epoch}:
                raise RuntimeError(
                    "maintenance fence Redis fencing epoch drifted"
                )

            cur.execute(
                """
                UPDATE control_plane_maintenance_fences
                SET lease_version=lease_version + 1,
                    refreshed_at=clock_timestamp(),
                    expires_at=clock_timestamp()
                        + make_interval(secs => %s),
                    last_stage=%s
                WHERE fence_id=%s
                  AND status='active'
                  AND expires_at > clock_timestamp()
                RETURNING lease_version,
                          expires_at,
                          account_evidence
                """,
                (lease_seconds, stage, str(fence_id)),
            )
            updated = cur.fetchone()
            if updated is None:
                raise RuntimeError(
                    "maintenance fence is inactive or expired"
                )
            next_lease_version, expires_at, frozen_evidence = updated
            details = {
                "account_evidence_sha256": account_evidence_sha256,
                "redis_fencing_epoch": active_redis_fencing_epoch,
                "verification_mode": "frozen_pre_stop_evidence",
            }
            cur.execute(
                """
                INSERT INTO control_plane_maintenance_fence_events (
                    fence_id,
                    event_type,
                    operation,
                    actor,
                    stage,
                    lease_version,
                    expires_at,
                    account_evidence,
                    details
                )
                VALUES (
                    %s,
                    'stage_verified',
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    str(fence_id),
                    operation,
                    actor,
                    stage,
                    next_lease_version,
                    expires_at,
                    Json(frozen_evidence),
                    Json(details),
                ),
            )
    return _maintenance_fence_receipt(
        (
            str(fence_id),
            next_lease_version,
            expires_at,
            frozen_evidence,
        )
    )


def release_maintenance_fence(
    conn,
    *,
    fence_id: UUID,
    owner_token: str,
    actor: str,
    reason: str,
) -> dict[str, object]:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT release_control_plane_maintenance_fence(
                    %s, %s, %s, %s
                )
                """,
                (str(fence_id), owner_token, actor, reason),
            )
            row = cur.fetchone()
    released = bool(row and row[0])
    return {
        "fence_id": str(fence_id),
        "released": released,
    }


def _maintenance_fence_receipt(row) -> dict[str, object]:
    account_evidence = row[3]
    return {
        "schema_version": "trader-v3-maintenance-fence-receipt/v1",
        "fence_id": str(row[0]),
        "lease_version": int(row[1]),
        "expires_at": row[2].isoformat(),
        "account_evidence": account_evidence,
        "account_evidence_sha256": _canonical_json_sha256(
            account_evidence
        ),
    }


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_uuid(value: str, label: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise SystemExit(f"{label} must be a UUID") from exc


def _add_database_source_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-env-file",
        type=Path,
        help="Environment file containing exactly one DATABASE_URL.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage PostgreSQL migrations and maintenance fences.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up", help="Apply pending migrations.")
    commands.add_parser("down", help="Roll back all applied migrations.")

    fence_parser = commands.add_parser(
        "maintenance-fence",
        help="Acquire, verify, or release the shared maintenance fence.",
    )
    fence_actions = fence_parser.add_subparsers(
        dest="fence_action",
        required=True,
    )
    acquire_parser = fence_actions.add_parser("acquire")
    verify_parser = fence_actions.add_parser("verify")
    verify_frozen_parser = fence_actions.add_parser("verify-frozen")
    release_parser = fence_actions.add_parser("release")
    for action_parser in (
        acquire_parser,
        verify_parser,
        verify_frozen_parser,
        release_parser,
    ):
        _add_database_source_argument(action_parser)
        action_parser.add_argument("--owner-token", required=True)

    acquire_parser.add_argument("--fence-id", default=str(uuid4()))
    acquire_parser.add_argument("--operation", required=True)
    acquire_parser.add_argument("--actor", required=True)
    acquire_parser.add_argument("--lease-seconds", type=int, default=120)
    acquire_parser.add_argument(
        "--heartbeat-max-age-seconds",
        type=int,
        default=15,
    )

    verify_parser.add_argument("--fence-id", required=True)
    verify_parser.add_argument("--stage", required=True)
    verify_parser.add_argument("--lease-seconds", type=int, default=120)
    verify_parser.add_argument(
        "--heartbeat-max-age-seconds",
        type=int,
        default=15,
    )

    verify_frozen_parser.add_argument("--fence-id", required=True)
    verify_frozen_parser.add_argument("--stage", required=True)
    verify_frozen_parser.add_argument("--lease-seconds", type=int, default=120)
    verify_frozen_parser.add_argument(
        "--account-evidence-sha256",
        required=True,
    )

    release_parser.add_argument("--fence-id", required=True)
    release_parser.add_argument("--actor", required=True)
    release_parser.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    database_env_file = getattr(args, "database_env_file", None)
    conn = psycopg2.connect(_database_url(database_env_file))
    try:
        if args.command in {"up", "down"}:
            migrations = _discover_migrations()
            if not migrations:
                raise SystemExit(f"No migrations found in {MIGRATIONS_DIR}")
        if args.command == "up":
            migrate_up(conn, migrations)
            return 0
        if args.command == "down":
            migrate_down(conn, migrations)
            return 0

        fence_id = _required_uuid(args.fence_id, "fence_id")
        if args.fence_action == "acquire":
            receipt = acquire_maintenance_fence(
                conn,
                fence_id=fence_id,
                operation=args.operation,
                actor=args.actor,
                owner_token=args.owner_token,
                lease_seconds=args.lease_seconds,
                heartbeat_max_age_seconds=(
                    args.heartbeat_max_age_seconds
                ),
            )
        elif args.fence_action == "verify":
            receipt = verify_maintenance_fence(
                conn,
                fence_id=fence_id,
                owner_token=args.owner_token,
                stage=args.stage,
                lease_seconds=args.lease_seconds,
                heartbeat_max_age_seconds=(
                    args.heartbeat_max_age_seconds
                ),
            )
        elif args.fence_action == "verify-frozen":
            receipt = verify_frozen_maintenance_fence(
                conn,
                fence_id=fence_id,
                owner_token=args.owner_token,
                stage=args.stage,
                lease_seconds=args.lease_seconds,
                account_evidence_sha256=args.account_evidence_sha256,
            )
        else:
            receipt = release_maintenance_fence(
                conn,
                fence_id=fence_id,
                owner_token=args.owner_token,
                actor=args.actor,
                reason=args.reason,
            )
        print(json.dumps(receipt, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
