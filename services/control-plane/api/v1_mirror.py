"""M1b read routers: GET /v1/mirror/positions and GET /v1/reconcile."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Header
from psycopg2.extras import RealDictCursor

from position_protection import _dec_text, protection_status

router = APIRouter()
MIRROR_STALE_SECONDS = 300
_OPEN_PROJECTION_STATUSES = ("accepted", "partially_filled", "updated", "working")


def _dec_or_none(value) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    return format(number, "f")


def _position_side(raw) -> str:
    side = str(raw or "").strip().upper()
    if side in ("LONG", "BUY"):
        return "LONG"
    if side in ("SHORT", "SELL"):
        return "SHORT"
    if side in ("BOTH", "NET"):
        return "BOTH"
    return side or "BOTH"


def _filter_value(payload: dict, symbol: str, *keys: str) -> str:
    filters = payload.get("filters") or payload.get("exchange_filters") or {}
    per_symbol = filters.get(symbol) if isinstance(filters, dict) else None
    if isinstance(per_symbol, dict):
        for key in keys:
            if per_symbol.get(key) is not None:
                return _dec_text(per_symbol.get(key))
    for key in keys:
        if payload.get(key) is not None:
            return _dec_text(payload.get(key))
    return "0.001"


def _client_id(order: dict) -> str:
    for key in ("client_order_id", "clientOrderId", "client_algo_id", "clientAlgoId"):
        value = order.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _exchange_order_id(order: dict) -> str:
    for key in ("order_id", "orderId", "algo_id", "algoId", "exchange_order_id"):
        value = order.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return _client_id(order)


def _exchange_orders(payload: dict) -> list[dict]:
    return list(payload.get("open_orders") or []) + list(payload.get("algo_orders") or [])


def _mirror_position_row(raw: dict, payload: dict) -> dict | None:
    symbol = str(raw.get("symbol") or "").split("-", 1)[0]
    if not symbol:
        return None
    qty = raw.get("quantity")
    if qty is None:
        qty = raw.get("position_amt") or raw.get("positionAmt")
    try:
        qty_dec = Decimal(str(qty or "0"))
    except (InvalidOperation, TypeError, ValueError):
        qty_dec = Decimal("0")
    if qty_dec == 0:
        return None
    side = _position_side(raw.get("position_side") or raw.get("positionSide") or raw.get("side"))
    open_orders = payload.get("open_orders") or []
    algo_orders = payload.get("algo_orders") or []
    protection = protection_status(
        symbol=symbol,
        position_side=side,
        quantity=qty_dec.copy_abs(),
        open_orders=open_orders,
        algo_orders=algo_orders,
    )
    return {
        "symbol": symbol,
        "position_side": side,
        "quantity": format(qty_dec.copy_abs(), "f"),
        "entry_price": _dec_or_none(raw.get("entry_price") or raw.get("entryPrice")) or "0",
        "mark_price": _dec_or_none(raw.get("mark_price") or raw.get("markPrice")) or "0",
        "unrealized_pnl": _dec_or_none(raw.get("unrealized_pnl") or raw.get("unRealizedProfit")) or "0",
        "leverage": _dec_or_none(raw.get("leverage")) or "0",
        "quantity_step": _filter_value(payload, symbol, "quantity_step", "stepSize", "qtyStep"),
        "min_quantity": _filter_value(payload, symbol, "min_quantity", "minQty"),
        "protection": protection,
    }


@router.get("/v1/mirror/positions")
def v1_mirror_positions(authorization: str | None = Header(default=None)):
    import read_api as ra

    ra.require_reader(authorization)
    conn = ra._read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = ra._envelope(cur)
            cur.execute(
                """
                SELECT account_id, payload, updated_at,
                       EXTRACT(EPOCH FROM (now() - updated_at)) AS age_seconds
                FROM exchange_state_mirror
                ORDER BY account_id
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        accounts = []
        any_stale = False
        for row in rows:
            payload = row.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            age = float(row.get("age_seconds") or 0)
            stale = age > MIRROR_STALE_SECONDS
            any_stale = any_stale or stale
            open_orders = payload.get("open_orders") or []
            algo_orders = payload.get("algo_orders") or []
            positions = []
            for raw in payload.get("positions") or []:
                if not isinstance(raw, dict):
                    continue
                built = _mirror_position_row(raw, payload)
                if built is not None:
                    positions.append(built)
            accounts.append(
                {
                    "account_id": row["account_id"],
                    "mirror_age_seconds": round(age, 1),
                    "stale": stale,
                    "positions": positions,
                    "open_orders_count": len(open_orders),
                    "algo_orders_count": len(algo_orders),
                }
            )
        env["data_source"] = "exchange_state_mirror"
        env["stale"] = bool(env.get("stale") or any_stale)
        return {**env, "data": {"accounts": accounts}}
    finally:
        conn.close()


