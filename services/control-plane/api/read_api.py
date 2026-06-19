"""Control-plane read API.

Serves the real SystemSnapshotV1 from PostgreSQL projections. Reader auth is
fail-closed (tokens come from the environment; if none are configured the endpoint
returns 503 rather than allowing anonymous reads). When the projection store is
unavailable it returns 503 — never fixtures.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from fastapi import FastAPI, Header, HTTPException

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from snapshot import build_system_snapshot  # noqa: E402

READER_TOKEN_ENV = {
    "SYSTEM_OBSERVER_TOKEN": "system_observer",
    "VIEWER_TOKEN": "viewer",
    "RISK_ADMIN_TOKEN": "risk_admin",
    "REVIEWER_TOKEN": "reviewer",
}

app = FastAPI(title="Hermes control-plane read API", version="contracts-v1")


def _reader_tokens() -> dict[str, str]:
    tokens = {}
    for env_name, role in READER_TOKEN_ENV.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            tokens[value] = role
    return tokens


def require_reader(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    tokens = _reader_tokens()
    if not tokens:
        # fail closed: no reader credentials configured
        raise HTTPException(status_code=503, detail="reader auth not configured")
    role = tokens.get(authorization[len("Bearer "):].strip())
    if not role:
        raise HTTPException(status_code=403, detail="forbidden")
    return role


@app.get("/api/system/snapshot")
def system_snapshot(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="projection store unavailable")
    conn = psycopg2.connect(database_url)
    try:
        return build_system_snapshot(conn)
    finally:
        conn.close()
