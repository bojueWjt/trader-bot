from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE = REPO_ROOT / "services" / "control-plane" / "db" / "migrate.py"
PG_PORT = "55443"
PG_SOCKET_DIR = Path("/tmp/pg-trade-outcomes")
DATABASE_URL = f"postgresql://localhost/postgres?host={PG_SOCKET_DIR}&port={PG_PORT}"


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
    data_dir = Path(tempfile.mkdtemp(prefix="pg-trade-outcomes-data-"))
    log_file = data_dir / "postgres.log"

    if PG_SOCKET_DIR.exists():
        shutil.rmtree(PG_SOCKET_DIR)
    PG_SOCKET_DIR.mkdir(parents=True)

    try:
        subprocess.run(
            [
                initdb,
                "-D",
                str(data_dir),
                "-A",
                "trust",
                "-U",
                getpass.getuser(),
                "-c",
                "dynamic_shared_memory_type=mmap",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(PG_SOCKET_DIR, ignore_errors=True)
        pytest.skip(f"temporary PostgreSQL initdb failed: {exc.stderr.strip()}")
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data_dir),
            "-l",
            str(log_file),
            "-o",
            f"-p {PG_PORT} -k {PG_SOCKET_DIR}",
            "-w",
            "start",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    try:
        yield {"url": DATABASE_URL, "pg_ctl": pg_ctl, "data_dir": data_dir}
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-w", "stop"],
            text=True,
            capture_output=True,
        )
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(PG_SOCKET_DIR, ignore_errors=True)


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
