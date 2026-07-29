#!/usr/bin/env python3
"""v3-trader — place and manage orders through the trader-v3 control plane.

Hermes is the decision maker; this script is its ONLY order interface.
Orders become approved trade intents in the control plane; the nautilus
execution node (the only component allowed to touch the exchange) executes
them. Never call exchange APIs directly.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("V3_CONTROL_PLANE_URL", "http://127.0.0.1:8080")
ENV_FILE = os.environ.get("V3_ENV_FILE", "/srv/trader-v3/.env.v3")


def _token() -> str:
    tok = os.environ.get("RISK_ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("RISK_ADMIN_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    print(json.dumps({"error": "RISK_ADMIN_TOKEN unavailable (check /srv/trader-v3/.env.v3)"}))
    sys.exit(2)


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"},
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except (ValueError, AttributeError):
            detail = body
        print(json.dumps({"error": f"HTTP {exc.code}", "detail": detail}, ensure_ascii=False))
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(json.dumps({"error": "control-plane unreachable", "detail": str(exc.reason)}))
        sys.exit(1)


_TERMINAL = {"filled", "rejected", "canceled", "cancelled", "denied", "expired"}
_TERMINAL_EVENTS = {"orderfilled", "orderrejected", "orderdenied", "ordercanceled",
                    "orderexpired", "positionclosed", "positionopened"}
_TG_OPEN_REF_RE = re.compile(
    r"tg-sig-c(?P<channel>\d+)-m(?P<message>\d+)(?:-e\d+)?"
)
_OPERATOR_OPEN_REF_RE = re.compile(
    r"operator(?:-[a-z0-9][a-z0-9._-]*)?"
)


def _report(intent_id: str, wait: bool) -> dict:
    tries = 8 if wait else 1
    status = _call("GET", f"/v1/operator/orders/{intent_id}")
    for _ in range(tries - 1):
        orders = status.get("orders") or []
        events = status.get("execution_events") or []
        if orders and all(str(o.get("status", "")).lower() in _TERMINAL for o in orders):
            break
        if any(str(e.get("event_type", "")).lower() in _TERMINAL_EVENTS for e in events):
            break
        time.sleep(4)
        status = _call("GET", f"/v1/operator/orders/{intent_id}")
    return status


def _fills_summary(events: list) -> list:
    out = []
    for e in events:
        payload = e.get("payload") or {}
        item = {"event_type": e.get("event_type")}
        for k in ("last_qty", "last_px", "reason"):
            if payload.get(k) is not None:
                item[k] = payload[k]
        out.append(item)
    return out


def _print_order_result(placed: dict, status: dict) -> None:
    orders = status.get("orders") or []
    events = status.get("execution_events") or []
    out = {
        "intent_id": placed["intent_id"],
        "intent_status": (status.get("intent") or {}).get("status", placed.get("status")),
        "replayed_existing_intent": placed.get("replay", False),
        "execution_orders": orders,
        "execution_events": _fills_summary(events),
        "open_positions_on_instrument": status.get("open_positions"),
    }
    if not orders and not events:
        out["note"] = ("no execution activity yet — the node polls every few seconds; "
                       "re-check with: v3_trade.py status " + placed["intent_id"])
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


def _require_open_provenance(args) -> str:
    channel = str(getattr(args, "channel", "") or "").strip()
    client_ref = str(getattr(args, "ref", "") or "").strip()
    if not channel:
        print(json.dumps({
            "error": "--channel is required for open; pass the Telegram channel id "
                     "or operator",
        }, ensure_ascii=False))
        sys.exit(1)
    if not client_ref:
        print(json.dumps({
            "error": "--ref is required for open; use tg-sig-c<channel>-m<message> "
                     "or operator-<stable-id>",
        }, ensure_ascii=False))
        sys.exit(1)

    if channel == "operator":
        if _OPERATOR_OPEN_REF_RE.fullmatch(client_ref):
            return channel
        print(json.dumps({
            "error": "operator open requires --ref operator or "
                     "operator-<stable-id>",
        }, ensure_ascii=False))
        sys.exit(1)

    normalized_channel = channel.lstrip("-")
    match = _TG_OPEN_REF_RE.fullmatch(client_ref)
    if not normalized_channel.isdigit() or match is None:
        print(json.dumps({
            "error": "signal open requires --channel <numeric-id> and canonical "
                     "--ref tg-sig-c<channel>-m<message>",
        }, ensure_ascii=False))
        sys.exit(1)
    if match.group("channel") != normalized_channel:
        print(json.dumps({
            "error": "--channel conflicts with the channel encoded in --ref",
            "channel": channel,
            "client_ref": client_ref,
        }, ensure_ascii=False))
        sys.exit(1)
    return channel


def _add_authorization_context(payload: dict, args) -> None:
    created_by_service = str(args.created_by_service).strip()
    payload["authorized_by_type"] = str(args.authorized_by_type).strip()
    payload["authorized_by_id"] = str(args.authorized_by_id).strip()
    payload["source_message_id"] = str(args.source_message_id).strip()
    payload["created_by_service"] = created_by_service
    payload["source"] = created_by_service
    parent_intent_id = str(args.parent_intent_id or "").strip()
    if parent_intent_id:
        payload["parent_intent_id"] = parent_intent_id


def cmd_open(args) -> None:
    source_channel = _require_open_provenance(args)
    entry = {"type": args.entry_type}
    if args.price is not None:
        entry["price"] = args.price
    if args.price_min is not None:
        entry["price_min"] = args.price_min
    if args.price_max is not None:
        entry["price_max"] = args.price_max
    payload = {
        "action": "open_position",
        "symbol": args.symbol.upper(),
        "side": args.side,
        "entry": entry,
        "account_id": args.account,
        "reason": args.reason,
        "source": "hermes-agent",
        "source_channel": source_channel,
    }
    _add_authorization_context(payload, args)
    if args.notional is not None:
        payload["notional_usdt"] = args.notional
    if args.sl is not None:
        payload["stop_loss"] = args.sl
    if args.tp:
        payload["take_profits"] = [float(x) for x in args.tp.split(",") if x.strip()]
    if args.leverage is not None:
        payload["leverage"] = args.leverage
    if args.expire_hours is not None:
        payload["expire_hours"] = args.expire_hours
    payload["client_ref"] = args.ref
    placed = _call("POST", "/v1/operator/orders", payload)
    _print_order_result(placed, _report(placed["intent_id"], wait=not args.no_wait))


def _require_management_ref(args, action: str) -> None:
    if args.ref:
        return
    symbol = re.sub(r"usdt$", "", str(args.symbol).lower())
    entry_ref = str(getattr(args, "entry_ref", "") or "").strip()
    suffix = entry_ref
    if not suffix:
        channel = str(getattr(args, "channel", "") or "").strip().lstrip("-")
        if channel:
            suffix = f"tg-sig-c{channel}-m<message-id>"
        else:
            suffix = "<stable-operation-id>"
    suggestion = f"{action}-{symbol}-{suffix}"
    print(json.dumps({
        "error": "--ref is required for management actions; pass a stable operation "
                 "ref and reuse it for every retry",
        "suggested_ref": suggestion,
        "example": f"--ref {suggestion}",
    }, ensure_ascii=False))
    sys.exit(1)


def _add_attribution_context(payload: dict, args) -> None:
    channel = str(getattr(args, "channel", "") or "").strip()
    entry_ref = str(getattr(args, "entry_ref", "") or "").strip()
    if channel:
        payload["channel"] = channel
    if entry_ref:
        payload["entry_ref"] = entry_ref


def cmd_close(args) -> None:
    _require_management_ref(args, "close")
    payload = {
        "action": "close_position",
        "symbol": args.symbol.upper(),
        "account_id": args.account,
        "reason": args.reason,
        "source": "hermes-agent",
    }
    if getattr(args, "side", None):
        payload["position_side"] = args.side
    payload["client_ref"] = args.ref
    _add_attribution_context(payload, args)
    _add_authorization_context(payload, args)
    placed = _call("POST", "/v1/operator/orders", payload)
    _print_order_result(placed, _report(placed["intent_id"], wait=not args.no_wait))


def cmd_partial(args) -> None:
    _require_management_ref(args, "partial")
    payload = {
        "action": "partial_close",
        "symbol": args.symbol.upper(),
        "quantity": args.quantity,
        "account_id": args.account,
        "reason": args.reason,
        "source": "hermes-agent",
    }
    if getattr(args, "side", None):
        payload["position_side"] = args.side
    payload["client_ref"] = args.ref
    _add_attribution_context(payload, args)
    _add_authorization_context(payload, args)
    placed = _call("POST", "/v1/operator/orders", payload)
    _print_order_result(placed, _report(placed["intent_id"], wait=not args.no_wait))


def _position_for(symbol: str, account_id: str, side: str | None = None) -> dict | None:
    snap = _call("GET", "/api/system/snapshot")
    for p in (snap.get("data") or {}).get("positions") or []:
        if (str(p.get("status")) == "open"
                and str(p.get("account_id")) == account_id
                and str(p.get("instrument_id", "")).upper().startswith(symbol.upper())
                and (side is None or str(p.get("side", "")).lower() == side)
                and float(p.get("quantity") or 0) != 0):
            return p
    return None


def _split_quantities(total, parts: int) -> list[float]:
    """Even split that never exceeds the position: floor每档到仓位数量的精度,余数进第一档。"""
    from decimal import Decimal, ROUND_DOWN
    t = Decimal(str(total))
    step = Decimal(1).scaleb(t.as_tuple().exponent) if t.as_tuple().exponent < 0 else Decimal(1)
    base = (t / parts).quantize(step, rounding=ROUND_DOWN)
    if base <= 0:
        print(json.dumps({"error": f"position {total} too small to split into {parts} tiers"}))
        sys.exit(1)
    first = t - base * (parts - 1)
    return [float(first)] + [float(base)] * (parts - 1)


def _report_protect(intent_id: str, wait: bool) -> dict:
    """Protection orders end in 'accepted' (they rest on the venue), so wait for
    accept/deny events rather than fills."""
    done = {"orderaccepted", "orderdenied", "orderrejected", "ordercanceled"}
    status = _call("GET", f"/v1/operator/orders/{intent_id}")
    for _ in range(7 if wait else 0):
        events = status.get("execution_events") or []
        if any(str(e.get("event_type", "")).lower() in done for e in events):
            break
        time.sleep(4)
        status = _call("GET", f"/v1/operator/orders/{intent_id}")
    return status


def cmd_set_sl(args) -> None:
    _require_management_ref(args, "set-sl")
    payload = {
        "action": "move_stop_loss",
        "symbol": args.symbol.upper(),
        "stop_loss": args.sl,
        "account_id": args.account,
        "reason": args.reason,
        "source": "hermes-agent",
    }
    if getattr(args, "side", None):
        payload["position_side"] = args.side
    payload["client_ref"] = args.ref
    _add_attribution_context(payload, args)
    _add_authorization_context(payload, args)
    placed = _call("POST", "/v1/operator/orders", payload)
    _print_order_result(placed, _report_protect(placed["intent_id"], wait=not args.no_wait))


def cmd_set_tps(args) -> None:
    _require_management_ref(args, "set-tps")
    prices = [float(x) for x in args.tp.split(",") if x.strip()]
    if not prices:
        print(json.dumps({"error": "--tp requires at least one price"}))
        sys.exit(1)
    if args.qty:
        quantities = [float(x) for x in args.qty.split(",") if x.strip()]
        if len(quantities) != len(prices):
            print(json.dumps({"error": "--qty count must match --tp count"}))
            sys.exit(1)
    else:
        pos = _position_for(args.symbol, args.account, getattr(args, "side", None))
        if pos is None:
            print(json.dumps({"error": f"no open position on {args.symbol} ({args.account})"}))
            sys.exit(1)
        quantities = _split_quantities(pos.get("quantity"), len(prices))
    payload = {
        "action": "replace_take_profits",
        "symbol": args.symbol.upper(),
        "take_profits": [
            {"price": p, "quantity": q} for p, q in zip(prices, quantities)
        ],
        "account_id": args.account,
        "reason": args.reason,
        "source": "hermes-agent",
    }
    if getattr(args, "side", None):
        payload["position_side"] = args.side
    payload["client_ref"] = args.ref
    _add_attribution_context(payload, args)
    _add_authorization_context(payload, args)
    placed = _call("POST", "/v1/operator/orders", payload)
    _print_order_result(placed, _report_protect(placed["intent_id"], wait=not args.no_wait))


def cmd_disable_tps(args) -> None:
    _require_management_ref(args, "disable-tps")
    payload = {
        "action": "replace_take_profits",
        "symbol": args.symbol.upper(),
        "take_profits": [],
        "disable_take_profits": True,
        "position_side": args.side,
        "account_id": args.account,
        "reason": args.reason,
        "source": "hermes-agent",
    }
    payload["client_ref"] = args.ref
    _add_attribution_context(payload, args)
    _add_authorization_context(payload, args)
    placed = _call("POST", "/v1/operator/orders", payload)
    _print_order_result(
        placed,
        _report_protect(placed["intent_id"], wait=not args.no_wait),
    )


def cmd_cancel(args) -> None:
    _require_management_ref(args, "cancel")
    order_id = str(args.order or "").strip()
    if not (len(order_id) == 35 and order_id.startswith("B")):
        print(json.dumps({"error": "需要完整的 35 位系统订单号(B 开头);"
                                   "外部/手动订单不能经此撤销"}, ensure_ascii=False))
        sys.exit(1)
    payload = {
        "action": "cancel_order",
        "symbol": args.symbol.upper(),
        "client_order_id": order_id,
        "account_id": args.account,
        "reason": args.reason,
        "source": "hermes-agent",
    }
    payload["client_ref"] = args.ref
    _add_attribution_context(payload, args)
    _add_authorization_context(payload, args)
    placed = _call("POST", "/v1/operator/orders", payload)
    _print_order_result(placed, _report_protect(placed["intent_id"], wait=not args.no_wait))


def cmd_status(args) -> None:
    print(json.dumps(_call("GET", f"/v1/operator/orders/{args.intent_id}"),
                     ensure_ascii=False, indent=2, default=str))


def cmd_positions(args) -> None:
    snap = _call("GET", "/api/system/snapshot")
    data = snap.get("data") or {}
    open_pos = [
        {k: p.get(k) for k in ("account_id", "instrument_id", "side", "quantity", "avg_entry_price")}
        for p in (data.get("positions") or [])
        if str(p.get("status")) == "open" and float(p.get("quantity") or 0) != 0
    ]
    exchange_state = [
        {
            "account_id": r.get("account_id"),
            "stale": r.get("stale"),
            "updated_at": r.get("updated_at"),
            "protections": (r.get("payload") or {}).get("protections"),
            "open_order_count": len((r.get("payload") or {}).get("open_orders") or []),
            "algo_order_count": len((r.get("payload") or {}).get("algo_orders") or []),
        }
        for r in (data.get("exchange_state") or [])
    ]
    print(json.dumps({
        "generated_at": snap.get("generated_at"),
        "stale": snap.get("stale"),
        "open_positions": open_pos,
        "balances": data.get("balances"),
        # Direct exchange truth (incl. algo/conditional SL/TP invisible to the
        # projections). If stale=true here, trust the venue app over this data.
        "exchange_protections": exchange_state,
    }, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(prog="v3_trade.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, needs_reason=True, management=False):
        p.add_argument(
            "--account",
            required=True,
            choices=["account-a", "account-b"],
        )
        if needs_reason:
            p.add_argument("--reason", required=True, help="audit reason (why this order)")
        p.add_argument(
            "--authorized-by-type",
            required=True,
            choices=["user", "channel"],
            help="explicit authority behind this order",
        )
        p.add_argument(
            "--authorized-by-id",
            required=True,
            help="user/operator identity or channel identifier",
        )
        p.add_argument(
            "--source-message-id",
            required=True,
            help="stable id of the authorizing request or channel message",
        )
        p.add_argument(
            "--created-by-service",
            default="hermes-agent",
            help="service creating this request",
        )
        p.add_argument(
            "--parent-intent-id",
            default=None,
            help="authorized parent intent for internal derived management",
        )
        ref_help = "idempotency key, e.g. the signal message id"
        if management:
            ref_help = "stable operation ref (required; reuse for retries)"
        p.add_argument("--ref", default=None, help=ref_help)
        if management:
            p.add_argument("--channel", default=None, help="requesting channel id or operator")
            p.add_argument("--entry-ref", default=None, help="entry signal ref for attribution")
        p.add_argument("--no-wait", action="store_true", help="do not wait for execution result")

    p = sub.add_parser("open", help="open a position (long/short)")
    p.add_argument("symbol")
    p.add_argument("side", choices=["long", "short"])
    p.add_argument("--notional", type=float, default=None,
                   help="explicit notional in USDT; omit to auto-size from the risk "
                        "config (requires --sl). Caps enforced server-side.")
    p.add_argument("--entry-type", default="market", choices=["market", "limit", "zone"])
    p.add_argument("--price", type=float, default=None)
    p.add_argument("--price-min", type=float, default=None)
    p.add_argument("--price-max", type=float, default=None)
    p.add_argument("--sl", type=float, default=None, help="stop loss price")
    p.add_argument("--tp", default=None, help="take profit price(s), comma separated")
    p.add_argument("--leverage", type=float, default=None)
    p.add_argument("--expire-hours", type=float, default=48,
                   help="limit/zone 挂单的交易所侧自动过期(GTD)小时数;0 表示不过期(GTC)")
    p.add_argument("--channel", default=None,
                   help="source Telegram channel id or operator")
    common(p)
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser("close", help="close the whole position on a symbol")
    p.add_argument("symbol")
    p.add_argument("--side", choices=["long", "short"], default=None,
                   help="hedge mode: which book to manage when the symbol holds both long and short")
    common(p, management=True)
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("partial", help="partially close a position")
    p.add_argument("symbol")
    p.add_argument("--side", choices=["long", "short"], default=None,
                   help="hedge mode: which book to manage when the symbol holds both long and short")
    p.add_argument("--quantity", type=float, required=True, help="base quantity to close")
    common(p, management=True)
    p.set_defaults(fn=cmd_partial)

    p = sub.add_parser("set-sl", help="move/replace the stop loss on an open position")
    p.add_argument("symbol")
    p.add_argument("--side", choices=["long", "short"], default=None,
                   help="hedge mode: which book to manage when the symbol holds both long and short")
    p.add_argument("--sl", type=float, required=True, help="new stop loss price")
    common(p, management=True)
    p.set_defaults(fn=cmd_set_sl)

    p = sub.add_parser("set-tps", help="replace ALL take profits on an open position")
    p.add_argument("symbol")
    p.add_argument("--side", choices=["long", "short"], default=None,
                   help="hedge mode: which book to manage when the symbol holds both long and short")
    p.add_argument("--tp", required=True, help="take profit price(s), comma separated")
    p.add_argument("--qty", default=None,
                   help="optional per-tier quantities, comma separated; omit to split "
                        "the current position evenly")
    common(p, management=True)
    p.set_defaults(fn=cmd_set_tps)

    p = sub.add_parser(
        "disable-tps",
        help="disable automatic take profits for one position book",
    )
    p.add_argument("symbol")
    p.add_argument(
        "--side",
        choices=["long", "short"],
        required=True,
        help="hedge-mode position book receiving the TP tombstone",
    )
    common(p, management=True)
    p.set_defaults(fn=cmd_disable_tps)

    p = sub.add_parser("cancel", help="cancel ONE resting system order by client_order_id")
    p.add_argument("symbol")
    p.add_argument("--order", required=True, help="full 35-char system client_order_id (B + hex + 2 digits)")
    common(p, management=True)
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("status", help="execution status of an intent")
    p.add_argument("intent_id")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("positions", help="current open positions + balances")
    p.set_defaults(fn=cmd_positions)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
