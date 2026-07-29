"""Control-plane read API.

Serves the real SystemSnapshotV1 from PostgreSQL projections. Reader auth is
fail-closed (tokens come from the environment; if none are configured the endpoint
returns 503 rather than allowing anonymous reads). When the projection store is
unavailable it returns 503 — never fixtures.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, StreamingResponse
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


# Dashboard realtime feed (SSE). The v3 SPA opens EventSource("/v1/stream") and
# listens for `dashboard_snapshot` + `heartbeat` events. We stream the same
# SystemSnapshotV1 projection that /api/system/snapshot returns, on a fixed
# interval, plus heartbeats to keep the connection warm through the Caddy proxy.
# Read-only: no trading side effects.
_STREAM_INTERVAL_S = 5.0


def _dashboard_snapshot_payload() -> dict:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("projection store unavailable")
    conn = psycopg2.connect(database_url)
    try:
        return build_system_snapshot(conn)
    finally:
        conn.close()


@app.get("/v1/stream")
async def v1_stream(authorization: str | None = Header(default=None)):
    require_reader(authorization)

    async def event_gen():
        while True:
            try:
                snap = await asyncio.to_thread(_dashboard_snapshot_payload)
                data = json.dumps(jsonable_encoder(snap), separators=(",", ":"))
                yield f"event: dashboard_snapshot\ndata: {data}\n\n"
            except Exception as exc:  # keep the stream alive; surface as an SSE error event
                err = json.dumps({"error": str(exc)})
                yield f"event: error\ndata: {err}\n\n"
            beat = json.dumps({"ts": datetime.now(timezone.utc).isoformat()})
            yield f"event: heartbeat\ndata: {beat}\n\n"
            await asyncio.sleep(_STREAM_INTERVAL_S)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


def _binance_mark_price(symbol: str | None) -> float | None:
    """Live futures mark price for sizing a MARKET order that carries no entry price.
    fapi.binance.com is dest-routed via the JP WireGuard tunnel on hk, so the HK 451
    geo-block does not apply. Fails soft (returns None) so a fetch error just leaves the
    order unsized (denied downstream) rather than throwing in the intent-serving path."""
    if not symbol:
        return None
    sym = str(symbol).split("-")[0].split(".")[0].upper()
    import json as _json
    import urllib.request as _url
    url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
    for _attempt in range(2):
        try:
            with _url.urlopen(url, timeout=3) as resp:
                data = _json.loads(resp.read())
            price = data.get("markPrice")
            return float(price) if price and float(price) > 0 else None
        except Exception:
            continue
    return None


_MANAGEMENT_ACTIONS = frozenset(
    {
        "close_position",
        "partial_close",
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
        "cancel_order",
    }
)

# zone-ladder v1 已定参数，改动须过再校准。
_ZONE_LADDER_TRANCHES = (
    (1, "t1_near", Decimal("0.55"), Decimal("0")),
    (2, "t2_mid", Decimal("0.30"), Decimal("0.50")),
    (3, "t3_deep", Decimal("0.15"), Decimal("0.85")),
)
_ZONE_LADDER_MIN_WIDTH_FRACTION = Decimal("0.0015")
_ZONE_LADDER_QTY_QUANTUM = Decimal("0.000000000001")


def _execution_order_plan(order_plan: dict | None, risk_budget: dict | None,
                          symbol: str | None = None,
                          action: str | None = None) -> dict:
    """A↔B seam: translate A's semantic order_plan ({side:long/short, entry:{type,price}})
    to B's execution order_plan ({side:buy/sell, type, quantity, ...}). Sizes the order
    from risk_budget.max_notional / price (notional-capped): limit/zone use the entry price;
    a MARKET order with no entry price uses the live Binance mark price. Pass through if
    already in B's shape."""
    op = dict(order_plan or {})
    side = str(op.get("side") or "").lower()
    if op.get("type") and op.get("quantity") is not None and side in ("buy", "sell"):
        return op  # already B execution format
    b_side = {"long": "buy", "buy": "buy", "short": "sell", "sell": "sell"}.get(side, side)
    entry = op.get("entry") or {}
    raw_entry_type = str(entry.get("type") or op.get("type") or "").lower()
    act = str(action or "").lower()
    if act in _MANAGEMENT_ACTIONS:
        out: dict = {"side": b_side}
        if raw_entry_type and raw_entry_type != "none":
            out["type"] = raw_entry_type
            out["time_in_force"] = "IOC" if raw_entry_type == "market" else "GTC"
        if op.get("quantity") is not None:
            out["quantity"] = str(op.get("quantity"))
        if op.get("price") is not None:
            out["price"] = op.get("price")
        if op.get("limit_price") is not None:
            out["limit_price"] = op.get("limit_price")
        if op.get("stop_price") is not None:
            out["stop_price"] = op.get("stop_price")
        elif act == "move_stop_loss" and op.get("stop_loss") is not None:
            out["stop_price"] = op.get("stop_loss")
        if op.get("trigger_price") is not None:
            out["trigger_price"] = op.get("trigger_price")
        if op.get("stop_loss") is not None:
            out["stop_loss"] = op.get("stop_loss")
        if op.get("take_profits") is not None:
            out["take_profits"] = op.get("take_profits")
        if op.get("cancel_client_order_id") is not None:
            out["cancel_client_order_id"] = op.get("cancel_client_order_id")
        if op.get("leverage") is not None:
            out["leverage"] = op.get("leverage")
        # Hedge-mode book hint must survive the A->B translation, otherwise the
        # node planner denies dual-side instruments with position_not_unique
        # (2026-07-12 ETH short SL incident, intent 15748ddd).
        if op.get("position_side") is not None:
            out["position_side"] = op.get("position_side")
        return out

    entry_type = raw_entry_type or "market"
    if entry_type == "none":
        entry_type = "market"
    entry_price = entry.get("price") if entry.get("price") is not None else entry.get("price_min")
    price_min = entry.get("price_min")
    price_max = entry.get("price_max")

    single_plan = _single_execution_order_plan(
        op,
        risk_budget,
        symbol,
        b_side,
        entry_type,
        entry_price,
        price_min,
        price_max,
    )
    if entry_type == "zone":
        try:
            ladder_plan = _zone_ladder_order_plan(
                op,
                risk_budget,
                symbol,
                b_side,
                price_min,
                price_max,
            )
            if ladder_plan is not None:
                return ladder_plan
        except Exception:
            return single_plan
    return single_plan


def _single_execution_order_plan(
    op: dict,
    risk_budget: dict | None,
    symbol: str | None,
    b_side: str,
    entry_type: str,
    entry_price,
    price_min,
    price_max,
) -> dict:
    max_notional = (risk_budget or {}).get("max_notional")
    quantity = op.get("quantity")
    # Price used ONLY to size the notional cap into a quantity. limit/zone: the entry price.
    # market (no entry price): the live mark price (fetched over the JP-routed fapi).
    if entry_type == "zone":
        sizing_price = price_min if b_side == "sell" else price_max
    else:
        sizing_price = entry_price
    if quantity is None and sizing_price is None and entry_type == "market" and max_notional:
        sizing_price = _binance_mark_price(symbol)
    if quantity is None and sizing_price and max_notional:
        try:
            quantity = float(max_notional) / float(sizing_price)
        except (TypeError, ValueError, ZeroDivisionError):
            quantity = None
    out: dict = {"side": b_side, "type": entry_type,
                 "time_in_force": "IOC" if entry_type == "market" else "GTC"}
    if quantity is not None:
        out["quantity"] = str(quantity)
    if entry_type in ("limit", "zone") and entry_price is not None:
        out["price"] = entry_price
    if entry_type == "zone":
        if price_min is not None:
            out["price_min"] = price_min
        if price_max is not None:
            out["price_max"] = price_max
    if op.get("stop_loss") is not None:
        out["stop_loss"] = op.get("stop_loss")
    if op.get("take_profits") is not None:
        out["take_profits"] = op.get("take_profits")
    if op.get("leverage") is not None:
        out["leverage"] = op.get("leverage")
    if op.get("expire_hours") is not None and entry_type in ("limit", "zone"):
        out["expire_hours"] = op.get("expire_hours")
    return out


