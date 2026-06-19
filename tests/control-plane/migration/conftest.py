from __future__ import annotations

import getpass
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg2
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
DDL = REPO_ROOT / "services" / "control-plane" / "migration" / "legacy_ddl.sql"
IMPORTER = REPO_ROOT / "scripts" / "migrate_legacy_sqlite.py"
PG_PORT = "55435"
DEFAULT_SOCKET_DIR = Path("/tmp")
DATABASE_URL = f"postgresql://localhost/postgres?host={DEFAULT_SOCKET_DIR}&port={PG_PORT}"
LEGACY_TABLES = (
    "legacy_signal_events",
    "legacy_signal_operations",
    "legacy_signals",
)


def _find_pg_tool(name: str) -> str:
    candidates = [
        f"/opt/homebrew/opt/postgresql@16/bin/{name}",
        f"/usr/local/opt/postgresql@16/bin/{name}",
        shutil.which(name),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError(f"{name} not found; install Homebrew postgresql@16")


def _can_connect(dsn: str) -> bool:
    try:
        with psycopg2.connect(dsn):
            return True
    except psycopg2.OperationalError:
        return False


def _reset_legacy_tables(dsn: str) -> None:
    ddl = DDL.read_text()
    with psycopg2.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS "
                + ", ".join(LEGACY_TABLES)
                + " CASCADE"
            )
            cur.execute(ddl)


@pytest.fixture(scope="session")
def pg_cluster():
    if _can_connect(DATABASE_URL):
        yield {"url": DATABASE_URL, "started": False}
        return

    initdb = _find_pg_tool("initdb")
    pg_ctl = _find_pg_tool("pg_ctl")
    data_dir = Path(tempfile.mkdtemp(prefix="pg-a10-data-"))
    socket_dir = Path(tempfile.mkdtemp(prefix="pg-a10-", dir="/tmp"))
    log_file = data_dir / "postgres.log"

    subprocess.run(
        [initdb, "-D", str(data_dir), "-A", "trust", "-U", getpass.getuser()],
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data_dir),
            "-l",
            str(log_file),
            "-o",
            f"-p {PG_PORT} -k {socket_dir}",
            "-w",
            "start",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    dsn = f"postgresql://localhost/postgres?host={socket_dir}&port={PG_PORT}"
    try:
        yield {"url": dsn, "started": True, "pg_ctl": pg_ctl, "data_dir": data_dir}
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-w", "stop"],
            text=True,
            capture_output=True,
        )
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture()
def pg_dsn(pg_cluster):
    _reset_legacy_tables(pg_cluster["url"])
    return pg_cluster["url"]


@pytest.fixture()
def legacy_sqlite_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'raw',
                message_type TEXT NOT NULL DEFAULT 'new_signal',
                pair TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                received_at TEXT NOT NULL DEFAULT '',
                approved_at TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_kind TEXT NOT NULL DEFAULT 'audit',
                event_type TEXT NOT NULL DEFAULT '',
                signal_id TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE signal_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'reserved',
                created_at TEXT NOT NULL DEFAULT '',
                UNIQUE(signal_id, operation_type)
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO signals (
                signal_id, status, message_type, pair, payload,
                received_at, approved_at, expires_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "sig-1",
                    "raw",
                    "new_signal",
                    "BTCUSDT",
                    '{"entry": 100, "api_key": "remove-me", "nested": {"secret": "remove-me"}}',
                    "2026-06-19T12:00:00Z",
                    "",
                    "",
                    "2026-06-19T12:00:01Z",
                    "2026-06-19T12:00:02Z",
                ),
                (
                    "sig-2",
                    "approved",
                    "close_update",
                    "ETHUSDT",
                    '{"close": true, "notes": ["safe"]}',
                    "2026-06-19T13:00:00Z",
                    "2026-06-19T13:01:00Z",
                    "",
                    "2026-06-19T13:00:01Z",
                    "2026-06-19T13:00:02Z",
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO signal_events (
                event_kind, event_type, signal_id, payload, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    "audit",
                    "received",
                    "sig-1",
                    '{"telegram_message_id": "m-1", "token": "remove-me"}',
                    "2026-06-19T12:00:03Z",
                ),
                (
                    "decision",
                    "approved",
                    "sig-2",
                    '{"approved_by": "operator", "credentials": {"password": "remove-me"}}',
                    "2026-06-19T13:01:03Z",
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO signal_operations (
                signal_id, operation_type, status, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            [
                ("sig-1", "reserve", "reserved", "2026-06-19T12:00:04Z"),
                ("sig-2", "submit", "completed", "2026-06-19T13:01:04Z"),
            ],
        )
    return db_path


@pytest.fixture()
def run_import(pg_dsn: str, legacy_sqlite_path: Path):
    def _run() -> str:
        result = subprocess.run(
            [
                sys.executable,
                str(IMPORTER),
                "--source",
                str(legacy_sqlite_path),
                "--pg-dsn",
                pg_dsn,
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            env=os.environ.copy(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    return _run
