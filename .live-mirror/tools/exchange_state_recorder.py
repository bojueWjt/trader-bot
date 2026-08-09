"""Record-only mirror of real exchange state (positions + orders + algo orders).

Why this exists: orders_projection is event-sourced and drifts whenever a node
misses events (freeze/restart windows), and Binance USDT-M migrated conditional
orders (STOP_MARKET / TAKE_PROFIT) to the algo-order system on 2025-12-09 —
they are invisible to /fapi/v1/openOrders and to the projections. This recorder
polls the exchange directly (read-only, signed GET only) and atomically upserts
exchange_state_mirror plus accounts_projection for each account. It never
touches the trading path; on any exchange error it logs and skips the cycle,
letting the existing rows go stale.

API keys are read from the running node containers' env (single source of
truth; survives key rotation via the recreate scripts).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError

import psycopg2

ACCOUNTS = {
    "account-a": ("trader-v3-node-a", "BINANCE_ACCOUNT_A"),
    "account-b": ("trader-v3-node-b", "BINANCE_ACCOUNT_B"),
}
BINANCE_RECV_WINDOW_MS = 30_000
DEFAULT_RECENT_HISTORY_MAX_SYMBOLS = 16
ABSOLUTE_RECENT_HISTORY_MAX_SYMBOLS = 64

UPSERT_SQL = """
INSERT INTO exchange_state_mirror (account_id, payload, updated_at)
VALUES (%s, %s::jsonb, now())
ON CONFLICT (account_id) DO UPDATE
  SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
"""

ACCOUNT_UPSERT_SQL = """
INSERT INTO accounts_projection (
    account_id,
    currency,
    equity,
    margin,
    available_balance,
    updated_at,
    payload
)
VALUES (%s, %s, %s, %s, %s, now(), %s::jsonb)
ON CONFLICT (account_id) DO UPDATE
  SET currency = EXCLUDED.currency,
      equity = EXCLUDED.equity,
      margin = EXCLUDED.margin,
      available_balance = EXCLUDED.available_balance,
      updated_at = EXCLUDED.updated_at,
      payload = COALESCE(accounts_projection.payload, '{}'::jsonb) || EXCLUDED.payload
