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
CONTROL_PLANE = ROOT / "services" / "control-plane"
CONTROL_PLANE_RISK = CONTROL_PLANE / "risk"
NAUTILUS_NODE = ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"

for _path in (NAUTILUS_NODE, CONTROL_PLANE, CONTROL_PLANE_RISK, EXECUTION_DOMAIN):
    _p = str(_path)
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(scope="session")
def simulated_db_url():
    url = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om8b")
    try:
        with psycopg2.connect(url):
            pass
    except psycopg2.OperationalError as exc:
        yield from _temporary_migrated_postgres(f"simulated venue DB unavailable at {url}: {exc}")
    else:
        yield url


@pytest.fixture()
def db_conn(simulated_db_url: str):
    conn = psycopg2.connect(simulated_db_url)
    try:
        _clean_simulated_rows(conn)
        yield conn
    finally:
        conn.rollback()
        _clean_simulated_rows(conn)
        conn.close()


def _clean_simulated_rows(conn) -> None:
    conn.rollback()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for table in (
                "order_links",
                "protective_orders_projection",
                "reconciliation_findings",
                "reconciliation_runs",
                "price_feed_status",
                "risk_reservations",
                "orders_projection",
                "positions_projection",
                "accounts_projection",
                "execution_events",
                "audit_events",
                "outbox_events",
            ):
                cur.execute(f"DELETE FROM {table} WHERE account_id LIKE 'acct-sim%'") if table not in {
                    "audit_events",
                    "outbox_events",
                } else cur.execute(
                    f"DELETE FROM {table} WHERE aggregate_id LIKE 'acct-sim%' OR payload::text LIKE '%acct-sim%'"
                )
    finally:
        conn.autocommit = False


def _temporary_migrated_postgres(reason: str):
    data_dir = Path(tempfile.mkdtemp(prefix="om8-sim-pg-data-", dir="/private/tmp"))
    socket_dir = Path(tempfile.mkdtemp(prefix="om8-sim-pg-socket-", dir="/private/tmp"))
    pg_ctl: str | None = None
    started = False
    try:
        initdb = _find_pg_tool("initdb")
        pg_ctl = _find_pg_tool("pg_ctl")
        port = str(55580 + (os.getpid() % 1000))
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
            [sys.executable, str(ROOT / "services" / "control-plane" / "db" / "migrate.py"), "up"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        assert migrated.returncode == 0, migrated.stdout + migrated.stderr
        yield database_url
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"{reason}; temporary postgres could not start: {exc}")
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