def _zone_ladder_order_plan(
    op: dict,
    risk_budget: dict | None,
    symbol: str | None,
    b_side: str,
    price_min,
    price_max,
) -> dict | None:
    if price_min is None or price_max is None:
        return None
    if op.get("stop_loss") is None:
        return None

    min_price = _positive_decimal_or_none(price_min)
    max_price = _positive_decimal_or_none(price_max)
    stop_loss = _positive_decimal_or_none(op.get("stop_loss"))
    max_notional = _positive_decimal_or_none((risk_budget or {}).get("max_notional"))
    if min_price is None or max_price is None or stop_loss is None or max_notional is None:
        return None
    if min_price >= max_price:
        return None

    if b_side == "sell":
        if stop_loss <= max_price:
            return None
        near_edge = min_price
        far_edge = max_price
    elif b_side == "buy":
        if stop_loss >= min_price:
            return None
        near_edge = max_price
        far_edge = min_price
    else:
        return None

    if (max_price - min_price) / near_edge < _ZONE_LADDER_MIN_WIDTH_FRACTION:
        return None

    mark_price = _positive_decimal_or_none(_binance_mark_price(symbol))
    if mark_price is None:
        return None
    if b_side == "sell" and mark_price >= min_price:
        return None
    if b_side == "buy" and mark_price <= max_price:
        return None

    single_qty = max_notional / near_edge
    total_risk = single_qty * abs(near_edge - stop_loss)
    if single_qty <= 0 or total_risk <= 0:
        return None

    raw_tranches: list[tuple[int, str, Decimal, Decimal]] = []
    for seq, tranche_id, weight, depth in _ZONE_LADDER_TRANCHES:
        price = near_edge + (depth * (far_edge - near_edge))
        stop_distance = abs(price - stop_loss)
        if price <= 0 or stop_distance <= 0:
            return None
        quantity = (total_risk * weight) / stop_distance
        if quantity <= 0:
            return None
        raw_tranches.append((seq, tranche_id, price, quantity))

    total_notional = sum(quantity * price for _, _, price, quantity in raw_tranches)
    if total_notional <= 0:
        return None
    scale = Decimal("1")
    if total_notional > max_notional:
        scale = max_notional / total_notional

    tranches = []
    for seq, tranche_id, price, quantity in raw_tranches:
        scaled_quantity = _round_down_ladder_quantity(quantity * scale)
        if scaled_quantity is None or scaled_quantity <= 0:
            return None
        tranches.append(
            {
                "seq": seq,
                "tranche_id": tranche_id,
                "price": float(price),
                "quantity": _format_decimal_plain(scaled_quantity),
            }
        )
    if len(tranches) != len(_ZONE_LADDER_TRANCHES):
        return None

    out: dict = {
        "side": b_side,
        "type": "zone_ladder",
        "time_in_force": "GTC",
        "tranches": tranches,
        "stop_loss": op.get("stop_loss"),
        "take_profits": op.get("take_profits") or [],
        "price_min": price_min,
        "price_max": price_max,
    }
    if op.get("leverage") is not None:
        out["leverage"] = op.get("leverage")
    return out


def _positive_decimal_or_none(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number > 0 else None


def _round_down_ladder_quantity(value: Decimal) -> Decimal | None:
    try:
        rounded = (value / _ZONE_LADDER_QTY_QUANTUM).quantize(
            Decimal("1"),
            rounding=ROUND_DOWN,
        ) * _ZONE_LADDER_QTY_QUANTUM
    except (InvalidOperation, ValueError):
        return None
    return rounded if rounded > 0 else None


def _format_decimal_plain(value: Decimal) -> str:
    text = format(value.quantize(_ZONE_LADDER_QTY_QUANTUM), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


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
                "FROM trade_intents WHERE account_id=%s AND status='approved' AND valid_until > now()"
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
                "order_plan": _execution_order_plan(order_plan, risk_budget, instr, action=act),
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
    _cp_paths()
    from repository import ProjectionWriter

    conn = psycopg2.connect(database_url)
    try:
        writer = ProjectionWriter(conn)
        event = {**body, "node_id": body.get("node_id") or node_id}
        writer.insert_execution_event(event)
        hints = event.get("payload") or {}
        ev_id, ts = event["event_id"], event.get("ts_event")
        account_hint = _account_projection_hint(event, hints)
        if account_hint:
            writer.upsert_account_projection(account_hint)
        if isinstance(hints.get("position"), dict):
            pos_hint = _normalize_position_hint(
                {**hints["position"], "event_id": ev_id, "ts_event": ts},
                event.get("event_type"),
            )
            if pos_hint is not None:
                writer.upsert_position_projection(pos_hint)
        _derive_projection_from_event(writer, event)
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


def _first_finite_account_number(payload: dict, *names: str) -> float | None:
    for name in names:
        raw = payload.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _account_projection_hint(event: dict, hints: dict) -> dict | None:
    account = hints.get("account")
    if not isinstance(account, dict):
        return None
    account_id = event.get("account_id")
    if not account_id:
        return None
    equity = _first_finite_account_number(account, "equity", "balance", "total")
    margin = _first_finite_account_number(account, "margin", "margin_balance", "locked")
    available = _first_finite_account_number(account, "available_balance", "free")
    if equity is None or equity <= 0:
        return None
    if margin is None or margin < 0:
        return None
    if available is not None and available < 0:
        return None
    normalized = dict(account)
    normalized["account_id"] = account_id
    normalized["equity"] = equity
    normalized["margin"] = margin
    normalized["available_balance"] = available
    normalized["event_id"] = event.get("event_id")
    normalized["last_execution_event_at"] = event.get("ts_event")
    return normalized


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

    status = str(getattr(body.get("status"), "value", body.get("status") or "received")).lower()
    conn = psycopg2.connect(database_url)
    try:
        intent_status = None
        if status in ("rejected", "expired"):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trade_intents SET status=%s "
                    "WHERE intent_id=%s AND status='approved' RETURNING status::text",
                    (status, intent_id),
                )
                row = cur.fetchone()
                if row is not None:
                    intent_status = row[0]
                else:
                    cur.execute(
                        "SELECT status::text FROM trade_intents WHERE intent_id=%s",
                        (intent_id,),
                    )
                    row = cur.fetchone()
                    intent_status = row[0] if row is not None else None
        record_audit_event(
            conn, event_type=f"intent_ack.{status}", aggregate_type="trade_intent",
            aggregate_id=intent_id, actor=f"node:{node_id}",
            payload={"status": status, "detail": body.get("detail"), "account_id": body.get("account_id")},
        )
        conn.commit()
        result = {"ok": True}
        if intent_status is not None:
            result["intent_status"] = intent_status
        return result
    finally:
        conn.close()


# Nautilus enum ints: OrderSide BUY=1/SELL=2 ; PositionSide FLAT=1/LONG=2/SHORT=3.
_ORDER_SIDE = {
    1: "long", 2: "short",
    "BUY": "long", "SELL": "short", "buy": "long", "sell": "short",
    "LONG": "long", "SHORT": "short", "long": "long", "short": "short",
}
_POSITION_SIDE = {1: "flat", 2: "long", 3: "short", "FLAT": "flat", "flat": "flat",
                  "LONG": "long", "SHORT": "short", "long": "long", "short": "short"}


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


def _enum_key(value):
    if hasattr(value, "value"):
        value = value.value
    if hasattr(value, "name"):
        value = value.name
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        return stripped
    return value


def _bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "t", "1", "yes", "y"):
        return True
    if text in ("false", "f", "0", "no", "n"):
        return False
    return None


def _position_side(value) -> str | None:
    return _POSITION_SIDE.get(_enum_key(value))