"""


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S", time.gmtime()) + f" {msg}", flush=True)


def container_keys(container: str, prefix: str) -> tuple[str, str] | None:
    try:
        out = subprocess.run(
            ["docker", "inspect", container, "--format", "{{json .Config.Env}}"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
        env = dict(item.split("=", 1) for item in json.loads(out) if "=" in item)
        key, sec = env.get(f"{prefix}_API_KEY"), env.get(f"{prefix}_API_SECRET")
        if key and sec:
            return key, sec
    except Exception as exc:  # noqa: BLE001 - missing keys must not kill the loop
        log(f"key fetch failed for {container}: {exc}")
    return None


def signed_get(base: str, path: str, key: str, sec: str, params: dict | None = None) -> object:
    q = dict(params or {})
    q["recvWindow"] = BINANCE_RECV_WINDOW_MS
    q["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(q)
    sig = hmac.new(sec.encode(), query.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{base}{path}?{query}&signature={sig}", headers={"X-MBX-APIKEY": key}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except HTTPError as exc:
        raw = exc.read()
        raw_text = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            payload = {}
        code = exc.code
        message = raw_text or exc.reason
        if isinstance(payload, dict):
            code = payload.get("code", exc.code)
            message = payload.get("msg", message)
        raise RuntimeError(f"Binance API {code}: {message}") from exc


def slim_order(o: dict, order_kind: str = "regular") -> dict:
    venue_order_id = o.get("orderId")
    if order_kind == "algo":
        venue_order_id = o.get("algoId")
    return {
        "symbol": o.get("symbol"),
        "position_side": o.get("positionSide"),
        "side": o.get("side"),
        "type": o.get("type") or o.get("orderType"),
        "quantity": o.get("origQty") or o.get("quantity"),
        "price": o.get("price"),
        "trigger_price": o.get("stopPrice") or o.get("triggerPrice"),
        "reduce_only": o.get("reduceOnly"),
        "client_order_id": o.get("clientOrderId") or o.get("clientAlgoId"),
        "order_kind": order_kind,
        "venue_order_id": venue_order_id,
        "status": o.get("status") or o.get("algoStatus"),
        "executed_quantity": o.get("executedQty") or o.get("actualQty"),
        "average_price": o.get("avgPrice") or o.get("averagePrice"),
        "created_at_ms": o.get("time") or o.get("createTime"),
        "updated_at_ms": o.get("updateTime") or o.get("workingTime"),
        # Retained for dashboard compatibility. Consumers must route by order_kind.
        "order_id": o.get("orderId") or o.get("algoId"),
    }


def protections(positions: list[dict], algo: list[dict], regular: list[dict]) -> list[dict]:
    """Per open position: which stop-loss / take-profit orders protect it."""
    out = []
    for p in positions:
        sym, amt = p["symbol"], float(p["positionAmt"])
        closing_side = "SELL" if amt > 0 else "BUY"
        stops = [o for o in algo
                 if o["symbol"] == sym and o["side"] == closing_side and "STOP" in (o["type"] or "")]
        tps = [o for o in algo
               if o["symbol"] == sym and o["side"] == closing_side and "TAKE_PROFIT" in (o["type"] or "")]
        tps += [o for o in regular
                if o["symbol"] == sym and o["side"] == closing_side and o.get("reduce_only")]
        out.append({
            "symbol": sym,
            "position_amt": p["positionAmt"],
            "entry_price": p.get("entryPrice"),
            "has_stop_loss": bool(stops),
            "stop_loss_orders": stops,
            "take_profit_orders": tps,
        })
    return out


def canonical_account_summary(account_info: dict) -> dict:
    equity = _required_decimal(account_info, "totalMarginBalance")
    margin = _required_decimal(account_info, "totalInitialMargin")
    available = _required_decimal(account_info, "availableBalance")
    if equity <= 0:
        raise ValueError("totalMarginBalance must be positive")
    if margin < 0:
        raise ValueError("totalInitialMargin must be non-negative")
    if available < 0:
        raise ValueError("availableBalance must be non-negative")
    return {
        "currency": "USDT",
        "equity": str(equity),
        "margin": str(margin),
        "free": str(available),
    }


def _required_decimal(payload: dict, field: str) -> Decimal:
    raw = payload.get(field)
    if raw is None or raw == "":
        raise ValueError(f"{field} missing from account information")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def _order_rows(payload: object) -> list[dict]:
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("orders", [])
    if not isinstance(rows, list):
        raise TypeError("order history response must be a collection")
    return [row for row in rows if isinstance(row, dict)]


def _recent_history_symbols(
    positions: list[dict],
    regular_orders: list[dict],
    algo_orders: list[dict],
    targeted_symbols: tuple[str, ...] = (),
    *,
    max_symbols: int = DEFAULT_RECENT_HISTORY_MAX_SYMBOLS,
) -> tuple[str, ...]:
    active_symbols = sorted({
        str(row.get("symbol") or "").strip().upper()
        for row in positions + regular_orders + algo_orders
        if str(row.get("symbol") or "").strip()
    })
    ordered = tuple(
        dict.fromkeys(
            symbol
            for symbol in (
                _normalized_symbols(targeted_symbols)
                + tuple(active_symbols)
            )
            if symbol
        )
    )
    return ordered[:max_symbols]


def _normalized_symbols(
    values: tuple[str, ...],
) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        symbol = str(value or "").strip().upper()
        if not symbol:
            continue
        if "-PERP." in symbol:
            symbol = symbol.split("-", 1)[0]
        if not re.fullmatch(r"[A-Z0-9]{3,24}", symbol):
            continue
        normalized.append(symbol)
    return tuple(dict.fromkeys(normalized))


def _history_symbol_limit() -> int:
    raw = os.environ.get(
        "EXCHANGE_STATE_HISTORY_MAX_SYMBOLS",
        str(DEFAULT_RECENT_HISTORY_MAX_SYMBOLS),
    )
    try:
        requested = int(raw)
    except ValueError:
        requested = DEFAULT_RECENT_HISTORY_MAX_SYMBOLS
    return max(
        1,
        min(requested, ABSOLUTE_RECENT_HISTORY_MAX_SYMBOLS),
    )


def _configured_history_symbols(
    account_id: str,
) -> tuple[str, ...]:
    account_env = re.sub(
        r"[^A-Z0-9]",
        "_",
        account_id.upper(),
    )
    raw_values = (
        os.environ.get("EXCHANGE_STATE_HISTORY_SYMBOLS", ""),
        os.environ.get(
            f"EXCHANGE_STATE_HISTORY_SYMBOLS_{account_env}",
            "",
        ),
    )
    return _normalized_symbols(
        tuple(
            value
            for raw in raw_values
            for value in re.split(r"[\s,]+", raw)
            if value
        )
    )


def pending_opening_symbols(
    conn,
    account_id: str,
    *,
    limit: int,
) -> tuple[str, ...]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT instrument_id
            FROM trade_intents
            WHERE account_id=%s
              AND status='approved'
              AND valid_until > now()
              AND action IN ('open_position', 'add_position')
            ORDER BY updated_at DESC, intent_id
            LIMIT %s
            """,
            (account_id, limit),
        )
        rows = cur.fetchall()
    return _normalized_symbols(
        tuple(str(row[0]) for row in rows if row)
    )


def recent_order_history(
    base: str,
    key: str,
    sec: str,
    symbols: tuple[str, ...],
) -> tuple[list[dict], list[dict]]:
    regular_history: list[dict] = []
    algo_history: list[dict] = []
    for symbol in symbols:
        regular_rows = _order_rows(
            signed_get(
                base,
                "/fapi/v1/allOrders",
                key,
                sec,
                {"symbol": symbol, "limit": 1000},
            )
        )
        regular_history.extend(slim_order(row, "regular") for row in regular_rows)

        algo_rows = _order_rows(
            signed_get(
                base,
                "/fapi/v1/allAlgoOrders",
                key,
                sec,
                {"symbol": symbol, "limit": 1000},
            )
        )
        algo_history.extend(slim_order(row, "algo") for row in algo_rows)
    return regular_history, algo_history


