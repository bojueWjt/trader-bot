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
import urllib.parse

BASE = os.environ.get("V3_CONTROL_PLANE_URL", "http://127.0.0.1:8080")
_DEFAULT_OPERATOR_ACCOUNTS = (
    "account-a",
    "account-b",
    "account-c",
    "account-d",
)
_ACCOUNT_ID_RE = re.compile(r"^account-[a-z0-9][a-z0-9-]{0,31}$")
_CHANNEL_ID_RE = re.compile(r"^-?\d+$")
_ENABLED_ACCOUNT_STATUSES = frozenset({"1", "active", "enabled", "true"})


def _configured_operator_accounts() -> tuple[str, ...]:
    raw_accounts = os.environ.get("V3_OPERATOR_ACCOUNTS", "").strip()
    if raw_accounts:
        accounts = tuple(
            account.strip()
            for account in raw_accounts.split(",")
            if account.strip()
        )
    else:
        raw_registry = os.environ.get(
            "OPERATOR_ACCOUNT_REGISTRY_JSON",
            "",
        ).strip()
        if not raw_registry:
            return _DEFAULT_OPERATOR_ACCOUNTS
        try:
            registry = json.loads(raw_registry)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError(
                "OPERATOR_ACCOUNT_REGISTRY_JSON is invalid"
            ) from exc
        if not isinstance(registry, dict) or not registry:
            raise argparse.ArgumentTypeError(
                "OPERATOR_ACCOUNT_REGISTRY_JSON must be a non-empty object"
            )
        accounts = tuple(str(account or "").strip() for account in registry)

    if not accounts or len(set(accounts)) != len(accounts):
        raise argparse.ArgumentTypeError(
            "configured operator accounts must be non-empty and unique"
        )
    if any(_ACCOUNT_ID_RE.fullmatch(account) is None for account in accounts):
        raise argparse.ArgumentTypeError(
            "configured operator account id is invalid"
        )
    return accounts


def _operator_account_arg(value: str) -> str:
    account_id = str(value or "").strip()
    accounts = _configured_operator_accounts()
    if account_id not in accounts:
        allowed = ", ".join(accounts)
        raise argparse.ArgumentTypeError(
            f"account must be one of: {allowed}"
        )
    return account_id


def _channel_route_error(message: str) -> None:
    print(json.dumps({"error": message}, ensure_ascii=False))
    sys.exit(1)


def _channel_execution_account(channel_id: str) -> str:
    query = urllib.parse.urlencode({"channel": channel_id})
    route = _call("GET", "/v1/query/channel-route?" + query)
    account_id = route.get("execution_account_id")
    if not isinstance(account_id, str) or account_id not in _configured_operator_accounts():
        _channel_route_error("control-plane channel route has no registered execution account")
    return account_id


def _token() -> str:
    token = os.environ.get("RISK_ADMIN_TOKEN", "").strip()
    if not token:
        print(json.dumps({"error": "RISK_ADMIN_TOKEN unavailable; inject the credential through the process environment"}))
        raise SystemExit(2)
    return token


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    headers = {
        "Authorization": "Bearer " + _token(),
        "Content-Type": "application/json",
    }
    if payload is not None:
        request_id = str(payload.get("source_message_id") or "").strip()
        if request_id:
            headers["X-Request-Id"] = request_id
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers=headers,
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
_TG_SIGNAL_REF_RE = re.compile(
    r"(?:^|-)tg-sig-c(?P<channel>\d+)-m(?P<message>\d+)"
    r"(?:-e\d+)?(?:$|-)"
)
_OPERATOR_OPEN_REF_RE = re.compile(
    r"operator(?:-[a-z0-9][a-z0-9._-]*)?"
)


def _channel_from_signal_ref(value: str | None) -> str | bool:
    ref = str(value or "").strip()
    if not ref:
        return False
    match = _TG_SIGNAL_REF_RE.search(ref)
    if match is None:
        return False
    return "-" + match.group("channel")


