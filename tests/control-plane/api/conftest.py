from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CP = REPO_ROOT / "services" / "control-plane"
MIGRATE = CP / "db" / "migrate.py"
_CP_TESTS = Path(__file__).resolve().parent.parent
if str(_CP_TESTS) not in sys.path:
    sys.path.insert(0, str(_CP_TESTS))
from ephemeral_pg import start_ephemeral_postgres  # noqa: E402

for _p in (str(CP / "api"), str(CP / "db")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(scope="session")
def pg_cluster():
    cluster = start_ephemeral_postgres(prefix="pg-api")
    try:
        yield cluster.url
    finally:
        cluster.stop()


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
