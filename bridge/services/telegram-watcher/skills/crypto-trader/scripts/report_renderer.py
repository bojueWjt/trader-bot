#!/usr/bin/env python3
"""
交易日报 Markdown 渲染器

生成 Hexo 兼容的 Markdown 日报，直接写入博客 source/report/index.md。
Hexo server 实时渲染，通过 blog.balen.wang/blog/report/ 访问。

Usage:
    # 从 stdin 读取 JSON
    echo '{"date":"2026-02-10",...}' | python3 report_renderer.py

    # 从文件读取 JSON
    python3 report_renderer.py --input data.json

    # 指定输出路径（默认写入 Hexo source）
    python3 report_renderer.py --input data.json --output /path/to/output.md

JSON 结构:
{
    "date": "2026-02-10",
    "generated_at": "2026-02-10 08:00:15",
    "account": {
        "balance": 1926.92,
        "margin_used": 500.0,
        "pnl": 120.5
    },
    "positions": [
        {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "qty": 0.357,
            "entry": 68250,
            "current": 69000,
            "sl": 67550,
            "pnl": 267.75
        }
    ],
    "pending_orders": [
        {
            "symbol": "DOTUSDT",
            "side": "BUY",
            "qty": 100,
            "price": 4.85
        }
    ],
    "signals_24h": [
        {
            "symbol": "BTCUSDT",
            "action": "LONG",
            "status": "executed",
            "time": "09:15"
        }
    ],
    "risk": {
        "total_exposure": 24385,
        "exposure_pct": 12.6
    }
}
"""

import argparse
import json
import sys
import os
from datetime import datetime

# Default output directory: Hexo blog source
HEXO_REPORT_DIR = os.path.expanduser(
    "~/.openclaw/workspace/blog/source/report"
)


def format_number(n, decimals=2):
    """Format number with commas and fixed decimals."""
    if n is None:
        return "-"
    if isinstance(n, int) or (isinstance(n, float) and n == int(n) and abs(n) > 100):
        return f"{int(n):,}"
    return f"{n:,.{decimals}f}"


def pnl_display(pnl):
    """Format PnL with sign and color hint."""
    if pnl is None or pnl == 0:
        return "-"
    sign = "+" if pnl > 0 else ""
    return f"{sign}{format_number(pnl)}"


def side_emoji(side):
    """Return emoji for side."""
    s = side.upper()
    if s in ("LONG", "BUY"):
        return "🟢"
    elif s in ("SHORT", "SELL"):
        return "🔴"
    return ""


def status_emoji(status):
    """Return emoji for signal status."""
    s = status.lower()
    if s in ("executed", "filled", "open"):
        return "✅"
    elif s in ("pending", "pending_entry", "waiting"):
        return "⏳"
    elif s in ("cancelled", "canceled", "failed"):
        return "❌"
    elif s in ("partial", "partial_closed"):
        return "🔶"
    return "•"