def _order_side(value) -> str | None:
    return _ORDER_SIDE.get(_enum_key(value))


def _canonical_position_id(instrument_id: str | None, side: str | None,
                           fallback: str | None = None) -> str | None:
    if instrument_id and side in ("long", "short"):
        return f"{instrument_id}-{side.upper()}"
    return fallback


def _normalize_position_hint(hint: dict, event_type: str | None = None) -> dict | None:
    out = dict(hint or {})
    instrument_id = out.get("instrument_id")
    side = _position_side(out.get("side") or out.get("position_side")) or out.get("side")
    if side not in ("long", "short", "flat"):
        side = "long"
    original_position_id = out.get("position_id")
    canonical_id = _canonical_position_id(instrument_id, side, original_position_id)
    if canonical_id is None:
        return None
    qty = abs(_num(out.get("quantity")) or 0.0)
    status = out.get("status")
    if event_type == "PositionClosed" or qty == 0 or side == "flat":
        status = "closed"
    else:
        status = status or "open"
    out["position_id"] = canonical_id
    out["side"] = "long" if side == "flat" else side
    out["quantity"] = qty
    out["status"] = status
    if original_position_id and original_position_id != canonical_id:
        payload = dict(out.get("payload") or {})
        payload.setdefault("node_position_id", original_position_id)
        out["payload"] = payload
    return out


def _normalize_order_hint(hint: dict) -> dict | None:
    out = dict(hint or {})
    if not out.get("client_order_id"):
        return None
    side = _order_side(out.get("side") or out.get("order_side"))
    if side is not None:
        out["side"] = side
    if out.get("order_type") is not None:
        out["order_type"] = str(out.get("order_type"))
    for src, dst in (("price", "price"), ("trigger_price", "trigger_price"),
                     ("quantity", "quantity"), ("filled_quantity", "filled_quantity")):
        if src in out:
            out[dst] = _num(out.get(src))
    if "reduce_only" in out:
        out["reduce_only"] = _bool(out.get("reduce_only"))
    return out


def _position_projection_from_event(ev: dict) -> dict | None:
    et = str(ev.get("event_type") or "")
    p = ev.get("payload") or {}
    acct = ev.get("account_id")
    instrument_id = p.get("instrument_id")
    if not acct or not et.startswith("Position") or not instrument_id:
        return None
    qty = abs(_num(p.get("quantity")) or 0.0)
    hint = {
        "account_id": acct,
        "position_id": p.get("position_id"),
        "instrument_id": instrument_id,
        "side": p.get("side") or p.get("position_side"),
        "quantity": qty,
        "avg_entry_price": _num(p.get("avg_entry_price")) or _num(p.get("last_px")),
        "mark_price": _num(p.get("mark_price")),
        "unrealized_pnl": _num(p.get("unrealized_pnl")),
        "status": "closed" if (et == "PositionClosed" or qty == 0) else "open",
        "event_id": ev.get("event_id"),
        "ts_event": ev.get("ts_event"),
        "payload": p,
    }
    return _normalize_position_hint(hint, et)


def _order_projection_from_event(ev: dict) -> dict | None:
    et = str(ev.get("event_type") or "")
    p = ev.get("payload") or {}
    acct = ev.get("account_id")
    cid = ev.get("client_order_id") or p.get("client_order_id")
    if not acct or not et.startswith("Order") or not cid:
        return None
    fill_qty = _num(p.get("last_qty")) or _num(p.get("filled_qty"))
    hint = {
        "account_id": acct,
        "instrument_id": p.get("instrument_id"),
        "client_order_id": cid,
        "venue_order_id": ev.get("venue_order_id") or p.get("venue_order_id"),
        "status": (et[5:].lower() or "submitted"),
        "side": p.get("side") or p.get("order_side"),
        "order_type": p.get("order_type"),
        "quantity": _num(p.get("quantity")) or fill_qty,
        "filled_quantity": fill_qty,
        "price": p.get("price"),
        "trigger_price": p.get("trigger_price"),
        "reduce_only": p.get("reduce_only"),
        "event_id": ev.get("event_id"),
        "ts_event": ev.get("ts_event"),
        "payload": p,
    }
    return _normalize_order_hint(hint)


def _derive_projection_from_event(writer, ev: dict) -> None:
    """Derive guarded read-model projections from one raw execution event."""
    et = str(ev.get("event_type") or "")
    payload = ev.get("payload") or {}
    if not ev.get("account_id"):
        return
    position_hint = _position_projection_from_event(ev)
    if position_hint is not None:
        writer.upsert_position_projection(position_hint)
        return
    if not et.startswith("Order"):
        return

    order_payload = dict(payload)
    nested_order = payload.get("order")
    if isinstance(nested_order, dict):
        order_payload.update(nested_order)
    client_order_id = ev.get("client_order_id") or order_payload.get("client_order_id")
    venue_order_id = ev.get("venue_order_id") or order_payload.get("venue_order_id")
    if not client_order_id and not venue_order_id:
        return

    side = _order_side(order_payload.get("side") or order_payload.get("order_side"))
    if side is not None:
        order_payload["side"] = side
    if order_payload.get("order_type") is not None:
        order_payload["order_type"] = str(order_payload.get("order_type"))
    if "reduce_only" in order_payload:
        order_payload["reduce_only"] = _bool(order_payload.get("reduce_only"))

    from order_management.order_reducer import OrderProjectionReducer

    reducer_event = {
        **ev,
        "client_order_id": client_order_id,
        "venue_order_id": venue_order_id,
        "payload": order_payload,
    }
    OrderProjectionReducer().apply_event(
        writer.conn,
        reducer_event,
        manage_transaction=False,
    )


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
            event = {**ev, "node_id": ev.get("node_id") or node_id}
            writer.insert_execution_event(event)
            hints = event.get("payload") or {}
            ev_id, ts = event["event_id"], event.get("ts_event")
            with conn.cursor() as sp:
                sp.execute("SAVEPOINT proj")
            try:
                account_hint = _account_projection_hint(event, hints)
                if account_hint:
                    writer.upsert_account_projection(account_hint)
                if isinstance(hints.get("position"), dict):
                    pos_hint = _normalize_position_hint(
                        {**hints["position"], "event_id": ev_id, "ts_event": ts},
                        event.get("event_type"),
                    )
                    if pos_hint is not None:
                        writer.upsert_position_projection(pos_hint)
                _derive_projection_from_event(writer, event)
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
                       ("readiness", "projection_lag_ms", "reconciliation_state", "last_event_id", "ts",
                        "open_orders")})),
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


@app.get("/v1/nodes/{node_id}/exchange-state")
def node_exchange_state(
    node_id: str,
    account_id: str,
    authorization: str | None = Header(default=None),
):
    """Return one account's read-only venue mirror for node reconciliation."""
    require_node(authorization)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT account_id FROM node_heartbeats WHERE node_id=%s",
                (node_id,),
            )
            node_row = cur.fetchone()
            if node_row is not None:
                bound_account = node_row["account_id"]
                if bound_account and bound_account != account_id:
                    raise HTTPException(status_code=403, detail="node account mismatch")
            cur.execute(
                "SELECT to_regclass('public.exchange_state_mirror') IS NOT NULL AS present"
            )
            if not cur.fetchone()["present"]:
                raise HTTPException(status_code=503, detail="exchange state mirror unavailable")
            cur.execute(
                "SELECT account_id, payload, updated_at, "
                "(now() - updated_at) > interval '180 seconds' AS stale "
                "FROM exchange_state_mirror WHERE account_id=%s",
                (account_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="exchange state mirror missing")
        return dict(row)
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
    limit: int = 200,
    status: str | None = None,
    authorization: str | None = Header(default=None),
):
    require_reader(authorization)
    limit = max(1, min(int(limit), 500))
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


