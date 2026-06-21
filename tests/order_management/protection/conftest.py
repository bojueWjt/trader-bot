from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
import pytest


ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = ROOT / "services" / "control-plane"
CONTROL_PLANE_RISK_STATE = CONTROL_PLANE / "risk_state"
NAUTILUS_NODE = ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"

for _path in (CONTROL_PLANE, CONTROL_PLANE_RISK_STATE, NAUTILUS_NODE, EXECUTION_DOMAIN):
    _raw = str(_path)
    if _raw not in sys.path:
        sys.path.insert(0, _raw)


@pytest.fixture(scope="session")
def protection_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om5")
    try:
        with psycopg2.connect(url):
            pass
    except psycopg2.OperationalError as exc:
        pytest.skip(f"protection DB unavailable at {url}: {exc}")
    return url


@pytest.fixture()
def db_conn(protection_db_url: str):
    conn = psycopg2.connect(protection_db_url)
    try:
        _clean_om5_rows(conn)
        yield conn
    finally:
        conn.rollback()
        _clean_om5_rows(conn)
        conn.close()


def _clean_om5_rows(conn) -> None:
    conn.rollback()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_links WHERE account_id LIKE 'acct-om5%'")
            cur.execute("DELETE FROM protective_orders_projection WHERE account_id LIKE 'acct-om5%'")
            cur.execute("DELETE FROM reconciliation_findings WHERE account_id LIKE 'acct-om5%'")
            cur.execute("DELETE FROM reconciliation_runs WHERE account_id LIKE 'acct-om5%'")
            cur.execute("DELETE FROM price_feed_status WHERE account_id LIKE 'acct-om5%'")
            cur.execute("DELETE FROM risk_state WHERE account_id LIKE 'acct-om5%'")
            cur.execute("DELETE FROM orders_projection WHERE account_id LIKE 'acct-om5%'")
            cur.execute("DELETE FROM positions_projection WHERE account_id LIKE 'acct-om5%'")
            cur.execute("DELETE FROM execution_events WHERE account_id LIKE 'acct-om5%'")
            cur.execute("DELETE FROM audit_events WHERE aggregate_id LIKE 'acct-om5%'")
    finally:
        conn.autocommit = False

