#!/usr/bin/env python3
"""Portable global read CLI: every query uses the authenticated control plane.

No Docker socket, host database, host env file, or node-local HTTP ports needed.
Missing/stale evidence is returned as reported by the control plane.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("V3_CONTROL_PLANE_URL", "http://127.0.0.1:8080").rstrip("/")
TEXT_TRUNCATE = 600

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


def die(message):
    print(json.dumps({"error": message}, ensure_ascii=False))
    raise SystemExit(1)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=1))


def api_get(path, **params):
    token = os.environ.get("V3_READ_TOKEN") or os.environ.get("RISK_ADMIN_TOKEN") or os.environ.get("VIEWER_TOKEN")
    if not token:
        die("Set V3_READ_TOKEN (or RISK_ADMIN_TOKEN / VIEWER_TOKEN)")
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
    url = BASE + path + ("?" + query if query else "")
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", "query failed")
        except (ValueError, AttributeError):
            detail = "query failed"
        die(f"HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, ValueError):
        die("Control plane unavailable or invalid response; no local fallback")


def cmd_channels(args):
    emit(api_get("/v1/query/channels"))


def cmd_messages(args):
    result = api_get("/v1/query/messages", channel=resolve_channel(args.channel), limit=args.limit)
    if not args.full:
        for row in result.get("messages", []):
            text = row.get("message_text")
            if isinstance(text, str) and len(text) > TEXT_TRUNCATE:
                row["message_text"] = text[:TEXT_TRUNCATE] + "…"
    emit(result)


def cmd_positions(args):
    emit(api_get("/v1/mirror/positions"))


def cmd_orders(args):
    emit(api_get("/v1/query/orders"))


def cmd_intents(args):
    emit(api_get("/v1/query/intents", limit=args.limit, status=args.status, symbol=args.symbol))


def cmd_intent(args):
    emit(api_get("/v1/query/intent", prefix=args.intent_id))


def cmd_fills(args):
    emit(api_get("/v1/query/fills", hours=args.hours))


def cmd_outcomes(args):
    emit(api_get("/v1/query/outcomes", days=args.days))


def cmd_nodes(args):
    emit(api_get("/v1/nodes"))


def cmd_reconcile(args):
    emit(api_get("/v1/reconcile"))


def cmd_report(args):
    emit(api_get("/v1/query/report", hours=args.hours))


def cmd_signals(args):
    emit(api_get("/v1/signals", days=args.days, limit=args.limit))


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("channels", help="信号频道列表").set_defaults(fn=cmd_channels)

    p = sub.add_parser("messages", help="某频道最近消息(含媒体标识)")
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

    sub.add_parser("nodes", help="节点健康(控制面心跳)").set_defaults(fn=cmd_nodes)
    sub.add_parser("reconcile", help="投影vs交易所对账").set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("report", help="系统一页摘要")
    p.add_argument("--hours", default=24)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("signals", help="消息逐条处理结果与原因，含未处理和未知证据")
    p.add_argument("--days", default=2, type=int)
    p.add_argument("--limit", default=500, type=int)
    p.set_defaults(fn=cmd_signals)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