# ---------------------------------------------------------------------------
# Operator order entry (Hermes agent = the decision maker; added 2026-07-02)
#
# Hermes decides WHETHER to trade (from channel signals or the user's verbal
# instruction) and places the order HERE — never on the exchange directly.
# This endpoint writes the same audit chain the decision gateway wrote
# (raw_message -> processing_run -> context_snapshot -> hermes_decision ->
# risk_decision -> approved trade_intent), so the nautilus node pulls and
# executes it exactly like any other approved intent (JP-routed egress,
# hedge-mode planner, reduce-only exits). Caps are enforced server-side and
# fail closed because the caller is an LLM agent.
# ---------------------------------------------------------------------------

_OPERATOR_ACTIONS = (
    "open_position",
    "close_position",
    "partial_close",
    "move_stop_loss",
    "replace_take_profits",
    "cancel_order",
)
# Protection management: no new exposure (node places reduce-only orders sized to
# the live position), so these skip notional sizing entirely.
_OPERATOR_PROTECT_ACTIONS = ("move_stop_loss", "replace_take_profits")
_OPERATOR_MANAGEMENT_ACTIONS = (
    "close_position",
    "partial_close",
    "move_stop_loss",
    "replace_take_profits",
    "cancel_order",
)
_EXECUTABLE_PARENT_ACTIONS = frozenset(
    {
        "open_position",
        "add_position",
        "partial_close",
        "close_position",
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
        "cancel_order",
    }
)
_OPERATOR_ACCOUNTS = ("account-a", "account-b")
_ORDER_AUTHORIZATION_TYPES = ("user", "channel")
_INTERNAL_ORDER_SERVICE_RE = re.compile(r"internal|watchdog|reconciler", re.IGNORECASE)
_TG_SIGNAL_REF_RE = re.compile(
    r"(?:^|-)tg-sig-c(?P<channel>\d+)-m(?P<message>\d+)(?:-e\d+)?(?:$|-)"
)
_WATCHER_TRADING_DB = os.environ.get(
    "WATCHER_TRADING_DB",
    "/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db",
)
_ATTRIBUTION_SHADOW_LOG = "/srv/trader-v3/logs/attribution-shadow.jsonl"


def _channel_from_signal_ref(value) -> str | bool:
    ref = str(value or "").strip()
    if not ref:
        return False
    match = _TG_SIGNAL_REF_RE.search(ref)
    if not match:
        return False
    return "-" + match.group("channel")


def _open_source_channel(body: dict, client_ref: str) -> tuple[str, bool]:
    source_channel = str(body.get("source_channel") or "").strip()
    ref_channel = _channel_from_signal_ref(client_ref)
    if source_channel and ref_channel and source_channel != ref_channel:
        raise HTTPException(
            status_code=400,
            detail=f"source_channel conflicts with client_ref channel {ref_channel}",
        )
    if source_channel:
        return source_channel, True
    if ref_channel:
        return str(ref_channel), True
    return "hermes-operator", False


def _authorized_parent(
    database_url: str,
    parent_intent_id: str,
    account_id: str,
    instrument_id: str,
) -> dict:
    try:
        UUID(parent_intent_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="parent_intent_id must be a uuid")

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT account_id, instrument_id, action::text, status::text, order_plan "
                "FROM trade_intents "
                "WHERE intent_id::text=%s",
                (parent_intent_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id does not reference an authorized trade intent",
        )
    (
        parent_account,
        parent_instrument,
        parent_action,
        parent_status,
        parent_plan,
    ) = row
    if parent_account != account_id:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id account does not match account_id",
        )
    normalized_parent = _attribution_symbol(parent_instrument)
    normalized_current = _attribution_symbol(instrument_id)
    if normalized_parent != normalized_current:
        raise HTTPException(
            status_code=400,
            detail=(
                f"parent_intent_id instrument does not match request instrument "
                f"{normalized_current}"
            ),
        )
    if str(parent_status or "").lower() != "approved":
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id status is not executable",
        )
    if str(parent_action or "").lower() not in _EXECUTABLE_PARENT_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id action is not executable",
        )
    plan = parent_plan or {}
    parent_authorization = plan.get("authorization")
    if not isinstance(parent_authorization, dict):
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id has no auditable authorization evidence",
        )
    parent_type = str(parent_authorization.get("authorized_by_type") or "").strip()
    parent_id = str(parent_authorization.get("authorized_by_id") or "").strip()
    parent_message = str(parent_authorization.get("source_message_id") or "").strip()
    if parent_type not in _ORDER_AUTHORIZATION_TYPES:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id authorization type is invalid",
        )
    if not parent_id or not parent_message:
        raise HTTPException(
            status_code=400,
            detail="parent_intent_id authorization evidence is incomplete",
        )
    return parent_authorization


def _order_authorization(
    body: dict,
    database_url: str,
    account_id: str,
    instrument_id: str,
    reason: str,
    source: str,
) -> dict:
    authorized_by_type = str(body.get("authorized_by_type") or "").strip().lower()
    if authorized_by_type not in _ORDER_AUTHORIZATION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"authorized_by_type must be one of {list(_ORDER_AUTHORIZATION_TYPES)}",
        )
    authorized_by_id = str(body.get("authorized_by_id") or "").strip()
    if not authorized_by_id:
        raise HTTPException(status_code=400, detail="authorized_by_id required")
    source_message_id = str(body.get("source_message_id") or "").strip()
    if not source_message_id:
        raise HTTPException(status_code=400, detail="source_message_id required")
    created_by_service = str(body.get("created_by_service") or "").strip()
    if not created_by_service:
        raise HTTPException(status_code=400, detail="created_by_service required")
    if source != created_by_service:
        raise HTTPException(
            status_code=400,
            detail="source must match created_by_service",
        )

    parent_intent_id = str(body.get("parent_intent_id") or "").strip()
    is_internal = _INTERNAL_ORDER_SERVICE_RE.search(created_by_service) is not None
    if is_internal and not parent_intent_id:
        raise HTTPException(
            status_code=400,
            detail="internal/watchdog/reconciler source requires parent_intent_id",
        )
    if parent_intent_id:
        parent_authorization = _authorized_parent(
            database_url,
            parent_intent_id,
            account_id,
            instrument_id,
        )
        parent_type = str(parent_authorization.get("authorized_by_type") or "").strip()
        parent_id = str(parent_authorization.get("authorized_by_id") or "").strip()
        parent_message = str(parent_authorization.get("source_message_id") or "").strip()
        if (
            authorized_by_type != parent_type
            or authorized_by_id != parent_id
            or source_message_id != parent_message
        ):
            raise HTTPException(
                status_code=400,
                detail="authorization evidence does not match parent_intent_id",
            )

    return {
        "authorized_by_type": authorized_by_type,
        "authorized_by_id": authorized_by_id,
        "reason": reason,
        "source_message_id": source_message_id,
        "created_by_service": created_by_service,
        "parent_intent_id": parent_intent_id or False,
    }


def _write_attribution_shadow(event: dict) -> None:
    path = os.environ.get("ATTRIBUTION_SHADOW_LOG", _ATTRIBUTION_SHADOW_LOG)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        print(f"attribution shadow log write failed: {exc}", file=sys.stderr)


def _attribution_intent(database_url: str, account_id: str, entry_ref: str):
    accounts = [account_id]
    for candidate in _OPERATOR_ACCOUNTS:
        if candidate != account_id:
            accounts.append(candidate)
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            for candidate in accounts:
                idem = hashlib.sha256(
                    f"operator|{candidate}|{entry_ref}".encode()
                ).hexdigest()
                cur.execute(
                    "SELECT ti.intent_id::text, ti.action::text, ti.instrument_id, "
                    "ti.account_id, ti.order_plan, rm.channel_id, rm.source_message_id "
                    "FROM trade_intents ti "
                    "JOIN hermes_decisions hd ON hd.decision_id = ti.hermes_decision_id "
                    "JOIN raw_messages rm ON rm.id = hd.raw_message_id "
                    "WHERE ti.idempotency_key=%s",
                    (idem,),
                )
                row = cur.fetchone()
                if row:
                    return row
    finally:
        conn.close()
    return False


