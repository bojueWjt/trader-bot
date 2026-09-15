#!/usr/bin/env python3
"""Read-only lifecycle anomaly notifications for trader-v3.

Only robot-owned orders (^B[0-9a-f]{32}[0-9]{2}$) are monitored. Exchange mirror
absence is a discrepancy, never a fabricated cancel/fill. Protection lifecycle
belongs to the node; fills belong to trade_event_notifier. This monitor neither
launches agents/jobs nor submits, cancels, renews, or writes trading projections.
Ordinary GTC entry orders older than 48 hours only trigger a direct notification;
there is no automatic 48-hour cancellation or signal renewal policy here.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

PSQL = ["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on",
        "trader-v3-postgres", "psql", "-U", "postgres", "-d", "trader", "-Atc"]
STATE_PATH = "/srv/trader-v3/scripts/.order_lifecycle_state.json"
POLL_SECONDS = 15
TTL_SWEEP_SECONDS = 600
RECON_SWEEP_SECONDS = 3600
PENDING_CANCEL_SWEEP_SECONDS = 60
INTENT_STALL_SECONDS = float(os.environ.get("INTENT_STALL_SECONDS", "300"))
MIRROR_MAX_AGE_SECONDS = 300
HEARTBEAT_STALE_SECONDS = 300
ORDER_TTL_HOURS = float(os.environ.get("ORDER_TTL_HOURS", "48"))
ALERT_DEDUP_SECONDS = 24 * 3600
STATE_PRUNE_AGE_SECONDS = 7 * 24 * 3600
OPERATOR_HALT_SUPPRESS_SECONDS = 24 * 3600
REPORT_DIR = os.environ.get("REPORT_DIR", "/srv/trader-v3/reports")
REPORT_HEALTH_URL = os.environ.get("REPORT_HEALTH_URL", "http://127.0.0.1:8090/healthz")
DAILY_REPORT_EXPECTED_HOUR_UTC = 13
DAILY_REPORT_EXPECTED_MINUTE_UTC = 32
DAILY_REPORT_GRACE_SECONDS = 90 * 60
PENDING_CANCEL_TIMEOUT_MINUTES = float(os.environ.get("PENDING_CANCEL_TIMEOUT_MINUTES", "10"))
PENDING_CANCEL_LOOKBACK_HOURS = float(os.environ.get("PENDING_CANCEL_LOOKBACK_HOURS", "48"))
BRAIN_PROBE_SECONDS = 600
BRAIN_FAIL_ALERT_AFTER = 2
HERMES_ENV_FILE = "/srv/hermes/profiles/trader/.env"
TG_CHAT_ID = "8545234287"
BRAIN_PROBES = (
    ("primary", "https://api.balenw.cloud/v1/chat/completions", "CLIPROXYAPI_API_KEY",
     "openai", "deepseek-v4-flash"),
    ("fallback", "https://open.bigmodel.cn/api/anthropic/v1/messages", "BIGMODEL_API_KEY",
     "anthropic", "glm-5.2"),
)
ROBOT_ORDER_PATTERN = r"^B[0-9a-f]{32}[0-9]{2}$"
ROBOT_ORDER_RE = re.compile(ROBOT_ORDER_PATTERN)
ENTRY_INTENT_ACTIONS = {"open_position", "add_position"}
PENDING_CANCEL_TERMINALS = {"OrderCanceled", "CancelRejected", "OrderCancelRejected", "OrderFilled"}



def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def q(sql: str) -> list[list[str]]:
    out = subprocess.run(PSQL + [sql], capture_output=True, text=True, timeout=25)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:200]}")
    return [line.split("|") for line in out.stdout.splitlines() if line.strip()]


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


def is_system_id(client_order_id: str) -> bool:
    return isinstance(client_order_id, str) and ROBOT_ORDER_RE.fullmatch(client_order_id) is not None


def is_entry_order(client_order_id: str) -> bool:
    return is_system_id(client_order_id) and 1 <= int(client_order_id[-2:]) <= 9


def _send_deduplicated_alert(state, key, text, dry_run, now_ts, dedup_seconds=ALERT_DEDUP_SECONDS):
    last = state.get(key)
    if isinstance(last, (int, float)) and now_ts - last < dedup_seconds:
        return False
    if dry_run:
        log(f"DRY-RUN would send alert: {text}")
        return False
    if not tg_send_direct(text):
        return False
    state[key] = now_ts
    return True


def prune_state(state: dict, now_ts: float) -> None:
    """Discard obsolete agent prompts and old delivered notifications, not pending risks."""
    obsolete = ("alert:", "authalert:", "fill:", "ttl:", "ttlwake:", "naked:", "markfail:", "recon:")
    for key, value in list(state.items()):
        if key.startswith(obsolete):
            state.pop(key, None)
        elif key.startswith("pendingcancel:"):
            if not isinstance(value, dict) or not is_system_id(value.get("client_order_id")):
                state.pop(key, None)
        elif key.startswith(("ttlalert:", "orderdiff:", "dailyreport:", "intentstall:", "protectionfreeze:")):
            if not isinstance(value, (int, float)) or now_ts - value >= STATE_PRUNE_AGE_SECONDS:
                state.pop(key, None)


def _exchange_state_snapshots() -> dict[str, dict]:
    """Account-scoped complete regular/algo snapshots; an explicit empty pair is valid.

    Missing/malformed order sections never masquerade as an empty exchange.
    A stale account remains stale even when another account is fresh.
    """
    snapshots = {}
    rows = q("SELECT account_id, EXTRACT(EPOCH FROM (now() - updated_at)), payload::text FROM exchange_state_mirror")
    for account_id, age_raw, payload_raw in rows:
        try:
            age = float(age_raw)
            payload = json.loads(payload_raw)
        except (TypeError, ValueError):
            snapshots[account_id] = {"fresh": False}
            continue
        if not isinstance(payload, dict):
            snapshots[account_id] = {"fresh": False}
            continue
        regular = payload.get("open_orders")
        algo = payload.get("algo_orders")
        complete = (
            isinstance(regular, list) and isinstance(algo, list)
            and all(isinstance(order, dict) for order in regular + algo)
        )
        if complete:
            complete = all(_order_client_id(order) for order in regular + algo)
        snapshots[account_id] = {
            "fresh": complete and math.isfinite(age) and 0 <= age <= MIRROR_MAX_AGE_SECONDS,
            "open_orders": regular if complete else [],
            "algo_orders": algo if complete else [],
        }
    return snapshots


def _order_client_id(order: dict) -> str | None:
    for field in ("client_order_id", "clientOrderId", "clientAlgoId", "client_algo_id"):
        value = order.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _snapshot_open_order_ids(snapshot: dict) -> set[str]:
    ids = set()
    for order in snapshot.get("open_orders", []) + snapshot.get("algo_orders", []):
        cid = _order_client_id(order)
        if is_system_id(cid):
            ids.add(cid)
    return ids


def sweep_ttl(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    """Notify on live robot entries only; never infer cancel/renew from an alert."""
    now_ts = time.time() if now_ts is None else now_ts
    rows = q(
        "SELECT o.client_order_id,o.account_id,o.instrument_id,"
        "EXTRACT(EPOCH FROM (now()-o.ts_event))/3600.0,ti.action::text "
        "FROM orders_projection o JOIN trade_intents ti "
        "ON ti.intent_id=o.intent_id AND ti.account_id=o.account_id "
        "WHERE o.status IN ('accepted','partially_filled','updated') "
        f"AND o.client_order_id ~ '{ROBOT_ORDER_PATTERN}' "
        "AND ti.action::text IN ('open_position','add_position')"
    )
    snapshots = _exchange_state_snapshots()
    for cid, account_id, instrument_id, age_raw, action in rows:
        if not is_entry_order(cid) or action not in ENTRY_INTENT_ACTIONS:
            continue
        try:
            age = float(age_raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(age) or age < ORDER_TTL_HOURS:
            continue
        snapshot = snapshots.get(account_id, {})
        if not snapshot.get("fresh") or cid not in _snapshot_open_order_ids(snapshot):
            continue
        _send_deduplicated_alert(
            state, f"ttlalert:{account_id}:{cid}",
            f"⚠️ 机器人入场挂单超龄:{account_id} {instrument_id} 订单 {cid} "
            f"已挂 {age:.1f} 小时，超过 {ORDER_TTL_HOURS:g} 小时提醒阈值。"
            "普通 GTC 的自动到期撤单尚未接管；请人工核对。监控仅通知，未撤单、未续期。",
            dry_run, now_ts,
        )


def sweep_reconcile(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    """Notify discrepancies, never fabricate exchange terminal states."""
    now_ts = time.time() if now_ts is None else now_ts
    projected = {}
    rows = q(
        "SELECT account_id,client_order_id FROM orders_projection "
        "WHERE status IN ('accepted','partially_filled','updated') "
        f"AND client_order_id ~ '{ROBOT_ORDER_PATTERN}'"
    )
    for account_id, cid in rows:
        if is_system_id(cid):
            projected.setdefault(account_id, set()).add(cid)
    snapshots = _exchange_state_snapshots()
    for account_id, snapshot in snapshots.items():
        if not snapshot.get("fresh"):
            continue
        venue = _snapshot_open_order_ids(snapshot)
        local = projected.get(account_id, set())
        differences = [(cid, "absent") for cid in sorted(local - venue)]
        differences.extend((cid, "unprojected") for cid in sorted(venue - local))
        pending = []
        for cid, kind in differences:
            key = f"orderdiff:{account_id}:{kind}:{cid}"
            last = state.get(key)
            if not isinstance(last, (int, float)) or now_ts - last >= ALERT_DEDUP_SECONDS:
                pending.append((key, cid, kind))
        if not pending:
            continue
        # Keep each alert within Telegram's message limit. Unsent chunks retain
        # their eligibility and will retry next sweep.
        for start in range(0, len(pending), 20):
            chunk = pending[start:start + 20]
            lines = [
                f"- {cid}: " + ("账本仍在场，交易所镜像未见" if kind == "absent" else "交易所镜像在场，账本未见")
                for _key, cid, kind in chunk
            ]
            text = (f"⚠️ 机器人订单对账差异:{account_id}\n" + "\n".join(lines)
                    + "\n缺席不代表已撤销或已成交。请核对订单和成交记录；监控未改账、未撤单。")
            if dry_run:
                log(f"DRY-RUN would send alert: {text}")
            elif tg_send_direct(text):
                for key, _cid, _kind in chunk:
                    state[key] = now_ts


def sweep_brain(state: dict, dry_run: bool, now_ts: float | None = None, prober=None) -> None:
    now_ts = time.time() if now_ts is None else now_ts
    fn = prober or probe_llm
    results = {name: fn(name, url, key_env, style, model) for name, url, key_env, style, model in BRAIN_PROBES}
    fails = 0 if results.get("primary") else int(state.get("brainfail:primary", 0)) + 1
    state["brainfail:primary"] = fails
    if fails < BRAIN_FAIL_ALERT_AFTER:
        return
    if results.get("fallback"):
        key, cooldown = "brainalert:primary", 3600
        text = "⚠️ 交易模型主通道连续探活失败，备用通道探活正常。请检查模型服务；信号处理可能延迟。"
    else:
        key, cooldown = "brainalert:critical", 1800
        text = "🚨 交易模型主、备用通道均探活失败。请检查模型服务；信号处理可能延迟。"
    _send_deduplicated_alert(state, key, text, dry_run, now_ts, dedup_seconds=cooldown)



def sweep_node_health(state: dict, dry_run: bool, now_ts: float | None = None) -> None:
    now_ts = time.time() if now_ts is None else now_ts
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
                _send_deduplicated_alert(state, stale_key, text, dry_run, now_ts)
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
            "请核查节点状态与最近事件。"
            "禁止自动 RESUME,等待人工确认根因与安全状态。"
        )
        _send_deduplicated_alert(state, alert_key, prompt, dry_run, now_ts)


def sweep_intent_stalls(
    state: dict,
    dry_run: bool,
    now_ts: float | None = None,
) -> None:
    now_ts = time.time() if now_ts is None else now_ts
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
    now_ts = time.time() if now_ts is None else now_ts
    rows = q(
        "SELECT event_id, account_id, COALESCE(intent_id::text,''), "
        "COALESCE(client_order_id,''), payload::text "
        "FROM execution_events "
        "WHERE event_type IN ('ProtectionFrozen','ProtectionWatchdogSymbolStopped') "
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
        if client_order_id and not is_system_id(client_order_id):
            continue
        reason = str(
            payload.get("reason")
            or payload.get("denial_reason")
            or "unknown"
        )
        instrument_id = str(payload.get("instrument_id") or "?")
        text = (
            f"🔴 保护生命周期异常:{account_id} {instrument_id} intent={intent_id or '?'} "
            f"reason={reason} revision={payload.get('protection_revision', '?')}。"
            "节点报告确定性保护异常，请人工核对持仓和交易所保护单。"
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
    now_ts = time.time() if now_ts is None else now_ts
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


def _pending_cancel_rows() -> list[list[str]]:
    terminal_types = "','".join(sorted(PENDING_CANCEL_TERMINALS))
    return q(
        "WITH latest_pending AS ("
        " SELECT DISTINCT ON (account_id, client_order_id)"
        " account_id, client_order_id, event_id, ts_event"
        " FROM execution_events"
        " WHERE event_type='OrderPendingCancel' AND client_order_id IS NOT NULL"
        f" AND client_order_id ~ '{ROBOT_ORDER_PATTERN}'"
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
        truth = "交易所镜像显示订单已消失，事件流仍缺少对应终态，需核对订单与成交记录"
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
    now_ts = time.time() if now_ts is None else now_ts
    timeout_seconds = PENDING_CANCEL_TIMEOUT_MINUTES * 60
    # Manual records from a previous monitor version must not survive restart.
    for key, entry in list(state.items()):
        if key.startswith("pendingcancel:") and (
            not isinstance(entry, dict) or not is_system_id(entry.get("client_order_id"))
        ):
            state.pop(key, None)
    records: dict[str, dict] = {}
    for account_id, cid, event_id, pending_raw, terminal_type in _pending_cancel_rows():
        if not is_system_id(cid):
            continue
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
        if account_id and is_system_id(cid):
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
            # Resolve this notification, not the order. Disappearance is never
            # written as an exchange terminal state. Still-open orders remain
            # eligible for daily reminders until authoritative evidence arrives.
            if outcome in ("filled", "disappeared"):
                entry["resolved"] = True


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
        prune_state(state, now_ts)
        for name, fn, due in (
            ("nodes", sweep_node_health, True),
            ("intent-stalls", sweep_intent_stalls, True),
            ("protection-events", sweep_protection_events, True),
            ("daily-report", sweep_daily_report, True),
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
