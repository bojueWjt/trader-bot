#!/usr/bin/env python3
"""Order lifecycle monitor for trader-v3 (hk).

Three read-only sweeps, all actions delegated to Hermes jobs / operator flows —
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

State lives in STATE_PATH (atomic JSON). Any exception in a sweep is logged
and skipped — one bad row must never kill the service (feeder P1-1 lesson).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

import hermes_signal_feeder as feeder  # run_hermes + channel context helpers

PSQL = ["docker", "exec", "trader-v3-postgres", "psql", "-U", "postgres", "-d", "trader", "-Atc"]
STATE_PATH = "/srv/trader-v3/scripts/.order_lifecycle_state.json"
MARK_URL = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"

POLL_SECONDS = 15
TTL_SWEEP_SECONDS = 600
RECON_SWEEP_SECONDS = 3600
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
BRAIN_PROBE_SECONDS = 600
BRAIN_FAIL_ALERT_AFTER = 2
HERMES_ENV_FILE = "/srv/hermes/profiles/trader/.env"
TG_CHAT_ID = "8545234287"
BRAIN_PROBES = (
    # (name, url, key_env, style, model)
    ("primary", "https://api.balenw.cloud/v1/chat/completions", "CLIPROXYAPI_API_KEY",
     "openai", "gpt-5.6-sol"),
    ("fallback", "https://open.bigmodel.cn/api/anthropic/v1/messages", "BIGMODEL_API_KEY",
     "anthropic", "glm-5.2"),
)
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


def snapshot_stop_loss_symbols(snapshot_orders: list[dict], position_sides: dict[str, str]) -> set[str]:
    """Symbols whose fresh exchange snapshot contains a stop-like closing order."""
    out: set[str] = set()
    for order in snapshot_orders:
        symbol = _normalize_symbol(order.get("instrument_id") or order.get("symbol"))
        if not symbol or symbol not in position_sides:
            continue
        order_type = str(order.get("order_type") or order.get("type") or "").upper()
        reduce_only = _boolish(order.get("reduce_only"))
        if not reduce_only and "STOP" not in order_type:
            continue
        pos_side = str(position_sides.get(symbol) or "").lower()
        order_side = _normalize_order_side(order.get("side"))
        closes_long = pos_side == "long" and order_side in ("short", None)
        closes_short = pos_side == "short" and order_side in ("long", None)
        if closes_long or closes_short:
            out.add(symbol)
    return out


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
        rows = q(
            "SELECT account_id, EXTRACT(EPOCH FROM (now() - updated_at)), "
            "COALESCE(payload->'open_orders','[]'::jsonb)::text "
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
        if parts[0] not in ("alert", "fill", "recon", "ttlwake"):
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
        return ("🚨 交易大脑双通道全部断供(主 gpt-5.5 与备胎 glm-5.2 均探活失败)。"
                "信号将进入重试队列不会丢,但暂时无人决策——请尽快检查模型通道。")
    key = "brainalert:primary"
    if now_ts - float(state.get(key, 0)) < 3600:
        return None
    state[key] = now_ts
    return ("⚠️ 交易大脑主通道(gpt-5.5)探活连续失败,已由备胎 glm-5.2 承接。"
            "决策质量可能略降,建议尽快检查 new-api 的 codex 通道。")


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


def sweep_ttl(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    now_ts = now_ts or time.time()
    rows = q(
        "SELECT client_order_id, account_id, instrument_id, "
        "EXTRACT(EPOCH FROM (now() - ts_event))/3600.0 "
        "FROM orders_projection WHERE status IN ('accepted','partially_filled')"
    )
    exchange_ids = _exchange_open_order_ids()
    uuids = sorted({u for u in (intent_uuid_of(r[0]) for r in rows) if u})
    actions: dict[str, str] = {}
    if uuids:
        placeholders = ",".join(f"'{u}'" for u in uuids)
        for iid_, act_ in q(
            f"SELECT intent_id::text, action::text FROM trade_intents "
            f"WHERE intent_id::text IN ({placeholders})"
        ):
            actions[iid_] = act_
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
            intent_action = actions.get(intent_uuid_of(cid) or "", "")
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
            stage = "已续期一次后再次超龄,不可再续,请直接撤销" if decision == "exhausted" else \
                    "默认应撤销;仅当你判断原信号仍有效时可续期一次(在回复里明确说明'续期')"
            prompt = (
                f"订单生命周期管理:系统挂单 {cid}({symbol},挂出已 {float(age_h):.1f} 小时,"
                f"超过 {ORDER_TTL_HOURS:.0f} 小时有效期)。{stage}。\n"
                f"撤销命令: python3 /srv/hermes/profiles/trader/skills/trading/v3-trader/scripts/v3_trade.py "
                f"cancel {symbol} --order {cid} --reason '48h超龄撤单' --ref ttl-{cid[-8:]}\n"
                f"最终用口语化短消息(1~3行)告知用户你的决定和依据。"
            )
            if wake_hermes(prompt, name=f"ttl-{cid[-8:]}", dry_run=dry_run) and not dry_run:
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
        "SELECT o.client_order_id, o.instrument_id FROM orders_projection o "
        "WHERE o.status IN ('accepted','partially_filled')"
    )
    candidates = []
    for cid, instrument_id in rows:
        seq = id_sequence(cid)
        if seq is None:
            continue
        candidates.append((cid, instrument_id, seq))
    uuids = sorted({intent_uuid_of(c[0]) for c in candidates if intent_uuid_of(c[0])})
    plans: dict[str, tuple[str, dict]] = {}
    if uuids:
        placeholders = ",".join(f"'{u}'" for u in uuids)
        for iid_, act_, plan_json in q(
            f"SELECT intent_id::text, action::text, order_plan::text FROM trade_intents "
            f"WHERE intent_id::text IN ({placeholders})"
        ):
            try:
                plans[iid_] = (act_, json.loads(plan_json))
            except ValueError:
                continue
    result = []
    for cid, instrument_id, seq in candidates:
        act_plan = plans.get(intent_uuid_of(cid) or "")
        if act_plan is None:
            continue
        action, plan = act_plan
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
            "symbol": instrument_id.split("-")[0],
            "price": price,
            "is_stop_loss": is_sl,
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
                prompt = (
                    f"价格监控告警:{symbol} 现价 {mark},{level_cn}保护单价位 "
                    f"{order['price']}(订单 {order['client_order_id'][-8:]})。\n"
                    f"请查询该品种当前持仓与挂单状态,按原信号计划判断是否需要订单管理"
                    f"(减仓/移动止损/等待),并用口语化短消息(2~3行)把现状和你的动作告知用户。"
                )
                if wake_hermes(prompt, name=f"palert-{order['client_order_id'][-8:]}-{event['level']}",
                               dry_run=dry_run) and not dry_run:
                    state[event["key"]] = now_ts


def sweep_fill_alerts(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    """A protection order FILLING is the ground-truth 'TP/SL level hit' event:
    notify immediately (per-order dedupe), independent of price polling."""
    now_ts = now_ts or time.time()
    rows = q(
        "SELECT client_order_id, payload->>'instrument_id', payload->>'last_qty', "
        "payload->>'last_px' FROM execution_events "
        "WHERE event_type='OrderFilled' AND ts_event > now() - interval '15 minutes' "
        "AND client_order_id IS NOT NULL"
    )
    for cid, instrument_id, last_qty, last_px in rows:
        try:
            if not is_protection_order(cid):
                continue
            key = f"fill:{cid}"
            if state.get(key):
                continue
            symbol = (instrument_id or "").split("-")[0] or "?"
            prompt = (
                f"保护单成交通知:{symbol} 的止损/止盈单(单号 {cid[-8:]})刚成交 "
                f"{last_qty} @ {last_px}。请查询该品种当前持仓与剩余挂单,"
                f"判断是否需要后续订单管理(例如剩余仓位的止损上移),"
                f"并用口语化短消息(2~3行)把成交与现状告知用户。"
            )
            if wake_hermes(prompt, name=f"pfill-{cid[-8:]}", dry_run=dry_run) and not dry_run:
                state[key] = now_ts
        except Exception as exc:  # noqa: BLE001
            log(f"fill sweep row {cid} failed: {exc!r}")


def naked_positions(position_symbols: set[str], sl_symbols: set[str],
                    recent_fill_symbols: set[str], now_ts: float,
                    state: dict) -> list[str]:
    """Pure: open positions with NO live stop-loss order (the 2026-07-07 naked
    ETH incident class). Symbols with a fill in the grace window are skipped —
    protections may legitimately still be in flight."""
    out = []
    for symbol in sorted(position_symbols):
        if symbol in sl_symbols or symbol in recent_fill_symbols:
            continue
        key = f"naked:{symbol}"
        if now_ts - float(state.get(key, 0)) < NAKED_DEDUP_SECONDS:
            continue
        out.append(symbol)
    return out


def sweep_naked(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    now_ts = now_ts or time.time()
    position_sides = {
        row[0].split("-")[0]: str(row[1] or "").lower()
        for row in q(
            "SELECT DISTINCT instrument_id, side::text FROM positions_projection "
            "WHERE status='open' AND quantity::numeric != 0"
        )
    }
    position_symbols = set(position_sides)
    if not position_symbols:
        return
    sl_symbols = {o["symbol"] for o in _live_protection_rows() if o.get("is_stop_loss")}
    snapshot_orders, snapshot_age = _heartbeat_open_order_snapshot()
    if snapshot_orders is not None:
        sl_symbols |= snapshot_stop_loss_symbols(snapshot_orders, position_sides)
    elif snapshot_age is not None and snapshot_age > ORDER_SNAPSHOT_MAX_AGE_SECONDS:
        log(f"naked sweep: open_orders snapshot stale ({snapshot_age:.0f}s), using system orders only")
    recent_fill_symbols = {
        (row[0] or "").split("-")[0]
        for row in q(
            "SELECT DISTINCT payload->>'instrument_id' FROM execution_events "
            f"WHERE event_type='OrderFilled' AND ts_event > now() - interval '{NAKED_GRACE_SECONDS} seconds'"
        )
    }
    for symbol in naked_positions(position_symbols, sl_symbols, recent_fill_symbols, now_ts, state):
        prompt = (
            f"⚠️ 裸仓检测:{symbol} 有未平仓位但系统里没有任何在场的止损单。"
            f"请立刻查询该品种持仓与挂单,若确认裸仓,按最近相关信号的止损价用 set-sl 补上"
            f"(找不到依据就通知用户手动处理),并用口语化短消息告知用户现状与你的动作。"
        )
        if wake_hermes(prompt, name=f"naked-{symbol}", dry_run=dry_run) and not dry_run:
            state[f"naked:{symbol}"] = now_ts


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
    log(f"lifecycle monitor start ttl={ORDER_TTL_HOURS}h dry_run={args.dry_run}")
    while True:
        now_ts = time.time()
        for name, fn, due in (
            ("price", sweep_price_alerts, True),
            ("fills", sweep_fill_alerts, True),
            ("naked", sweep_naked, True),
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