def _attribution_symbol(value) -> str:
    return str(value or "").upper().split("-")[0].split(".")[0]


def _resolve_attribution(database_url: str, action: str, symbol: str,
                         account_id: str, channel: str, entry_ref: str,
                         position_side: str | None) -> tuple[dict, str | bool]:
    resolution = "none"
    owner_channel: str | bool = False
    channel_match: bool | str = "unknown"
    errors = []
    hard_error: str | bool = False
    bypass = channel == "operator"

    row = False
    if entry_ref:
        try:
            row = _attribution_intent(database_url, account_id, entry_ref)
        except Exception as exc:
            errors.append(f"shadow_lookup_failed:{str(exc)[:160]}")
    else:
        errors.append("entry_ref_missing")

    if row:
        resolution = "intent"
        _, entry_action, entry_symbol, entry_account, order_plan, raw_channel, source_ref = row
        owner_channel = str(raw_channel or "").strip()
        if owner_channel == "hermes-operator":
            parsed_channel = _channel_from_signal_ref(source_ref)
            if parsed_channel:
                owner_channel = parsed_channel
        if entry_action != "open_position":
            errors.append(f"entry_action:{entry_action}")
        if _attribution_symbol(entry_symbol) != symbol:
            errors.append(f"instrument_mismatch:{entry_symbol}")
            hard_error = (
                f"entry_ref instrument {entry_symbol} does not match request symbol {symbol}"
            )
        if entry_account != account_id:
            errors.append(f"account_mismatch:{entry_account}")
            hard_error = (
                f"entry_ref account {entry_account} does not match request account {account_id}"
            )
        entry_plan = order_plan or {}
        entry_side = str(entry_plan.get("side") or "").lower()
        if position_side:
            if entry_side != position_side:
                errors.append(f"position_side_mismatch:{entry_side or 'unknown'}")
        elif entry_side:
            errors.append("position_side_missing")
    elif entry_ref:
        parsed_channel = _channel_from_signal_ref(entry_ref)
        if parsed_channel:
            resolution = "ref_parse_only"
            owner_channel = parsed_channel
            errors.append("entry_intent_not_found")
        else:
            errors.append("entry_ref_unresolved")

    if channel and owner_channel:
        channel_match = channel == owner_channel
        if channel_match is False:
            errors.append("channel_mismatch")
    elif not channel:
        errors.append("channel_missing")

    would_reject = bool(errors)
    if bypass:
        would_reject = False
    error: str | bool = False
    if errors:
        error = ";".join(errors)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "symbol": symbol,
        "account": account_id,
        "channel": channel or False,
        "entry_ref": entry_ref or False,
        "resolution": resolution,
        "owner_channel": owner_channel,
        "channel_match": channel_match,
        "would_reject": would_reject,
        "bypass": bypass,
        "error": error,
    }
    return event, hard_error


def _operator_caps() -> dict:
    return {
        # optional extra fixed ceiling; unset/0 = disabled (risk cap governs)
        "max_notional": float(os.environ.get("OPERATOR_MAX_NOTIONAL_USDT", "0") or 0) or None,
        "max_leverage": float(os.environ.get("OPERATOR_MAX_LEVERAGE", "10")),
        # per-order max loss at stop, as fraction of account equity (user rule: 6%)
        "max_risk_fraction": float(os.environ.get("OPERATOR_MAX_RISK_FRACTION", "0.06")),
        # orders without a stop loss cannot be risk-checked: cap notional instead
        "no_sl_equity_fraction": float(os.environ.get("OPERATOR_NO_SL_EQUITY_FRACTION", "0.2")),
    }


def _account_equity(account_id: str) -> float | None:
    """Return fresh projected equity, with an env value only for first bootstrap."""
    max_age_raw = os.environ.get("OPERATOR_EQUITY_MAX_AGE_SECONDS", "180")
    try:
        max_age_seconds = max(1.0, float(max_age_raw))
    except ValueError:
        max_age_seconds = 180.0
    if not math.isfinite(max_age_seconds) or max_age_seconds > 3600:
        max_age_seconds = 180.0

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        try:
            conn = psycopg2.connect(database_url)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT equity, EXTRACT(EPOCH FROM (now() - updated_at)) "
                        "FROM accounts_projection WHERE account_id=%s "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (account_id,),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
        except Exception:
            return None

        if row:
            try:
                equity = float(row[0])
                age_seconds = float(row[1])
            except (TypeError, ValueError):
                return None
            if not math.isfinite(equity) or not math.isfinite(age_seconds):
                return None
            if equity <= 0 or age_seconds < 0 or age_seconds > max_age_seconds:
                return None
            return equity

    env_key = "OPERATOR_EQUITY_" + account_id.upper().replace("-", "_")
    raw = os.environ.get(env_key, "").strip()
    if raw:
        try:
            equity = float(raw)
        except ValueError:
            return None
        if math.isfinite(equity) and equity > 0:
            return equity
    return None


def _symbol_risk_ratio(symbol: str) -> float:
    """Per-symbol risk fraction — the user's config in the watcher DB is the
    single source of truth (e.g. BTCUSDT 0.02 = risk 2% of equity per trade);
    symbols without a config default to 1%."""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{_WATCHER_TRADING_DB}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT risk_ratio FROM symbol_risk_configs WHERE symbol=?", (symbol,)
            ).fetchone()
            if row and row[0] and 0 < float(row[0]) <= 0.1:
                return float(row[0])
        finally:
            conn.close()
    except Exception:
        pass
    return float(os.environ.get("OPERATOR_DEFAULT_RISK_RATIO", "0.01"))


def _size_open_order(explicit_notional, symbol, account_id, side, entry_type,
                     entry_price, entry_price_min, entry_price_max,
                     stop_loss, caps, checks) -> float:
    """Risk-based sizing: notional = equity * risk_ratio / stop_distance.
    Hard cap (fail closed): loss at stop <= max_risk_fraction of equity.
    Without a stop loss the order cannot be risk-checked, so an explicit
    notional is required and capped at no_sl_equity_fraction of equity."""
    equity = _account_equity(account_id)
    if equity is None:
        raise HTTPException(
            status_code=503,
            detail=f"account equity unknown for {account_id}: set OPERATOR_EQUITY_"
                   f"{account_id.upper().replace('-', '_')} in the control-plane env",
        )
    # price reference for the stop distance
    if entry_type == "limit":
        ref = entry_price
    elif entry_type == "zone":
        if stop_loss is not None:
            # conservative: the zone boundary FARTHEST from the stop gives the
            # largest loss per unit if filled there — size against that
            ref = (entry_price_min
                   if abs(entry_price_min - stop_loss) >= abs(entry_price_max - stop_loss)
                   else entry_price_max)
        else:
            ref = (entry_price_min + entry_price_max) / 2
    else:
        ref = _binance_mark_price(symbol)

    notional: float
    if stop_loss is not None and ref:
        if side == "short" and stop_loss <= ref:
            raise HTTPException(status_code=400, detail="short stop_loss must be above entry")
        if side == "long" and stop_loss >= ref:
            raise HTTPException(status_code=400, detail="long stop_loss must be below entry")
        stop_frac = abs(ref - stop_loss) / ref
        risk_ratio = _symbol_risk_ratio(symbol)
        auto = equity * risk_ratio / stop_frac
        notional = explicit_notional if explicit_notional is not None else auto
        max_risk = equity * caps["max_risk_fraction"]
        est_risk = notional * stop_frac
        if est_risk > max_risk + 1e-9:
            raise HTTPException(
                status_code=400,
                detail=f"loss at stop ~{est_risk:.1f}U exceeds per-order risk cap "
                       f"{max_risk:.1f}U ({caps['max_risk_fraction']:.0%} of equity {equity:.0f}U)",
            )
        checks.append({"name": "risk_sizing", "passed": True, "equity": equity,
                       "risk_ratio": risk_ratio, "stop_frac": round(stop_frac, 5),
                       "sizing_price": ref, "notional": round(notional, 1),
                       "est_risk": round(est_risk, 1), "risk_cap": round(max_risk, 1),
                       "auto_sized": explicit_notional is None})
    else:
        if explicit_notional is None:
            raise HTTPException(
                status_code=400,
                detail="auto-sizing needs a stop_loss (and a resolvable price); "
                       "pass notional_usdt explicitly for stop-less orders",
            )
        ceiling = equity * caps["no_sl_equity_fraction"]
        if explicit_notional > ceiling:
            raise HTTPException(
                status_code=400,
                detail=f"stop-less order notional {explicit_notional:.0f}U exceeds "
                       f"{ceiling:.0f}U ({caps['no_sl_equity_fraction']:.0%} of equity)",
            )
        notional = explicit_notional
        checks.append({"name": "no_sl_notional_cap", "passed": True,
                       "notional": notional, "ceiling": round(ceiling, 1)})

    lev_ceiling = equity * caps["max_leverage"]
    if notional > lev_ceiling:
        raise HTTPException(status_code=400,
                            detail=f"notional {notional:.0f}U exceeds equity*max_leverage {lev_ceiling:.0f}U")
    if caps["max_notional"] and notional > caps["max_notional"]:
        raise HTTPException(status_code=400,
                            detail=f"notional {notional:.0f}U exceeds OPERATOR_MAX_NOTIONAL_USDT {caps['max_notional']:.0f}U")
    return notional


