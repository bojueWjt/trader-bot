"""Control-plane read API.

Serves the real SystemSnapshotV1 from PostgreSQL projections. Reader auth is
fail-closed (tokens come from the environment; if none are configured the endpoint
returns 503 rather than allowing anonymous reads). When the projection store is
unavailable it returns 503 — never fixtures.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from psycopg2.extras import RealDictCursor

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from snapshot import (  # noqa: E402
    DEFAULT_STALENESS_MS,
    _missing_nodes,
    _worst_reconciliation_state,
    build_system_snapshot,
)

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


def _nautilus_instrument_id(instr: str | None) -> str | None:
    """A↔B seam: map a bare Binance USDT-M symbol (BTCUSDT) to the Nautilus
    InstrumentId the node's cache uses (BTCUSDT-PERP.BINANCE). Pass through if
    already venue-qualified."""
    if not instr or "." in instr:
        return instr
    return f"{instr}-PERP.BINANCE"


def _execution_order_plan(order_plan: dict | None, risk_budget: dict | None) -> dict:
    """A↔B seam: translate A's semantic order_plan ({side:long/short, entry:{type,price}})
    to B's execution order_plan ({side:buy/sell, type, quantity, ...}). Sizes the order
    from risk_budget.max_notional / entry price (notional-capped). Pass through if already
    in B's shape."""
    op = dict(order_plan or {})
    side = str(op.get("side") or "").lower()
    if op.get("type") and op.get("quantity") is not None and side in ("buy", "sell"):
        return op  # already B execution format
    b_side = {"long": "buy", "buy": "buy", "short": "sell", "sell": "sell"}.get(side, side)
    entry = op.get("entry") or {}
    entry_type = str(entry.get("type") or "market").lower()
    if entry_type == "none":
        entry_type = "market"
    entry_price = entry.get("price") if entry.get("price") is not None else entry.get("price_min")
    max_notional = (risk_budget or {}).get("max_notional")
    quantity = op.get("quantity")
    if quantity is None and entry_price and max_notional:
        try:
            quantity = float(max_notional) / float(entry_price)
        except (TypeError, ValueError, ZeroDivisionError):
            quantity = None
    out: dict = {"side": b_side, "type": entry_type,
                 "time_in_force": "IOC" if entry_type == "market" else "GTC"}
    if quantity is not None:
        out["quantity"] = str(quantity)
    if entry_type in ("limit", "zone") and entry_price is not None:
        out["price"] = entry_price
    return out


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
                "risk_decision_id": str(risk), "account_id": acct,
                "instrument_id": _nautilus_instrument_id(instr),
                "action": act,
                "order_plan": _execution_order_plan(order_plan, risk_budget),
                "risk_budget": risk_budget,
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


# Nautilus enum ints: OrderSide BUY=1/SELL=2 ; PositionSide LONG=2/SHORT=3.
_ORDER_SIDE = {1: "long", 2: "short", "BUY": "long", "SELL": "short"}
_POSITION_SIDE = {2: "long", 3: "short", "LONG": "long", "SHORT": "short",
                  "long": "long", "short": "short"}


def _num(value):
    """Coerce a possibly-string/None numeric payload field to float, else None.
    Required because repository upserts wrap quantities in COALESCE(%s, 0): a
    string like '0.0008' would be cast to integer and raise InvalidTextRepresentation."""
    if value is None:
        return None
    try:
        return float(str(value).split()[0])  # tolerates '−0.02 USDT' style suffixes
    except (ValueError, IndexError):
        return None


