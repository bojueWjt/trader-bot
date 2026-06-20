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
from fastapi import Body, FastAPI, Header, HTTPException

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


@app.post("/v1/commands")
def issue_operator_command(
    body: dict = Body(default={}),
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    """Operator dangerous-op command (HALT/REDUCE/RESUME/CANCEL_ALL/CLOSE_ALL).
    risk_admin only; requires request_id + reason + confirm=true; writes a durable
    audit_events row; issue_command sets risk_state so the gateway fails closed."""
    role = require_reader(authorization)
    if role != "risk_admin":
        raise HTTPException(status_code=403, detail="risk_admin required")
    command_type = (body.get("type") or body.get("command_type") or "").upper()
    if command_type not in ("HALT", "REDUCE", "RESUME", "CANCEL_ALL", "CLOSE_ALL"):
        raise HTTPException(status_code=400, detail="invalid command_type")
    reason = (body.get("reason") or "").strip()
    request_id = (x_request_id or body.get("request_id") or "").strip()
    if not reason or body.get("confirm") is not True or not request_id:
        raise HTTPException(status_code=400, detail="dangerous op requires request_id + reason + confirm=true")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="command store unavailable")
    _cp = _HERE.parent
    for _p in (_cp, _cp / "commands", _cp / "security", _cp / "db", _cp / "risk", _cp / "risk_state"):
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
    from commands import issue_command
    from audit import dangerous_operation_payload, record_audit_event

    target_nodes = body.get("target_nodes") or []
    scope = body.get("scope") or {}
    conn = psycopg2.connect(database_url)
    try:
        result = issue_command(
            conn, command_type=command_type, requested_by="risk_admin", reason=reason,
            idempotency_key=body.get("idempotency_key") or request_id,
            target_nodes=target_nodes, scope=scope,
        )
        record_audit_event(
            conn, event_type="operator_command", aggregate_type="operator_command",
            aggregate_id=result["command_id"], actor="risk_admin",
            payload=dangerous_operation_payload(
                request_id=request_id, reason=reason,
                actor={"actor_id": "risk-admin", "role": "risk_admin"},
                operation=command_type, payload={"target_nodes": target_nodes, "scope": scope},
            ),
        )
        conn.commit()
        return result
    finally:
        conn.close()


@app.post("/v1/nodes/{node_id}/events")
def ingest_execution_event(
    node_id: str,
    body: dict = Body(default={}),
    authorization: str | None = Header(default=None),
):
    """A↔B seam: a node pushes an ExecutionEventEnvelopeV1; idempotent by event_id.
    Projection hints embedded in payload.{account,position,order} update the read model."""
    require_node(authorization)
    if not body.get("event_id") or not body.get("event_type"):
        raise HTTPException(status_code=400, detail="event_id and event_type required")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="projection store unavailable")
    _cp = _HERE.parent
    for _p in (_cp, _cp / "db"):
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
    from repository import ProjectionWriter

    conn = psycopg2.connect(database_url)
    try:
        writer = ProjectionWriter(conn)
        writer.insert_execution_event({**body, "node_id": body.get("node_id") or node_id})
        hints = body.get("payload") or {}
        ev_id, ts = body["event_id"], body.get("ts_event")
        if isinstance(hints.get("account"), dict):
            writer.upsert_account_projection({**hints["account"], "event_id": ev_id})
        if isinstance(hints.get("position"), dict):
            writer.upsert_position_projection({**hints["position"], "event_id": ev_id, "ts_event": ts})
        if isinstance(hints.get("order"), dict):
            writer.upsert_order_projection({**hints["order"], "event_id": ev_id, "ts_event": ts})
        conn.commit()
        return {"ingested": ev_id, "status": "ok"}
    finally:
        conn.close()