def _require_channel_account_route(args, action: str) -> None:
    channel = str(getattr(args, "channel", "") or "").strip()
    authorized_by_type = str(args.authorized_by_type or "").strip().lower()
    authorized_by_id = str(args.authorized_by_id or "").strip()
    source_message_id = str(args.source_message_id or "").strip()

    if channel == "operator":
        if authorized_by_type == "channel":
            _channel_route_error(
                f"{action} operator request cannot use channel authorization"
            )
        return
    if not channel:
        if authorized_by_type == "channel":
            _channel_route_error(
                f"{action} channel authorization requires --channel"
            )
        return
    if authorized_by_type != "channel":
        _channel_route_error(
            f"{action} Telegram channel request requires "
            "--authorized-by-type channel"
        )
    if authorized_by_id != channel:
        _channel_route_error(
            f"{action} --authorized-by-id must match --channel"
        )

    source_channel = _channel_from_signal_ref(source_message_id)
    if source_channel != channel:
        _channel_route_error(
            f"{action} --source-message-id must encode the same channel"
        )

    if action not in ("open", "add"):
        entry_ref = str(getattr(args, "entry_ref", "") or "").strip()
        entry_channel = _channel_from_signal_ref(entry_ref)
        if entry_channel != channel:
            _channel_route_error(
                f"{action} --entry-ref must encode the same channel"
            )

    if action in ("open", "add"):
        route_account = _channel_execution_account(channel)
        if args.account != route_account:
            _channel_route_error(
                f"{action} account conflicts with channel route: "
                f"{channel} requires {route_account}"
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


def _require_open_provenance(args, action: str = "open") -> str:
    channel = str(getattr(args, "channel", "") or "").strip()
    client_ref = str(getattr(args, "ref", "") or "").strip()
    if not channel:
        print(json.dumps({
            "error": f"--channel is required for {action}; pass the Telegram "
                     "channel id or operator",
        }, ensure_ascii=False))
        sys.exit(1)
    if not client_ref:
        print(json.dumps({
            "error": f"--ref is required for {action}; use "
                     "tg-sig-c<channel>-m<message> or operator-<stable-id>",
        }, ensure_ascii=False))
        sys.exit(1)

    if channel == "operator":
        if _OPERATOR_OPEN_REF_RE.fullmatch(client_ref):
            _require_channel_account_route(args, action)
            return channel
        print(json.dumps({
            "error": f"operator {action} requires --ref operator or "
                     "operator-<stable-id>",
        }, ensure_ascii=False))
        sys.exit(1)

    normalized_channel = channel.lstrip("-")
    match = _TG_OPEN_REF_RE.fullmatch(client_ref)
    if not normalized_channel.isdigit() or match is None:
        print(json.dumps({
            "error": f"signal {action} requires --channel <numeric-id> and "
                     "canonical --ref tg-sig-c<channel>-m<message>",
        }, ensure_ascii=False))
        sys.exit(1)
    if match.group("channel") != normalized_channel:
        print(json.dumps({
            "error": "--channel conflicts with the channel encoded in --ref",
            "channel": channel,
            "client_ref": client_ref,
        }, ensure_ascii=False))
        sys.exit(1)
    _require_channel_account_route(args, action)
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


_ENTRY_OFFSET_PCT = "0.001"


def _apply_entry_offset(entry: dict, side: str) -> str:
    """Fuzzy-wording entry concession (user convention): shift entry prices
    0.1% toward easier fill — long up, short down. Exact Decimal math; tick
    rounding stays downstream in the planner. Returns the audit suffix."""
    from decimal import Decimal

    pct = Decimal(_ENTRY_OFFSET_PCT)
    factor = (Decimal("1") + pct) if side == "long" else (Decimal("1") - pct)
    raw_parts = []
    for key, raw_key in (
        ("price", "price_raw"),
        ("price_min", "price_min_raw"),
        ("price_max", "price_max_raw"),
        ('second_price', 'second_price_raw'),
    ):
        value = entry.get(key)
        if value is None:
            continue
        raw = Decimal(str(value))
        entry[raw_key] = float(raw)
        entry[key] = float(raw * factor)
        raw_parts.append(f"{key}={value:g}")
    if not raw_parts:
        print(json.dumps({
            "error": "--entry-offset requires a limit price or zone bounds "
                     "(market entry has no price to shift)",
        }, ensure_ascii=False))
        sys.exit(1)
    entry["offset_pct"] = float(pct)
    return f"；按用户约定：入场模糊点位让利0.1%（原值 {', '.join(raw_parts)}）"


def resolve_entry_action(*, intended_action: str, canary: bool, second_price) -> str:
    """Preserve explicit intent; the control plane validates current positions."""
    if canary and intended_action != "open_position":
        raise SystemExit("canary_open_position_only")
    if second_price is not None and intended_action != "open_position":
        raise SystemExit("second_price requires market/limit open_position")
    return intended_action


def _cmd_entry(args, intended_action: str) -> None:
    label = "add" if intended_action == "add_position" else "open"
    source_channel = _require_open_provenance(args, label)
    entry = {"type": args.entry_type}
    if args.price is not None:
        entry["price"] = args.price
    second_price = getattr(args, "second_price", None)
    if second_price is not None:
        if args.entry_type not in ("market", "limit") or args.sl is None:
            raise SystemExit("--second-price requires market/limit entry and --sl")
        entry["second_price"] = second_price
    if args.price_min is not None:
        entry["price_min"] = args.price_min
    if args.price_max is not None:
        entry["price_max"] = args.price_max
    if args.time_in_force is not None:
        entry["time_in_force"] = args.time_in_force
    reason = args.reason
    if getattr(args, "entry_offset", False):
        reason = reason + _apply_entry_offset(entry, args.side)
    actual_action = resolve_entry_action(
        intended_action=intended_action,
        canary=getattr(args, "canary_permit_id", None) is not None,
        second_price=second_price,
    )
    replay_of = str(getattr(args, "replay_of", None) or "").strip()
    if replay_of and actual_action != "add_position":
        print(json.dumps({
            "error": "replay_of is only valid when submitting add_position",
        }, ensure_ascii=False))
        sys.exit(1)
    payload = {
        "action": actual_action,
        "intended_action": intended_action,
        "signal_intent": "加仓" if intended_action == "add_position" else "开仓",
        "symbol": args.symbol.upper(),
        "side": args.side,
        "entry": entry,
        "account_id": args.account,
        "reason": reason,
        "source": "hermes-agent",
        "source_channel": source_channel,
    }
    if replay_of:
        payload["replay_of"] = replay_of
    _add_authorization_context(payload, args)
    if args.notional is not None:
        payload["notional_usdt"] = args.notional
    if args.quantity is not None:
        payload["quantity"] = args.quantity
    if args.canary_permit_id is not None:
        payload["canary_permit_id"] = args.canary_permit_id
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


def cmd_open(args) -> None:
    _cmd_entry(args, "open_position")


def cmd_add(args) -> None:
    _cmd_entry(args, "add_position")


def _require_management_ref(args, action: str) -> None:
    if not args.ref:
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
    _require_channel_account_route(args, action)


def _add_attribution_context(payload: dict, args) -> None:
    channel = str(getattr(args, "channel", "") or "").strip()
    entry_ref = str(getattr(args, "entry_ref", "") or "").strip()
    if channel:
        payload["channel"] = channel
    if entry_ref:
        payload["entry_ref"] = entry_ref


def _illegal_close_ratio_error(detail: str) -> None:
    print(json.dumps({
        "error": "illegal_close_ratio",
        "detail": detail,
        "hint": "use `partial --percent 20` (or --quantity) for a ratio; "
                "`close` always flattens 100% and must not be used as a fallback",
    }, ensure_ascii=False))
    sys.exit(1)


def _finite_decimal(raw, label: str):
    from decimal import Decimal, InvalidOperation

    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"illegal {label}") from exc
    if not value.is_finite():
        raise ValueError(f"illegal {label}")
    return value