def _derive_projection_from_event(writer, ev: dict) -> None:
    """C-06: derive read-model projections from the node's raw (flat-payload)
    execution events. Raises on DB error so the caller's SAVEPOINT can roll back
    just this event instead of poisoning the whole batch transaction."""
    et = str(ev.get("event_type") or "")
    p = ev.get("payload") or {}
    acct = ev.get("account_id")
    ev_id, ts = ev.get("event_id"), ev.get("ts_event")
    if not acct:
        return
    if et.startswith("Position") and p.get("position_id"):
        qty = abs(_num(p.get("quantity")) or 0.0)
        writer.upsert_position_projection({
            "account_id": acct, "position_id": p["position_id"],
            "instrument_id": p.get("instrument_id"),
            "side": _POSITION_SIDE.get(p.get("side"), "long"),
            "quantity": qty, "avg_entry_price": _num(p.get("last_px")),
            "status": "closed" if (et == "PositionClosed" or qty == 0) else "open",
            "event_id": ev_id, "ts_event": ts,
        })
    elif et.startswith("Order") and (ev.get("client_order_id") or p.get("client_order_id")):
        fill_qty = _num(p.get("last_qty")) or _num(p.get("filled_qty"))
        writer.upsert_order_projection({
            "account_id": acct, "instrument_id": p.get("instrument_id"),
            "client_order_id": ev.get("client_order_id") or p.get("client_order_id"),
            "venue_order_id": ev.get("venue_order_id"),
            "status": (et[5:].lower() or "submitted"),
            "side": _ORDER_SIDE.get(p.get("order_side")),
            "order_type": (str(p.get("order_type")) if p.get("order_type") is not None else None),
            "quantity": _num(p.get("quantity")) or fill_qty,
            "filled_quantity": fill_qty,
            "event_id": ev_id, "ts_event": ts,
        })


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
            # Persist the raw event first (idempotent, outside the savepoint) so it is
            # always durable even if projection derivation fails on a malformed payload.
            writer.insert_execution_event({**ev, "node_id": ev.get("node_id") or node_id})
            hints = ev.get("payload") or {}
            ev_id, ts = ev["event_id"], ev.get("ts_event")
            with conn.cursor() as sp:
                sp.execute("SAVEPOINT proj")
            try:
                if isinstance(hints.get("account"), dict):
                    writer.upsert_account_projection({**hints["account"], "event_id": ev_id})
                if isinstance(hints.get("position"), dict):
                    writer.upsert_position_projection({**hints["position"], "event_id": ev_id, "ts_event": ts})
                if isinstance(hints.get("order"), dict):
                    writer.upsert_order_projection({**hints["order"], "event_id": ev_id, "ts_event": ts})
                _derive_projection_from_event(writer, ev)
                with conn.cursor() as sp:
                    sp.execute("RELEASE SAVEPOINT proj")
            except Exception:
                # One bad payload must not abort the batch: roll back just this
                # event's projection writes; the raw event above stays committed.
                with conn.cursor() as sp:
                    sp.execute("ROLLBACK TO SAVEPOINT proj")
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


# ---------------------------------------------------------------------------
# Operator read surface (/v1/*) consumed by the v3 dashboard.
#
# Every response embeds the §2.2 SystemSnapshotV1 data-quality envelope at the top
# level (data_source / stale / missing_nodes / reconciliation_state / ...), because
# the dashboard derives freshness from the SAME payload object it reads rows from.
# Reads are PostgreSQL-only projections and fail-closed (503 if the store is gone).
# Reader auth = viewer|reviewer|risk_admin|system_observer; review writes need reviewer.
# ---------------------------------------------------------------------------

# terminal order states: anything not in this set is still "open/working".
_TERMINAL_ORDER_STATES = (
    "filled", "canceled", "cancelled", "rejected", "expired", "denied", "closed", "done",
)


def _read_conn() -> "psycopg2.extensions.connection":
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="projection store unavailable")
    return psycopg2.connect(database_url)


