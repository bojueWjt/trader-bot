#!/usr/bin/env python3
"""Read-only Telegram notifications for robot-owned trader-v3 events.

The service polls PostgreSQL projections and audit rows, formats Chinese
notifications, and sends them through the Hermes trader profile bot. It never
writes to the trading database or calls an exchange/control-plane endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable


PSQL = [
    "docker",
    "exec",
    "trader-v3-postgres",
    "psql",
    "-U",
    "postgres",
    "-d",
    "trader",
    "-Atc",
]
STATE_PATH = os.environ.get(
    "TRADE_EVENT_NOTIFY_STATE_PATH",
    "/srv/trader-v3/scripts/.trade_event_notifier_state.json",
)
HERMES_ENV_FILE = os.environ.get(
    "HERMES_TRADER_ENV_FILE",
    "/srv/hermes/profiles/trader/.env",
)
POLL_SECONDS = float(os.environ.get("TRADE_EVENT_NOTIFY_POLL_SECONDS", "2"))
BATCH_SETTLE_SECONDS = float(
    os.environ.get("TRADE_EVENT_NOTIFY_BATCH_SETTLE_SECONDS", "4")
)
PER_CLASS_PER_MINUTE = int(
    os.environ.get("TRADE_EVENT_NOTIFY_PER_MINUTE", "20")
)
MAX_DELIVERY_ATTEMPTS = int(
    os.environ.get("TRADE_EVENT_NOTIFY_MAX_ATTEMPTS", "6")
)
RETRY_BASE_SECONDS = float(
    os.environ.get("TRADE_EVENT_NOTIFY_RETRY_BASE_SECONDS", "5")
)
MAX_PENDING_MESSAGES = int(
    os.environ.get("TRADE_EVENT_NOTIFY_MAX_PENDING", "500")
)
DEDUPE_RETENTION_SECONDS = 7 * 24 * 3600
MAX_SEEN_IDS = 4000
MAX_DEAD_LETTERS = 100

ROBOT_ORDER_RE = re.compile(r"^B[0-9a-f]{32}[0-9]{2}$")
PROTECTION_ACTIONS = {
    "move_stop_loss",
    "replace_take_profits",
    "set_stop_loss",
    "set_take_profit",
}
ENTRY_ACTIONS = {"open_position", "add_position"}
EXECUTION_EVENT_TYPES = {
    "OrderAccepted",
    "OrderFilled",
    "OrderPartiallyFilled",
}

Sender = Callable[[str], tuple[bool, dict[str, Any]]]


def log(event: str, **fields: Any) -> None:
    payload = {"event": event, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def run_sql_json(sql: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        PSQL + [sql],
        capture_output=True,
        text=True,
        timeout=25,
    )
    if result.returncode != 0:
        error = result.stderr.strip()[:300]
        raise RuntimeError(f"psql failed: {error}")
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        value = json.loads(text)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def database_epoch() -> float:
    rows = run_sql_json(
        "SELECT json_build_object('epoch', EXTRACT(EPOCH FROM clock_timestamp()))::text"
    )
    if not rows:
        raise RuntimeError("database clock query returned no rows")
    return float(rows[0]["epoch"])


def load_state(path: str = STATE_PATH) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return ensure_state(value)


def ensure_state(state: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "version": 1,
        "initialized": False,
        "audit_cursor": 0.0,
        "execution_cursor": 0.0,
        "seen_audit_ids": [],
        "seen_execution_ids": [],
        "semantic_dedupe": {},
        "order_batches": {},
        "pending": [],
        "rate_limits": {},
        "dead_letters": [],
    }
    for key, value in defaults.items():
        if key not in state:
            state[key] = value
    return state


def save_state(state: dict[str, Any], path: str = STATE_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp, target)


def _hermes_env_value(name: str) -> str:
    try:
        with open(HERMES_ENV_FILE, encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line.startswith(name + "="):
                    continue
                value = line.split("=", 1)[1].strip()
                return value.strip('"').strip("'")
    except OSError:
        return ""
    return ""


def telegram_send(text: str) -> tuple[bool, dict[str, Any]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        token = _hermes_env_value("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_HOME_CHANNEL", "")
    if not chat_id:
        chat_id = _hermes_env_value("TELEGRAM_HOME_CHANNEL")
    if not token or not chat_id:
        return False, {"error": "telegram_credentials_missing"}

    base_url = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
    body = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return False, {"error": "telegram_http_error", "status": exc.code}
    except urllib.error.URLError as exc:
        reason = type(exc.reason).__name__
        return False, {"error": "telegram_url_error", "reason": reason}
    except Exception as exc:  # noqa: BLE001 - notification errors stay isolated
        return False, {"error": "telegram_send_error", "type": type(exc).__name__}

    result = response_body.get("result")
    if not response_body.get("ok") or not isinstance(result, dict):
        return False, {"error": "telegram_api_rejected"}
    return True, {
        "message_id": result.get("message_id"),
        "text": result.get("text"),
    }


def is_robot_order_id(client_order_id: Any) -> bool:
    return bool(ROBOT_ORDER_RE.fullmatch(str(client_order_id or "")))


def order_sequence(client_order_id: Any) -> int:
    text = str(client_order_id or "")
    if not is_robot_order_id(text):
        return 0
    return int(text[-2:])


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _decimal(value: Any) -> Decimal | bool:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not number.is_finite():
        return False
    return number


def _format_decimal(value: Any, places: int = 2) -> str:
    number = _decimal(value)
    if number is False:
        return "—"
    quantizer = Decimal("1")
    if places > 0:
        quantizer = Decimal("1").scaleb(-places)
    formatted = format(number.quantize(quantizer, rounding=ROUND_HALF_UP), "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def clean_symbol(value: Any) -> str:
    symbol = str(value or "?")
    symbol = re.sub(r"-PERP\.BINANCE$", "", symbol)
    return symbol


def direction_cn(row: dict[str, Any]) -> str:
    plan = _mapping(row.get("order_plan"))
    raw_side = plan.get("side")
    if not raw_side:
        raw_side = plan.get("position_side")
    side = str(raw_side or "").lower()
    if side in {"long", "buy"}:
        return "做多"
    if side in {"short", "sell"}:
        return "做空"
    return "方向待确认"


def action_cn(action: Any) -> str:
    labels = {
        "open_position": "开仓",
        "add_position": "加仓",
        "reduce_position": "减仓",
        "close_position": "平仓",
        "move_stop_loss": "移动止损",
        "replace_take_profits": "更新止盈",
        "cancel_order": "撤单",
    }
    text = str(action or "")
    return labels.get(text, text or "交易")


def approved_notional(row: dict[str, Any]) -> Decimal | bool:
    budget = _mapping(row.get("risk_budget"))
    return _decimal(budget.get("max_notional"))


def rejection_reason(detail: Any) -> str:
    raw = str(detail or "unknown").strip()
    raw = re.sub(r"\s+", " ", raw)
    translations = {
        "halted": "账户处于 HALTED",
        "live_entry_instrument_not_allowed": "品种未通过实时开仓风控",
        "intent_exchange_confirmation_required": "重放 intent 需要交易所状态确认",
        "position_required": "缺少对应持仓",
        "insufficient_margin": "可用保证金不足",
        "expired": "intent 已过有效期",
    }
    for key, label in translations.items():
        if key in raw:
            return f"{label}（{raw}）"
    return raw


def format_intent_message(row: dict[str, Any]) -> str:
    accepted = row.get("event_type") == "intent_ack.accepted"
    title = "✅ Intent 已批准"
    if not accepted:
        title = "⛔ Intent 已拒绝"
    account_id = str(row.get("account_id") or "?")
    symbol = clean_symbol(row.get("instrument_id"))
    action = action_cn(row.get("action"))
    intent_id = str(row.get("intent_id") or "?")
    short_intent = intent_id[-8:]
    lines = [
        title,
        f"账户 {account_id}｜{symbol}｜{direction_cn(row)}｜{action}",
    ]
    if accepted:
        notional = approved_notional(row)
        notional_text = "—"
        if notional is not False and notional > 0:
            notional_text = f"{_format_decimal(notional)} USDT"
        lines.append(f"批准名义 {notional_text}｜intent {short_intent}")
    else:
        payload = _mapping(row.get("payload"))
        detail = payload.get("detail")
        lines.append(f"拒因 {rejection_reason(detail)}")
        lines.append(f"intent {short_intent}")
    return "\n".join(lines)


def rejection_dedupe_key(row: dict[str, Any]) -> str:
    payload = _mapping(row.get("payload"))
    detail = str(payload.get("detail") or "unknown")
    detail = re.sub(r"\s+", " ", detail).strip().lower()
    intent_id = str(row.get("intent_id") or "?")
    material = f"intent_rejected:{intent_id}:{detail}"
    return hashlib.sha256(material.encode()).hexdigest()


def intent_dedupe_key(row: dict[str, Any]) -> str:
    event_type = str(row.get("event_type") or "")
    intent_id = str(row.get("intent_id") or "?")
    if event_type == "intent_ack.rejected":
        return rejection_dedupe_key(row)
    return f"intent_accepted:{intent_id}"


def classify_order(row: dict[str, Any]) -> tuple[str, bool]:
    action = str(row.get("action") or "")
    sequence = order_sequence(row.get("client_order_id"))
    plan = _mapping(row.get("order_plan"))
    authorization = _mapping(plan.get("authorization"))
    created_by = str(authorization.get("created_by_service") or "")
    is_protection = action in PROTECTION_ACTIONS or sequence >= 11
    if not is_protection:
        return "order", False
    is_repair = sequence >= 21 or created_by == "order-lifecycle-monitor"
    return "protection", is_repair


def add_order_batch(
    state: dict[str, Any],
    row: dict[str, Any],
    now_ts: float,
) -> bool:
    message_class, is_repair = classify_order(row)
    intent_id = str(row.get("intent_id") or "?")
    batch_key = f"{intent_id}:{message_class}:{int(is_repair)}"
    batches = _mapping(state.get("order_batches"))
    state["order_batches"] = batches
    batch = batches.get(batch_key)
    if not isinstance(batch, dict):
        batch = {
            "message_class": message_class,
            "is_repair": is_repair,
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "rows": {},
        }
        batches[batch_key] = batch
    rows = _mapping(batch.get("rows"))
    event_id = str(row.get("source_id") or "")
    if event_id in rows:
        return False
    rows[event_id] = row
    batch["rows"] = rows
    batch["last_seen_at"] = now_ts
    return True


def fetch_exchange_order_index() -> dict[str, dict[str, Any]]:
    rows = run_sql_json(
        "SELECT json_build_object("
        "'account_id', account_id, "
        "'open_orders', COALESCE(payload->'open_orders','[]'::jsonb), "
        "'algo_orders', COALESCE(payload->'algo_orders','[]'::jsonb)"
        ")::text FROM exchange_state_mirror"
    )
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        account_id = str(row.get("account_id") or "")
        orders = _list(row.get("open_orders"))
        orders.extend(_list(row.get("algo_orders")))
        for order in orders:
            if not isinstance(order, dict):
                continue
            client_order_id = str(order.get("client_order_id") or "")
            if not is_robot_order_id(client_order_id):
                continue
            key = f"{account_id}:{client_order_id}"
            index[key] = order
    return index


def _order_details(
    rows: list[dict[str, Any]],
    exchange_orders: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for row in rows:
        account_id = str(row.get("account_id") or "")
        client_order_id = str(row.get("client_order_id") or "")
        key = f"{account_id}:{client_order_id}"
        detail = exchange_orders.get(key)
        if isinstance(detail, dict):
            details.append(detail)
    return details


def _entry_range(row: dict[str, Any]) -> str:
    plan = _mapping(row.get("order_plan"))
    entry = _mapping(plan.get("entry"))
    low = entry.get("price_min")
    high = entry.get("price_max")
    if low is None:
        low = plan.get("price_min")
    if high is None:
        high = plan.get("price_max")
    if low is not None and high is not None:
        return f"{_format_decimal(low, 6)}～{_format_decimal(high, 6)}"
    price = entry.get("price")
    if price is None:
        price = plan.get("price")
    if price is not None:
        return _format_decimal(price, 6)
    return "—"


def _actual_order_notional(details: list[dict[str, Any]]) -> Decimal | bool:
    total = Decimal("0")
    matched = False
    for order in details:
        quantity = _decimal(order.get("quantity"))
        price = _decimal(order.get("price"))
        if quantity is False or price is False:
            continue
        if quantity <= 0 or price <= 0:
            continue
        total += quantity * price
        matched = True
    if not matched:
        return False
    return total


def _price_span(details: list[dict[str, Any]]) -> str:
    prices: list[Decimal] = []
    for order in details:
        price = _decimal(order.get("price"))
        if price is False or price <= 0:
            continue
        prices.append(price)
    if not prices:
        return "—"
    low = min(prices)
    high = max(prices)
    if low == high:
        return _format_decimal(low, 6)
    return f"{_format_decimal(low, 6)}～{_format_decimal(high, 6)}"


def _quantity_total(details: list[dict[str, Any]]) -> Decimal | bool:
    total = Decimal("0")
    matched = False
    for order in details:
        quantity = _decimal(order.get("quantity"))
        if quantity is False or quantity <= 0:
            continue
        total += quantity
        matched = True
    if not matched:
        return False
    return total


def _protection_prices(
    rows: list[dict[str, Any]],
    details: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    stop_prices: list[str] = []
    take_profit_prices: list[str] = []
    for order in details:
        order_type = str(order.get("type") or "").upper()
        trigger = order.get("trigger_price")
        price = order.get("price")
        if "STOP" in order_type:
            if trigger is not None:
                stop_prices.append(_format_decimal(trigger, 6))
            continue
        if price is not None:
            take_profit_prices.append(_format_decimal(price, 6))

    row = rows[0]
    plan = _mapping(row.get("order_plan"))
    if not stop_prices and plan.get("stop_loss") is not None:
        stop_prices.append(_format_decimal(plan.get("stop_loss"), 6))
    if not take_profit_prices:
        for target in _list(plan.get("take_profits")):
            price = target
            if isinstance(target, dict):
                price = target.get("price")
            if price is not None:
                take_profit_prices.append(_format_decimal(price, 6))
    return sorted(set(stop_prices)), sorted(set(take_profit_prices))


def format_order_batch(
    batch: dict[str, Any],
    exchange_orders: dict[str, dict[str, Any]],
) -> str:
    row_map = _mapping(batch.get("rows"))
    rows = [row for row in row_map.values() if isinstance(row, dict)]
    rows.sort(key=lambda item: str(item.get("client_order_id") or ""))
    row = rows[0]
    details = _order_details(rows, exchange_orders)
    account_id = str(row.get("account_id") or "?")
    symbol = clean_symbol(row.get("instrument_id"))
    intent_id = str(row.get("intent_id") or "?")
    short_intent = intent_id[-8:]
    count = len(rows)

    if batch.get("message_class") == "protection":
        title = "🛡 保护单已挂出"
        if batch.get("is_repair"):
            title = "🛠 保护单已补挂"
        stop_prices, take_profit_prices = _protection_prices(rows, details)
        stop_text = "—"
        take_profit_text = "—"
        if stop_prices:
            stop_text = "、".join(stop_prices)
        if take_profit_prices:
            take_profit_text = "、".join(take_profit_prices)
        return "\n".join(
            [
                title,
                f"账户 {account_id}｜{symbol}｜{direction_cn(row)}",
                f"共 {count} 笔｜止损 {stop_text}｜止盈 {take_profit_text}",
                f"intent {short_intent}",
            ]
        )

    quantity = _quantity_total(details)
    quantity_text = "—"
    if quantity is not False:
        quantity_text = _format_decimal(quantity, 8)
    price_text = _price_span(details)
    if price_text == "—":
        price_text = _entry_range(row)
    notional = _actual_order_notional(details)
    if notional is False:
        notional = approved_notional(row)
    notional_text = "—"
    if notional is not False and notional > 0:
        notional_text = f"{_format_decimal(notional)} USDT"
    return "\n".join(
        [
            "📋 订单已挂出",
            f"账户 {account_id}｜{symbol}｜{direction_cn(row)}",
            f"阶梯 {count} 档｜总数量 {quantity_text}｜价格 {price_text}",
            f"挂单名义约 {notional_text}｜intent {short_intent}",
        ]
    )


def format_fill_message(row: dict[str, Any]) -> str:
    payload = _mapping(row.get("payload"))
    account_id = str(row.get("account_id") or "?")
    symbol = clean_symbol(row.get("instrument_id"))
    quantity = payload.get("last_qty")
    if quantity is None:
        quantity = payload.get("last_fill_qty")
    if quantity is None:
        quantity = payload.get("filled_qty")
    if quantity is None:
        quantity = payload.get("quantity")
    price = payload.get("last_px")
    if price is None:
        price = payload.get("avg_px")
    if price is None:
        price = payload.get("price")
    trade_id = str(row.get("trade_id") or "?")
    event_type = str(row.get("event_type") or "")
    title = "💰 成交"
    if event_type == "OrderPartiallyFilled":
        title = "💰 部分成交"
    notional = False
    quantity_number = _decimal(quantity)
    price_number = _decimal(price)
    if quantity_number is not False and price_number is not False:
        notional = quantity_number * price_number
    notional_text = "—"
    if notional is not False:
        notional_text = f"{_format_decimal(notional)} USDT"
    return "\n".join(
        [
            title,
            f"账户 {account_id}｜{symbol}｜{direction_cn(row)}",
            f"{_format_decimal(quantity, 8)} @ {_format_decimal(price, 8)}｜约 {notional_text}",
            f"trade {trade_id}",
        ]
    )


def _pending_ids(state: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in _list(state.get("pending")):
        if isinstance(item, dict):
            ids.add(str(item.get("id") or ""))
    return ids


def enqueue_message(
    state: dict[str, Any],
    message_class: str,
    dedupe_key: str,
    text: str,
    now_ts: float,
) -> bool:
    dedupe = _mapping(state.get("semantic_dedupe"))
    state["semantic_dedupe"] = dedupe
    if dedupe_key in dedupe:
        return False
    message_id = hashlib.sha256(dedupe_key.encode()).hexdigest()
    if message_id in _pending_ids(state):
        return False
    pending = _list(state.get("pending"))
    state["pending"] = pending
    if len(pending) >= MAX_PENDING_MESSAGES:
        log(
            "notification_queue_degraded",
            message_class=message_class,
            pending=len(pending),
            dedupe_key=dedupe_key,
        )
        return False
    pending.append(
        {
            "id": message_id,
            "class": message_class,
            "dedupe_key": dedupe_key,
            "text": text,
            "created_at": now_ts,
            "attempts": 0,
            "next_attempt_at": now_ts,
        }
    )
    dedupe[dedupe_key] = now_ts
    return True


def flush_order_batches(
    state: dict[str, Any],
    now_ts: float,
    exchange_orders: dict[str, dict[str, Any]] | None = None,
) -> int:
    batches = _mapping(state.get("order_batches"))
    due = [
        key
        for key, batch in batches.items()
        if isinstance(batch, dict)
        and now_ts - float(batch.get("last_seen_at") or now_ts)
        >= BATCH_SETTLE_SECONDS
    ]
    if not due:
        return 0
    if exchange_orders is None:
        exchange_orders = fetch_exchange_order_index()
    count = 0
    for key in due:
        batch = batches.get(key)
        if not isinstance(batch, dict):
            batches.pop(key, None)
            continue
        row_map = _mapping(batch.get("rows"))
        source_ids = sorted(str(source_id) for source_id in row_map)
        material = f"order_batch:{key}:{','.join(source_ids)}"
        dedupe_key = hashlib.sha256(material.encode()).hexdigest()
        text = format_order_batch(batch, exchange_orders)
        message_class = str(batch.get("message_class") or "order")
        if enqueue_message(
            state,
            message_class,
            dedupe_key,
            text,
            now_ts,
        ):
            count += 1
        batches.pop(key, None)
    return count


def _retry_delay(attempts: int) -> float:
    exponent = max(0, attempts - 1)
    delay = RETRY_BASE_SECONDS * (3 ** exponent)
    return min(delay, 300.0)


def _rate_limit_wait(
    state: dict[str, Any],
    message_class: str,
    now_ts: float,
) -> float:
    limits = _mapping(state.get("rate_limits"))
    state["rate_limits"] = limits
    minute = int(now_ts // 60)
    bucket = limits.get(message_class)
    bucket_minute = -1
    if isinstance(bucket, dict):
        raw_minute = bucket.get("minute")
        if raw_minute is not None:
            bucket_minute = int(raw_minute)
    if not isinstance(bucket, dict) or bucket_minute != minute:
        limits[message_class] = {"minute": minute, "count": 0}
        return 0.0
    count = int(bucket.get("count") or 0)
    if count < PER_CLASS_PER_MINUTE:
        return 0.0
    return ((minute + 1) * 60) - now_ts + 0.1


def _record_rate_success(
    state: dict[str, Any],
    message_class: str,
    now_ts: float,
) -> None:
    limits = _mapping(state.get("rate_limits"))
    state["rate_limits"] = limits
    minute = int(now_ts // 60)
    bucket = limits.get(message_class)
    bucket_minute = -1
    if isinstance(bucket, dict):
        raw_minute = bucket.get("minute")
        if raw_minute is not None:
            bucket_minute = int(raw_minute)
    if not isinstance(bucket, dict) or bucket_minute != minute:
        bucket = {"minute": minute, "count": 0}
        limits[message_class] = bucket
    bucket["count"] = int(bucket.get("count") or 0) + 1


def process_pending(
    state: dict[str, Any],
    sender: Sender = telegram_send,
    now_ts: float | None = None,
) -> dict[str, int]:
    if now_ts is None:
        now_ts = time.time()
    pending = _list(state.get("pending"))
    state["pending"] = pending
    stats = {"sent": 0, "retried": 0, "degraded": 0, "rate_limited": 0}
    kept: list[dict[str, Any]] = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        if float(item.get("next_attempt_at") or 0) > now_ts:
            kept.append(item)
            continue
        message_class = str(item.get("class") or "unknown")
        wait = _rate_limit_wait(state, message_class, now_ts)
        if wait > 0:
            item["next_attempt_at"] = now_ts + wait
            kept.append(item)
            stats["rate_limited"] += 1
            log(
                "notification_rate_limited",
                message_class=message_class,
                wait_seconds=round(wait, 1),
            )
            continue
        success, result = sender(str(item.get("text") or ""))
        if success:
            _record_rate_success(state, message_class, now_ts)
            stats["sent"] += 1
            log(
                "notification_sent",
                message_class=message_class,
                message_id=result.get("message_id"),
                text=result.get("text"),
            )
            continue

        attempts = int(item.get("attempts") or 0) + 1
        item["attempts"] = attempts
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            dead_letters = _list(state.get("dead_letters"))
            state["dead_letters"] = dead_letters
            item["failed_at"] = now_ts
            item["last_error"] = result
            dead_letters.append(item)
            state["dead_letters"] = dead_letters[-MAX_DEAD_LETTERS:]
            stats["degraded"] += 1
            log(
                "notification_delivery_degraded",
                message_class=message_class,
                attempts=attempts,
                error=result,
            )
            continue

        delay = _retry_delay(attempts)
        item["next_attempt_at"] = now_ts + delay
        item["last_error"] = result
        kept.append(item)
        stats["retried"] += 1
        log(
            "notification_delivery_retry",
            message_class=message_class,
            attempts=attempts,
            retry_in_seconds=delay,
            error=result,
        )
    state["pending"] = kept
    return stats


def fetch_audit_rows(cursor: float) -> list[dict[str, Any]]:
    return run_sql_json(
        "SELECT json_build_object("
        "'source_id', ae.audit_event_id::text, "
        "'cursor', EXTRACT(EPOCH FROM ae.created_at), "
        "'event_type', ae.event_type, "
        "'intent_id', COALESCE(ae.intent_id::text, ae.aggregate_id), "
        "'payload', ae.payload, "
        "'account_id', COALESCE(ti.account_id, ae.payload->>'account_id'), "
        "'instrument_id', ti.instrument_id, "
        "'action', ti.action::text, "
        "'order_plan', ti.order_plan, "
        "'risk_budget', ti.risk_budget"
        ")::text "
        "FROM audit_events ae "
        "LEFT JOIN trade_intents ti ON ti.intent_id::text = "
        "COALESCE(ae.intent_id::text, ae.aggregate_id) "
        f"WHERE ae.created_at >= to_timestamp({cursor:.6f}) "
        "AND ae.event_type IN ('intent_ack.accepted','intent_ack.rejected') "
        "ORDER BY ae.created_at, ae.audit_event_id "
        "LIMIT 500"
    )


def fetch_execution_rows(cursor: float) -> list[dict[str, Any]]:
    event_types = "','".join(sorted(EXECUTION_EVENT_TYPES))
    return run_sql_json(
        "SELECT json_build_object("
        "'source_id', ee.event_id, "
        "'cursor', EXTRACT(EPOCH FROM ee.ts_ingest), "
        "'event_type', ee.event_type, "
        "'account_id', ee.account_id, "
        "'intent_id', ee.intent_id::text, "
        "'client_order_id', ee.client_order_id, "
        "'venue_order_id', ee.venue_order_id, "
        "'trade_id', ee.trade_id, "
        "'payload', ee.payload, "
        "'instrument_id', COALESCE(ti.instrument_id, ee.payload->>'instrument_id'), "
        "'action', ti.action::text, "
        "'order_plan', ti.order_plan, "
        "'risk_budget', ti.risk_budget"
        ")::text "
        "FROM execution_events ee "
        "LEFT JOIN trade_intents ti ON ti.intent_id=ee.intent_id "
        f"WHERE ee.ts_ingest >= to_timestamp({cursor:.6f}) "
        f"AND ee.event_type IN ('{event_types}') "
        "AND ee.client_order_id ~ '^B[0-9a-f]{32}[0-9]{2}$' "
        "ORDER BY ee.ts_ingest, ee.event_id "
        "LIMIT 1000"
    )


def _append_seen(state: dict[str, Any], key: str, source_id: str) -> None:
    seen = _list(state.get(key))
    seen.append(source_id)
    state[key] = seen[-MAX_SEEN_IDS:]


def _process_audit_rows(
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    now_ts: float,
) -> int:
    seen = set(str(value) for value in _list(state.get("seen_audit_ids")))
    count = 0
    cursor = float(state.get("audit_cursor") or 0)
    for row in rows:
        source_id = str(row.get("source_id") or "")
        row_cursor = float(row.get("cursor") or cursor)
        cursor = max(cursor, row_cursor)
        if source_id in seen:
            continue
        seen.add(source_id)
        _append_seen(state, "seen_audit_ids", source_id)
        dedupe_key = intent_dedupe_key(row)
        if enqueue_message(
            state,
            "intent",
            dedupe_key,
            format_intent_message(row),
            now_ts,
        ):
            count += 1
    state["audit_cursor"] = cursor
    return count


def _process_execution_rows(
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    now_ts: float,
) -> int:
    seen = set(str(value) for value in _list(state.get("seen_execution_ids")))
    count = 0
    cursor = float(state.get("execution_cursor") or 0)
    for row in rows:
        source_id = str(row.get("source_id") or "")
        row_cursor = float(row.get("cursor") or cursor)
        cursor = max(cursor, row_cursor)
        if source_id in seen:
            continue
        seen.add(source_id)
        _append_seen(state, "seen_execution_ids", source_id)
        event_type = str(row.get("event_type") or "")
        if event_type == "OrderAccepted":
            if add_order_batch(state, row, now_ts):
                count += 1
            continue
        dedupe_key = f"fill:{source_id}"
        if enqueue_message(
            state,
            "fill",
            dedupe_key,
            format_fill_message(row),
            now_ts,
        ):
            count += 1
    state["execution_cursor"] = cursor
    return count


def prune_state(state: dict[str, Any], now_ts: float) -> None:
    dedupe = _mapping(state.get("semantic_dedupe"))
    for key, raw_timestamp in list(dedupe.items()):
        try:
            timestamp = float(raw_timestamp)
        except (TypeError, ValueError):
            timestamp = 0.0
        if now_ts - timestamp > DEDUPE_RETENTION_SECONDS:
            dedupe.pop(key, None)


def poll_once(state: dict[str, Any], now_ts: float | None = None) -> dict[str, int]:
    if now_ts is None:
        now_ts = time.time()
    if not state.get("initialized"):
        epoch = database_epoch()
        state["audit_cursor"] = epoch
        state["execution_cursor"] = epoch
        state["initialized"] = True
        log("notifier_bootstrap", database_epoch=epoch)
        return {"audit": 0, "execution": 0, "batches": 0}

    audit_rows = fetch_audit_rows(float(state.get("audit_cursor") or 0))
    execution_rows = fetch_execution_rows(float(state.get("execution_cursor") or 0))
    audit_count = _process_audit_rows(state, audit_rows, now_ts)
    execution_count = _process_execution_rows(state, execution_rows, now_ts)
    batch_count = flush_order_batches(state, now_ts)
    prune_state(state, now_ts)
    return {
        "audit": audit_count,
        "execution": execution_count,
        "batches": batch_count,
    }


def test_message() -> str:
    return "\n".join(
        [
            "🧪 交易事件通知验收",
            "账户 account-a｜MUUSDT｜做多",
            "Intent 已批准｜名义 100 USDT",
            "安全模拟事件｜不会产生订单",
        ]
    )


def send_test(sender: Sender = telegram_send) -> int:
    state = ensure_state({})
    now_ts = time.time()
    enqueue_message(
        state,
        "test",
        f"acceptance-test:{now_ts}",
        test_message(),
        now_ts,
    )
    stats = process_pending(state, sender=sender, now_ts=now_ts)
    log(
        "notification_test_result",
        sent=stats["sent"],
        queued_for_retry=stats["retried"],
        degraded=stats["degraded"],
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--send-test", action="store_true")
    parser.add_argument("--state-path", default=STATE_PATH)
    args = parser.parse_args()

    if args.send_test:
        raise SystemExit(send_test())

    state = load_state(args.state_path)
    log(
        "notifier_start",
        poll_seconds=POLL_SECONDS,
        batch_settle_seconds=BATCH_SETTLE_SECONDS,
        per_class_per_minute=PER_CLASS_PER_MINUTE,
    )
    while True:
        try:
            stats = poll_once(state)
            save_state(state, args.state_path)
            delivery = process_pending(state)
            save_state(state, args.state_path)
            if any(stats.values()) or any(delivery.values()):
                log("notifier_cycle", queued=stats, delivery=delivery)
        except Exception as exc:  # noqa: BLE001 - notifier remains fail-open
            log(
                "notifier_cycle_failed",
                error=type(exc).__name__,
                detail=str(exc)[:300],
            )
        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