def resolve_partial_close_quantity(
    *,
    quantity: float | None,
    percent: float | None,
    position_quantity: float | None,
) -> float:
    """Size a partial close. Missing/illegal ratios fail closed — never 100%."""
    from decimal import Decimal

    has_quantity = quantity is not None
    has_percent = percent is not None
    if has_quantity == has_percent:
        raise ValueError("partial requires exactly one of --quantity or --percent")
    if has_percent:
        ratio = _finite_decimal(percent, "percent")
        if ratio <= 0 or ratio > 100:
            raise ValueError("percent must be >0 and <=100")
        if position_quantity is None:
            raise ValueError("no open position to apply percent against")
        pos = _finite_decimal(position_quantity, "position_quantity")
        if pos <= 0:
            raise ValueError("no open position to apply percent against")
        sized = pos * ratio / Decimal("100")
        if not sized.is_finite() or sized <= 0:
            raise ValueError("percent rounds to zero quantity")
        return float(sized)
    sized = _finite_decimal(quantity, "quantity")
    if sized <= 0:
        raise ValueError("quantity must be positive")
    return float(sized)


def cmd_close(args) -> None:
    _require_management_ref(args, "close")
    if getattr(args, "percent", None) is not None or getattr(args, "quantity", None) is not None:
        _illegal_close_ratio_error(
            "close ignores ratios and would silently flatten 100%"
        )
    payload = {
        "action": "close_position",
        "symbol": args.symbol.upper(),
        "account_id": args.account,
        "reason": args.reason,
        "source": "hermes-agent",
    }
    payload["position_side"] = args.side
    payload["client_ref"] = args.ref
    _add_attribution_context(payload, args)
    _add_authorization_context(payload, args)
    placed = _call("POST", "/v1/operator/orders", payload)
    _print_order_result(placed, _report(placed["intent_id"], wait=not args.no_wait))