def _f(value):
    """Coerce numeric/Decimal/str to float, else None (dashboard tolerates null)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value):
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


def _symbol(instrument_id: str | None) -> str | None:
    """Nautilus instrument id (BTCUSDT-PERP.BINANCE) -> venue symbol (BTCUSDT) for display."""
    if not instrument_id:
        return instrument_id
    return instrument_id.split("-", 1)[0]


def _envelope(cur, *, now: datetime | None = None, threshold_ms: int = DEFAULT_STALENESS_MS) -> dict:
    """Compute the SystemSnapshotV1 §2.2 envelope from the live projections — identical
    semantics to snapshot.build_system_snapshot, reused here so every /v1 row payload
    carries the same freshness verdict the snapshot endpoint reports."""
    now = now or datetime.now(timezone.utc)
    cur.execute("SELECT account_id, reconciliation_state::text AS reconciliation_state FROM accounts_projection")
    accounts = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT node_id, last_seen_at FROM node_heartbeats")
    nodes = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT max(ts_event) AS t FROM execution_events")
    last_event = cur.fetchone()["t"]
    lag = max(0, int((now - last_event).total_seconds() * 1000)) if last_event is not None else 0
    recon = _worst_reconciliation_state(accounts)
    missing = _missing_nodes(nodes, now, threshold_ms)
    stale = bool((last_event is not None and lag > threshold_ms) or recon == "failed" or missing)
    return {
        "schema_version": "1.0",
        "data_source": "postgres_projection",
        "snapshot_id": str(uuid4()),
        "generated_at": now.isoformat(),
        "last_execution_event_at": last_event.isoformat() if last_event is not None else None,
        "projection_lag_ms": lag,
        "stale": stale,
        "missing_nodes": missing,
        "reconciliation_state": recon,
    }


def _valid_uuid(value: str) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


@app.get("/v1/accounts")
def v1_accounts(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                "SELECT account_id, currency, equity, margin, available_balance, "
                "reconciliation_state::text AS reconciliation_state, projection_lag_ms, "
                "updated_at, payload FROM accounts_projection ORDER BY account_id"
            )
            rows = [dict(r) for r in cur.fetchall()]
        accounts = []
        for r in rows:
            payload = r.get("payload") or {}
            accounts.append({
                "account_id": r["account_id"],
                "currency": r["currency"],
                "equity": _f(r["equity"]),
                "available": _f(r.get("available_balance")),
                "margin_used": _f(r["margin"]),
                "realized_pnl_today": _f(payload.get("realized_pnl_today") or payload.get("daily_pnl") or 0),
                "reconciliation_state": r["reconciliation_state"],
                "updated_at": _iso(r["updated_at"]),
            })
        return {**env, "accounts": accounts}
    finally:
        conn.close()


@app.get("/v1/nodes")
def v1_nodes(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                """
                SELECT nh.node_id, nh.account_id, nh.status, nh.version, nh.payload, nh.last_seen_at,
                       (SELECT count(*) FROM positions_projection p
                          WHERE p.account_id = nh.account_id AND p.status = 'open') AS open_position_count,
                       (SELECT count(DISTINCT p.instrument_id) FROM positions_projection p
                          WHERE p.account_id = nh.account_id AND p.status = 'open') AS instrument_count
                FROM node_heartbeats nh ORDER BY nh.last_seen_at DESC
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        nodes = []
        for r in rows:
            payload = r.get("payload") or {}
            nodes.append({
                "node_id": r["node_id"],
                "name": r["node_id"],
                "account_id": r["account_id"],
                "trading_state": r["status"],
                "status": r["status"],
                "readiness": payload.get("readiness"),
                "last_heartbeat_at": _iso(r["last_seen_at"]),
                "open_position_count": int(r["open_position_count"] or 0),
                "instrument_count": int(r["instrument_count"] or 0),
                "projection_lag_ms": payload.get("projection_lag_ms"),
                "reconciliation_state": payload.get("reconciliation_state"),
                "version": r["version"],
            })
        return {**env, "nodes": nodes}
    finally:
        conn.close()


