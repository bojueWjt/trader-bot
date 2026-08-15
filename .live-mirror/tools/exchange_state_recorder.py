"""Record-only mirror of real exchange state (positions + orders + algo orders).

Why this exists: orders_projection is event-sourced and drifts whenever a node
misses events (freeze/restart windows), and Binance USDT-M migrated conditional
orders (STOP_MARKET / TAKE_PROFIT) to the algo-order system on 2025-12-09 —
they are invisible to /fapi/v1/openOrders and to the projections. This recorder
polls the exchange directly (read-only, signed GET only) and atomically upserts
exchange_state_mirror plus accounts_projection for each account. It never
touches the trading path; on any exchange error it logs and skips the cycle,
letting the existing rows go stale.

API keys are read from the running node containers' env or reviewed read-only
secret mounts (single source of truth; survives key rotation via recreate).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError

import psycopg2

ACCOUNTS = {
    "account-a": ("trader-v3-node-a", "BINANCE_ACCOUNT_A"),
    "account-b": ("trader-v3-node-b", "BINANCE_ACCOUNT_B"),
    "account-c": ("trader-v3-node-c", "BINANCE_ACCOUNT_C"),
    "account-d": ("trader-v3-node-d", "BINANCE_ACCOUNT_D"),
}
BINANCE_RECV_WINDOW_MS = 30_000
BINANCE_PROXY_ENV = "BINANCE_PROXY_URL"

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


def _secret_mount_value(inspected: dict, destination: str) -> str | None:
    mounts = inspected.get("Mounts")
    if not isinstance(mounts, list):
        return None
    matches = [
        mount
        for mount in mounts
        if isinstance(mount, dict)
        and str(mount.get("Destination") or "") == destination
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError(f"secret mount is not unique: {destination}")
    mount = matches[0]
    if mount.get("RW") is not False:
        raise RuntimeError(f"secret mount must be read-only: {destination}")
    source = Path(str(mount.get("Source") or ""))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    try:
        source_stat = os.fstat(descriptor)
        source_mode = stat.S_IMODE(source_stat.st_mode)
        if not stat.S_ISREG(source_stat.st_mode):
            raise RuntimeError(
                f"secret mount source must be a regular file: {destination}"
            )
        if (
            source_stat.st_uid != 0
            or source_stat.st_gid != 999
            or source_mode != 0o440
        ):
            raise RuntimeError(
                "secret mount source ownership or mode is invalid: "
                f"{destination}"
            )
        raw = os.read(descriptor, 8193)
    finally:
        os.close(descriptor)
    if len(raw) > 8192:
        raise RuntimeError(f"secret mount value is too large: {destination}")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"secret mount value is not UTF-8: {destination}"
        ) from exc
    if not value:
        raise RuntimeError(f"secret mount value is empty: {destination}")
    return value


def container_keys(container: str, prefix: str) -> tuple[str, str] | None:
    try:
        out = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
        inspected_rows = json.loads(out)
        if not isinstance(inspected_rows, list) or len(inspected_rows) != 1:
            raise RuntimeError("docker inspect must return one container")
        inspected = inspected_rows[0]
        env_rows = inspected.get("Config", {}).get("Env", [])
        env = dict(
            item.split("=", 1)
            for item in env_rows
            if isinstance(item, str) and "=" in item
        )
        key = env.get(f"{prefix}_API_KEY")
        secret = env.get(f"{prefix}_API_SECRET")
        if key and secret:
            return key, secret

        secret_stem = prefix.lower()
        key = _secret_mount_value(
            inspected,
            f"/run/secrets/{secret_stem}_api_key",
        )
        secret = _secret_mount_value(
            inspected,
            f"/run/secrets/{secret_stem}_api_secret",
        )
        if key and secret:
            return key, secret
    except Exception as exc:  # noqa: BLE001 - missing keys must not kill the loop
        log(f"key fetch failed for {container}: {exc}")
    return None


def build_binance_opener(proxy_url: str | None = None):
    configured_proxy = proxy_url
    if configured_proxy is None:
        configured_proxy = os.environ.get(BINANCE_PROXY_ENV, "")
    configured_proxy = configured_proxy.strip()

    proxies: dict[str, str] = {}
    if configured_proxy:
        parsed_proxy = urllib.parse.urlsplit(configured_proxy)
        if (
            parsed_proxy.scheme not in {"http", "https"}
            or not parsed_proxy.hostname
        ):
            raise ValueError(
                f"{BINANCE_PROXY_ENV} must be an http(s) URL"
            )
        if (
            parsed_proxy.username is not None
            or parsed_proxy.password is not None
        ):
            raise ValueError(
                f"{BINANCE_PROXY_ENV} must not contain credentials"
            )
        proxies = {
            "http": configured_proxy,
            "https": configured_proxy,
        }

    return urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies)
    )


def signed_get(
    base: str,
    path: str,
    key: str,
    sec: str,
    params: dict | None = None,
    opener=None,
) -> object:
    q = dict(params or {})
    q["recvWindow"] = BINANCE_RECV_WINDOW_MS
    q["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(q)
    sig = hmac.new(sec.encode(), query.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{base}{path}?{query}&signature={sig}", headers={"X-MBX-APIKEY": key}
    )
    request_opener = opener
    if request_opener is None:
        request_opener = build_binance_opener()
    try:
        with request_opener.open(req, timeout=15) as resp:
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
    if equity < 0:
        raise ValueError("totalMarginBalance must be non-negative")
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


def snapshot_account(base: str, key: str, sec: str, opener=None) -> dict:
    request_opener = opener
    if request_opener is None:
        request_opener = build_binance_opener()
    account_info = signed_get(
        base,
        "/fapi/v3/account",
        key,
        sec,
        opener=request_opener,
    )
    if not isinstance(account_info, dict):
        raise TypeError("account information response must be an object")
    account = canonical_account_summary(account_info)
    positions = [p for p in signed_get(
        base,
        "/fapi/v2/positionRisk",
        key,
        sec,
        opener=request_opener,
    )
                 if float(p.get("positionAmt") or 0) != 0]
    regular = [
        slim_order(o, "regular")
        for o in signed_get(
            base,
            "/fapi/v1/openOrders",
            key,
            sec,
            opener=request_opener,
        )
    ]
    algo_raw = signed_get(
        base,
        "/fapi/v1/openAlgoOrders",
        key,
        sec,
        opener=request_opener,
    )
    algo_rows = algo_raw.get("orders", algo_raw) if isinstance(algo_raw, dict) else algo_raw
    algo = [slim_order(o, "algo") for o in algo_rows]
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
        "protections": protections(positions, algo, regular),
    }


def run_once(conn, base: str, opener=None) -> None:
    request_opener = opener
    if request_opener is None:
        request_opener = build_binance_opener()
    for account_id, (container, prefix) in ACCOUNTS.items():
        creds = container_keys(container, prefix)
        if not creds:
            continue
        try:
            payload = snapshot_account(
                base,
                *creds,
                opener=request_opener,
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
    opener = build_binance_opener()
    conn = psycopg2.connect(args.db_url)
    log(f"exchange state recorder start interval={args.interval}s base={args.base_url}")
    while True:
        try:
            run_once(conn, args.base_url, opener=opener)
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