@router.get("/v1/reconcile")
def v1_reconcile(authorization: str | None = Header(default=None)):
    import read_api as ra

    ra.require_reader(authorization)
    conn = ra._read_conn()
    now = datetime.now(timezone.utc)
    detected_at = now.isoformat()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = ra._envelope(cur)
            cur.execute(
                """
                SELECT account_id, payload,
                       EXTRACT(EPOCH FROM (now() - updated_at)) AS age_seconds
                FROM exchange_state_mirror
                ORDER BY account_id
                """
            )
            mirrors = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT account_id, client_order_id, instrument_id, status, venue_order_id
                FROM orders_projection
                WHERE status = ANY(%s)
                """,
                (list(_OPEN_PROJECTION_STATUSES),),
            )
            projection = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT reconciliation_run_id::text AS run_id, completed_at
                FROM reconciliation_runs
                ORDER BY completed_at DESC NULLS LAST, started_at DESC
                LIMIT 1
                """
            )
            last = cur.fetchone()
            cur.execute(
                "SELECT count(*)::int AS n FROM reconciliation_findings WHERE status = 'open'"
            )
            findings_open = int((cur.fetchone() or {}).get("n") or 0)

        skipped = []
        live_ids: dict[str, set[str]] = {}
        exchange_rows: dict[str, list[dict]] = {}
        for row in mirrors:
            account_id = row["account_id"]
            payload = row.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            age = float(row.get("age_seconds") or 0)
            if age > MIRROR_STALE_SECONDS:
                skipped.append(account_id)
                continue
            orders = _exchange_orders(payload)
            exchange_rows[account_id] = orders
            live_ids[account_id] = {
                cid for order in orders if (cid := _client_id(order))
            }

        ghosts = []
        for proj in projection:
            account_id = proj["account_id"]
            if account_id in skipped or account_id not in live_ids:
                continue
            cid = str(proj.get("client_order_id") or "")
            if cid and cid in live_ids[account_id]:
                continue
            ghosts.append(
                {
                    "account_id": account_id,
                    "symbol": ra._symbol(proj.get("instrument_id")),
                    "client_order_id": cid,
                    "projection_status": proj.get("status"),
                    "detected_at": detected_at,
                }
            )

        missing = []
        proj_ids_by_account: dict[str, set[str]] = {}
        for proj in projection:
            proj_ids_by_account.setdefault(proj["account_id"], set()).add(
                str(proj.get("client_order_id") or "")
            )
        for account_id, orders in exchange_rows.items():
            known = proj_ids_by_account.get(account_id, set())
            for order in orders:
                cid = _client_id(order)
                if cid and cid in known:
                    continue
                missing.append(
                    {
                        "account_id": account_id,
                        "symbol": str(order.get("symbol") or "").split("-", 1)[0],
                        "exchange_order_id": _exchange_order_id(order),
                        "detected_at": detected_at,
                    }
                )

        last_run = None
        if last:
            completed = last.get("completed_at")
            last_run = {
                "run_id": last.get("run_id"),
                "completed_at": completed.isoformat() if hasattr(completed, "isoformat") else completed,
                "findings_open": findings_open,
            }
        env["data_source"] = env.get("data_source") or "postgres_projection"
        return {
            **env,
            "data": {
                "ghost_orders": ghosts,
                "missing_orders": missing,
                "skipped_accounts": skipped,
                "last_run": last_run,
            },
        }
    finally:
        conn.close()