def snapshot_account(
    base: str,
    key: str,
    sec: str,
    *,
    targeted_history_symbols: tuple[str, ...] = (),
    history_symbol_limit: int = DEFAULT_RECENT_HISTORY_MAX_SYMBOLS,
) -> dict:
    account_info = signed_get(base, "/fapi/v3/account", key, sec)
    if not isinstance(account_info, dict):
        raise TypeError("account information response must be an object")
    account = canonical_account_summary(account_info)
    positions = [p for p in signed_get(base, "/fapi/v2/positionRisk", key, sec)
                 if float(p.get("positionAmt") or 0) != 0]
    regular = [
        slim_order(o, "regular")
        for o in signed_get(base, "/fapi/v1/openOrders", key, sec)
    ]
    algo_raw = signed_get(base, "/fapi/v1/openAlgoOrders", key, sec)
    algo_rows = algo_raw.get("orders", algo_raw) if isinstance(algo_raw, dict) else algo_raw
    algo = [slim_order(o, "algo") for o in algo_rows]
    history_symbols = _recent_history_symbols(
        positions,
        regular,
        algo,
        targeted_history_symbols,
        max_symbols=history_symbol_limit,
    )
    all_history_symbols = _recent_history_symbols(
        positions,
        regular,
        algo,
        targeted_history_symbols,
        max_symbols=ABSOLUTE_RECENT_HISTORY_MAX_SYMBOLS,
    )
    regular_history, algo_history = recent_order_history(
        base,
        key,
        sec,
        history_symbols,
    )
    return {
        "source": "binance_fapi",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "account": account,
        "positions": [
            {"symbol": p["symbol"], "position_amt": p["positionAmt"],
             "entry_price": p.get("entryPrice"), "mark_price": p.get("markPrice"),
             "unrealized_pnl": p.get("unRealizedProfit"),
             "position_side": p.get("positionSide")}
            for p in positions
        ],
        "open_orders": regular,
        "algo_orders": algo,
        "recent_order_history": regular_history,
        "recent_algo_order_history": algo_history,
        "recent_order_history_symbols": list(history_symbols),
        "recent_order_history_coverage": {
            "max_symbols": history_symbol_limit,
            "targeted_symbols": list(
                _normalized_symbols(targeted_history_symbols)
            ),
            "queried_symbols": list(history_symbols),
            "truncated": len(all_history_symbols) > len(
                history_symbols
            ),
        },
        "protections": protections(positions, algo, regular),
    }


def run_once(conn, base: str) -> None:
    history_symbol_limit = _history_symbol_limit()
    for account_id, (container, prefix) in ACCOUNTS.items():
        creds = container_keys(container, prefix)
        if not creds:
            continue
        try:
            pending_symbols = pending_opening_symbols(
                conn,
                account_id,
                limit=history_symbol_limit,
            )
        except psycopg2.Error as exc:
            conn.rollback()
            pending_symbols = ()
            log(
                f"{account_id}: pending opening symbol query "
                f"degraded: {exc}"
            )
        else:
            conn.rollback()
        targeted_symbols = tuple(
            dict.fromkeys(
                pending_symbols
                + _configured_history_symbols(account_id)
            )
        )
        try:
            payload = snapshot_account(
                base,
                *creds,
                targeted_history_symbols=targeted_symbols,
                history_symbol_limit=history_symbol_limit,
            )
        except Exception as exc:  # noqa: BLE001 - stale row is the failure signal
            log(f"{account_id}: exchange fetch failed, leaving row stale: {exc}")
            continue
        account = payload["account"]
        account_payload = {
            "exchange_account": account,
            "account_snapshot_source": "binance_fapi_account_v3",
            "account_snapshot_fetched_at": payload["fetched_at"],
        }
        try:
            with conn.cursor() as cur:
                cur.execute(UPSERT_SQL, (account_id, json.dumps(payload)))
                cur.execute(
                    ACCOUNT_UPSERT_SQL,
                    (
                        account_id,
                        account["currency"],
                        account["equity"],
                        account["margin"],
                        account["free"],
                        json.dumps(account_payload),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=int, default=45)
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--base-url", default=os.environ.get("EXCHANGE_STATE_BASE_URL", "https://fapi.binance.com"))
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if not args.db_url:
        print("DATABASE_URL not set and --db-url missing", file=sys.stderr)
        sys.exit(2)
    conn = psycopg2.connect(args.db_url)
    log(f"exchange state recorder start interval={args.interval}s base={args.base_url}")
    while True:
        try:
            run_once(conn, args.base_url)
        except psycopg2.Error as exc:
            log(f"db error, reconnecting: {exc}")
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110
                pass
            time.sleep(5)
            conn = psycopg2.connect(args.db_url)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
