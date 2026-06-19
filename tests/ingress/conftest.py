from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg2
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE = REPO_ROOT / "services" / "control-plane" / "db" / "migrate.py"
PG_PORT = "55436"
PG_SOCKET_PREFIX = "pg-a03-"


def _find_pg_tool(name: str) -> str:
    candidates = [
        f"/opt/homebrew/opt/postgresql@16/bin/{name}",
        shutil.which(name),
        f"/usr/local/opt/postgresql@16/bin/{name}",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError(f"{name} not found; install Homebrew postgresql@16")


def _database_url(socket_dir: Path) -> str:
    return f"postgresql://localhost/postgres?host={socket_dir}&port={PG_PORT}"


@pytest.fixture(scope="session")
def pg_cluster():
    initdb = _find_pg_tool("initdb")
    pg_ctl = _find_pg_tool("pg_ctl")
    data_dir = Path(tempfile.mkdtemp(prefix="pg-a03-data-", dir="/tmp"))
    socket_dir = Path(tempfile.mkdtemp(prefix=PG_SOCKET_PREFIX, dir="/tmp"))
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
            # canonical plane operates in UTC; keeps timestamptz::text deterministic across machine TZs
            f"-p {PG_PORT} -k {socket_dir} -c timezone=UTC",
            "-w",
            "start",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    database_url = _database_url(socket_dir)
    os.environ["DATABASE_URL"] = database_url
    try:
        yield {"url": database_url, "pg_ctl": pg_ctl, "data_dir": data_dir, "socket_dir": socket_dir}
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-w", "stop"],
            text=True,
            capture_output=True,
        )
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture()
def db_url(pg_cluster):
    return pg_cluster["url"]


@pytest.fixture()
def run_migration(pg_cluster):
    def _run(direction: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["DATABASE_URL"] = pg_cluster["url"]
        result = subprocess.run(
            [sys.executable, str(MIGRATE), direction],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    return _run


@pytest.fixture()
def migrated_db(run_migration, db_url):
    run_migration("down")
    run_migration("up")
    try:
        yield db_url
    finally:
        run_migration("down")


@pytest.fixture()
def db_conn(migrated_db):
    conn = psycopg2.connect(migrated_db)
    try:
        yield conn
    finally:
        conn.close()