def _validate_stop_direction(symbol: str, account_id: str, stop_loss: float,
                             position_side: str | None = None) -> None:
    """A stop on the wrong side of the mark price is rejected by Binance (-2021)
    only AFTER the node has already cancelled the old stop — validate up front.
    Soft check: skipped when the position or mark price cannot be resolved.
    position_side (long|short) narrows the check to one book in hedge mode."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return
    side = None
    try:
        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT side FROM positions_projection WHERE account_id=%s "
                    "AND instrument_id LIKE %s AND status='open' AND quantity::numeric != 0",
                    (account_id, symbol + "%"),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        if position_side:
            rows = [r for r in rows if str(r[0]).lower() == position_side]
        if len(rows) != 1:
            return  # none/ambiguous: let the node planner decide
        side = str(rows[0][0]).lower()
    except Exception:
        return
    mark = _binance_mark_price(symbol)
    if not mark:
        return
    if side == "long" and stop_loss >= mark:
        raise HTTPException(
            status_code=400,
            detail=f"stop_loss {stop_loss} is above mark {mark} for a long position",
        )
    if side == "short" and stop_loss <= mark:
        raise HTTPException(
            status_code=400,
            detail=f"stop_loss {stop_loss} is below mark {mark} for a short position",
        )


def _validate_take_profit_direction(symbol: str, account_id: str, take_profits: list,
                                    position_side: str | None = None) -> None:
    """A take-profit too close to or through the mark can execute immediately and
    cascade reduce-only exits. Soft check: skipped when the position or mark price
    cannot be resolved. position_side (long|short) narrows hedge-mode books."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return
    side = None
    try:
        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT side FROM positions_projection WHERE account_id=%s "
                    "AND instrument_id LIKE %s AND status='open' AND quantity::numeric != 0",
                    (account_id, symbol + "%"),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        if position_side:
            rows = [r for r in rows if str(r[0]).lower() == position_side]
        if len(rows) != 1:
            return  # none/ambiguous: let the node planner decide
        side = str(rows[0][0]).lower()
    except Exception:
        return
    mark = _binance_mark_price(symbol)
    if not mark:
        return
    if side == "long":
        min_price = mark * 1.001
        for tp in take_profits:
            price = float(tp["price"])
            if price <= min_price:
                raise HTTPException(
                    status_code=400,
                    detail=f"take_profit {price} is not at least 0.1% beyond mark "
                           f"{mark} for a long position",
                )
    if side == "short":
        max_price = mark * 0.999
        for tp in take_profits:
            price = float(tp["price"])
            if price >= max_price:
                raise HTTPException(
                    status_code=400,
                    detail=f"take_profit {price} is not at least 0.1% beyond mark "
                           f"{mark} for a short position",
                )


def _safe_execution_preview(order_plan, risk_budget, symbol, action):
    """The intent row is committed before the response is built: a preview failure
    must never turn a successfully-placed intent into an HTTP error."""
    try:
        return _execution_order_plan(order_plan, risk_budget, symbol, action=action)
    except Exception as exc:  # noqa: BLE001
        return {"preview_unavailable": str(exc)[:200]}


def _op_num(value, field: str, required: bool = False):
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail=f"{field} required")
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")
    if num <= 0:
        raise HTTPException(status_code=400, detail=f"{field} must be > 0")
    return num


