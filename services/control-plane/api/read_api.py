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


def require_node(authorization: str | None) -> None:
    # nautilus_node auth is fail-closed and least-privilege (intent pull + ack only).
    expected = os.environ.get("NAUTILUS_NODE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="node auth not configured")
    token = authorization[len("Bearer "):].strip() if (authorization or "").startswith("Bearer ") else ""
    if token != expected:
        raise HTTPException(status_code=401, detail="node token required")


@app.get("/v1/nodes/{node_id}/intents")
def node_intents(
    node_id: str,
    account_id: str,
    after: str | None = None,
    limit: int = 50,
    authorization: str | None = Header(default=None),
):
    """A↔B seam: a node pulls approved ApprovedTradeIntentV1 for its account, cursor-based
    (durable, restart-safe). Only this account's approved intents are returned."""
    require_node(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="intent store unavailable")
    limit = max(1, min(int(limit), 500))
    params: list = [account_id]
    cursor_clause = ""
    if after:
        ts, _, iid = after.partition("|")
        cursor_clause = " AND (created_at, intent_id) > (%s, %s)"
        params += [ts, iid]
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT intent_id, hermes_decision_id, risk_decision_id, schema_version, "
                "account_id, instrument_id, action::text, order_plan, risk_budget, "
                "target_position_id, valid_until, idempotency_key, approved_at, created_at "
                "FROM trade_intents WHERE account_id=%s AND status='approved'"
                + cursor_clause
                + " ORDER BY created_at, intent_id LIMIT %s",
                params + [limit],
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    items = []
    next_cursor = after
    for r in rows:
        (iid_, dec, risk, ver, acct, instr, act, order_plan, risk_budget,
         tpid, valid_until, idem, approved_at, created_at) = r
        cur_str = f"{created_at.isoformat()}|{iid_}"
        items.append({
            "cursor": cur_str,
            "intent": {
                "schema_version": ver, "intent_id": str(iid_), "decision_id": str(dec),
                "risk_decision_id": str(risk), "account_id": acct, "instrument_id": instr,
                "action": act, "order_plan": order_plan, "risk_budget": risk_budget,
                "target_position_id": tpid,
                "valid_until": valid_until.isoformat() if valid_until else None,
                "idempotency_key": idem,
                "approved_at": approved_at.isoformat() if approved_at else None,
            },
        })
        next_cursor = cur_str
    return {"items": items, "next_cursor": next_cursor}
