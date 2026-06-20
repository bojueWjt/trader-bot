#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import Json


REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_DDL = REPO_ROOT / "services" / "control-plane" / "migration" / "legacy_ddl.sql"
SENSITIVE_FIELD_SUBSTRINGS = (
    "api_key",
    "secret",
    "token",
    "password",
    "credential",
)
TABLE_ORDER = (
    "legacy_signals",
    "legacy_signal_events",
    "legacy_signal_operations",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import read-only legacy SQLite signal history into PostgreSQL legacy_* tables."
    )
    parser.add_argument("--source", required=True, help="Path to the legacy SQLite database")
    parser.add_argument("--pg-dsn", required=True, help="Target PostgreSQL DSN")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.exists():
        parser.error(f"--source does not exist: {source}")

    with sqlite3.connect(source) as sqlite_conn:
        sqlite_conn.row_factory = sqlite3.Row
        with psycopg2.connect(args.pg_dsn) as pg_conn:
            ensure_legacy_tables(pg_conn)
            # Legacy SQLite schemas vary across deployments. Import each source table
            # that exists; skip (don't abort) the ones this DB doesn't have. The
            # sqlite read raises before any PostgreSQL write, so the tx stays clean.
            for label, importer in (
                ("signals", import_signals),
                ("signal_events", import_signal_events),
                ("signal_operations", import_signal_operations),
            ):
                try:
                    importer(sqlite_conn, pg_conn)
                except sqlite3.OperationalError as exc:
                    print(f"skip legacy source '{label}': {exc}")
            pg_conn.commit()

            for table_name in TABLE_ORDER:
                row_count, checksum = reconciliation_for_table(pg_conn, table_name)
                print(f"{table_name} row_count={row_count} sha256={checksum}")

    return 0


def ensure_legacy_tables(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(LEGACY_DDL.read_text())


def import_signals(sqlite_conn: sqlite3.Connection, pg_conn) -> None:
    rows = sqlite_conn.execute(
        """
        SELECT
            signal_id, status, message_type, pair, payload,
            received_at, approved_at, expires_at, created_at, updated_at
        FROM signals
        ORDER BY signal_id
        """
    ).fetchall()

    with pg_conn.cursor() as cur:
        for row in rows:
            mapped = {
                "legacy_id": str(row["signal_id"]),
                "signal_id": row["signal_id"],
                "status": row["status"],
                "message_type": row["message_type"],
                "pair": row["pair"],
                "payload": sanitized_payload(row["payload"]),
                "received_at": row["received_at"],
                "approved_at": row["approved_at"],
                "expires_at": row["expires_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            source_hash = source_hash_for(mapped)
            cur.execute(
                """
                INSERT INTO legacy_signals (
                    legacy_id, signal_id, status, message_type, pair, payload,
                    received_at, approved_at, expires_at, created_at, updated_at,
                    source_hash
                )
                VALUES (
                    %(legacy_id)s, %(signal_id)s, %(status)s, %(message_type)s,
                    %(pair)s, %(payload)s, %(received_at)s, %(approved_at)s,
                    %(expires_at)s, %(created_at)s, %(updated_at)s, %(source_hash)s
                )
                ON CONFLICT (legacy_id, source_hash) DO NOTHING
                """,
                {**mapped, "payload": Json(mapped["payload"]), "source_hash": source_hash},
            )


def import_signal_events(sqlite_conn: sqlite3.Connection, pg_conn) -> None:
    rows = sqlite_conn.execute(
        """
        SELECT id, event_kind, event_type, signal_id, payload, created_at
        FROM signal_events
        ORDER BY id
        """
    ).fetchall()

    with pg_conn.cursor() as cur:
        for row in rows:
            mapped = {
                "legacy_id": str(row["id"]),
                "source_event_id": row["id"],
                "event_kind": row["event_kind"],
                "event_type": row["event_type"],
                "signal_id": row["signal_id"],
                "payload": sanitized_payload(row["payload"]),
                "created_at": row["created_at"],
            }
            source_hash = source_hash_for(mapped)
            cur.execute(
                """
                INSERT INTO legacy_signal_events (
                    legacy_id, source_event_id, event_kind, event_type,
                    signal_id, payload, created_at, source_hash
                )
                VALUES (
                    %(legacy_id)s, %(source_event_id)s, %(event_kind)s,
                    %(event_type)s, %(signal_id)s, %(payload)s,
                    %(created_at)s, %(source_hash)s
                )
                ON CONFLICT (legacy_id, source_hash) DO NOTHING
                """,
                {**mapped, "payload": Json(mapped["payload"]), "source_hash": source_hash},
            )


def import_signal_operations(sqlite_conn: sqlite3.Connection, pg_conn) -> None:
    rows = sqlite_conn.execute(
        """
        SELECT id, signal_id, operation_type, status, created_at
        FROM signal_operations
        ORDER BY id
        """
    ).fetchall()

    with pg_conn.cursor() as cur:
        for row in rows:
            mapped = {
                "legacy_id": str(row["id"]),
                "source_operation_id": row["id"],
                "signal_id": row["signal_id"],
                "operation_type": row["operation_type"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            source_hash = source_hash_for(mapped)
            cur.execute(
                """
                INSERT INTO legacy_signal_operations (
                    legacy_id, source_operation_id, signal_id, operation_type,
                    status, created_at, source_hash
                )
                VALUES (
                    %(legacy_id)s, %(source_operation_id)s, %(signal_id)s,
                    %(operation_type)s, %(status)s, %(created_at)s,
                    %(source_hash)s
                )
                ON CONFLICT (legacy_id, source_hash) DO NOTHING
                """,
                {**mapped, "source_hash": source_hash},
            )


def sanitized_payload(raw_payload: str | None) -> Any:
    if raw_payload is None or raw_payload == "":
        return {}

    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError:
        parsed = raw_payload
    return remove_sensitive_fields(parsed)


def remove_sensitive_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        clean = {}
        for key, item in value.items():
            if is_sensitive_field_name(str(key)):
                continue
            clean[key] = remove_sensitive_fields(item)
        return clean
    if isinstance(value, list):
        return [remove_sensitive_fields(item) for item in value]
    return value


def is_sensitive_field_name(field_name: str) -> bool:
    lower_name = field_name.lower()
    return any(substring in lower_name for substring in SENSITIVE_FIELD_SUBSTRINGS)


def source_hash_for(mapped_row: Mapping[str, Any]) -> str:
    payload = json.dumps(
        mapped_row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reconciliation_for_table(pg_conn, table_name: str) -> tuple[int, str]:
    if table_name == "legacy_signals":
        select_sql = """
            SELECT
                legacy_id, source_hash, signal_id, status, message_type, pair,
                payload::text AS payload, received_at, approved_at, expires_at,
                created_at, updated_at
            FROM legacy_signals
            ORDER BY legacy_id, source_hash
        """
    elif table_name == "legacy_signal_events":
        select_sql = """
            SELECT
                legacy_id, source_hash, source_event_id, event_kind, event_type,
                signal_id, payload::text AS payload, created_at
            FROM legacy_signal_events
            ORDER BY legacy_id, source_hash
        """
    elif table_name == "legacy_signal_operations":
        select_sql = """
            SELECT
                legacy_id, source_hash, source_operation_id, signal_id,
                operation_type, status, created_at
            FROM legacy_signal_operations
            ORDER BY legacy_id, source_hash
        """
    else:
        raise ValueError(f"unsupported legacy table for reconciliation: {table_name}")

    with pg_conn.cursor() as cur:
        cur.execute(select_sql)
        columns = [description.name for description in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return len(rows), hashlib.sha256(payload.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