@app.post("/v1/operator/orders")
def operator_order(
    body: dict = Body(default={}),
    authorization: str | None = Header(default=None),
):
    from datetime import timedelta
    from psycopg2.extras import Json

    role = require_reader(authorization)
    if role != "risk_admin":
        raise HTTPException(status_code=403, detail="risk_admin required")

    action = str(body.get("action") or "").strip()
    if action not in _OPERATOR_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {list(_OPERATOR_ACTIONS)}")
    symbol = str(body.get("symbol") or "").upper().split("-")[0].split(".")[0]
    if not symbol or not symbol.isalnum() or not symbol.endswith("USDT"):
        raise HTTPException(status_code=400, detail="symbol required, e.g. BTCUSDT")
    dry_run = body.get("dry_run") is True
    explicit_account_id = str(body.get("account_id") or "").strip()
    if not explicit_account_id and not dry_run:
        raise HTTPException(status_code=400, detail="account_id is required")
    account_id = explicit_account_id
    if not account_id:
        account_id = str(
            os.environ.get("OPERATOR_DEFAULT_ACCOUNT", "account-a")
        ).strip()
    if account_id not in _OPERATOR_ACCOUNTS:
        raise HTTPException(status_code=400, detail=f"account_id must be one of {list(_OPERATOR_ACCOUNTS)}")
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason required (audit trail)")
    source = str(body.get("source") or "hermes-agent")[:64]
    client_ref = str(body.get("client_ref") or "").strip()

    caps = _operator_caps()
    checks = [{"name": "operator_auth", "passed": True}]
    entry = dict(body.get("entry") or {"type": "market"})
    entry_type = str(entry.get("type") or "market").lower()
    if entry_type == "none":
        entry_type = "market"
    if entry_type not in ("market", "limit", "zone"):
        raise HTTPException(status_code=400, detail="entry.type must be market|limit|zone")
    entry_price = _op_num(entry.get("price"), "entry.price")
    entry_price_min = _op_num(entry.get("price_min"), "entry.price_min")
    entry_price_max = _op_num(entry.get("price_max"), "entry.price_max")
    if entry_type == "limit" and entry_price is None:
        raise HTTPException(status_code=400, detail="limit entry requires entry.price")
    if entry_type == "zone" and (entry_price_min is None or entry_price_max is None):
        raise HTTPException(status_code=400, detail="zone entry requires entry.price_min + entry.price_max")

    side = str(body.get("side") or "").lower() or None
    # Hedge mode holds LONG and SHORT simultaneously: management actions on a
    # dual-side instrument need an explicit book, otherwise the node planner
    # denies with position_not_unique (2026-07-10 ETH incident).
    position_side = str(body.get("position_side") or "").strip().lower() or None
    if position_side is not None and position_side not in ("long", "short"):
        raise HTTPException(status_code=400, detail="position_side must be long|short")
    leverage = _op_num(body.get("leverage"), "leverage")
    if leverage is not None and leverage > caps["max_leverage"]:
        raise HTTPException(status_code=400, detail=f"leverage {leverage} exceeds cap {caps['max_leverage']}")
    cancel_client_order_id = None
    if action == "cancel_order":
        cancel_client_order_id = str(body.get("client_order_id") or "").strip()
        # Only OUR deterministic ids are cancellable: external/manual orders on
        # the same account must never be touchable through this endpoint.
        if not re.fullmatch(r"B[0-9a-f]{32}[0-9]{2}", cancel_client_order_id):
            raise HTTPException(
                status_code=400,
                detail="cancel_order requires client_order_id in system format "
                       "(B + 32 hex + 2 digits); external orders cannot be cancelled here",
            )
        # Format alone is forgeable: the embedded uuid must reference an intent WE
        # issued. This is the actual ownership proof (adversarial review P1-1).
        _own_db = os.environ.get("DATABASE_URL")
        if _own_db:
            _own_conn = psycopg2.connect(_own_db)
            try:
                with _own_conn.cursor() as _own_cur:
                    _own_cur.execute(
                        "SELECT 1 FROM trade_intents WHERE intent_id::text = %s",
                        (str(UUID(hex=cancel_client_order_id[1:33])),),
                    )
                    if _own_cur.fetchone() is None:
                        raise HTTPException(
                            status_code=400,
                            detail="client_order_id does not belong to a system intent",
                        )
            finally:
                _own_conn.close()
    stop_loss = _op_num(body.get("stop_loss"), "stop_loss")
    if action == "move_stop_loss":
        if stop_loss is None:
            raise HTTPException(status_code=400, detail="move_stop_loss requires stop_loss")
        _validate_stop_direction(symbol, account_id, stop_loss, position_side)
    if action == "open_position" and not client_ref \
            and body.get("dry_run") is not True:
        # The caller is an LLM: a timeout-retry without an idempotency key would
        # double the position (hedge mode never blocks a second open).
        raise HTTPException(
            status_code=400,
            detail="open_position requires client_ref (idempotency key): use the "
                   "signal message id, or a stable slug for verbal orders",
        )
    if action in _OPERATOR_MANAGEMENT_ACTIONS and not client_ref \
            and body.get("dry_run") is not True:
        raise HTTPException(
            status_code=400,
            detail=f"{action} requires client_ref: pass a stable operation ref "
                   "(for example close-btc-tg-sig-c1002136478186-m5026) and reuse "
                   "the same ref for every retry",
        )
    disable_take_profits = body.get("disable_take_profits") is True
    if disable_take_profits and action != "replace_take_profits":
        raise HTTPException(
            status_code=400,
            detail="disable_take_profits is only valid for replace_take_profits",
        )
    if action == "replace_take_profits":
        raw_tps = body.get("take_profits")
        if disable_take_profits:
            if raw_tps != []:
                raise HTTPException(
                    status_code=400,
                    detail="disable_take_profits requires take_profits=[]",
                )
            if not position_side:
                raise HTTPException(
                    status_code=400,
                    detail="disable_take_profits requires position_side",
                )
            take_profits = []
        elif not isinstance(raw_tps, list) or not raw_tps:
            raise HTTPException(
                status_code=400,
                detail="replace_take_profits requires take_profits: [{price, quantity}, ...]",
            )
        else:
            take_profits = []
            for i, item in enumerate(raw_tps):
                if not isinstance(item, dict):
                    raise HTTPException(
                        status_code=400,
                        detail="take_profits entries must be {price, quantity} objects",
                    )
                take_profits.append({
                    "price": _op_num(
                        item.get("price"),
                        f"take_profits[{i}].price",
                        required=True,
                    ),
                    "quantity": _op_num(
                        item.get("quantity"),
                        f"take_profits[{i}].quantity",
                        required=True,
                    ),
                })
            _validate_take_profit_direction(
                symbol,
                account_id,
                take_profits,
                position_side,
            )
    else:
        take_profits = [
            _op_num(tp, "take_profits[]") for tp in (body.get("take_profits") or [])
        ]

    notional = None
    quantity = None
    if action == "open_position":
        if side not in ("long", "short"):
            raise HTTPException(status_code=400, detail="side must be long|short for open_position")
        notional = _size_open_order(
            _op_num(body.get("notional_usdt"), "notional_usdt"),
            symbol, account_id, side, entry_type,
            entry_price, entry_price_min, entry_price_max,
            stop_loss, caps, checks,
        )
    elif action == "partial_close":
        quantity = _op_num(body.get("quantity"), "quantity", required=True)

    expire_hours = _op_num(body.get("expire_hours"), "expire_hours")
    valid_seconds = int(_op_num(body.get("valid_seconds"), "valid_seconds") or 900)
    valid_seconds = max(60, min(valid_seconds, 3600))

    if action == "cancel_order":
        order_plan = {"cancel_client_order_id": cancel_client_order_id}
    elif action in _OPERATOR_PROTECT_ACTIONS:
        # Minimal semantic plan: the node planner derives side/quantity from the
        # live position; an entry block here would be misparsed as an order type.
        order_plan = {}
        if action == "move_stop_loss":
            order_plan["stop_loss"] = stop_loss
        else:
            order_plan["take_profits"] = take_profits
            if disable_take_profits:
                order_plan["disable_take_profits"] = True
        if position_side:
            order_plan["position_side"] = position_side
    else:
        order_plan = {
            "side": side,
            "entry": {"type": entry_type, "price": entry_price,
                      "price_min": entry_price_min, "price_max": entry_price_max},
            "stop_loss": stop_loss,
            "take_profits": take_profits,
            "leverage": leverage,
        }
        if expire_hours and entry_type in ("limit", "zone"):
            order_plan["expire_hours"] = expire_hours
        if quantity is not None:
            order_plan["quantity"] = str(quantity)
        if position_side and action in ("close_position", "partial_close"):
            order_plan["position_side"] = position_side
    # Shape must match the node RiskBudget contract exactly (extra=forbid,
    # all three fields required): risk_fraction/max_notional/max_leverage.
    risk_budget: dict = {
        "risk_fraction": 0.0,
        "max_notional": notional if notional is not None else 0.0,
        "max_leverage": leverage or caps["max_leverage"],
    }

    raw_channel = "hermes-operator"
    has_provenance = False
    if action == "open_position":
        raw_channel, has_provenance = _open_source_channel(body, client_ref)

    if dry_run:
        order_plan_preview = {
            "side": side,
            "entry": {"type": entry_type},
            "stop_loss": stop_loss,
            "take_profits": take_profits,
        }
        if position_side:
            order_plan_preview["position_side"] = position_side
        if disable_take_profits:
            order_plan_preview["disable_take_profits"] = True
        return {
            "dry_run": True, "action": action, "symbol": symbol, "account_id": account_id,
            "computed_notional": notional, "checks": checks,
            "order_plan_preview": order_plan_preview,
        }

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="intent store unavailable")

    authorization_evidence = _order_authorization(
        body,
        database_url,
        account_id,
        symbol,
        reason,
        source,
    )
    if (
        authorization_evidence["authorized_by_type"] == "channel"
        and action == "open_position"
    ):
        if raw_channel != authorization_evidence["authorized_by_id"]:
            raise HTTPException(
                status_code=400,
                detail="channel authorization does not match source channel",
            )
        if not has_provenance:
            raise HTTPException(
                status_code=400,
                detail="channel authorization requires source_channel or canonical client_ref",
            )
    if (
        action == "open_position"
        and raw_channel not in ("hermes-operator", "operator")
        and authorization_evidence["authorized_by_type"] != "channel"
    ):
        raise HTTPException(
            status_code=400,
            detail="channel-sourced order requires authorized_by_type=channel",
        )
    if (
        action in _OPERATOR_MANAGEMENT_ACTIONS
        and authorization_evidence["authorized_by_type"] == "channel"
    ):
        raw_channel = authorization_evidence["authorized_by_id"]

    attribution = False
    if action in _OPERATOR_MANAGEMENT_ACTIONS:
        channel = str(body.get("channel") or "").strip()
        if (
            channel
            and channel != "operator"
            and authorization_evidence["authorized_by_type"] != "channel"
        ):
            raise HTTPException(
                status_code=400,
                detail="channel-sourced management requires authorized_by_type=channel",
            )
        entry_ref = str(body.get("entry_ref") or "").strip()
        attribution_event, hard_error = _resolve_attribution(
            database_url, action, symbol, account_id, channel, entry_ref, position_side
        )
        _write_attribution_shadow(attribution_event)
        attribution = {
            "resolution": attribution_event["resolution"],
            "owner_channel": attribution_event["owner_channel"],
            "channel_match": attribution_event["channel_match"],
            "would_reject": attribution_event["would_reject"],
        }
        if hard_error:
            raise HTTPException(status_code=400, detail=hard_error)
        if authorization_evidence["authorized_by_type"] == "channel":
            channel_valid = (
                channel == authorization_evidence["authorized_by_id"]
                and attribution_event["resolution"] == "intent"
                and attribution_event["channel_match"] is True
                and attribution_event["would_reject"] is False
            )
            if not channel_valid:
                raise HTTPException(
                    status_code=400,
                    detail="channel authorization attribution failed",
                )

    order_plan["authorization"] = authorization_evidence
    if attribution:
        order_plan["attribution"] = attribution

    now = datetime.now(timezone.utc)
    raw_id, run_id, ctx_id, dec_id, risk_id, intent_id = (str(uuid4()) for _ in range(6))
    if action == "open_position":
        idem = hashlib.sha256(
            f"operator|{account_id}|{client_ref}".encode()
        ).hexdigest()
    else:
        idem = hashlib.sha256(
            f"operator-v2|{account_id}|{action}|{symbol}|"
            f"{position_side or ''}|{client_ref}".encode()
        ).hexdigest()
    message_type = "new_signal" if action == "open_position" else "position_update"
    valid_until = now + timedelta(seconds=valid_seconds)

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT intent_id::text, status::text, valid_until FROM trade_intents WHERE idempotency_key=%s",
                (idem,),
            )
            existing = cur.fetchone()
            if existing:
                replay_response = {
                    "intent_id": existing[0], "status": existing[1], "replay": True,
                    "valid_until": existing[2].isoformat() if existing[2] else None,
                }
                if attribution:
                    replay_response["attribution"] = attribution
                replay_response["authorization"] = authorization_evidence
                return replay_response
            cur.execute(
                "INSERT INTO raw_messages (id, source, channel_id, source_message_id, source_version, "
                "source_received_at, content_hash, message_text) "
                "VALUES (%s,'operator',%s,%s,'v1',%s,%s,%s)",
                (raw_id, raw_channel, client_ref or f"operator-{raw_id}", now,
                 hashlib.sha256(f"{raw_id}|{json.dumps(body, sort_keys=True, default=str)}".encode()).hexdigest(),
                 f"[{source}] {action} {symbol}: {reason}"),
            )
            cur.execute(
                "INSERT INTO message_processing_runs (processing_run_id, raw_message_id, status) "
                "VALUES (%s,%s,'succeeded')",
                (run_id, raw_id),
            )
            cur.execute(
                "INSERT INTO context_snapshots (context_snapshot_id, raw_message_id, snapshot_type, "
                "context_version, snapshot) VALUES (%s,%s,'system','v1',%s)",
                (ctx_id, raw_id, Json({"operator_request": body})),
            )
            cur.execute(
                "INSERT INTO hermes_decisions (decision_id, raw_message_id, processing_run_id, "
                "context_snapshot_id, message_type, action, ambiguous, account_scope, target_account_id, "
                "instrument_symbol, side, entry_type, entry_price, entry_price_min, entry_price_max, "
                "stop_loss, take_profits, leverage, valid_until, evidence, model_provider, model_version, "
                "prompt_version, context_version, temperature, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,false,'single',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'hermes',%s,"
                "'operator-v1','v1',0,%s)",
                (dec_id, raw_id, run_id, ctx_id, message_type, action, account_id, symbol, side,
                 entry_type, entry_price, entry_price_min, entry_price_max, stop_loss,
                 Json(take_profits), leverage, valid_until,
                 Json([authorization_evidence]), source, now),
            )
            cur.execute(
                "INSERT INTO risk_decisions (risk_decision_id, hermes_decision_id, status, account_id, "
                "instrument_id, risk_budget, checks, reason, decided_by) "
                "VALUES (%s,%s,'approved',%s,%s,%s,%s,%s,'hermes-operator')",
                (risk_id, dec_id, account_id, symbol, Json(risk_budget), Json(checks), reason),
            )
            cur.execute(
                "INSERT INTO trade_intents (intent_id, hermes_decision_id, risk_decision_id, schema_version, "
                "account_id, instrument_id, action, status, order_plan, risk_budget, target_position_id, "
                "valid_until, idempotency_key, approved_at) "
                "VALUES (%s,%s,%s,'1.0',%s,%s,%s,'approved',%s,%s,%s,%s,%s, now())",
                (intent_id, dec_id, risk_id, account_id, symbol, action, Json(order_plan),
                 Json(risk_budget), body.get("target_position_id"), valid_until, idem),
            )
            cur.execute(
                "INSERT INTO outbox_events (outbox_event_id, status, aggregate_type, aggregate_id, "
                "event_type, payload) VALUES (%s,'pending','trade_intent',%s,'trade_intent.approved',%s)",
                (str(uuid4()), intent_id,
                 Json({
                     "intent_id": intent_id,
                     "risk_decision_id": risk_id,
                     "source": source,
                     "authorization": authorization_evidence,
                 })),
            )
        conn.commit()
    finally:
        conn.close()

    response = {
        "intent_id": intent_id,
        "status": "approved",
        "replay": False,
        "account_id": account_id,
        "instrument_id": symbol,
        "action": action,
        "order_plan": order_plan,
        "execution_preview": _safe_execution_preview(order_plan, risk_budget, symbol, action),
        "risk_budget": risk_budget,
        "valid_until": valid_until.isoformat(),
        "authorization": authorization_evidence,
    }
    if attribution:
        response["attribution"] = attribution
    return response


