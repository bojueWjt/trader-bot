from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
import pytest


ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = ROOT / "services" / "control-plane"
CONTROL_PLANE_RISK = CONTROL_PLANE / "risk"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"

for _path in (CONTROL_PLANE, CONTROL_PLANE_RISK, EXECUTION_DOMAIN):
    _p = str(_path)
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(scope="session")
def risk_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om3")
    try:
        with psycopg2.connect(url):
            pass
    except psycopg2.OperationalError as exc:
        pytest.skip(f"risk DB unavailable at {url}: {exc}")
    return url


@pytest.fixture()
def db_conn(risk_db_url: str):
    conn = psycopg2.connect(risk_db_url)
    try:
        _clean_om3_rows(conn)
        yield conn
    finally:
        conn.rollback()
        _clean_om3_rows(conn)
        conn.close()


def _clean_om3_rows(conn) -> None:
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_reservations WHERE account_id LIKE 'acct-om3%'")
            cur.execute("DELETE FROM positions_projection WHERE account_id LIKE 'acct-om3%'")
            cur.execute("DELETE FROM accounts_projection WHERE account_id LIKE 'acct-om3%'")
            cur.execute("DELETE FROM execution_events WHERE account_id LIKE 'acct-om3%'")
    finally:
        conn.autocommit = False

