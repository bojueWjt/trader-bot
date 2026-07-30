#!/usr/bin/env python3
"""v3_query — read-only system query CLI for the Hermes trader agent (hk).

Answers "what is going on" questions across the whole trader-v3 system:
channel messages (with images), exchange-truth positions/orders/protections,
intent audit chains, fills & outcomes, node health, projection reconciliation.

STRICTLY READ-ONLY: every DB statement is a SELECT; health checks are GETs.
Truth hierarchy: exchange_state_mirror (Binance REST, ~45s) > projections.

Usage examples:
  v3_query.py channels
  v3_query.py messages 舒琴 --limit 5
  v3_query.py positions
  v3_query.py orders
  v3_query.py intents --limit 10
  v3_query.py intent 456d6cea
  v3_query.py fills --hours 24
  v3_query.py outcomes --days 7
  v3_query.py nodes
  v3_query.py reconcile
  v3_query.py report --hours 24
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

PSQL = ["docker", "exec", "trader-v3-postgres", "psql", "-U", "postgres", "-d", "trader", "-Atc"]
NODE_HEALTH = {
    "nautilus-node-account-a": "http://127.0.0.1:8081/ready",
    "nautilus-node-account-b": "http://127.0.0.1:8082/ready",
}
TEXT_TRUNCATE = 600

# Known signal channels (watcher whitelist). Alias matching is
# case-insensitive substring; a raw channel id always works too.
CHANNEL_ALIASES = {
    "-1002136478186": ["舒琴", "淑琴", "shuqin", "c02"],
    "-1002198013097": ["titan", "b01-02", "02titan"],
    "-1002228497993": ["gauls", "b01-03", "03gauls"],
    "-1002328068747": ["coinalert", "coin", "测试"],
    "hermes-operator": ["operator", "操作", "口头"],
}
CHANNEL_NAMES = {
    "-1002136478186": "C02-舒琴",
    "-1002198013097": "B01-02Titan",
    "-1002228497993": "B01-03Gauls",
    "-1002328068747": "coinAlert(测试群)",
    "hermes-operator": "operator(经我下的单)",
}


def die(msg: str) -> None:
    print(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(1)


def q_json(sql: str):
    """Run a SELECT wrapped in json_agg; returns a list (possibly empty)."""
    wrapped = f"SELECT COALESCE(json_agg(t), '[]'::json) FROM ({sql}) t"
    out = subprocess.run(PSQL + [wrapped], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        die(f"query failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout.strip() or "[]")


def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def trunc(s, n=TEXT_TRUNCATE):
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[:n] + f"…(截断,共{len(s)}字)"


def resolve_channel(token: str) -> str:
    t = token.strip().lower()
    if re.fullmatch(r"-?\d{5,}", t) or t in CHANNEL_ALIASES:
        return token.strip()
    for cid, aliases in CHANNEL_ALIASES.items():
        if any(a in t or t in a for a in aliases):
            return cid
        if t in CHANNEL_NAMES.get(cid, "").lower():
            return cid
    die(f"unknown channel {token!r}; use `channels` to list, or pass the raw channel_id")


def _ident_ok(value: str, pattern: str, what: str) -> str:
    if not re.fullmatch(pattern, value):
        die(f"invalid {what}: {value!r}")
    return value


# ------------------------------------------------------------- commands ----

def cmd_channels(args) -> None:
    rows = q_json(
        "SELECT channel_id, count(*) AS messages, max(source_received_at) AS last_message_at "
        "FROM raw_messages GROUP BY channel_id ORDER BY max(source_received_at) DESC"
    )
    for r in rows:
        r["name"] = CHANNEL_NAMES.get(r["channel_id"], r["channel_id"])
    emit({"channels": rows, "note": "messages <频道名或id> 查看具体消息"})


def cmd_messages(args) -> None:
    cid = resolve_channel(args.channel)
    limit = max(1, min(int(args.limit), 50))
    rows = q_json(
        "SELECT m.source_message_id, m.source_received_at, m.message_text, "
        "(SELECT json_agg(json_build_object('path', a.object_key, 'mime', a.mime, 'status', a.download_status)) "
        " FROM media_assets a WHERE a.raw_message_id = m.id) AS media "
        f"FROM raw_messages m WHERE m.channel_id = '{cid}' "
        f"ORDER BY m.source_received_at DESC LIMIT {limit}"
    )
    for r in rows:
        r["message_text"] = trunc(r["message_text"], TEXT_TRUNCATE if not args.full else 100000)
        if not r.get("media"):
            r.pop("media", None)
    emit({
        "channel": {"id": cid, "name": CHANNEL_NAMES.get(cid, cid)},
        "messages_newest_first": rows,
        "note": "media.path 是本机图片路径,可直接查看图片内容",
    })


def _mirror() -> list:
    return q_json(
        "SELECT account_id, updated_at, EXTRACT(EPOCH FROM (now()-updated_at)) AS age_seconds, payload "
        "FROM exchange_state_mirror"
    )


def cmd_positions(args) -> None:
    accounts = q_json("SELECT account_id, currency, equity, available_balance, updated_at FROM accounts_projection")
    out = {"source": "exchange_state_mirror(币安真相,45s刷新)", "accounts": {}}
    for row in _mirror():
        payload = row["payload"] or {}
        protections = payload.get("protections") or []
        positions = []
        for p in payload.get("positions") or []:
            sym, side = p.get("symbol"), (p.get("position_side") or "").lower()
            # hedge mode has one protections entry per position book; merge
            # every entry for the symbol, then split SL/TP by exit side
            # (long exits SELL, short exits BUY). Taking only the first entry
            # made dual-side shorts look naked (2026-07-12 ETH incident).
            prots = [x for x in protections if x.get("symbol") == sym]
            prot = {
                "stop_loss_orders": [o for x in prots for o in x.get("stop_loss_orders") or []],
                "take_profit_orders": [o for x in prots for o in x.get("take_profit_orders") or []],
            }
            exit_side = "SELL" if side == "long" else "BUY"
            sl = [{"trigger_price": o.get("trigger_price"), "quantity": o.get("quantity")}
                  for o in prot.get("stop_loss_orders") or [] if o.get("side") == exit_side]
            tp = [{"price": o.get("price") if o.get("price") not in (None, "0.0", "0") else o.get("trigger_price"),
                   "quantity": o.get("quantity")}
                  for o in prot.get("take_profit_orders") or [] if o.get("side") == exit_side]
            positions.append({
                "symbol": sym, "side": side,
                "quantity": p.get("position_amt"), "entry_price": p.get("entry_price"),
                "unrealized_pnl": p.get("unrealized_pnl"),
                "has_stop_loss": bool(sl),
                "stop_loss_orders": sl or None,
                "take_profit_orders": tp or None,
            })
        out["accounts"][row["account_id"]] = {
            "mirror_age_seconds": round(float(row["age_seconds"]), 1),
            "positions": positions,
            "balance": next((a for a in accounts if a["account_id"] == row["account_id"]), None),
        }
    if any(v["mirror_age_seconds"] > 300 for v in out["accounts"].values()):
        out["warning"] = "镜像超过5分钟未刷新,数据可能过期,先检查 trader-v3-exchange-state 服务"
    emit(out)


def cmd_orders(args) -> None:
    out = {"source": "exchange_state_mirror(币安真相)", "accounts": {}}
    for row in _mirror():
        payload = row["payload"] or {}
        orders = []
        for o in payload.get("open_orders") or []:
            cid = str(o.get("client_order_id") or "")
            entry = {
                "symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("type"),
                "price": o.get("price"), "quantity": o.get("quantity"),
                "reduce_only": o.get("reduce_only"), "client_order_id": cid,
                "origin": "system" if re.fullmatch(r"B[0-9a-f]{32}[0-9]{2}", cid) else "external/manual",
            }
            m = re.fullmatch(r"B([0-9a-f]{32})[0-9]{2}", cid)
            if m:
                u = m.group(1)
                intent_uuid = f"{u[0:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:32]}"
                meta = q_json(
                    "SELECT created_at, EXTRACT(EPOCH FROM (now()-created_at))/3600.0 AS age_hours "
                    f"FROM trade_intents WHERE intent_id = '{intent_uuid}'"
                )
                if meta:
                    entry["placed_at"] = meta[0]["created_at"]
                    entry["age_hours"] = round(float(meta[0]["age_hours"]), 1)
            orders.append(entry)
        algo = [{
            "symbol": a.get("symbol"), "type": a.get("type") or a.get("order_type"),
            "side": a.get("side"), "trigger_price": a.get("trigger_price") or a.get("stop_price"),
            "quantity": a.get("quantity"), "position_side": a.get("position_side"),
        } for a in payload.get("algo_orders") or []]
        out["accounts"][row["account_id"]] = {"open_orders": orders, "algo_orders_SL_TP": algo}
    emit(out)


def cmd_intents(args) -> None:
    limit = max(1, min(int(args.limit), 50))
    where = ["1=1"]
    if args.status:
        where.append(f"ti.status = '{_ident_ok(args.status, r'[a-z_]+', 'status')}'")
    if args.symbol:
        sym = _ident_ok(args.symbol.upper(), r"[A-Z0-9]+", "symbol")
        where.append(f"ti.instrument_id LIKE '{sym}%'")
    rows = q_json(
        "SELECT left(ti.intent_id::text, 8) AS intent, ti.action, ti.instrument_id, ti.account_id, "
        "ti.status, ti.created_at, rd.reason "
        "FROM trade_intents ti JOIN risk_decisions rd ON rd.risk_decision_id = ti.risk_decision_id "
        f"WHERE {' AND '.join(where)} ORDER BY ti.created_at DESC LIMIT {limit}"
    )
    emit({"intents_newest_first": rows, "note": "intent <前8位> 查完整审计链"})


def cmd_intent(args) -> None:
    prefix = _ident_ok(args.intent_id.lower(), r"[0-9a-f-]{4,36}", "intent id")
    intents = q_json(
        "SELECT ti.*, rd.reason AS risk_reason, rd.checks AS risk_checks, hd.message_type, hd.evidence "
        "FROM trade_intents ti "
        "JOIN risk_decisions rd ON rd.risk_decision_id = ti.risk_decision_id "
        "JOIN hermes_decisions hd ON hd.decision_id = ti.hermes_decision_id "
        f"WHERE ti.intent_id::text LIKE '{prefix}%' ORDER BY ti.created_at DESC"
    )
    if not intents:
        die(f"no intent matches {prefix!r}")
    if len(intents) > 1:
        candidates = []
        for item in intents:
            candidates.append({
                "intent": str(item["intent_id"])[:8],
                "created_at": item.get("created_at"),
                "symbol": item.get("instrument_id"),
            })
        print(json.dumps({
            "error": f"multiple intents match {prefix!r}; pass a longer unique prefix",
            "candidates": candidates,
        }, ensure_ascii=False))
        sys.exit(1)
    it = intents[0]
    full_id = it["intent_id"]
    events = q_json(
        "SELECT event_type, client_order_id, ts_event, payload->>'last_qty' AS qty, "
        "payload->>'last_px' AS px FROM execution_events "
        f"WHERE intent_id = '{full_id}' ORDER BY ts_event"
    )
    acks = q_json(
        "SELECT event_type, actor, payload, created_at FROM audit_events "
        f"WHERE aggregate_id = '{full_id}' ORDER BY created_at"
    )
    raw = q_json(
        "SELECT m.message_text, m.channel_id, m.source_received_at FROM raw_messages m "
        "JOIN hermes_decisions hd ON hd.raw_message_id = m.id "
        f"WHERE hd.decision_id = '{it['hermes_decision_id']}'"
    )
    if raw:
        raw[0]["message_text"] = trunc(raw[0]["message_text"])
    emit({"intent": it, "origin_message": raw[0] if raw else None,
          "execution_events": events, "node_acks_and_audit": acks})


def cmd_fills(args) -> None:
    hours = max(1, min(int(args.hours), 24 * 14))
    rows = q_json(
        "SELECT account_id, payload->>'instrument_id' AS instrument, client_order_id, "
        "payload->>'order_side' AS side, payload->>'last_qty' AS qty, payload->>'last_px' AS price, "
        "ts_event FROM execution_events WHERE event_type = 'OrderFilled' "
        f"AND ts_event > now() - interval '{hours} hours' ORDER BY ts_event DESC LIMIT 100"
    )
    side_map = {"1": "buy", "2": "sell", "BUY": "buy", "SELL": "sell"}
    for r in rows:
        r["side"] = side_map.get(str(r.get("side")), r.get("side"))
    emit({"window_hours": hours, "fills_newest_first": rows})


def cmd_outcomes(args) -> None:
    days = max(1, min(int(args.days), 90))
    rows = q_json(
        "SELECT instrument_id, account_id, side, entry_avg_price, exit_avg_price, filled_quantity, "
        "realized_pnl, fees, r_multiple, holding_seconds, closed_at FROM trade_outcomes "
        f"WHERE closed_at > now() - interval '{days} days' ORDER BY closed_at DESC LIMIT 100"
    )
    total = sum(float(r["realized_pnl"] or 0) for r in rows)
    emit({"window_days": days, "closed_trades": rows,
          "total_realized_pnl": round(total, 2), "count": len(rows)})


def cmd_nodes(args) -> None:
    beats = q_json(
        "SELECT node_id, status, last_seen_at, EXTRACT(EPOCH FROM (now()-last_seen_at)) AS age_seconds, "
        "payload->>'reconciliation_state' AS reconciliation FROM node_heartbeats ORDER BY node_id"
    )
    for b in beats:
        b["age_seconds"] = round(float(b["age_seconds"]), 1)
        url = NODE_HEALTH.get(b["node_id"])
        if url:
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    h = json.loads(resp.read().decode())
                b["local_health"] = {"trading_state": h.get("trading_state"),
                                     "halt_reason": h.get("halt_reason"), "ready": h.get("ready")}
            except urllib.error.HTTPError as exc:
                try:
                    h = json.loads(exc.read().decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    b["local_health"] = {"error": str(exc)[:120]}
                else:
                    b["local_health"] = {
                        "trading_state": h.get("trading_state"),
                        "halt_reason": h.get("halt_reason"),
                        "ready": h.get("ready"),
                        "http_status": exc.code,
                    }
            except Exception as exc:  # noqa: BLE001
                b["local_health"] = {"error": str(exc)[:120]}
    commands = q_json(
        "SELECT command_type, status, reason, created_at, completed_at "
        "FROM operator_commands ORDER BY created_at DESC LIMIT 5"
    )
    warnings = []
    for b in beats:
        if b["age_seconds"] > 120:
            warnings.append(f"{b['node_id']} 心跳已停 {int(b['age_seconds'])}s — 节点可能僵死")
        lh = b.get("local_health") or {}
        local_state = str(lh.get("trading_state") or b.get("status") or "").upper()
        if local_state == "HALTED" and lh.get("halt_reason"):
            warnings.append(f"{b['node_id']} HALTED 原因: {lh['halt_reason']}")
    out = {
        "nodes": beats,
        "operator_command_history": commands,
        "operator_command_history_note": "审计历史，不代表当前节点状态；当前状态以 nodes 为准",
    }
    if warnings:
        out["warnings"] = warnings
    emit(out)


def cmd_reconcile(args) -> None:
    projection = q_json(
        "SELECT account_id, client_order_id, instrument_id, status, quantity FROM orders_projection "
        "WHERE status IN ('accepted','partially_filled','updated')"
    )
    ghosts, live_by_account = [], {}
    for row in _mirror():
        stale = float(row["age_seconds"]) > 300
        ids = set() if stale else {
            str(o.get("client_order_id") or "") for o in (row["payload"] or {}).get("open_orders") or []
        }
        live_by_account[row["account_id"]] = None if stale else ids
    matched = 0
    for p in projection:
        live = live_by_account.get(p["account_id"])
        if live is None:
            continue
        if p["client_order_id"] in live:
            matched += 1
        else:
            ghosts.append(p)
    missing = []
    for row in _mirror():
        proj_ids = {p["client_order_id"] for p in projection if p["account_id"] == row["account_id"]}
        for o in (row["payload"] or {}).get("open_orders") or []:
            if str(o.get("client_order_id")) not in proj_ids:
                missing.append({"account_id": row["account_id"], "symbol": o.get("symbol"),
                                "client_order_id": o.get("client_order_id")})
    emit({
        "projection_open_rows": len(projection), "matched_on_exchange": matched,
        "ghosts_projection_only": ghosts, "missing_exchange_only": missing,
        "note": "幽灵单会被 lifecycle monitor 自动终态化;漏记的外部单无害",
    })


def cmd_report(args) -> None:
    hours = max(1, min(int(args.hours), 24 * 7))
    fills = q_json(
        "SELECT count(*) AS fills, count(DISTINCT payload->>'instrument_id') AS symbols "
        f"FROM execution_events WHERE event_type='OrderFilled' AND ts_event > now() - interval '{hours} hours'"
    )[0]
    intents = q_json(
        "SELECT status::text, count(*) AS n FROM trade_intents "
        f"WHERE created_at > now() - interval '{hours} hours' GROUP BY 1"
    )
    outcomes = q_json(
        "SELECT COALESCE(sum(realized_pnl),0) AS pnl, count(*) AS closed FROM trade_outcomes "
        f"WHERE closed_at > now() - interval '{hours} hours'"
    )[0]
    beats = q_json("SELECT node_id, status FROM node_heartbeats")
    mirror_summary = {}
    for row in _mirror():
        p = row["payload"] or {}
        mirror_summary[row["account_id"]] = {
            "positions": len(p.get("positions") or []),
            "open_orders": len(p.get("open_orders") or []),
            "protections_SL_TP": len(p.get("algo_orders") or []),
        }
    emit({
        "window_hours": hours,
        "nodes": {b["node_id"]: b["status"] for b in beats},
        "exchange": mirror_summary,
        "fills": fills, "intents_by_status": intents,
        "closed_pnl": outcomes,
        "note": "细节用 fills/outcomes/orders/positions/intents 子命令查",
    })


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("channels", help="信号频道列表").set_defaults(fn=cmd_channels)

    p = sub.add_parser("messages", help="某频道最近消息(含图片路径)")
    p.add_argument("channel", help="频道名(舒琴/titan/gauls/coinalert/operator)或channel_id")
    p.add_argument("--limit", default=5)
    p.add_argument("--full", action="store_true", help="不截断长文本")
    p.set_defaults(fn=cmd_messages)

    sub.add_parser("positions", help="持仓+保护单(交易所真相)").set_defaults(fn=cmd_positions)
    sub.add_parser("orders", help="在场挂单+条件单(交易所真相,含挂龄)").set_defaults(fn=cmd_orders)

    p = sub.add_parser("intents", help="最近交易意向")
    p.add_argument("--limit", default=10)
    p.add_argument("--status", default=None, help="approved/rejected/expired…")
    p.add_argument("--symbol", default=None)
    p.set_defaults(fn=cmd_intents)

    p = sub.add_parser("intent", help="单笔意向完整审计链(消息→决策→风控→执行)")
    p.add_argument("intent_id", help="intent id 或前缀")
    p.set_defaults(fn=cmd_intent)

    p = sub.add_parser("fills", help="最近成交")
    p.add_argument("--hours", default=24)
    p.set_defaults(fn=cmd_fills)

    p = sub.add_parser("outcomes", help="已平仓结果(盈亏/R倍数)")
    p.add_argument("--days", default=7)
    p.set_defaults(fn=cmd_outcomes)

    sub.add_parser("nodes", help="节点健康(心跳/HALT原因/最近命令)").set_defaults(fn=cmd_nodes)
    sub.add_parser("reconcile", help="投影vs交易所对账").set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("report", help="系统一页摘要")
    p.add_argument("--hours", default=24)
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