@app.get("/v1/operator/orders/{intent_id}")
def operator_order_status(intent_id: str, authorization: str | None = Header(default=None)):
    """Execution status of an operator-placed intent: intent row + order projections
    (fills) + the current position on that instrument, so Hermes can report back."""
    require_reader(authorization)
    try:
        UUID(intent_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="intent_id must be a uuid")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="store unavailable")
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT intent_id::text, account_id, instrument_id, action::text, status::text, "
                "order_plan, risk_budget, valid_until, approved_at, created_at "
                "FROM trade_intents WHERE intent_id=%s",
                (intent_id,),
            )
            intent = cur.fetchone()
            if intent is None:
                raise HTTPException(status_code=404, detail="intent not found")
            cur.execute(
                "SELECT client_order_id, status::text, filled_quantity, average_fill_price, updated_at "
                "FROM orders_projection WHERE intent_id=%s ORDER BY updated_at",
                (intent_id,),
            )
            orders = cur.fetchall()
            cur.execute(
                "SELECT account_id, instrument_id, side::text, quantity, avg_entry_price, status::text "
                "FROM positions_projection WHERE account_id=%s AND instrument_id LIKE %s AND status='open'",
                (intent["account_id"], intent["instrument_id"].split("-")[0] + "%"),
            )
            positions = cur.fetchall()
            cur.execute(
                "SELECT event_type, client_order_id, venue_order_id, trade_id, payload, ts_event "
                "FROM execution_events WHERE intent_id=%s ORDER BY ts_event",
                (intent_id,),
            )
            events = cur.fetchall()
    finally:
        conn.close()
    return jsonable_encoder({"intent": intent, "orders": orders,
                             "execution_events": events, "open_positions": positions})