def _cp_paths() -> None:
    """Put the control-plane sibling packages on sys.path (idempotent)."""
    cp = _HERE.parent
    for p in (cp, cp / "commands", cp / "security", cp / "db", cp / "risk", cp / "risk_state"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


# operator_commands.command_type (A) -> node CommandType (B)
_NODE_COMMAND_TYPE_MAP = {
    "HALT": "halt", "RESUME": "resume", "REDUCE": "set_reducing",
    "CANCEL_ALL": "cancel_all", "CLOSE_ALL": "close_all",
}
# node CommandAckStatus (B) -> command_node_acks.status (A)
_NODE_ACK_STATUS_MAP = {"accepted": "acked", "completed": "acked", "failed": "failed"}


@app.post("/v1/nodes/{node_id}/intents/{intent_id}/ack")
def ack_node_intent(node_id: str, intent_id: str, body: dict = Body(default={}),
                    authorization: str | None = Header(default=None)):
    """A<->B seam: node acks an intent (received/accepted/executed/rejected/...)."""
    require_node(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    _cp_paths()
    from audit import record_audit_event

    status = str(body.get("status") or "received")
    conn = psycopg2.connect(database_url)
    try:
        record_audit_event(
            conn, event_type=f"intent_ack.{status}", aggregate_type="trade_intent",
            aggregate_id=intent_id, actor=f"node:{node_id}",
            payload={"status": status, "detail": body.get("detail"), "account_id": body.get("account_id")},
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/v1/nodes/{node_id}/execution-events")
def post_node_events(node_id: str, body: dict = Body(default={}),
                     authorization: str | None = Header(default=None)):
    """A<->B seam: node pushes a batch of ExecutionEventEnvelopeV1; idempotent by event_id."""
    require_node(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    _cp_paths()
    from repository import ProjectionWriter

    conn = psycopg2.connect(database_url)
    acked: list[str] = []
    try:
        writer = ProjectionWriter(conn)
        for ev in body.get("events", []):
            if not ev.get("event_id"):
                continue
            writer.insert_execution_event({**ev, "node_id": ev.get("node_id") or node_id})
            hints = ev.get("payload") or {}
            ev_id, ts = ev["event_id"], ev.get("ts_event")
            if isinstance(hints.get("account"), dict):
                writer.upsert_account_projection({**hints["account"], "event_id": ev_id})
            if isinstance(hints.get("position"), dict):
                writer.upsert_position_projection({**hints["position"], "event_id": ev_id, "ts_event": ts})
            if isinstance(hints.get("order"), dict):
                writer.upsert_order_projection({**hints["order"], "event_id": ev_id, "ts_event": ts})
            acked.append(str(ev["event_id"]))
        conn.commit()
        return {"acked_event_ids": acked}
    finally:
        conn.close()


@app.post("/v1/nodes/{node_id}/heartbeat")
def node_heartbeat(node_id: str, body: dict = Body(default={}),
                   authorization: str | None = Header(default=None)):
    """A<->B seam: node liveness + readiness; feeds snapshot freshness/missing_nodes."""
    require_node(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    from psycopg2.extras import Json

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO node_heartbeats (node_id, account_id, status, version, payload, last_seen_at) "
                "VALUES (%s,%s,%s,%s,%s, now()) "
                "ON CONFLICT (node_id) DO UPDATE SET account_id=COALESCE(EXCLUDED.account_id, node_heartbeats.account_id), "
                "status=EXCLUDED.status, version=EXCLUDED.version, payload=EXCLUDED.payload, last_seen_at=now()",
                (node_id, body.get("account_id"), str(body.get("trading_state") or "UNKNOWN"),
                 body.get("version"),
                 Json({k: body.get(k) for k in
                       ("readiness", "projection_lag_ms", "reconciliation_state", "last_event_id", "ts")})),
            )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/v1/nodes/{node_id}/commands")
def node_commands(node_id: str, after: str | None = None,
                  authorization: str | None = Header(default=None)):
    """A<->B seam: node polls its pending operator commands (kill-switch path)."""
    require_node(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT oc.command_id::text, oc.command_type, oc.scope, oc.created_at "
                "FROM operator_commands oc JOIN command_node_acks na ON na.command_id=oc.command_id "
                "WHERE na.node_id=%s AND na.status='pending' ORDER BY oc.created_at",
                (node_id,),
            )
            rows = cur.fetchall()
        return {"commands": [
            {"command_id": r[0], "type": _NODE_COMMAND_TYPE_MAP.get(r[1], r[1].lower()),
             "args": r[2] or {}, "issued_at": r[3].isoformat() if r[3] else None}
            for r in rows
        ]}
    finally:
        conn.close()


@app.post("/v1/nodes/{node_id}/commands/{command_id}/ack")
def ack_node_command(node_id: str, command_id: str, body: dict = Body(default={}),
                     authorization: str | None = Header(default=None)):
    """A<->B seam: node acks an operator command; recomputes the command's rollup."""
    require_node(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    _cp_paths()
    from commands import record_ack

    status = _NODE_ACK_STATUS_MAP.get(str(body.get("status")), "acked")
    conn = psycopg2.connect(database_url)
    try:
        record_ack(conn, command_id, node_id, status=status, detail=body.get("error"))
        return {"ok": True}
    finally:
        conn.close()


@app.get("/v1/accounts/{account_id}")
def account_generated_at(account_id: str, authorization: str | None = Header(default=None)):
    """A<->B seam: node reads the snapshot freshness (generated_at) for its account."""
    require_node(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    conn = psycopg2.connect(database_url)
    try:
        snap = build_system_snapshot(conn)
        return {"account_id": account_id, "generated_at": snap.get("generated_at")}
    finally:
        conn.close()
