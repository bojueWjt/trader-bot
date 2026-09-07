from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATE = REPO_ROOT / "services" / "control-plane" / "db" / "migrate.py"
_CP_TESTS = Path(__file__).resolve().parent.parent
if str(_CP_TESTS) not in sys.path:
    sys.path.insert(0, str(_CP_TESTS))
from ephemeral_pg import start_ephemeral_postgres  # noqa: E402


@pytest.fixture(scope="session")
def pg_cluster():
    cluster = start_ephemeral_postgres(prefix="pg-db")
    prior_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = cluster.url
    try:
        yield {"url": cluster.url, "pg_ctl": cluster.pg_ctl, "data_dir": cluster.data_dir}
    finally:
        if prior_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior_database_url
        cluster.stop()


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
