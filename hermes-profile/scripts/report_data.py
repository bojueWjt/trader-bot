#!/usr/bin/env python3
"""Dump a compact trading digest for the Hermes daily/weekly report cron jobs.

stdout is injected into the report prompt. Read-only. Usage: report_data.py [days]
"""
import json
import subprocess
import sys

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 1
PSQL = ["docker", "exec", "trader-v3-postgres", "psql", "-U", "postgres", "-d", "trader", "-Atc"]
ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")


def q(sql: str) -> list[str]:
    out = subprocess.run(PSQL + [sql], capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        return [f"(查询失败: {out.stderr.strip()[:120]})"]
    return [line for line in out.stdout.splitlines() if line.strip()]


def split_map(rows: list[str], nfields: int) -> dict[str, tuple[str, ...]]:
    parsed: dict[str, tuple[str, ...]] = {}
    for line in rows:
        parts = line.split("|")
        if len(parts) < nfields:
            continue
        parsed[parts[0]] = tuple(parts[1:nfields])
    return parsed


print(f"### 数据窗口: 最近 {DAYS} 天 (UTC)")

print("\n### 交易意向")
rows = q(
    "SELECT to_char(created_at,'MM-DD HH24:MI'), instrument_id, action, status, "
    "substr(order_plan::text,1,120) FROM trade_intents "
    f"WHERE created_at > now() - interval '{DAYS} days' ORDER BY created_at"
)
print("\n".join(rows) if rows else "(无)")

print("\n### 成交 (OrderFilled)")
rows = q(
    "SELECT to_char(ts_event,'MM-DD HH24:MI'), payload->>'last_qty', payload->>'last_px', "
    "substr(payload->>'instrument_id',1,24), CASE payload->>'order_side' WHEN '1' THEN 'BUY' ELSE 'SELL' END "
    "FROM execution_events WHERE event_type='OrderFilled' "
    f"AND ts_event > now() - interval '{DAYS} days' ORDER BY ts_event"
)
print("\n".join(rows) if rows else "(无)")

print("\n### 四账户收盘")
pnl = split_map(
    q(
        "SELECT account_id, COALESCE(sum(realized_pnl),0), count(*) "
        "FROM trade_outcomes "
        f"WHERE closed_at > now() - interval '{DAYS} days' "
        "GROUP BY account_id"
    ),
    3,
)
equity = split_map(
    q("SELECT account_id, equity, available_balance FROM accounts_projection"),
    3,
)
pos_rows = q(
    "SELECT account_id, instrument_id, side, quantity, avg_entry_price "
    "FROM positions_projection "
    "WHERE status='open' AND quantity::numeric != 0 "
    "ORDER BY account_id, instrument_id"
)
pos_by: dict[str, list[str]] = {account_id: [] for account_id in ACCOUNTS}
for line in pos_rows:
    account_id = line.split("|", 1)[0]
    pos_by.setdefault(account_id, []).append(line)
for account_id in ACCOUNTS:
    realized, closed = pnl.get(account_id, ("0", "0"))
    eq, available = equity.get(account_id, ("?", "?"))
    print(f"{account_id}: 权益 {eq} / 可用 {available} / 已实现 {realized} / 平仓 {closed} 笔")
    held = pos_by.get(account_id) or []
    if held:
        for line in held:
            print(f"  持仓 {line}")
    else:
        print("  持仓: 空仓,今日无在场仓")

print("\n### 当前挂单")
rows = q(
    "SELECT account_id, instrument_id, status, count(*) FROM orders_projection "
    "WHERE status IN ('accepted','partially_filled') GROUP BY 1,2,3 ORDER BY 1,2"
)
print("\n".join(rows) if rows else "(无)")

print("\n### 被拒/异常事件计数")
rows = q(
    "SELECT event_type, count(*) FROM execution_events "
    f"WHERE ts_event > now() - interval '{DAYS} days' "
    "AND event_type IN ('OrderRejected','OrderDenied','OrderExpired','OrderCanceled') GROUP BY 1"
)
print("\n".join(rows) if rows else "(无)")

print("\n### 系统健康")
try:
    svc = subprocess.run(
        ["systemctl", "is-active", "trader-v3-controlplane",
         "trader-v3-hermes-feeder", "trader-v3-lifecycle-monitor"],
        capture_output=True, text=True, timeout=10,
    )
    names = ["控制面", "信号投递", "生命周期监控"]
    print("服务: " + ", ".join(f"{n}={st}" for n, st in zip(names, svc.stdout.split())))
except Exception as exc:  # noqa: BLE001
    print(f"服务状态读取失败: {exc}")
rows = q("SELECT count(DISTINCT node_id) FROM execution_events WHERE ts_ingest > now() - interval '10 minutes'")
print(f"近10分钟有事件上报的节点数: {rows[0] if rows else 0}")
try:
    st = json.load(open("/srv/trader-v3/scripts/.order_lifecycle_state.json"))
    bf = int(st.get("brainfail:primary", 0))
    print("大脑主通道探活: " + (f"连续失败 {bf} 次(备胎在岗)" if bf else "正常"))
except Exception:  # noqa: BLE001
    print("大脑探活状态: (监控状态文件不可读)")
try:
    j = subprocess.run(
        ["journalctl", "-u", "trader-v3-hermes-feeder", "--since", f"-{DAYS}d", "--no-pager", "-q"],
        capture_output=True, text=True, timeout=15,
    )
    brainfails = sum(1 for line in j.stdout.splitlines() if "brain failure" in line)
    print(f"窗口内大脑失败重试次数: {brainfails}")
except Exception:  # noqa: BLE001
    pass

try:
    j = subprocess.run(
        ["journalctl", "-u", "trader-v3-hermes-feeder", "--since", f"-{DAYS}d", "--no-pager", "-q"],
        capture_output=True, text=True, timeout=15,
    )
    skips = [line.split("]: ", 1)[-1] for line in j.stdout.splitlines() if "SKIPPING" in line or "blocked" in line.lower()]
    print("\n### 信号投递异常")
    print("\n".join(skips[-5:]) if skips else "(无)")
except Exception as exc:  # noqa: BLE001
    print(f"\n### 信号投递异常\n(读取失败: {exc})")
