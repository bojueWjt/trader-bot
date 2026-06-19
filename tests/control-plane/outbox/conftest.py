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
CONTROL_PLANE = REPO_ROOT / "services" / "control-plane"
HERMES_WORKER = REPO_ROOT / "services" / "hermes-worker"
MIGRATE = CONTROL_PLANE / "db" / "migrate.py"
PG_PORT = "55433"


def _find_pg_tool(name: str) -> str:
    candidates = [
        shutil.which(name),
        f"/opt/homebrew/opt/postgresql@16/bin/{name}",
        f"/usr/local/opt/postgresql@16/bin/{name}",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError(f"{name} not found; install Homebrew postgresql@16")


@pytest.fixture(scope="session")
def pg_cluster():
    initdb = _find_pg_tool("initdb")
    pg_ctl = _find_pg_tool("pg_ctl")
    data_dir = Path(tempfile.mkdtemp(prefix="pg-a04-data-"))
    socket_dir = Path(f"/tmp/pg-a04-{uuid.uuid4()}")
    log_file = data_dir / "postgres.log"

    socket_dir.mkdir(parents=True)
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

    database_url = f"postgresql://localhost/postgres?host={socket_dir}&port={PG_PORT}"
    os.environ["DATABASE_URL"] = database_url
    try:
        yield {
            "url": database_url,
            "pg_ctl": pg_ctl,
            "data_dir": data_dir,
            "socket_dir": socket_dir,
        }
    finally:
        env = os.environ.copy()
        env["DATABASE_URL"] = database_url
        subprocess.run(
            [sys.executable, str(MIGRATE), "down"],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-w", "stop"],
            text=True,
            capture_output=True,
        )
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


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

    with psycopg2.connect(pg_cluster["url"]) as conn, conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE message_processing_runs
                DROP CONSTRAINT ck_message_processing_runs_status,
                ADD COLUMN lease_expires_at timestamptz;
            ALTER TABLE message_processing_runs
                ADD CONSTRAINT ck_message_processing_runs_status
                CHECK (
                    status IN (
                        'started',
                        'processing',
                        'succeeded',
                        'failed',
                        'skipped',
                        'hermes_timeout',
                        'hermes_failed',
                        'outbox_failed'
                    )
                );
            """
        )

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
