#!/usr/bin/env python3
"""Order lifecycle monitor for trader-v3 (hk).

Read-only sweeps, all actions delegated to Hermes jobs / operator flows —
this process NEVER touches the exchange and NEVER cancels anything itself:

1. Entry-order TTL (default 48h): a resting SYSTEM entry order (client id
   B{uuid32}{01-09}) older than ORDER_TTL_HOURS wakes a Hermes job that decides
   cancel-vs-renew (renewable exactly once). Protection orders (seq >= 11) and
   external/manual orders are never TTL'd. Before waking, the row is checked
   against exchange_state_mirror (the read-only Binance REST truth): a ledger
   row whose order is no longer on the exchange is a ghost — it is terminalized
   in orders_projection (the one DB write this process makes; it still never
   touches the exchange) instead of driving cancel wakes forever (the
   2026-07-10 spam storm: node restarts forget resting orders, so cancels were
   re-attempted every 2h against orders that no longer existed).
2. Price alerts: for every open position's live SL/TP protection orders, watch
   mark price; on approach (within APPROACH_FRACTION) or touch, wake a Hermes
   job (delivers to Telegram) with the channel context so it can manage the
   position per the original signal plan. Alerts deregister when the order or
   position disappears; one notification per order+level per 24h.
3. Hourly reconciliation: orders_projection rows that claim to be 'accepted'
   but whose event stream already shows a terminal event are ghosts (the
   2026-07-04 WLD case) -> one summary wake per day per discrepancy set.
4. Node health: every loop checks node_heartbeats. HALTED nodes and explicit
   readiness=false heartbeats wake one Chinese alert per node+reason per 24h.
5. Naked positions: fresh exchange_state_mirror positions and both regular/algo
   orders are checked per (account, symbol, position_side). A valid stop must
   be STOP-class, face the closing direction, have a positive trigger, and
   cover the full position quantity.
6. Pending cancels: OrderPendingCancel events without a terminal result are
   persisted in state. After the configured timeout, the exchange mirror is
   checked and a direct Telegram alert reports still-open, disappeared, or
   filled. This sweep only alerts; it never cancels or retries an order.

State lives in STATE_PATH (atomic JSON). Any exception in a sweep is logged
and skipped — one bad row must never kill the service (feeder P1-1 lesson).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from uuid import UUID

import hermes_signal_feeder as feeder  # run_hermes + channel context helpers

PSQL = ["docker", "exec", "trader-v3-postgres", "psql", "-U", "postgres", "-d", "trader", "-Atc"]
STATE_PATH = "/srv/trader-v3/scripts/.order_lifecycle_state.json"
MARK_URL = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"

POLL_SECONDS = 15
TTL_SWEEP_SECONDS = 600
RECON_SWEEP_SECONDS = 3600
PENDING_CANCEL_SWEEP_SECONDS = 60
INTENT_STALL_SECONDS = float(os.environ.get("INTENT_STALL_SECONDS", "300"))
ORDER_SNAPSHOT_MAX_AGE_SECONDS = 300
MIRROR_MAX_AGE_SECONDS = 300  # exchange_state_mirror refreshes every ~45s
ORDER_TTL_HOURS = float(os.environ.get("ORDER_TTL_HOURS", "48"))
APPROACH_FRACTION = 0.004
ALERT_DEDUP_SECONDS = 24 * 3600
EXHAUSTED_REWAKE_SECONDS = 2 * 3600
STATE_PRUNE_AGE_SECONDS = 7 * 24 * 3600
MARK_FAIL_ALERT_THRESHOLD = 20
NAKED_DEDUP_SECONDS = 24 * 3600
NAKED_GRACE_SECONDS = 180
OPERATOR_HALT_SUPPRESS_SECONDS = 24 * 3600
REPORT_DIR = os.environ.get("REPORT_DIR", "/srv/trader-v3/reports")
REPORT_HEALTH_URL = os.environ.get(
    "REPORT_HEALTH_URL",
    "http://127.0.0.1:8090/healthz",
)
DAILY_REPORT_EXPECTED_HOUR_UTC = 13
DAILY_REPORT_EXPECTED_MINUTE_UTC = 32
DAILY_REPORT_GRACE_SECONDS = 90 * 60
PENDING_CANCEL_TIMEOUT_MINUTES = float(
    os.environ.get("PENDING_CANCEL_TIMEOUT_MINUTES", "10")
)
# 查询窗口限制每轮扫描量。进入本地台账的未解决撤单会跨越窗口持续保留,
# 直到终态事件或 fresh 交易所镜像完成判定。
PENDING_CANCEL_LOOKBACK_HOURS = float(
    os.environ.get("PENDING_CANCEL_LOOKBACK_HOURS", "48")
)
BRAIN_PROBE_SECONDS = 600
BRAIN_FAIL_ALERT_AFTER = 2
HERMES_ENV_FILE = "/srv/hermes/profiles/trader/.env"
TG_CHAT_ID = "8545234287"
BRAIN_PROBES = (
    # (name, url, key_env, style, model)
    ("primary", "https://api.balenw.cloud/v1/chat/completions", "CLIPROXYAPI_API_KEY",
     "openai", "deepseek-v4-flash"),
    ("fallback", "https://open.bigmodel.cn/api/anthropic/v1/messages", "BIGMODEL_API_KEY",
     "anthropic", "glm-5.2"),
)
# 告警文案从 BRAIN_PROBES 取模型名,避免换模型后文案漂移(2026-08-04 前曾写死 gpt-5.5 误导排障)
BRAIN_PRIMARY_MODEL = BRAIN_PROBES[0][4]
BRAIN_FALLBACK_MODEL = BRAIN_PROBES[1][4]
V3_SKILL = "v3-trader"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def q(sql: str) -> list[list[str]]:
    out = subprocess.run(PSQL + [sql], capture_output=True, text=True, timeout=25)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:200]}")
    return [line.split("|") for line in out.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------- state ----

def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str = STATE_PATH) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ------------------------------------------------------------- helpers ----

def is_system_id(client_order_id: str) -> bool:
    return (
        len(client_order_id) == 35
        and client_order_id.startswith("B")
        and client_order_id[33:35].isdigit()
    )


def id_sequence(client_order_id: str) -> int | None:
    if not is_system_id(client_order_id):
        return None
    try:
        return int(client_order_id[33:35])
    except ValueError:
        return None


def is_entry_order(client_order_id: str) -> bool:
    seq = id_sequence(client_order_id)
    return seq is not None and 1 <= seq <= 9


def is_protection_order(client_order_id: str) -> bool:
    seq = id_sequence(client_order_id)
    return seq is not None and seq >= 11


ENTRY_INTENT_ACTIONS = {"open_position", "add_position"}
AUTHORIZED_BY_TYPES = {"user", "channel"}


def ttl_decision(client_order_id: str, age_hours: float, renewals: int,
                 ttl_hours: float = ORDER_TTL_HOURS,
                 intent_action: str = "open_position") -> str:
    """Returns 'skip' | 'wake' | 'exhausted'. Pure function for tests.

    Sequence alone cannot classify: a move_stop_loss management intent places
    its STOP at sequence 01 too. Only orders whose INTENT action is an entry
    action are TTL-able — protections must live as long as the position.
    """
    if intent_action not in ENTRY_INTENT_ACTIONS:
        return "skip"
    if not is_entry_order(client_order_id):
        return "skip"
    if age_hours < ttl_hours:
        return "skip"
    if renewals == 0:
        return "wake"
    if age_hours < ttl_hours * (renewals + 1):
        return "skip"  # renewed once, extended window still running
    return "exhausted"


def intent_uuid_of(client_order_id: str) -> str | None:
    if not is_system_id(client_order_id):
        return None
    h = client_order_id[1:33]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _intent_authorization(
    order_plan: dict,
    current_intent_id: str,
    account_id: str,
) -> dict:
    persisted = order_plan.get("authorization")
    if not isinstance(persisted, dict):
        return {}
    normalized_current_intent_id = str(current_intent_id or "").strip()
    persisted_parent_intent_id = str(
        persisted.get("parent_intent_id") or ""
    ).strip()
    effective_parent_intent_id = (
        persisted_parent_intent_id or normalized_current_intent_id
    )
    return {
        "parent_intent_id": effective_parent_intent_id,
        "current_intent_id": normalized_current_intent_id,
        "account_id": str(account_id or "").strip(),
        "source_message_id": str(
            persisted.get("source_message_id") or ""
        ).strip(),
        "authorized_by_type": str(
            persisted.get("authorized_by_type") or ""
        ).strip().lower(),
        "authorized_by_id": str(
            persisted.get("authorized_by_id") or ""
        ).strip(),
    }


def _has_complete_authorization(authorization: dict | None) -> bool:
    if not isinstance(authorization, dict):
        return False
    parent_intent_id = str(authorization.get("parent_intent_id") or "").strip()
    source_message_id = str(authorization.get("source_message_id") or "").strip()
    authorized_by_id = str(authorization.get("authorized_by_id") or "").strip()
    authorized_by_type = str(
        authorization.get("authorized_by_type") or ""
    ).strip().lower()
    return bool(
        parent_intent_id
        and source_message_id
        and authorized_by_id
        and authorized_by_type in AUTHORIZED_BY_TYPES
    )


def _authorization_prompt(authorization: dict) -> str:
    parent_intent_id = authorization["parent_intent_id"]
    source_message_id = authorization["source_message_id"]
    authorized_by_type = authorization["authorized_by_type"]
    authorized_by_id = authorization["authorized_by_id"]
    account_id = str(authorization.get("account_id") or "").strip()
    channel_id = "operator"
    if authorized_by_type == "channel":
        channel_id = authorized_by_id
    return (
        "\n授权依据:"
        f" parent_intent_id={parent_intent_id},"
        f" source_message_id={source_message_id},"
        f" authorized_by_type={authorized_by_type},"
        f" authorized_by_id={authorized_by_id}。"
        "任何订单管理命令必须携带:"
        f" --account {account_id}"
        f" --channel {channel_id}"
        f" --entry-ref {source_message_id}"
        f" --authorized-by-type {authorized_by_type}"
        f" --authorized-by-id {authorized_by_id}"
        f" --source-message-id {source_message_id}"
        " --created-by-service order-lifecycle-monitor"
        f" --parent-intent-id {parent_intent_id}。"
    )


def wake_authorized_management(
    prompt: str,
    name: str,
    dry_run: bool,
    authorization: dict | None,
) -> bool:
    """Wake Hermes for an order-writing workflow only with complete provenance."""
    if not _has_complete_authorization(authorization):
        log(f"management wake blocked: {name} missing complete authorization")
        return False
    return wake_hermes(
        prompt + _authorization_prompt(authorization),
        name=name,
        dry_run=dry_run,
    )


def _send_read_only_alert(
    state: dict,
    key: str,
    text: str,
    dry_run: bool,
    now_ts: float,
) -> bool:
    last_alert = float(state.get(key) or 0)
    if last_alert and now_ts - last_alert < ALERT_DEDUP_SECONDS:
        return False
    alert = text + " 监控已降级为只读告警，订单管理保持冻结。"
    if dry_run:
        log(f"DRY-RUN would send read-only alert: {alert}")
        return True
    if not tg_send_direct(alert):
        return False
    state[key] = now_ts
    return True


def _send_deduplicated_alert(
    state: dict,
    key: str,
    text: str,
    dry_run: bool,
    now_ts: float,
) -> bool:
    last_alert = float(state.get(key) or 0)
    if last_alert and now_ts - last_alert < ALERT_DEDUP_SECONDS:
        return False
    if dry_run:
        log(f"DRY-RUN would send alert: {text}")
        return True
    if not tg_send_direct(text):
        return False
    state[key] = now_ts
    return True


_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_UUID_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _normalize_intent_id(value: str) -> str | None:
    text = str(value or "").strip()
    if not _CANONICAL_UUID_RE.fullmatch(text) and not _UUID_HEX_RE.fullmatch(text):
        return None
    try:
        return str(UUID(text))
    except ValueError:
        return None


def _load_intent_contexts(intent_ids: set[str]) -> dict[str, dict]:
    normalized_ids = {
        normalized
        for normalized in (_normalize_intent_id(value) for value in intent_ids)
        if normalized
    }
    if not normalized_ids:
        return {}
    placeholders = ",".join(
        f"'{intent_id}'" for intent_id in sorted(normalized_ids)
    )
    rows = q(
        "SELECT ti.intent_id::text, ti.action::text, ti.account_id, "
        "ti.instrument_id, ti.order_plan::text "
        "FROM trade_intents ti "
        f"WHERE ti.intent_id::text IN ({placeholders})"
    )
    contexts: dict[str, dict] = {}
    for row in rows:
        if len(row) < 5:
            continue
        (
            intent_id,
            action,
            account_id,
            instrument_id,
            plan_raw,
        ) = row[:5]
        try:
            plan = json.loads(plan_raw)
        except (TypeError, ValueError):
            plan = {}
        if not isinstance(plan, dict):
            plan = {}
        contexts[intent_id] = {
            "action": action,
            "account_id": account_id,
            "instrument_id": instrument_id,
            "order_plan": plan,
            "authorization": _intent_authorization(
                plan,
                intent_id,
                account_id,
            ),
        }
    return contexts


def _position_parent_authorizations(
    position_keys: set[tuple[str, str, str]],
) -> dict[tuple[str, str, str], dict]:
    if not position_keys:
        return {}
    rows = q(
        "SELECT ti.intent_id::text, ti.account_id, ti.instrument_id, "
        "ti.order_plan::text "
        "FROM trade_intents ti "
        "WHERE ti.status='approved' "
        "AND ti.action::text IN ('open_position','add_position') "
        "AND ti.approved_at > now() - interval '30 days' "
        "ORDER BY ti.approved_at DESC, ti.created_at DESC"
    )
    authorizations: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        if len(row) < 4:
            continue
        (
            intent_id,
            account_id,
            instrument_id,
            plan_raw,
        ) = row[:4]
        symbol = _normalize_symbol(instrument_id)
        try:
            plan = json.loads(plan_raw)
        except (TypeError, ValueError):
            plan = {}
        if not isinstance(plan, dict):
            plan = {}
        position_side = _position_side(plan.get("side"))
        key = (account_id, symbol or "", position_side or "")
        if key not in position_keys or key in authorizations:
            continue
        authorization = _intent_authorization(
            plan,
            intent_id,
            account_id,
        )
        if _has_complete_authorization(authorization):
            authorizations[key] = authorization
    return authorizations


def mark_price(symbol: str, fetch=None) -> float | None:
    try:
        if fetch is not None:
            raw = fetch(symbol)
        else:
            with urllib.request.urlopen(MARK_URL.format(symbol=symbol), timeout=8) as resp:
                raw = resp.read()
        return float(json.loads(raw)["markPrice"])
    except Exception as exc:  # noqa: BLE001
        log(f"mark price {symbol} failed: {exc!r}")
        return None


def alert_events(order: dict, mark: float, now_ts: float, state: dict,
                 approach_fraction: float = APPROACH_FRACTION,
                 dedup_seconds: float = ALERT_DEDUP_SECONDS) -> list[dict]:
    """Pure: which alert notifications fire for one protection order at `mark`.

    order: {client_order_id, symbol, role ('sl'|'tp'), price(float)}
    state keys: alert:{client_order_id}:{level} -> last fired epoch
    """
    events = []
    price = float(order["price"])
    distance = abs(mark - price) / price if price else 1.0
    # Advisory pre-warnings only (direction-free): the authoritative "level was
    # hit" signal is the protection-order FILL sweep below, straight from the
    # execution event stream.
    for level, hit in (("approach", distance <= approach_fraction),
                       ("touch", distance <= 0.001)):
        if not hit:
            continue
        key = f"alert:{order['client_order_id']}:{level}"
        last = float(state.get(key, 0))
        if now_ts - last < dedup_seconds:
            continue
        events.append({"level": level, "key": key, "order": order, "mark": mark})
    return events


def reconcile_suspects(projection_rows: list[dict], terminal_ids: set[str],
                       now_ts: float, state: dict) -> list[dict]:
    """Pure: projection says 'accepted' but events already show a terminal state."""
    suspects = []
    for row in projection_rows:
        cid = row["client_order_id"]
        if cid not in terminal_ids:
            continue
        key = f"recon:{cid}"
        if now_ts - float(state.get(key, 0)) < ALERT_DEDUP_SECONDS:
            continue
        suspects.append({"key": key, "row": row})
    return suspects


def snapshot_reconcile_differences(projection_rows: list[dict], snapshot_orders: list[dict],
                                   now_ts: float, state: dict,
                                   snapshot_age_seconds: float | None = None) -> dict:
    """Pure: compare accepted projection rows with the exchange-open heartbeat snapshot."""
    if snapshot_age_seconds is not None and snapshot_age_seconds > ORDER_SNAPSHOT_MAX_AGE_SECONDS:
        return {"stale": True, "ghosts": [], "missing": []}
    projection_by_id = {
        str(row.get("client_order_id")): row
        for row in projection_rows
        if row.get("client_order_id")
    }
    snapshot_by_id = {
        str(row.get("client_order_id")): row
        for row in snapshot_orders
        if row.get("client_order_id")
    }
    ghosts = []
    for cid in sorted(set(projection_by_id) - set(snapshot_by_id)):
        key = f"recon:ghost:{cid}"
        if key not in state or now_ts - float(state.get(key, 0)) >= ALERT_DEDUP_SECONDS:
            ghosts.append({"key": key, "row": projection_by_id[cid]})
    missing = []
    for cid in sorted(set(snapshot_by_id) - set(projection_by_id)):
        key = f"recon:missing:{cid}"
        if key not in state or now_ts - float(state.get(key, 0)) >= ALERT_DEDUP_SECONDS:
            missing.append({"key": key, "row": snapshot_by_id[cid]})
    return {"stale": False, "ghosts": ghosts, "missing": missing}


def _normalize_symbol(instrument_id: str | None) -> str | None:
    if not instrument_id:
        return None
    return str(instrument_id).split("-", 1)[0]


def _normalize_order_side(side: str | None) -> str | None:
    text = str(side or "").strip().lower()
    if text in ("buy", "long"):
        return "long"
    if text in ("sell", "short"):
        return "short"
    return None


def _boolish(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "t", "1", "yes", "y")


def _positive_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _position_side(value, quantity=None) -> str | None:
    text = str(value or "").strip().lower()
    if text in ("long", "buy"):
        return "long"
    if text in ("short", "sell"):
        return "short"
    if text in ("both", "net", ""):
        try:
            amount = float(quantity)
        except (TypeError, ValueError):
            return None
        if amount > 0:
            return "long"
        if amount < 0:
            return "short"
    return None


def _stop_order_type(order: dict) -> bool:
    order_type = str(
        order.get("order_type") or order.get("type") or ""
    ).strip().upper()
    normalized = re.sub(r"[^A-Z0-9]+", "_", order_type).strip("_")
    return re.search(r"(^|_)STOP($|_)", normalized) is not None


def _stop_trigger_price(order: dict) -> float | None:
    for key in ("trigger_price", "stop_price", "triggerPrice", "stopPrice"):
        if key not in order:
            continue
        price = _positive_float(order.get(key))
        if price is not None:
            return price
    return None


def _order_quantity(order: dict) -> float | None:
    for key in ("quantity", "orig_qty", "origQty", "amount"):
        if key not in order:
            continue
        quantity = _positive_float(order.get(key))
        if quantity is not None:
            return quantity
    return None


def snapshot_stop_loss_symbols(
    snapshot_orders: list[dict],
    position_quantities: dict[tuple[str, str, str], float],
    account_id: str = "",
) -> set[tuple[str, str, str]]:
    """Position keys fully covered by valid STOP-class closing orders.

    The historical name is retained for deployment compatibility. The return
    value now carries the complete (account, symbol, position_side) key.
    """
    coverage: dict[tuple[str, str, str], float] = {}
    for order in snapshot_orders:
        symbol = _normalize_symbol(order.get("instrument_id") or order.get("symbol"))
        if not symbol or not _stop_order_type(order):
            continue
        if _stop_trigger_price(order) is None:
            continue
        quantity = _order_quantity(order)
        if quantity is None:
            continue
        order_side = _normalize_order_side(order.get("side"))
        if order_side is None:
            continue
        declared_position_side = _position_side(order.get("position_side"))
        target_side = "long" if order_side == "short" else "short"
        if declared_position_side and declared_position_side != target_side:
            continue
        key = (account_id, symbol, target_side)
        if key not in position_quantities:
            continue
        coverage[key] = coverage.get(key, 0.0) + quantity
    return {
        key
        for key, quantity in position_quantities.items()
        if coverage.get(key, 0.0) + 1e-12 >= quantity
    }


def _parse_snapshot_orders(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _parse_snapshot_payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _exchange_state_snapshots() -> dict[str, dict]:
    """Latest fresh exchange truth keyed by account.

    Stale rows remain present with fresh=False so callers can fail closed
    without substituting projections or heartbeat snapshots.
    """
    snapshots: dict[str, dict] = {}
    rows = q(
        "SELECT account_id, EXTRACT(EPOCH FROM (now() - updated_at)), payload::text "
        "FROM exchange_state_mirror"
    )
    for account_id, age_raw, payload_raw in rows:
        try:
            age = float(age_raw)
        except (TypeError, ValueError):
            age = float("inf")
        payload = _parse_snapshot_payload(payload_raw)
        snapshots[account_id] = {
            "age": age,
            "fresh": age <= MIRROR_MAX_AGE_SECONDS,
            "positions": payload.get("positions") or [],
            "open_orders": payload.get("open_orders") or [],
            "algo_orders": payload.get("algo_orders") or [],
        }
    return snapshots


def _heartbeat_open_order_snapshot() -> tuple[list[dict] | None, float | None]:
    rows = q(
        "SELECT node_id, EXTRACT(EPOCH FROM (now() - last_seen_at)), "
        "COALESCE(payload->'open_orders','[]'::jsonb)::text "
        "FROM node_heartbeats WHERE payload ? 'open_orders' ORDER BY last_seen_at DESC"
    )
    if not rows:
        return None, None
    newest_age = None
    orders: list[dict] = []
    for node_id, age_raw, orders_raw in rows:
        try:
            age = float(age_raw)
        except (TypeError, ValueError):
            continue
        newest_age = age if newest_age is None else min(newest_age, age)
        if age > ORDER_SNAPSHOT_MAX_AGE_SECONDS:
            continue
        for order in _parse_snapshot_orders(orders_raw):
            order.setdefault("node_id", node_id)
            orders.append(order)
    if not orders:
        return None, newest_age
    return orders, newest_age


def _exchange_open_order_ids() -> dict[str, set[str] | None]:
    """Per-account client_order_ids truly open on the exchange, from
    exchange_state_mirror (read-only Binance REST poll, ~45s refresh).

    NOT the node-heartbeat snapshot: nodes forget resting orders older than
    the reconciliation lookback on every restart (2026-07-10 incident), so
    their view cannot distinguish "gone from exchange" from "forgotten".

    An account maps to None when its mirror row is stale/absent — callers
    must fail open for that account (cannot classify ghosts safely)."""
    out: dict[str, set[str] | None] = {}
    try:
        # open_orders alone is NOT the full exchange view: conditional SL/TP
        # live in algo_orders. Reading only open_orders made every resting
        # protection order look like a ghost and heal_ghost_order falsely
        # terminalized 3 live stops on 2026-07-18.
        rows = q(
            "SELECT account_id, EXTRACT(EPOCH FROM (now() - updated_at)), "
            "(COALESCE(payload->'open_orders','[]'::jsonb) || "
            "COALESCE(payload->'algo_orders','[]'::jsonb))::text "
            "FROM exchange_state_mirror"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"exchange mirror read failed: {exc!r}")
        return out
    for account_id, age_raw, orders_raw in rows:
        try:
            age = float(age_raw)
        except (TypeError, ValueError):
            out[account_id] = None
            continue
        if age > MIRROR_MAX_AGE_SECONDS:
            out[account_id] = None
            continue
        ids: set[str] = set()
        for order in _parse_snapshot_orders(orders_raw):
            cid = str(order.get("client_order_id") or order.get("clientOrderId") or "")
            if cid:
                ids.add(cid)
        out[account_id] = ids
    return out


def heal_ghost_order(cid: str, account_id: str, dry_run: bool) -> None:
    """Terminalize a ledger row whose order no longer exists on the exchange.
    'canceled' is a best guess (the terminal event was lost while a node was
    down/amnesiac); the payload marker keeps the heal auditable."""
    if dry_run:
        log(f"DRY-RUN would heal ghost order {cid} ({account_id})")
        return
    q(
        "UPDATE orders_projection SET status='canceled', updated_at=now(), "
        "payload = payload || jsonb_build_object('lifecycle_heal', "
        "'not-on-exchange ' || to_char(now(), 'YYYY-MM-DD HH24:MI')) "
        f"WHERE client_order_id='{cid}' AND account_id='{account_id}' "
        "AND status IN ('accepted','partially_filled','updated')"
    )
    log(f"healed ghost order {cid} ({account_id}): projection -> canceled")


def prune_state(state: dict, live_ids: set[str], now_ts: float,
                max_age: float = None) -> None:
    """Drop bookkeeping for orders that left the book long ago (P2-1: the state
    file must not grow without bound). Timestamp-valued keys older than the age
    cap whose order id is no longer live are removed; ttl:* dicts follow their
    ttlwake sibling."""
    max_age = STATE_PRUNE_AGE_SECONDS if max_age is None else max_age
    for key in list(state):
        parts = key.split(":")
        if parts[0] not in (
            "alert",
            "authalert",
            "dailyreport",
            "fill",
            "intentstall",
            "protectionfreeze",
            "recon",
            "ttlwake",
        ):
            continue
        cid = parts[1] if len(parts) > 1 else ""
        if cid in live_ids:
            continue
        try:
            stamp = float(state.get(key) or 0)
        except (TypeError, ValueError):
            stamp = 0.0
        if now_ts - stamp >= max_age:
            state.pop(key, None)
            state.pop(f"ttl:{cid}", None)
    return None


# -------------------------------------------------------------- sweeps ----

def _hermes_env_value(name: str) -> str | None:
    try:
        with open(HERMES_ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def tg_send_direct(text: str) -> bool:
    """Telegram Bot API direct send — the LLM-independent alert channel for
    exactly the case where the agent brain itself is down."""
    token = _hermes_env_value("TELEGRAM_BOT_TOKEN")
    if not token:
        log("tg_send_direct: no bot token available")
        return False
    try:
        data = json.dumps({"chat_id": TG_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        log(f"tg_send_direct failed: {exc!r}")
        return False


def probe_llm(name: str, url: str, key_env: str, style: str, model: str,
              opener=None) -> bool:
    key = _hermes_env_value(key_env)
    if not key:
        return False
    try:
        if style == "anthropic":
            body = {"model": model, "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}]}
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                       "Content-Type": "application/json"}
        else:
            body = {"model": model, "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}]}
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        open_func = opener or urllib.request.urlopen
        with open_func(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001
        log(f"brain probe {name} failed: {exc!r}")
        return False


def brain_alert_decision(results: dict, state: dict, now_ts: float) -> str | None:
    """Pure: escalate after BRAIN_FAIL_ALERT_AFTER consecutive primary failures;
    critical when fallback is down too. Returns alert text or None."""
    fails = int(state.get("brainfail:primary", 0))
    fails = fails + 1 if not results.get("primary") else 0
    state["brainfail:primary"] = fails
    if fails < BRAIN_FAIL_ALERT_AFTER:
        return None
    if not results.get("fallback"):
        key = "brainalert:critical"
        if now_ts - float(state.get(key, 0)) < 1800:
            return None
        state[key] = now_ts
        return (f"🚨 交易大脑双通道全部断供(主 {BRAIN_PRIMARY_MODEL} 与备胎 {BRAIN_FALLBACK_MODEL} 均探活失败)。"
                "信号将进入重试队列不会丢,但暂时无人决策——请尽快检查模型通道。")
    key = "brainalert:primary"
    if now_ts - float(state.get(key, 0)) < 3600:
        return None
    state[key] = now_ts
    return (f"⚠️ 交易大脑主通道({BRAIN_PRIMARY_MODEL})探活连续失败,已由备胎 {BRAIN_FALLBACK_MODEL} 承接。"
            "决策质量可能略降,建议尽快检查 new-api 的模型通道。")


def sweep_brain(state: dict, dry_run: bool, now_ts: float | None = None,
                prober=None) -> None:
    now_ts = now_ts or time.time()
    results = {}
    for name, url, key_env, style, model in BRAIN_PROBES:
        fn = prober or probe_llm
        results[name] = fn(name, url, key_env, style, model)
    text = brain_alert_decision(results, state, now_ts)
    if text:
        log(f"BRAIN ALERT: {text}")
        if not dry_run:
            tg_send_direct(text)


def wake_hermes(prompt: str, name: str, dry_run: bool) -> bool:
    if dry_run:
        log(f"DRY-RUN would wake hermes job {name}: {prompt[:120]!r}...")
        return True
    return bool(feeder.run_hermes(feeder.sanitize_prompt(prompt), name=name, dry_run=False))


def _readiness_is_false(value) -> bool:
    if isinstance(value, bool):
        return value is False
    return str(value or "").strip().lower() in ("false", "f", "0", "no", "n")


def _clear_node_alert_state(state: dict, node_id: str, keep_reason: str | None = None) -> None:
    prefixes = (
        f"nodehalt:{node_id}:",
        f"nodehalt-first:{node_id}:",
    )
    keep_keys = set()
    if keep_reason is not None:
        keep_keys = {
            f"nodehalt:{node_id}:{keep_reason}",
            f"nodehalt-first:{node_id}:{keep_reason}",
        }
    for key in list(state):
        if key in keep_keys:
            continue
        if key.startswith(prefixes):
            state.pop(key, None)


def _duration_cn(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    return f"{max(0, seconds) / 60:.0f} 分钟"


HEARTBEAT_STALE_SECONDS = 300


def sweep_node_health(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    now_ts = now_ts or time.time()
    rows = q(
        "SELECT node_id, status, COALESCE(payload->>'readiness',''), "
        "COALESCE(payload->>'halt_reason',''), "
        "EXTRACT(EPOCH FROM (now() - GREATEST(last_seen_at, "
        "COALESCE((payload->>'ts')::timestamptz, last_seen_at)))), "
        "COALESCE((SELECT oc.command_type FROM operator_commands oc "
        "JOIN command_node_acks cna ON cna.command_id=oc.command_id "
        "WHERE cna.node_id=node_heartbeats.node_id AND cna.status='acked' "
        "AND oc.command_type IN ('HALT','RESUME','REDUCE') "
        "ORDER BY cna.ack_at DESC NULLS LAST, oc.created_at DESC LIMIT 1),'') "
        ", COALESCE((SELECT EXTRACT(EPOCH FROM (now() - "
        "COALESCE(cna.ack_at, oc.created_at))) FROM operator_commands oc "
        "JOIN command_node_acks cna ON cna.command_id=oc.command_id "
        "WHERE cna.node_id=node_heartbeats.node_id AND cna.status='acked' "
        "AND oc.command_type IN ('HALT','RESUME','REDUCE') "
        "ORDER BY cna.ack_at DESC NULLS LAST, oc.created_at DESC LIMIT 1),-1) "
        "FROM node_heartbeats ORDER BY node_id"
    )
    for row in rows:
        node_id, status_raw, readiness_raw, halt_reason_raw, hb_age_raw = row[:5]
        latest_lifecycle_command = str(row[5] if len(row) > 5 else "").strip().upper()
        try:
            latest_lifecycle_command_age = float(
                row[6] if len(row) > 6 else -1
            )
        except (TypeError, ValueError):
            latest_lifecycle_command_age = -1
        status = str(status_raw or "").strip().upper()
        readiness_false = _readiness_is_false(readiness_raw)
        # 2026-07-24 事故：双节点僵死 7 小时，心跳停更但表里残留
        # readiness=true，值检查永远看到"健康"。冻结的心跳本身就是熔断信号，
        # 且该场景大脑链路可能同样受损，必须走 LLM 无关的 TG 直发。
        hb_age = float(hb_age_raw or 0)
        if hb_age > HEARTBEAT_STALE_SECONDS:
            stale_key = f"nodehalt:{node_id}:heartbeat_stale"
            first_key = f"nodehalt-first:{node_id}:heartbeat_stale"
            state.setdefault(first_key, now_ts)
            last_alert = float(state.get(stale_key) or 0)
            if stale_key not in state or now_ts - last_alert >= ALERT_DEDUP_SECONDS:
                text = (
                    f"🔴 节点心跳停更告警:{node_id} 心跳已 {_duration_cn(hb_age)} 未更新"
                    f"(阈值 {HEARTBEAT_STALE_SECONDS}s),节点可能僵死。"
                    f"表内残留状态 {status or 'UNKNOWN'}/readiness={readiness_raw or 'unknown'} 不可信。"
                    "请人工核查进程与健康端点;禁止自动 RESUME。"
                )
                sent = tg_send_direct(text)
                wake_hermes(text, name=f"nodehalt-stale-{node_id}", dry_run=dry_run)
                if (sent or dry_run) and not dry_run:
                    state[stale_key] = now_ts
            continue
        state.pop(f"nodehalt:{node_id}:heartbeat_stale", None)
        state.pop(f"nodehalt-first:{node_id}:heartbeat_stale", None)
        halt_reason = str(halt_reason_raw or "").strip()
        if (
            status == "HALTED"
            and not readiness_false
            and latest_lifecycle_command == "HALT"
            and 0 <= latest_lifecycle_command_age <= OPERATOR_HALT_SUPPRESS_SECONDS
        ):
            # An acknowledged operator HALT is an expected control action. The
            # command path owns its audit trail; node health alerts cover faults.
            _clear_node_alert_state(state, node_id)
            continue
        if status != "HALTED" and not readiness_false:
            _clear_node_alert_state(state, node_id)
            continue

        reason = halt_reason
        if not reason:
            reason = "HALTED" if status == "HALTED" else "readiness=false"
        _clear_node_alert_state(state, node_id, keep_reason=reason)

        alert_key = f"nodehalt:{node_id}:{reason}"
        first_key = f"nodehalt-first:{node_id}:{reason}"
        if first_key not in state:
            state[first_key] = now_ts
        last_alert = float(state.get(alert_key) or 0)
        if alert_key in state and now_ts - last_alert < ALERT_DEDUP_SECONDS:
            continue

        duration = _duration_cn(now_ts - float(state[first_key]))
        prompt = (
            f"节点熔断告警:节点 {node_id} 当前 trading_state={status or 'UNKNOWN'},"
            f"readiness={readiness_raw or 'unknown'},原因={reason},已持续 {duration}。"
            "请立即用中文短消息通知用户并查询节点状态与最近事件。"
            "禁止自动 RESUME,等待人工确认根因与安全状态。"
        )
        if wake_hermes(
            prompt,
            name=f"nodehalt-{node_id}",
            dry_run=dry_run,
        ) and not dry_run:
            state[alert_key] = now_ts


def sweep_intent_stalls(
    state: dict,
    dry_run: bool,
    now_ts: float | None = None,
) -> None:
    now_ts = now_ts or time.time()
    rows = q(
        "SELECT ti.intent_id::text, ti.account_id, ti.instrument_id, "
        "ti.action::text, EXTRACT(EPOCH FROM (now() - ti.approved_at)) "
        "FROM trade_intents ti "
        "WHERE ti.status='approved' "
        f"AND ti.approved_at <= now() - interval '{INTENT_STALL_SECONDS} seconds' "
        # 回看窗:只报活跃事故,不翻历史债(07-26 撤单刷屏教训;2026-08-03 部署首轮
        # 曾对 18 条遗留 approved intent 一次性开火)。过 valid_until 的 intent
        # 自然失效,无消费者也不是事故。
        "AND ti.approved_at > now() - interval '24 hours' "
        "AND (ti.valid_until IS NULL OR ti.valid_until > now()) "
        "AND NOT EXISTS ("
        " SELECT 1 FROM audit_events ae "
        " WHERE (ae.intent_id=ti.intent_id OR ("
        " ae.aggregate_type='trade_intent' "
        " AND ae.aggregate_id=ti.intent_id::text)) "
        " AND ae.event_type LIKE 'intent_ack.%'"
        ") "
        "AND NOT EXISTS ("
        " SELECT 1 FROM execution_events ee WHERE ee.intent_id=ti.intent_id"
        ") "
        "ORDER BY ti.approved_at"
    )
    for intent_id, account_id, instrument_id, action, age_raw in rows:
        try:
            age_seconds = float(age_raw)
        except (TypeError, ValueError):
            age_seconds = INTENT_STALL_SECONDS
        text = (
            f"🔴 Intent 消费停滞:{account_id} {instrument_id} {action} "
            f"intent={intent_id} 已 approved {_duration_cn(age_seconds)}，"
            "仍无 intent_ack 或 execution event 推进。"
            "请检查 node intent consumer、最近异常和队列游标。"
        )
        _send_deduplicated_alert(
            state,
            f"intentstall:{intent_id}",
            text,
            dry_run,
            now_ts,
        )


def sweep_protection_events(
    state: dict,
    dry_run: bool,
    now_ts: float | None = None,
) -> None:
    now_ts = now_ts or time.time()
    rows = q(
        "SELECT event_id, account_id, COALESCE(intent_id::text,''), "
        "COALESCE(client_order_id,''), payload::text "
        "FROM execution_events "
        "WHERE event_type='ProtectionFrozen' "
        "AND ts_event > now() - interval '24 hours' "
        "ORDER BY ts_event"
    )
    for event_id, account_id, intent_id, client_order_id, payload_raw in rows:
        try:
            payload = json.loads(payload_raw)
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        reason = str(
            payload.get("reason")
            or payload.get("denial_reason")
            or "unknown"
        )
        instrument_id = str(payload.get("instrument_id") or "?")
        text = (
            f"🔴 保护单冻结:{account_id} {instrument_id} intent={intent_id or '?'} "
            f"reason={reason} revision={payload.get('protection_revision', '?')}。"
            "自动保护重同步已停止，请人工核对持仓和交易所保护单。"
        )
        if client_order_id:
            text += f" client_order_id={client_order_id}。"
        _send_deduplicated_alert(
            state,
            f"protectionfreeze:{event_id}",
            text,
            dry_run,
            now_ts,
        )


def _daily_report_file_exists(
    report_date: str,
    report_dir: str = REPORT_DIR,
) -> bool:
    prefix = f"{report_date}-daily-"
    try:
        with os.scandir(report_dir) as entries:
            return any(
                entry.is_file()
                and entry.name.startswith(prefix)
                and entry.name.endswith(".html")
                for entry in entries
            )
    except OSError:
        return False


def _report_health() -> dict:
    try:
        with urllib.request.urlopen(REPORT_HEALTH_URL, timeout=8) as response:
            payload = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        log(f"report health read failed: {exc!r}")
        return {}
    return payload if isinstance(payload, dict) else {}


def _health_has_daily_publication(health: dict, report_date: str) -> bool:
    publication = health.get("last_publication")
    if not isinstance(publication, dict):
        return False
    return (
        publication.get("status") == "succeeded"
        and publication.get("report_type") == "daily"
        and publication.get("report_date") == report_date
    )


def sweep_daily_report(
    state: dict,
    dry_run: bool,
    now_ts: float | None = None,
) -> None:
    now_ts = now_ts or time.time()
    now_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    expected = now_utc.replace(
        hour=DAILY_REPORT_EXPECTED_HOUR_UTC,
        minute=DAILY_REPORT_EXPECTED_MINUTE_UTC,
        second=0,
        microsecond=0,
    )
    if now_utc.timestamp() < expected.timestamp() + DAILY_REPORT_GRACE_SECONDS:
        return
    report_date = now_utc.date().isoformat()
    if _daily_report_file_exists(report_date):
        state.pop(f"dailyreport:{report_date}", None)
        return
    health = _report_health()
    if _health_has_daily_publication(health, report_date):
        state.pop(f"dailyreport:{report_date}", None)
        return
    publication = health.get("last_publication")
    if not isinstance(publication, dict):
        publication = {}
    text = (
        f"🔴 日报缺失:{report_date} 的 daily report 在预期生成点 "
        f"{DAILY_REPORT_EXPECTED_HOUR_UTC:02d}:"
        f"{DAILY_REPORT_EXPECTED_MINUTE_UTC:02d} UTC 后 90 分钟仍未出现。"
        f"healthz last_publication={publication.get('status', 'unavailable')}/"
        f"{publication.get('report_date', '')}。"
        "请检查日报调度、report service 和 publication 错误。"
    )
    _send_deduplicated_alert(
        state,
        f"dailyreport:{report_date}",
        text,
        dry_run,
        now_ts,
    )


def sweep_ttl(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    now_ts = now_ts or time.time()
    rows = q(
        "SELECT client_order_id, account_id, instrument_id, "
        "EXTRACT(EPOCH FROM (now() - ts_event))/3600.0 "
        "FROM orders_projection WHERE status IN ('accepted','partially_filled')"
    )
    exchange_ids = _exchange_open_order_ids()
    uuids = sorted({u for u in (intent_uuid_of(r[0]) for r in rows) if u})
    contexts = _load_intent_contexts(set(uuids))
    for cid, account_id, instrument_id, age_h in rows:
        try:
            live_ids = exchange_ids.get(account_id)
            if live_ids is not None and cid not in live_ids and float(age_h) > 1.0:
                # The mirror is fresh and the exchange has no such order: this
                # ledger row is a ghost. Heal it instead of waking Hermes to
                # cancel a nonexistent order (denied:order_not_found loop).
                heal_ghost_order(cid, account_id, dry_run)
                continue
            renew_key = f"ttl:{cid}"
            entry = state.get(renew_key) or {}
            context = contexts.get(intent_uuid_of(cid) or "", {})
            intent_action = str(context.get("action") or "")
            decision = ttl_decision(cid, float(age_h), int(entry.get("renewals", 0)),
                                    intent_action=intent_action)
            if decision == "skip":
                continue
            woken_key = f"ttlwake:{cid}:{int(entry.get('renewals', 0))}"
            last_wake = float(state.get(woken_key) or 0)
            if last_wake:
                # exhausted stage must not fail silently (node HALTED, Hermes
                # error): while the order is still alive, re-ask every 2h.
                if decision != "exhausted" or now_ts - last_wake < EXHAUSTED_REWAKE_SECONDS:
                    continue
            symbol = instrument_id.split("-")[0]
            authorization = context.get("authorization")
            if not _has_complete_authorization(authorization):
                _send_read_only_alert(
                    state,
                    f"authalert:ttl:{cid}",
                    (
                        f"订单生命周期告警:{account_id} 的 {symbol} 挂单 {cid} "
                        f"已超出 {ORDER_TTL_HOURS:.0f} 小时有效期，"
                        "缺少完整用户或频道父授权，需人工确认处理"
                    ),
                    dry_run,
                    now_ts,
                )
                continue
            stage = "已续期一次后再次超龄,不可再续,请直接撤销" if decision == "exhausted" else \
                    "默认应撤销;仅当你判断原信号仍有效时可续期一次(在回复里明确说明'续期')"
            prompt = (
                f"订单生命周期管理:系统挂单 {cid}({symbol},挂出已 {float(age_h):.1f} 小时,"
                f"超过 {ORDER_TTL_HOURS:.0f} 小时有效期)。{stage}。\n"
                f"撤销命令: python3 /srv/hermes/profiles/trader/skills/trading/v3-trader/scripts/v3_trade.py "
                f"cancel {symbol} --account {account_id} --order {cid} "
                f"--reason '48h超龄撤单' --ref ttl-{cid[-8:]}\n"
                f"最终用口语化短消息(1~3行)告知用户你的决定和依据。"
            )
            if wake_authorized_management(
                prompt,
                name=f"ttl-{cid[-8:]}",
                dry_run=dry_run,
                authorization=authorization,
            ) and not dry_run:
                state[woken_key] = now_ts
                if decision == "wake":
                    # Hermes may cancel (order disappears) or keep it (= renewal).
                    # Either way this order has consumed its one renewal window.
                    state[renew_key] = {"renewals": int(entry.get("renewals", 0)) + 1}
        except Exception as exc:  # noqa: BLE001
            log(f"ttl sweep row {cid} failed: {exc!r}")


def protection_level_from_plan(order_plan: dict, action: str, seq: int):
    """Resolve the SL/TP price a protection order was placed at, from its
    intent's order_plan (orders_projection stores no prices). Entry intents use
    the revision scheme (block position 1 = SL, 2.. = TP tiers); management
    intents use seq 1..n directly. Returns float or None."""
    try:
        if seq >= 11:  # revision scheme
            pos = seq % 10
            if pos == 1:
                value = order_plan.get("stop_loss")
                return float(value) if value is not None else None
            tps = order_plan.get("take_profits") or []
            idx = pos - 2
        elif action in ("move_stop_loss", "move_stop_to_entry"):
            value = order_plan.get("stop_loss") or order_plan.get("stop_price")
            return float(value) if value is not None else None
        elif action == "replace_take_profits":
            tps = order_plan.get("take_profits") or []
            idx = seq - 1
        else:
            return None
        if not 0 <= idx < len(tps):
            return None
        tier = tps[idx]
        value = tier.get("price") if isinstance(tier, dict) else tier
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _live_protection_rows() -> list[dict]:
    rows = q(
        "SELECT o.client_order_id, o.account_id, o.instrument_id "
        "FROM orders_projection o "
        "WHERE o.status IN ('accepted','partially_filled')"
    )
    candidates = []
    for cid, account_id, instrument_id in rows:
        seq = id_sequence(cid)
        if seq is None:
            continue
        candidates.append((cid, account_id, instrument_id, seq))
    uuids = sorted({intent_uuid_of(c[0]) for c in candidates if intent_uuid_of(c[0])})
    contexts = _load_intent_contexts(set(uuids))
    result = []
    for cid, account_id, instrument_id, seq in candidates:
        context = contexts.get(intent_uuid_of(cid) or "")
        if context is None:
            continue
        action = context["action"]
        plan = context["order_plan"]
        # protection roles: revision-scheme ids, or management-intent SL/TP ids
        if seq < 11 and action not in ("move_stop_loss", "move_stop_to_entry",
                                       "replace_take_profits"):
            continue
        price = protection_level_from_plan(plan, action, seq)
        if price is None:
            continue
        pos = seq % 10 if seq >= 11 else seq
        is_sl = (seq >= 11 and pos == 1) or (
            seq < 11 and action in ("move_stop_loss", "move_stop_to_entry")
        )
        result.append({
            "client_order_id": cid,
            "account_id": account_id,
            "symbol": instrument_id.split("-")[0],
            "price": price,
            "is_stop_loss": is_sl,
            "authorization": context.get("authorization"),
        })
    return result


def sweep_price_alerts(state: dict, dry_run: bool, fetch=None, now_ts: float | None = None) -> None:
    now_ts = now_ts or time.time()
    orders = _live_protection_rows()
    live_ids = {o["client_order_id"] for o in orders}
    prune_state(state, live_ids, now_ts)
    # deregister: drop alert dedup keys for orders that no longer exist
    for key in [k for k in state if k.startswith("alert:")]:
        cid = key.split(":", 2)[1]
        if cid not in live_ids:
            state.pop(key, None)
    by_symbol: dict[str, list[dict]] = {}
    for order in orders:
        by_symbol.setdefault(order["symbol"], []).append(order)
    for symbol, symbol_orders in by_symbol.items():
        mark = mark_price(symbol, fetch=fetch)
        fail_key = f"markfail:{symbol}"
        if mark is None:
            fails = int(state.get(fail_key, 0)) + 1
            state[fail_key] = fails
            if fails == MARK_FAIL_ALERT_THRESHOLD:
                wake_hermes(
                    f"价格监控数据源异常:{symbol} 行情连续 {fails} 次拉取失败,"
                    f"接近/触及类价格告警暂时失效(保护单成交通知不受影响)。"
                    f"请用一句话告知用户留意。",
                    name=f"markfail-{symbol}", dry_run=dry_run,
                )
            continue
        state.pop(fail_key, None)
        for order in symbol_orders:
            for event in alert_events(order, mark, now_ts, state):
                level_cn = "已接近" if event["level"] == "approach" else "已触及"
                authorization = order.get("authorization")
                if not _has_complete_authorization(authorization):
                    _send_read_only_alert(
                        state,
                        f"authalert:{event['key']}",
                        (
                            f"价格监控告警:{order.get('account_id') or '?'} 的 {symbol} "
                            f"现价 {mark}，{level_cn}保护单价位 {order['price']}，"
                            "该保护单缺少完整用户或频道父授权"
                        ),
                        dry_run,
                        now_ts,
                    )
                    continue
                prompt = (
                    f"价格监控告警:{symbol} 现价 {mark},{level_cn}保护单价位 "
                    f"{order['price']}(订单 {order['client_order_id'][-8:]})。\n"
                    f"请查询该品种当前持仓与挂单状态,按原信号计划判断是否需要订单管理"
                    f"(减仓/移动止损/等待),并用口语化短消息(2~3行)把现状和你的动作告知用户。"
                )
                if wake_authorized_management(
                    prompt,
                    name=f"palert-{order['client_order_id'][-8:]}-{event['level']}",
                    dry_run=dry_run,
                    authorization=authorization,
                ) and not dry_run:
                    state[event["key"]] = now_ts


def sweep_fill_alerts(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    """A protection order FILLING is the ground-truth 'TP/SL level hit' event:
    notify immediately (per-order dedupe), independent of price polling."""
    now_ts = now_ts or time.time()
    rows = q(
        "SELECT client_order_id, account_id, payload->>'instrument_id', "
        "payload->>'last_qty', payload->>'last_px' "
        "FROM execution_events "
        "WHERE event_type='OrderFilled' AND ts_event > now() - interval '15 minutes' "
        "AND client_order_id IS NOT NULL"
    )
    intent_ids = {
        intent_id
        for intent_id in (intent_uuid_of(row[0]) for row in rows)
        if intent_id
    }
    contexts = _load_intent_contexts(intent_ids)
    for cid, account_id, instrument_id, last_qty, last_px in rows:
        try:
            if not is_protection_order(cid):
                continue
            key = f"fill:{cid}"
            if state.get(key):
                continue
            symbol = (instrument_id or "").split("-")[0] or "?"
            context = contexts.get(intent_uuid_of(cid) or "", {})
            authorization = context.get("authorization")
            if not _has_complete_authorization(authorization):
                _send_read_only_alert(
                    state,
                    f"authalert:{key}",
                    (
                        f"保护单成交告警:{account_id} 的 {symbol} 保护单 {cid} "
                        f"刚成交 {last_qty} @ {last_px}，"
                        "该保护单缺少完整用户或频道父授权"
                    ),
                    dry_run,
                    now_ts,
                )
                continue
            prompt = (
                f"保护单成交通知:{symbol} 的止损/止盈单(单号 {cid[-8:]})刚成交 "
                f"{last_qty} @ {last_px}。请查询该品种当前持仓与剩余挂单,"
                f"判断是否需要后续订单管理(例如剩余仓位的止损上移),"
                f"并用口语化短消息(2~3行)把成交与现状告知用户。"
            )
            if wake_authorized_management(
                prompt,
                name=f"pfill-{cid[-8:]}",
                dry_run=dry_run,
                authorization=authorization,
            ) and not dry_run:
                state[key] = now_ts
        except Exception as exc:  # noqa: BLE001
            log(f"fill sweep row {cid} failed: {exc!r}")


PositionKey = tuple[str, str, str]


def naked_positions(position_keys: set[PositionKey], sl_keys: set[PositionKey],
                    recent_fill_keys: set[PositionKey], now_ts: float,
                    state: dict) -> list[PositionKey]:
    """Pure: open exchange positions lacking full live stop-loss coverage."""
    out: list[PositionKey] = []
    for account_id, symbol, position_side in sorted(position_keys):
        position_key = (account_id, symbol, position_side)
        if position_key in sl_keys or position_key in recent_fill_keys:
            continue
        key = f"naked:{account_id}:{symbol}:{position_side}"
        if key in state and now_ts - float(state.get(key, 0)) < NAKED_DEDUP_SECONDS:
            continue
        out.append(position_key)
    return out


def sweep_naked(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    now_ts = now_ts or time.time()
    snapshots = _exchange_state_snapshots()
    position_quantities: dict[PositionKey, float] = {}
    stop_keys: set[PositionKey] = set()
    for account_id, snapshot in snapshots.items():
        if not snapshot.get("fresh"):
            age = float(snapshot.get("age") or 0)
            log(f"naked sweep: exchange mirror stale for {account_id} ({age:.0f}s), skipping account")
            continue
        account_positions: dict[PositionKey, float] = {}
        for position in snapshot.get("positions") or []:
            if not isinstance(position, dict):
                continue
            symbol = _normalize_symbol(
                position.get("instrument_id") or position.get("symbol")
            )
            raw_quantity = (
                position.get("position_amt")
                if "position_amt" in position
                else position.get("positionAmt")
            )
            if raw_quantity is None:
                raw_quantity = position.get("quantity")
            try:
                quantity = abs(float(raw_quantity))
            except (TypeError, ValueError):
                continue
            if not symbol or not math.isfinite(quantity) or quantity <= 0:
                continue
            side_value = position.get("position_side")
            if side_value is None:
                side_value = position.get("positionSide")
            position_side = _position_side(side_value, raw_quantity)
            if position_side is None:
                continue
            key = (account_id, symbol, position_side)
            account_positions[key] = account_positions.get(key, 0.0) + quantity
        position_quantities.update(account_positions)
        orders = list(snapshot.get("open_orders") or [])
        orders.extend(snapshot.get("algo_orders") or [])
        stop_keys |= snapshot_stop_loss_symbols(
            orders,
            account_positions,
            account_id=account_id,
        )
    position_keys = set(position_quantities)
    if not position_keys:
        return
    recent_fill_keys: set[PositionKey] = set()
    for account_id, instrument_id, side_raw in q(
        "SELECT DISTINCT account_id, payload->>'instrument_id', "
        "COALESCE(payload->>'position_side', payload->>'positionSide', '') "
        "FROM execution_events "
        f"WHERE event_type='OrderFilled' AND ts_event > now() - interval '{NAKED_GRACE_SECONDS} seconds'"
    ):
        symbol = _normalize_symbol(instrument_id)
        position_side = _position_side(side_raw)
        if symbol and position_side:
            recent_fill_keys.add((account_id, symbol, position_side))
    naked_keys = naked_positions(
        position_keys,
        stop_keys,
        recent_fill_keys,
        now_ts,
        state,
    )
    authorization_lookup_keys: set[PositionKey] = set()
    for key in naked_keys:
        alert_key = f"authalert:naked:{key[0]}:{key[1]}:{key[2]}"
        if alert_key in state:
            last_alert = float(state.get(alert_key) or 0)
            if now_ts - last_alert < ALERT_DEDUP_SECONDS:
                continue
        authorization_lookup_keys.add(key)
    authorizations = _position_parent_authorizations(authorization_lookup_keys)
    for account_id, symbol, position_side in naked_keys:
        position_key = (account_id, symbol, position_side)
        if position_key not in authorization_lookup_keys:
            continue
        authorization = authorizations.get((account_id, symbol, position_side))
        if not _has_complete_authorization(authorization):
            _send_read_only_alert(
                state,
                f"authalert:naked:{account_id}:{symbol}:{position_side}",
                (
                    f"裸仓告警:{account_id} 的 {symbol} {position_side} 仓位 "
                    f"{position_quantities[(account_id, symbol, position_side)]:g} "
                    "缺少足量有效止损，同时缺少完整用户或频道父授权，"
                    "需人工核对原始指令与交易所挂单"
                ),
                dry_run,
                now_ts,
            )
            continue
        prompt = (
            f"⚠️ 裸仓检测:{account_id} 的 {symbol} {position_side} 仓位 "
            f"{position_quantities[(account_id, symbol, position_side)]:g} "
            "没有足量且有效的在场 STOP 止损单。"
            f"请立刻查询该品种持仓与挂单。若确认裸仓,调用 v3_trade.py "
            f"set-sl {symbol} --account {account_id} --side {position_side} 时,"
            f"按最近相关信号的止损价补上"
            f"(找不到依据就通知用户手动处理),并用口语化短消息告知用户现状与你的动作。"
        )
        name = f"naked-{account_id}-{symbol}-{position_side}"
        if wake_authorized_management(
            prompt,
            name=name,
            dry_run=dry_run,
            authorization=authorization,
        ) and not dry_run:
            state[f"naked:{account_id}:{symbol}:{position_side}"] = now_ts


PENDING_CANCEL_TERMINALS = {
    "OrderCanceled",
    "CancelRejected",
    "OrderCancelRejected",
    "OrderFilled",
}


def _pending_cancel_rows() -> list[list[str]]:
    terminal_types = "','".join(sorted(PENDING_CANCEL_TERMINALS))
    return q(
        "WITH latest_pending AS ("
        " SELECT DISTINCT ON (account_id, client_order_id)"
        " account_id, client_order_id, event_id, ts_event"
        " FROM execution_events"
        " WHERE event_type='OrderPendingCancel' AND client_order_id IS NOT NULL"
        f" AND ts_event > now() - interval '{PENDING_CANCEL_LOOKBACK_HOURS} hours'"
        " ORDER BY account_id, client_order_id, ts_event DESC, event_id DESC"
        ")"
        " SELECT p.account_id, p.client_order_id, p.event_id,"
        " EXTRACT(EPOCH FROM p.ts_event),"
        " COALESCE(("
        "  SELECT e.event_type FROM execution_events e"
        "  WHERE e.account_id=p.account_id"
        "  AND e.client_order_id=p.client_order_id"
        "  AND e.ts_event >= p.ts_event"
        f"  AND e.event_type IN ('{terminal_types}')"
        "  ORDER BY e.ts_event DESC, e.event_id DESC LIMIT 1"
        " ), '')"
        " FROM latest_pending p"
    )


def _snapshot_open_order_ids(snapshot: dict) -> set[str]:
    ids: set[str] = set()
    orders = list(snapshot.get("open_orders") or [])
    orders.extend(snapshot.get("algo_orders") or [])
    for order in orders:
        if not isinstance(order, dict):
            continue
        cid = str(
            order.get("client_order_id") or order.get("clientOrderId") or ""
        )
        if cid:
            ids.add(cid)
    return ids


def _pending_cancel_alert_text(
    account_id: str,
    client_order_id: str,
    elapsed_seconds: float,
    outcome: str,
) -> str:
    minutes = elapsed_seconds / 60
    if outcome == "filled":
        truth = "撤单竞态中订单已成交"
    elif outcome == "still_open":
        truth = "交易所镜像显示订单仍挂着"
    else:
        truth = "交易所镜像显示订单已消失，事件流仍缺少撤单终态"
    return (
        f"⚠️ 撤单确认超时:{account_id} 订单 {client_order_id} PendingCancel "
        f"已 {minutes:.1f} 分钟；{truth}。监控仅告警，未撤单、未重试。"
    )


def sweep_pending_cancels(
    state: dict,
    dry_run: bool,
    now_ts: float | None = None,
) -> None:
    """Persist and classify cancel requests whose terminal event is missing."""
    now_ts = now_ts or time.time()
    timeout_seconds = PENDING_CANCEL_TIMEOUT_MINUTES * 60
    records: dict[str, dict] = {}
    for account_id, cid, event_id, pending_raw, terminal_type in _pending_cancel_rows():
        try:
            pending_at = float(pending_raw)
        except (TypeError, ValueError):
            continue
        key = f"pendingcancel:{account_id}:{cid}"
        entry = state.get(key)
        if not isinstance(entry, dict) or entry.get("event_id") != event_id:
            entry = {
                "account_id": account_id,
                "client_order_id": cid,
                "event_id": event_id,
                "pending_at": pending_at,
            }
            state[key] = entry
        else:
            entry.setdefault("account_id", account_id)
            entry.setdefault("client_order_id", cid)
            entry.setdefault("pending_at", pending_at)
        records[key] = {
            "entry": entry,
            "terminal_type": str(terminal_type or ""),
        }

    for key, value in list(state.items()):
        if not key.startswith("pendingcancel:") or key in records:
            continue
        if not isinstance(value, dict):
            state.pop(key, None)
            continue
        if value.get("resolved"):
            state.pop(key, None)
            continue
        account_id = str(value.get("account_id") or "")
        cid = str(value.get("client_order_id") or "")
        if account_id and cid:
            # DB 查询窗口只负责发现新事件。未解决台账必须持续参与 fresh
            # 镜像判定,否则 stale/查询失败会让 48h 淘汰时钟静默删除活风险。
            records[key] = {"entry": value, "terminal_type": ""}
        else:
            state.pop(key, None)

    overdue: list[tuple[str, dict, str]] = []
    for key, record in records.items():
        entry = record["entry"]
        terminal_type = record["terminal_type"]
        elapsed = now_ts - float(entry.get("pending_at") or now_ts)
        if entry.get("resolved"):
            continue
        if terminal_type in ("OrderCanceled", "CancelRejected", "OrderCancelRejected"):
            state.pop(key, None)
            continue
        if terminal_type == "OrderFilled" and elapsed < timeout_seconds:
            state.pop(key, None)
            continue
        if elapsed < timeout_seconds:
            continue
        overdue.append((key, entry, terminal_type))

    if not overdue:
        return
    try:
        snapshots = _exchange_state_snapshots()
    except Exception as exc:  # noqa: BLE001
        log(f"pending cancel mirror read failed: {exc!r}")
        return
    for key, entry, terminal_type in overdue:
        account_id = str(entry["account_id"])
        cid = str(entry["client_order_id"])
        snapshot = snapshots.get(account_id)
        if not snapshot or not snapshot.get("fresh"):
            log(f"pending cancel {account_id}/{cid}: fresh exchange mirror unavailable")
            continue
        if terminal_type == "OrderFilled":
            outcome = "filled"
        elif cid in _snapshot_open_order_ids(snapshot):
            outcome = "still_open"
        else:
            outcome = "disappeared"
        last_outcome = str(entry.get("last_outcome") or "")
        last_alert_at = float(entry.get("last_alert_at") or 0)
        if last_outcome == outcome and now_ts - last_alert_at < ALERT_DEDUP_SECONDS:
            continue
        elapsed = now_ts - float(entry["pending_at"])
        text = _pending_cancel_alert_text(account_id, cid, elapsed, outcome)
        if dry_run:
            log(f"DRY-RUN would send pending cancel alert: {text}")
            continue
        if tg_send_direct(text):
            entry["last_outcome"] = outcome
            entry["last_alert_at"] = now_ts
            # filled/disappeared 都已尘埃落定(单子不在交易所了),缺的只是事件流终态,
            # 重播无人可行动。仍挂着的 still_open 才是活风险,保持每日重播。
            if outcome in ("filled", "disappeared"):
                entry["resolved"] = True


def sweep_reconcile(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    now_ts = now_ts or time.time()
    projection = [
        {"client_order_id": cid, "instrument_id": iid, "status": status}
        for cid, iid, status in q(
            "SELECT client_order_id, instrument_id, status FROM orders_projection "
            "WHERE status IN ('accepted','partially_filled')"
        )
    ]
    snapshot_orders, snapshot_age = _heartbeat_open_order_snapshot()
    if snapshot_orders is None:
        if snapshot_age is None:
            log("reconcile skipped: no heartbeat open_orders snapshot")
        else:
            log(f"reconcile skipped: open_orders snapshot stale ({snapshot_age:.0f}s)")
        return
    diff = snapshot_reconcile_differences(
        projection, snapshot_orders, now_ts, state, snapshot_age_seconds=snapshot_age
    )
    if diff.get("stale"):
        log(f"reconcile skipped: open_orders snapshot stale ({snapshot_age:.0f}s)")
        return
    ghosts = diff["ghosts"]
    missing = diff["missing"]
    if not ghosts and not missing:
        return
    ghost_lines = "\n".join(
        f"- 幽灵 {s['row']['client_order_id']} ({s['row'].get('instrument_id')}, 账本状态 {s['row'].get('status')})"
        for s in ghosts[:10]
    )
    missing_lines = "\n".join(
        f"- 漏记 {s['row']['client_order_id']} ({s['row'].get('instrument_id') or s['row'].get('symbol')})"
        for s in missing[:10]
    )
    sections = [part for part in (ghost_lines, missing_lines) if part]
    prompt = (
        f"对账告警:心跳真实挂单快照与系统订单账本不一致 "
        f"(幽灵 {len(ghosts)} 笔, 漏记 {len(missing)} 笔):\n"
        + "\n".join(sections)
        + "\n请用口语化短消息告知用户,并提醒他在交易所 App 核对这些单是否真实存在。不要执行任何撤单。"
    )
    if wake_hermes(prompt, name=f"recon-{int(now_ts)}", dry_run=dry_run) and not dry_run:
        for s in ghosts + missing:
            state[s["key"]] = now_ts


# ---------------------------------------------------------------- main ----

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state = load_state()
    last_ttl = 0.0
    last_recon = 0.0
    last_brain = 0.0
    last_pending_cancel = 0.0
    log(
        f"lifecycle monitor start ttl={ORDER_TTL_HOURS}h "
        f"pending_cancel={PENDING_CANCEL_TIMEOUT_MINUTES:g}m dry_run={args.dry_run}"
    )
    while True:
        now_ts = time.time()
        for name, fn, due in (
            ("nodes", sweep_node_health, True),
            ("intent-stalls", sweep_intent_stalls, True),
            ("protection-events", sweep_protection_events, True),
            ("daily-report", sweep_daily_report, True),
            ("price", sweep_price_alerts, True),
            ("fills", sweep_fill_alerts, True),
            ("naked", sweep_naked, True),
            (
                "pending-cancel",
                sweep_pending_cancels,
                now_ts - last_pending_cancel >= PENDING_CANCEL_SWEEP_SECONDS,
            ),
            ("brain", sweep_brain, now_ts - last_brain >= BRAIN_PROBE_SECONDS),
            ("ttl", sweep_ttl, now_ts - last_ttl >= TTL_SWEEP_SECONDS),
            ("recon", sweep_reconcile, now_ts - last_recon >= RECON_SWEEP_SECONDS),
        ):
            if not due:
                continue
            try:
                fn(state, args.dry_run)
                if name == "ttl":
                    last_ttl = now_ts
                elif name == "recon":
                    last_recon = now_ts
                elif name == "brain":
                    last_brain = now_ts
                elif name == "pending-cancel":
                    last_pending_cancel = now_ts
            except Exception as exc:  # noqa: BLE001
                log(f"{name} sweep failed (continuing): {exc!r}")
        if not args.dry_run:
            try:
                save_state(state)
            except OSError as exc:
                log(f"state save failed: {exc!r}")
        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
