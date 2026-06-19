from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from typing import Mapping
from urllib.parse import unquote, urlparse


SIGNAL_STORE_ENV_KEYS = (
    "HERMES_SIGNAL_STORE_URL",
    "SIGNAL_STORE_URL",
    "SIGNAL_STORE_DB_PATH",
    "TRADER_BRIDGE_SIGNAL_DB_PATH",
)

SIGNALS_COLUMNS: dict[str, str] = {
    "status": "TEXT NOT NULL DEFAULT 'raw'",
    "message_type": "TEXT NOT NULL DEFAULT 'new_signal'",
    "pair": "TEXT NOT NULL DEFAULT ''",
    "payload": "TEXT NOT NULL DEFAULT '{}'",
    "received_at": "TEXT NOT NULL DEFAULT ''",
    "approved_at": "TEXT NOT NULL DEFAULT ''",
    "expires_at": "TEXT NOT NULL DEFAULT ''",
    "created_at": "TEXT NOT NULL DEFAULT ''",
    "updated_at": "TEXT NOT NULL DEFAULT ''",
}

SIGNAL_OPERATIONS_COLUMNS: dict[str, str] = {
    "status": "TEXT NOT NULL DEFAULT 'reserved'",
    "created_at": "TEXT NOT NULL DEFAULT ''",
}

SIGNAL_EVENTS_COLUMNS: dict[str, str] = {
    "event_kind": "TEXT NOT NULL DEFAULT 'audit'",
    "event_type": "TEXT NOT NULL DEFAULT ''",
    "signal_id": "TEXT NOT NULL DEFAULT ''",
    "payload": "TEXT NOT NULL DEFAULT '{}'",
    "created_at": "TEXT NOT NULL DEFAULT ''",
}


def configured_signal_store_path(
    explicit_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    if explicit_path:
        return signal_store_path(explicit_path)

    active_environ = environ
    if active_environ is None:
        active_environ = os.environ

    for key in SIGNAL_STORE_ENV_KEYS:
        value = active_environ.get(key, "")
        if value:
            return signal_store_path(value)
    return None


def signal_store_path(value: str | Path) -> str:
    raw_value = str(value)
    if raw_value == ":memory:":
        return raw_value
    if raw_value.startswith("sqlite://"):
        return sqlite_path_from_url(raw_value)
    return raw_value


def sqlite_path_from_url(store_url: str) -> str:
    if store_url == "sqlite:///:memory:":
        return ":memory:"

    parsed = urlparse(store_url)
    if parsed.scheme != "sqlite":
        raise ValueError("unsupported signal store url")

    raw_path = unquote(parsed.path)
    if parsed.netloc:
        raw_path = f"{parsed.netloc}{raw_path}"

    if store_url.startswith("sqlite:////"):
        while raw_path.startswith("//"):
            raw_path = raw_path[1:]
        return f"/{raw_path.lstrip('/')}"

    if raw_path.startswith("/"):
        raw_path = raw_path[1:]

    while raw_path.startswith("//"):
        raw_path = raw_path[1:]
    return raw_path


def connect_signal_store(db_path: str | Path) -> sqlite3.Connection:
    active_path = signal_store_path(db_path)
    if active_path != ":memory:":
        Path(active_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(active_path, timeout=30, isolation_level="IMMEDIATE")
    connection.row_factory = sqlite3.Row
    return connection


def migrate_signal_store_schema(db_path_or_connection: str | Path | sqlite3.Connection) -> None:
    owns_connection = not isinstance(db_path_or_connection, sqlite3.Connection)
    if owns_connection:
        connection = connect_signal_store(db_path_or_connection)
    else:
        connection = db_path_or_connection
        connection.row_factory = sqlite3.Row

    try:
        _migrate_connection(connection)
        connection.commit()
    finally:
        if owns_connection:
            connection.close()


def _migrate_connection(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'raw',
            message_type TEXT NOT NULL DEFAULT 'new_signal',
            pair TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            received_at TEXT NOT NULL DEFAULT '',
            approved_at TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    _ensure_columns(connection, "signals", SIGNALS_COLUMNS)

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(signal_id, operation_type)
        )
        """
    )
    _ensure_columns(connection, "signal_operations", SIGNAL_OPERATIONS_COLUMNS)

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_kind TEXT NOT NULL DEFAULT 'audit',
            event_type TEXT NOT NULL DEFAULT '',
            signal_id TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    _ensure_columns(connection, "signal_events", SIGNAL_EVENTS_COLUMNS)

    connection.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_signals_pair ON signals(pair)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_signals_received_at ON signals(received_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_signal_events_signal_id ON signal_events(signal_id)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_operations_signal_id ON signal_operations(signal_id)"
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version)
        VALUES ('0001_signal_store')
        """
    )


def _ensure_columns(
    connection: sqlite3.Connection,
    table_name: str,
    required_columns: dict[str, str],
) -> None:
    existing_columns = _table_columns(connection, table_name)
    for column_name, definition in required_columns.items():
        if column_name in existing_columns:
            continue
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    cursor = connection.execute(f"PRAGMA table_info({table_name})")
    return {str(row["name"]) for row in cursor.fetchall()}