def render_report(data: dict) -> str:
    """Render report data dict to Hexo-compatible Markdown string."""
    date = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    generated_at = data.get("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    account = data.get("account", {})
    positions = data.get("positions", [])
    pending = data.get("pending_orders", [])
    signals = data.get("signals_24h", [])
    risk = data.get("risk", {})

    lines = []

    # Hexo front matter
    lines.append("---")
    lines.append(f"title: 📊 交易日报 — {date}")
    lines.append(f"date: {generated_at}")
    lines.append("layout: page")
    lines.append("comments: false")
    lines.append("---")
    lines.append("")

    # Account overview
    balance = account.get("balance", 0)
    margin = account.get("margin_used")
    pnl = account.get("pnl")
    available = balance - margin if margin else None

    lines.append("## 💰 账户概览")
    lines.append("")
    lines.append("| 项目 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 余额 | ${format_number(balance)} |")
    if margin is not None:
        lines.append(f"| 已用保证金 | ${format_number(margin)} |")
    if available is not None:
        lines.append(f"| 可用余额 | ${format_number(available)} |")
    if pnl is not None:
        lines.append(f"| 今日浮盈亏 | ${pnl_display(pnl)} |")
    lines.append("")

    # Positions + Pending — rendered as image
    if positions or pending:
        try:
            from report_image import render_positions_image
            img_dir = os.path.join(os.path.dirname(HEXO_REPORT_DIR), "report", "images")
            os.makedirs(img_dir, exist_ok=True)
            img_path = os.path.join(img_dir, f"{date}-positions.png")
            render_positions_image(data, img_path)
            lines.append(f"![持仓概览](/report/images/{date}-positions.png)")
            lines.append("")
        except Exception as e:
            # Fallback to text if image generation fails
            lines.append(f"## 📈 当前持仓 ({len(positions)} 笔)")
            lines.append("")
            lines.append(f"*图片生成失败: {e}*")
            lines.append("")
    else:
        lines.append("## 📈 当前持仓 (0 笔)")
        lines.append("")
        lines.append("*暂无持仓*")
        lines.append("")

    # Signals
    lines.append(f"## 📋 过去 24h 信号 ({len(signals)} 条)")
    lines.append("")
    if signals:
        lines.append("| 时间 | 品种 | 操作 | 状态 |")
        lines.append("|------|------|------|------|")
        for s in signals:
            emoji = status_emoji(s.get("status", ""))
            lines.append(
                f"| {s.get('time', '-')} | {s['symbol']} | "
                f"{s.get('action', '-')} | {emoji} {s.get('status', '-')} |"
            )
    else:
        lines.append("*过去 24 小时无信号*")
    lines.append("")

    # Risk
    if risk:
        lines.append("## ⚠️ 风险提示")
        lines.append("")
        exposure = risk.get("total_exposure", 0)
        pct = risk.get("exposure_pct", 0)
        lines.append(f"- 总敞口：${format_number(exposure)}（占余额 **{pct:.1f}%**）")
        if risk.get("notes"):
            for note in risk["notes"]:
                lines.append(f"- {note}")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append(f"> 生成时间：{generated_at} · *个人记录，不构成投资建议*")
    lines.append("")

    return "\n".join(lines)


def generate_index(report_dir: str):
    """Generate index.md listing all reports, newest first."""
    reports = []
    for f in os.listdir(report_dir):
        if f.endswith(".md") and f != "index.md":
            date_str = f.replace(".md", "")
            reports.append(date_str)
    reports.sort(reverse=True)

    lines = []
    lines.append("---")
    lines.append("title: 📊 交易日报归档")
    lines.append(f"date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("layout: page")
    lines.append("comments: false")
    lines.append("---")
    lines.append("")
    lines.append("| 日期 | 链接 |")
    lines.append("|------|------|")
    for r in reports:
        lines.append(f"| {r} | [查看日报](/blog/report/{r}/) |")
    lines.append("")

    index_path = os.path.join(report_dir, "index.md")
    with open(index_path, "w") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="生成交易日报 Markdown (Hexo 兼容)")
    parser.add_argument("--input", "-i", default=None, help="JSON 输入文件（默认 stdin）")
    parser.add_argument(
        "--output", "-o", default=None,
        help="输出 MD 路径（默认按日期生成到 Hexo source/report/YYYY-MM-DD.md）"
    )
    args = parser.parse_args()

    # Read JSON
    if args.input:
        with open(args.input, "r") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    date = data.get("date", datetime.now().strftime("%Y-%m-%d"))

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        os.makedirs(HEXO_REPORT_DIR, exist_ok=True)
        output_path = os.path.join(HEXO_REPORT_DIR, f"{date}.md")

    # Render
    md = render_report(data)

    # Write
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(md)

    # Update index
    if not args.output:
        generate_index(HEXO_REPORT_DIR)

    url = f"https://blog.balen.wang/blog/report/{date}/"
    result = {
        "status": "ok",
        "output": output_path,
        "url": url,
        "index": "https://blog.balen.wang/blog/report/",
        "date": date,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