def cmd_partial(args) -> None:
    _require_management_ref(args, "partial")
    percent = getattr(args, "percent", None)
    quantity = args.quantity
    position_quantity = None
    if percent is not None:
        position = _position_for(args.symbol.upper(), args.account, args.side)
        if position is not None:
            try:
                position_quantity = float(position.get("quantity") or 0)
            except (TypeError, ValueError):
                position_quantity = None
            if not position_quantity:
                position_quantity = None
    try:
        quantity = resolve_partial_close_quantity(
            quantity=quantity,
            percent=percent,
            position_quantity=position_quantity,
        )
    except ValueError as exc:
        _illegal_close_ratio_error(str(exc))
    payload = {
        "action": "partial_close",
        "symbol": args.symbol.upper(),
        "quantity": quantity,
        "account_id": args.account,
        "reason": args.reason,
        "source": "hermes-agent",
    }
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
            type=_operator_account_arg,
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

    def entry_args(parser, *, include_second_price: bool) -> None:
        parser.add_argument("symbol")
        parser.add_argument("side", choices=["long", "short"])
        parser.add_argument("--notional", type=float, default=None,
                           help="explicit notional in USDT; omit to auto-size from the risk "
                                "config (requires --sl). Caps enforced server-side.")
        parser.add_argument("--entry-type", default="market", choices=["market", "limit", "zone"])
        parser.add_argument("--price", type=float, default=None)
        if include_second_price:
            parser.add_argument('--second-price', type=float, default=None,
                               help='second explicit limit entry; submit both legs once with equal notionals and one total risk budget')
        parser.add_argument("--price-min", type=float, default=None)
        parser.add_argument("--price-max", type=float, default=None)
        parser.add_argument(
            "--time-in-force",
            "--time_in_force",
            dest="time_in_force",
            choices=["GTC", "IOC"],
            default=None,
            help="explicit entry time in force; live canary orders require IOC",
        )
        parser.add_argument(
            "--quantity",
            type=float,
            default=None,
            help="explicit base quantity for a reviewed live canary; omit for "
                 "normal server-side risk sizing",
        )
        parser.add_argument(
            "--canary-permit-id",
            "--canary_permit_id",
            dest="canary_permit_id",
            default=None,
            help="armed reviewed-release permit id for a live canary order",
        )
        parser.add_argument("--entry-offset", action="store_true",
                           help="signal wording is fuzzy (附近/左右/约): shift entry "
                                "prices 0.1%% toward fill (long up / short down). "
                                "Precise signal prices must NOT use this flag.")
        parser.add_argument("--sl", type=float, default=None, help="stop loss price")
        parser.add_argument("--tp", default=None, help="take profit price(s), comma separated")
        parser.add_argument("--leverage", type=float, default=None)
        parser.add_argument("--expire-hours", type=float, default=48,
                           help="limit/zone 挂单的交易所侧自动过期(GTD)小时数;0 表示不过期(GTC)")
        parser.add_argument("--channel", default=None,
                           help="source Telegram channel id or operator")
        parser.add_argument(
            "--replay-of",
            dest="replay_of",
            default=None,
            help="explicit resubmit of a rejected same-source intent with zero "
                 "execution commands/events; does not bypass source identity",
        )
        common(parser)

    p = sub.add_parser("open", help="open a position (long/short)")
    entry_args(p, include_second_price=True)
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser("add", help="add to a same-side position (incremental notional)")
    entry_args(p, include_second_price=False)
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("close", help="close the whole position on a symbol")
    p.add_argument("symbol")
    p.add_argument("--side", choices=["long", "short"], required=True,
                   help="position book to manage")
    p.add_argument("--percent", type=float, default=None,
                   help="rejected: close is always 100%%; use partial --percent")
    p.add_argument("--quantity", type=float, default=None,
                   help="rejected: close is always 100%%; use partial --quantity")
    common(p, management=True)
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("partial", help="partially close a position")
    p.add_argument("symbol")
    p.add_argument("--side", choices=["long", "short"], required=True,
                   help="position book to manage")
    p.add_argument("--quantity", type=float, default=None,
                   help="base quantity to close; mutually exclusive with --percent")
    p.add_argument("--percent", type=float, default=None,
                   help="percent of current position to close (e.g. 20); "
                        "missing/illegal percent is rejected, never treated as 100")
    common(p, management=True)
    p.set_defaults(fn=cmd_partial)

    p = sub.add_parser("set-sl", help="move/replace the stop loss on an open position")
    p.add_argument("symbol")
    p.add_argument("--side", choices=["long", "short"], required=True,
                   help="position book to manage")
    p.add_argument("--sl", type=float, required=True, help="new stop loss price")
    common(p, management=True)
    p.set_defaults(fn=cmd_set_sl)

    p = sub.add_parser("set-tps", help="replace ALL take profits on an open position")
    p.add_argument("symbol")
    p.add_argument("--side", choices=["long", "short"], required=True,
                   help="position book to manage")
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
