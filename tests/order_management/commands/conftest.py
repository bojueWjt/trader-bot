from __future__ import annotations

import os
import sys
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
def om6_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om6")
    try:
        with psycopg2.connect(url):
            pass
    except psycopg2.OperationalError as exc:
        pytest.skip(f"OM6 DB unavailable at {url}: {exc}")
    return url


@pytest.fixture()
def db_conn(om6_db_url: str):
    conn = psycopg2.connect(om6_db_url)
    try:
        _clean_om6_rows(conn)
        yield conn
    finally:
        conn.rollback()
        _clean_om6_rows(conn)
        conn.close()


def _clean_om6_rows(conn) -> None:
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM node_command_runs WHERE node_id LIKE 'om6-%'")
    finally:
        conn.autocommit = False
