from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
import pytest


ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = ROOT / "services" / "control-plane"
CONTROL_PLANE_OM = CONTROL_PLANE / "order_management"
CONTROL_PLANE_RISK = CONTROL_PLANE / "risk"
NAUTILUS_NODE = ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"

for path in (CONTROL_PLANE, CONTROL_PLANE_OM, CONTROL_PLANE_RISK, NAUTILUS_NODE, EXECUTION_DOMAIN):
    raw = str(path)
    if raw not in sys.path:
        sys.path.insert(0, raw)


@pytest.fixture(scope="session")
def execution_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om4")
    try:
        with psycopg2.connect(url):
            pass
    except psycopg2.OperationalError as exc:
        pytest.skip(f"execution DB unavailable at {url}: {exc}")
    return url

