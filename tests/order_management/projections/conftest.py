from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg2
import pytest


ROOT = Path(__file__).resolve().parents[3]
NAUTILUS_NODE = ROOT / "services" / "nautilus-node"
MIGRATE = ROOT / "services" / "control-plane" / "db" / "migrate.py"

for _path in (NAUTILUS_NODE,):
    _p = str(_path)
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(scope="session")
def projection_db_url():
    configured = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om2")
    try:
        with psycopg2.connect(configured):
            pass
    except psycopg2.OperationalError:
        yield from _temporary_migrated_postgres()
    else:
        yield configured


@pytest.fixture()
def db_conn(projection_db_url):
    conn = psycopg2.connect(projection_db_url)
    try:
        conn.autocommit = True
        _clean_projection_tables(conn)
        conn.autocommit = False
        yield conn
    finally:
        conn.rollback()
        conn.autocommit = True
        _clean_projection_tables(conn)
        conn.close()


def _clean_projection_tables(conn) -> None:
    with conn.cursor() as cur:
        for table in (
            "protective_orders_projection",
            "order_events",
            "reconciliation_findings",
            "reconciliation_runs",
            "price_feed_status",
            "orders_projection",
            "positions_projection",
            "accounts_projection",
            "execution_events",
        ):
            cur.execute(f"DELETE FROM {table}")


def _temporary_migrated_postgres():
    data_dir = Path(tempfile.mkdtemp(prefix="om2-pg-data-", dir="/private/tmp"))
    socket_dir = Path(tempfile.mkdtemp(prefix="om2-pg-socket-", dir="/private/tmp"))
    pg_ctl: str | None = None
    started = False
    try:
        initdb = _find_pg_tool("initdb")
        pg_ctl = _find_pg_tool("pg_ctl")
        port = str(55450 + (os.getpid() % 1000))
        log_file = data_dir / "postgres.log"
        subprocess.run(
            [initdb, "-D", str(data_dir), "-A", "trust", "-U", os.environ.get("USER", "pudu")],
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
                f"-p {port} -k {socket_dir} -c timezone=UTC",
                "-w",
                "start",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        started = True
        database_url = f"postgresql://localhost/postgres?host={socket_dir}&port={port}"
        env = os.environ.copy()
        env["DATABASE_URL"] = database_url
        migrated = subprocess.run(
            [sys.executable, str(MIGRATE), "up"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        assert migrated.returncode == 0, migrated.stdout + migrated.stderr
        yield database_url
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"projection DB unavailable and temporary postgres could not start: {exc}")
    finally:
        if started and pg_ctl is not None:
            subprocess.run(
                [pg_ctl, "-D", str(data_dir), "-w", "stop"],
                text=True,
                capture_output=True,
            )
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


def _find_pg_tool(name: str) -> str:
    for candidate in (
        shutil.which(name),
        f"/opt/homebrew/opt/postgresql@16/bin/{name}",
        f"/usr/local/opt/postgresql@16/bin/{name}",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError(f"{name} not found; install PostgreSQL 16")
