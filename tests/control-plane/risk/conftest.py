from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg2
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_DIR = REPO_ROOT / "services" / "control-plane" / "db"
RISK_DIR = REPO_ROOT / "services" / "control-plane" / "risk"
GATEWAY_DIR = REPO_ROOT / "services" / "control-plane" / "decision_gateway"
MIGRATE = DB_DIR / "migrate.py"
PG_PORT = "55438"

for _p in (str(GATEWAY_DIR), str(RISK_DIR), str(DB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _find_pg_tool(name: str) -> str:
    for candidate in (
        shutil.which(name),
        f"/opt/homebrew/opt/postgresql@16/bin/{name}",
        f"/usr/local/opt/postgresql@16/bin/{name}",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError(f"{name} not found; install Homebrew postgresql@16")


@pytest.fixture(scope="session")
def pg_cluster():
    initdb = _find_pg_tool("initdb")
    pg_ctl = _find_pg_tool("pg_ctl")
    data_dir = Path(tempfile.mkdtemp(prefix="pg-a06-data-"))
    socket_dir = Path(f"/tmp/pg-a06-{uuid.uuid4()}")
    socket_dir.mkdir(parents=True)
    subprocess.run([initdb, "-D", str(data_dir), "-A", "trust", "-U", getpass.getuser()],
                   check=True, text=True, capture_output=True)
    subprocess.run(
        [pg_ctl, "-D", str(data_dir), "-l", str(data_dir / "postgres.log"),
         "-o", f"-p {PG_PORT} -k {socket_dir} -c timezone=UTC", "-w", "start"],
        check=True, text=True, capture_output=True,
    )
    url = f"postgresql://localhost/postgres?host={socket_dir}&port={PG_PORT}"
    try:
        yield url
    finally:
        subprocess.run([pg_ctl, "-D", str(data_dir), "-w", "stop"], text=True, capture_output=True)
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


def _migrate(url: str, direction: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    result = subprocess.run([sys.executable, str(MIGRATE), direction],
                            cwd=REPO_ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture()
def migrated_db(pg_cluster):
    _migrate(pg_cluster, "down")
    _migrate(pg_cluster, "up")
    try:
        yield pg_cluster
    finally:
        _migrate(pg_cluster, "down")


@pytest.fixture()
def db_conn(migrated_db):
    conn = psycopg2.connect(migrated_db)
    try:
        yield conn
    finally:
        conn.close()