@app.get("/v1/orders")
def v1_orders(status: str | None = None, authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            sql = "SELECT * FROM orders_projection"
            params: list = []
            if status == "open":
                sql += " WHERE status NOT IN %s"
                params.append(_TERMINAL_ORDER_STATES)
            elif status:
                sql += " WHERE status = %s"
                params.append(status)
            sql += " ORDER BY updated_at DESC LIMIT 200"
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        orders = []
        for r in rows:
            qty = _f(r.get("quantity")) or 0.0
            filled = _f(r.get("filled_quantity")) or 0.0
            orders.append({
                "order_id": r.get("client_order_id") or str(r.get("order_projection_id")),
                "client_order_id": r.get("client_order_id"),
                "venue_order_id": r.get("venue_order_id"),
                "instrument_symbol": _symbol(r.get("instrument_id")),
                "instrument_id": r.get("instrument_id"),
                "side": r.get("side"),
                "order_type": r.get("order_type"),
                "type": r.get("order_type"),
                "status": r.get("status"),
                "price": _f(r.get("price")),
                "average_price": _f(r.get("average_fill_price")),
                "quantity": qty,
                "amount": qty,
                "filled": filled,
                "filled_quantity": filled,
                "remaining": max(0.0, qty - filled),
                "created_at": _iso(r.get("ts_event") or r.get("updated_at")),
                "trade_id": str(r["intent_id"]) if r.get("intent_id") else None,
            })
        return {**env, "orders": orders}
    finally:
        conn.close()


@app.get("/v1/positions")
def v1_positions(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                "SELECT * FROM positions_projection WHERE status = 'open' "
                "ORDER BY updated_at DESC LIMIT 200"
            )
            rows = [dict(r) for r in cur.fetchall()]
        positions = []
        for r in rows:
            payload = r.get("payload") or {}
            qty = _f(r.get("quantity"))
            entry = _f(r.get("avg_entry_price"))
            notional = _f(payload.get("notional"))
            if notional is None and qty is not None and entry is not None:
                notional = qty * entry
            positions.append({
                "position_id": r.get("position_id"),
                "instrument_symbol": _symbol(r.get("instrument_id")),
                "instrument_id": r.get("instrument_id"),
                "side": r.get("side"),
                "entry_price": entry,
                "mark_price": _f(r.get("mark_price")),
                "unrealized_pnl": _f(r.get("unrealized_pnl")),
                "quantity": qty,
                "size": qty,
                "notional": notional,
                "status": r.get("status"),
                "opened_at": _iso(payload.get("opened_at") or r.get("updated_at")),
                "leverage": _f(payload.get("leverage")),
                "stop_loss": _f(payload.get("stop_loss")),
                "take_profit": _f(payload.get("take_profit")),
                "signal_id": payload.get("signal_id") or payload.get("intent_id"),
                "intent_id": payload.get("intent_id"),
                "raw_signal": payload.get("raw_signal"),
            })
        return {**env, "positions": positions}
    finally:
        conn.close()


@app.get("/v1/trades")
def v1_trades(authorization: str | None = Header(default=None)):
    """Closed-position history projected as trades. There is no separate trades table;
    a closed positions_projection row IS the realized trade record we can honestly serve."""
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                "SELECT * FROM positions_projection WHERE status = 'closed' "
                "ORDER BY updated_at DESC LIMIT 200"
            )
            rows = [dict(r) for r in cur.fetchall()]
        trades = []
        for r in rows:
            payload = r.get("payload") or {}
            trades.append({
                "trade_id": r.get("position_id"),
                "instrument_symbol": _symbol(r.get("instrument_id")),
                "instrument_id": r.get("instrument_id"),
                "side": r.get("side"),
                "status": "closed",
                "open_price": _f(r.get("avg_entry_price")),
                "close_price": _f(r.get("mark_price")),
                "amount": _f(r.get("quantity")),
                "realized_pnl": _f(payload.get("realized_pnl") or r.get("unrealized_pnl")),
                "opened_at": _iso(payload.get("opened_at")),
                "closed_at": _iso(r.get("updated_at")),
            })
        return {**env, "trades": trades}
    finally:
        conn.close()


