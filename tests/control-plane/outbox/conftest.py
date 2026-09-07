from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = REPO_ROOT / "services" / "control-plane"
HERMES_WORKER = REPO_ROOT / "services" / "hermes-worker"
MIGRATE = CONTROL_PLANE / "db" / "migrate.py"
_CP_TESTS = Path(__file__).resolve().parent.parent
if str(_CP_TESTS) not in sys.path:
    sys.path.insert(0, str(_CP_TESTS))
from ephemeral_pg import start_ephemeral_postgres  # noqa: E402


@pytest.fixture(scope="session")
def pg_cluster():
    cluster = start_ephemeral_postgres(prefix="pg-outbox")
    os.environ["DATABASE_URL"] = cluster.url
    try:
        yield {
            "url": cluster.url,
            "pg_ctl": cluster.pg_ctl,
            "data_dir": cluster.data_dir,
            "socket_dir": cluster.socket_dir,
        }
    finally:
        env = os.environ.copy()
        env["DATABASE_URL"] = cluster.url
        subprocess.run(
            [sys.executable, str(MIGRATE), "down"],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        cluster.stop()


@pytest.fixture()
def migrated_db(pg_cluster):
    env = os.environ.copy()
    env["DATABASE_URL"] = pg_cluster["url"]
    down = subprocess.run(
        [sys.executable, str(MIGRATE), "down"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert down.returncode == 0, down.stdout + down.stderr
    up = subprocess.run(
        [sys.executable, str(MIGRATE), "up"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert up.returncode == 0, up.stdout + up.stderr

    # message_processing_runs lease column + queue statuses now come from
    # db/migrations/0002 (previously patched here, which hid an A-02/A-04 schema mismatch).
    yield pg_cluster["url"]

    down = subprocess.run(
        [sys.executable, str(MIGRATE), "down"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert down.returncode == 0, down.stdout + down.stderr


@pytest.fixture()
def db_conn(migrated_db):
    conn = psycopg2.connect(migrated_db)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def import_paths():
    for path in (str(CONTROL_PLANE), str(HERMES_WORKER)):
        if path not in sys.path:
            sys.path.insert(0, path)
