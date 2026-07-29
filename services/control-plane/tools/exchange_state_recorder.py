"""Record-only mirror of real exchange state (positions + orders + algo orders).

Why this exists: orders_projection is event-sourced and drifts whenever a node
misses events (freeze/restart windows), and Binance USDT-M migrated conditional
orders (STOP_MARKET / TAKE_PROFIT) to the algo-order system on 2025-12-09 —
they are invisible to /fapi/v1/openOrders and to the projections. This recorder
polls the exchange directly (read-only, signed GET only) and upserts one row
per account into exchange_state_mirror so SystemSnapshotV1 can expose the
truth. It never touches the trading path; on any error it logs and skips the
cycle, letting the row go stale (staleness is computed by the snapshot reader).

API keys are read from the running node containers' env (single source of
truth; survives key rotation via the recreate scripts).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError

import psycopg2

ACCOUNTS = {
    "account-a": ("trader-v3-node-a", "BINANCE_ACCOUNT_A"),
    "account-b": ("trader-v3-node-b", "BINANCE_ACCOUNT_B"),
}
BINANCE_RECV_WINDOW_MS = 30_000

UPSERT_SQL = """
INSERT INTO exchange_state_mirror (account_id, payload, updated_at)
VALUES (%s, %s::jsonb, now())
ON CONFLICT (account_id) DO UPDATE
  SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
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
        code = payload.get("code", exc.code)
        message = payload.get("msg", raw_text or exc.reason)
        raise RuntimeError(f"Binance API {code}: {message}") from exc


def slim_order(o: dict) -> dict:
    return {
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "type": o.get("type") or o.get("orderType"),
        "quantity": o.get("origQty") or o.get("quantity"),
        "price": o.get("price"),
        "trigger_price": o.get("stopPrice") or o.get("triggerPrice"),
        "reduce_only": o.get("reduceOnly"),
        "client_order_id": o.get("clientOrderId") or o.get("clientAlgoId"),
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


def snapshot_account(base: str, key: str, sec: str) -> dict:
    positions = [p for p in signed_get(base, "/fapi/v2/positionRisk", key, sec)
                 if float(p.get("positionAmt") or 0) != 0]
    regular = [slim_order(o) for o in signed_get(base, "/fapi/v1/openOrders", key, sec)]
    algo_raw = signed_get(base, "/fapi/v1/openAlgoOrders", key, sec)
    algo_rows = algo_raw.get("orders", algo_raw) if isinstance(algo_raw, dict) else algo_raw
    algo = [slim_order(o) for o in algo_rows]
    return {
        "source": "binance_fapi",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "positions": [
            {"symbol": p["symbol"], "position_amt": p["positionAmt"],
             "entry_price": p.get("entryPrice"), "unrealized_pnl": p.get("unRealizedProfit"),
             "position_side": p.get("positionSide")}
            for p in positions
        ],
        "open_orders": regular,
        "algo_orders": algo,
        "protections": protections(positions, algo, regular),
    }


def run_once(conn, base: str) -> None:
    for account_id, (container, prefix) in ACCOUNTS.items():
        creds = container_keys(container, prefix)
        if not creds:
            continue
        try:
            payload = snapshot_account(base, *creds)
        except Exception as exc:  # noqa: BLE001 - stale row is the failure signal
            log(f"{account_id}: exchange fetch failed, leaving row stale: {exc}")
            continue
        with conn.cursor() as cur:
            cur.execute(UPSERT_SQL, (account_id, json.dumps(payload)))
        conn.commit()


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
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
            conn = psycopg2.connect(args.db_url)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