@app.get("/v1/messages")
def v1_messages(
    limit: int = 50,
    status: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_reader(authorization)
    limit = max(1, min(int(limit), 200))
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            sql = """
                SELECT rm.id, rm.channel_id, rm.source, rm.source_received_at, rm.message_text,
                       hd.decision_id, hd.action::text AS action, hd.message_type::text AS message_type,
                       rd.status::text AS risk_status
                FROM raw_messages rm
                LEFT JOIN LATERAL (
                    SELECT decision_id, action, message_type FROM hermes_decisions h
                    WHERE h.raw_message_id = rm.id ORDER BY created_at DESC LIMIT 1
                ) hd ON true
                LEFT JOIN LATERAL (
                    SELECT status FROM risk_decisions r
                    WHERE r.hermes_decision_id = hd.decision_id ORDER BY decided_at DESC LIMIT 1
                ) rd ON true
            """
            if status == "needs_review":
                sql += " WHERE (rd.status = 'needs_review' OR hd.action = 'needs_review')"
            sql += " ORDER BY rm.source_received_at DESC LIMIT %s"
            cur.execute(sql, [limit])
            rows = [dict(r) for r in cur.fetchall()]
            messages = []
            for r in rows:
                cur.execute(
                    "SELECT mime FROM media_assets WHERE raw_message_id = %s ORDER BY created_at",
                    (r["id"],),
                )
                media = [
                    {"index": i, "mime_type": m["mime"]}
                    for i, m in enumerate(cur.fetchall())
                ]
                text = r.get("message_text") or ""
                messages.append({
                    "message_id": str(r["id"]),
                    "id": str(r["id"]),
                    "decision_id": str(r["decision_id"]) if r.get("decision_id") else None,
                    "channel_id": r.get("channel_id"),
                    "source": r.get("source"),
                    "raw_text": text,
                    "raw_message": text,
                    "summary": text[:140],
                    "received_at": _iso(r.get("source_received_at")),
                    "created_at": _iso(r.get("source_received_at")),
                    "status": r.get("risk_status") or r.get("action"),
                    "action": r.get("action"),
                    "message_type": r.get("message_type"),
                    "event_type": r.get("message_type"),
                    "media": media,
                })
        return {**env, "messages": messages}
    finally:
        conn.close()


@app.get("/v1/messages/{signal_id}/media/{index}")
def v1_message_media(signal_id: str, index: int, authorization: str | None = Header(default=None)):
    """Serve a message image. signal_id is a decision_id (dashboard) or raw_message_id.
    Bytes resolve from MEDIA_ROOT/object_key; 404 (graceful) when bytes are not present."""
    require_reader(authorization)
    sid = _valid_uuid(signal_id)
    if sid is None:
        raise HTTPException(status_code=404, detail="media missing")
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT raw_message_id FROM hermes_decisions WHERE decision_id = %s", (sid,))
            row = cur.fetchone()
            raw_message_id = str(row["raw_message_id"]) if row else sid
            cur.execute(
                "SELECT object_key, mime FROM media_assets WHERE raw_message_id = %s ORDER BY created_at",
                (raw_message_id,),
            )
            assets = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    if index < 0 or index >= len(assets):
        raise HTTPException(status_code=404, detail="media missing")
    asset = assets[index]
    media_root = os.environ.get("MEDIA_ROOT", "").strip()
    object_key = asset.get("object_key")
    if media_root and object_key:
        base = Path(media_root).resolve()
        candidate = Path(object_key)
        if not candidate.is_absolute():
            candidate = base / object_key
        try:
            candidate = candidate.resolve()
        except OSError:
            candidate = None
        # sandbox: the resolved path must live inside MEDIA_ROOT. This rejects path
        # traversal AND an absolute object_key that points outside the media store,
        # so a poisoned object_key can never exfiltrate an arbitrary host file.
        if (
            candidate is not None
            and candidate.is_file()
            and (candidate == base or str(candidate).startswith(str(base) + os.sep))
        ):
            return FileResponse(candidate, media_type=asset.get("mime") or "application/octet-stream")
    raise HTTPException(status_code=404, detail="media bytes unavailable")


@app.get("/v1/risk/state")
def v1_risk_state(authorization: str | None = Header(default=None)):
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                "SELECT account_id, instrument_id, exposure_notional, open_risk_fraction, state "
                "FROM risk_state"
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT payload FROM positions_projection WHERE status = 'open'")
            open_positions = [dict(r) for r in cur.fetchall()]
        modes = [str((r.get("state") or {}).get("mode") or "ACTIVE").upper() for r in rows]
        overall = "HALTED" if "HALTED" in modes else "REDUCING" if "REDUCING" in modes else "ACTIVE"
        total_open_risk = sum(_f(r.get("open_risk_fraction")) or 0.0 for r in rows)
        no_sl = sum(1 for p in open_positions if not (p.get("payload") or {}).get("stop_loss"))
        high_lev = sum(
            1 for p in open_positions
            if (_f((p.get("payload") or {}).get("leverage")) or 0) > 20
        )
        pair_locks = [
            {
                "instrument_symbol": _symbol(r.get("instrument_id")),
                "reason": str((r.get("state") or {}).get("mode")),
                "expires_at": None,
                "owner": "risk_governor",
            }
            for r in rows
            if str((r.get("state") or {}).get("mode") or "ACTIVE").upper() in ("HALTED", "REDUCING")
        ]
        blocking = []
        if overall == "HALTED":
            blocking.append("risk_state HALTED: no new risk")
        elif overall == "REDUCING":
            blocking.append("risk_state REDUCING: opening blocked")
        return {
            **env,
            "risk_state": overall,
            "state": overall,
            "run_mode": overall,
            "single_trade_risk_usage_pct": 0,
            "total_open_risk_usage_pct": total_open_risk * 100,
            "daily_loss_usage_pct": 0,
            "no_sl_trade_count": no_sl,
            "high_leverage_trade_count": high_lev,
            "blocking_reasons": blocking,
            "pair_locks": pair_locks,
        }
    finally:
        conn.close()


@app.get("/v1/risk/decisions")
def v1_risk_decisions(authorization: str | None = Header(default=None)):
    """The human-review queue: risk_decisions in needs_review joined to their Hermes
    decision so the dashboard can render the structured signal + classification."""
    require_reader(authorization)
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            cur.execute(
                """
                SELECT hd.decision_id, hd.raw_message_id, hd.created_at,
                       hd.message_type::text AS message_type, hd.action::text AS action,
                       hd.ambiguous, hd.ambiguity_reasons, hd.instrument_symbol,
                       hd.side::text AS side, hd.entry_type::text AS entry_type,
                       hd.entry_price, hd.stop_loss, hd.take_profits, hd.leverage, hd.confidence,
                       rd.risk_decision_id, rd.status::text AS risk_status, rd.reason AS risk_reason
                FROM risk_decisions rd
                JOIN hermes_decisions hd ON hd.decision_id = rd.hermes_decision_id
                WHERE rd.status = 'needs_review'
                ORDER BY hd.created_at DESC LIMIT 100
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        decisions = []
        for r in rows:
            reasons = r.get("ambiguity_reasons") or []
            decisions.append({
                "decision_id": str(r["decision_id"]),
                "risk_decision_id": str(r["risk_decision_id"]),
                "raw_message_id": str(r["raw_message_id"]),
                "created_at": _iso(r.get("created_at")),
                "classification": {
                    "action": r.get("action"),
                    "message_type": r.get("message_type"),
                    "conclusion": r.get("action"),
                    "confidence": str(_f(r.get("confidence")) or ""),
                    "ambiguity_reasons": reasons,
                    "reason_codes": reasons,
                    "proposal_types": [],
                },
                "intent": {
                    "instrument_symbol": r.get("instrument_symbol"),
                    "side": r.get("side"),
                    "leverage": _f(r.get("leverage")),
                    "stop_loss": _f(r.get("stop_loss")),
                    "take_profits": [_f(x) for x in (r.get("take_profits") or [])],
                    "entry": {"type": r.get("entry_type"), "price": _f(r.get("entry_price"))},
                },
                "risk_status": r.get("risk_status"),
                "reason": r.get("risk_reason"),
            })
        return {**env, "decisions": decisions}
    finally:
        conn.close()


def _review_decide(decision_id: str, new_status: str, body: dict, role: str) -> dict:
    sid = _valid_uuid(decision_id)
    if sid is None:
        raise HTTPException(status_code=404, detail="decision not found")
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT risk_decision_id, status::text AS status FROM risk_decisions "
                "WHERE hermes_decision_id = %s OR risk_decision_id = %s "
                "ORDER BY decided_at DESC LIMIT 1 FOR UPDATE",
                (sid, sid),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="decision not found")
            risk_decision_id = str(row["risk_decision_id"])
            previous = row["status"]
            cur.execute(
                "UPDATE risk_decisions SET status = %s, reason = %s, decided_by = %s, decided_at = now() "
                "WHERE risk_decision_id = %s",
                (new_status, f"manual_review:{reason}", f"reviewer:{role}", risk_decision_id),
            )
        _cp_paths()
        from audit import record_audit_event

        record_audit_event(
            conn, event_type=f"manual_review.{new_status}", aggregate_type="risk_decision",
            aggregate_id=risk_decision_id, actor=f"reviewer:{role}",
            payload={"reason": reason, "decision_id": sid, "from_status": previous, "to_status": new_status},
        )
        conn.commit()
        return {"ok": True, "risk_decision_id": risk_decision_id, "status": new_status,
                "from_status": previous}
    finally:
        conn.close()


@app.post("/v1/risk/decisions/{decision_id}/approve")
def v1_review_approve(decision_id: str, body: dict = Body(default={}),
                      authorization: str | None = Header(default=None)):
    """Reviewer marks a needs_review decision approved. Records an immutable audit trail
    and lifts the item out of the queue. Execution still flows only through the
    deterministic gateway path — a manual approval does not itself emit a trade intent."""
    role = require_reader(authorization)
    if role not in ("reviewer", "risk_admin"):
        raise HTTPException(status_code=403, detail="reviewer required")
    return _review_decide(decision_id, "approved", body, role)


@app.post("/v1/risk/decisions/{decision_id}/reject")
def v1_review_reject(decision_id: str, body: dict = Body(default={}),
                     authorization: str | None = Header(default=None)):
    role = require_reader(authorization)
    if role not in ("reviewer", "risk_admin"):
        raise HTTPException(status_code=403, detail="reviewer required")
    return _review_decide(decision_id, "rejected", body, role)
