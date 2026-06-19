#!/usr/bin/env python3
"""
Briefing Generator for Crypto Trading System

CLI tool and importable module for generating structured morning briefing
reports from collected Telegram channel analysis stored in the SQLite database.

Usage (CLI):
    python3 briefing.py --db ~/projects/trading-data/trading.db generate --hours 24
    python3 briefing.py --db ~/projects/trading-data/trading.db list --hours 48
    python3 briefing.py --db ~/projects/trading-data/trading.db stats --hours 24

Usage (import):
    from briefing import BriefingGenerator
    bg = BriefingGenerator()
    report = bg.generate_report(hours=24)
    entries = bg.list_briefings(hours=48)
    stats = bg.get_stats(hours=24)
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

# Add scripts directory to path so we can import db_manager
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from db_manager import DatabaseManager

DEFAULT_DB_PATH = os.path.expanduser("~/projects/trading-data/trading.db")

# Category display names (Chinese labels for the report)
CATEGORY_DISPLAY = {
    "analysis": "分析观点",
    "news": "市场动态",
    "signal": "交易信号",
    "other": "其他",
}


# ---------------------------------------------------------------------------
# BriefingGenerator class (importable API)
# ---------------------------------------------------------------------------
class BriefingGenerator:
    """High-level interface for generating briefing reports."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._db = DatabaseManager(db_path=self.db_path)

    # -- helpers -------------------------------------------------------------
    def _get_channel_name_map(self) -> dict[str, str]:
        """Build a mapping of channel_id -> channel_name from channel_routing."""
        channels = self._db.list_channels()
        return {
            ch["channel_id"]: ch["channel_name"]
            for ch in channels
            if ch.get("channel_name")
        }

    def _resolve_channel_name(self, channel_id: str, name_map: dict[str, str]) -> str:
        """Return human-readable channel name, falling back to raw channel_id."""
        return name_map.get(channel_id) or channel_id

    def _category_label(self, category: str) -> str:
        """Return the Chinese display label for a category."""
        return CATEGORY_DISPLAY.get(category, CATEGORY_DISPLAY["other"])

    # -- public API ----------------------------------------------------------
    def generate_report(self, hours: int = 24) -> dict:
        """Generate a structured Markdown morning briefing report.

        Returns dict with keys: status, report, entry_count, channel_count.
        """
        if hours < 1:
            hours = 1
        entries = self._db.list_briefings(hours=hours)

        if not entries:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return {
                "status": "ok",
                "report": f"# 每日晨报 — {today}\n\n> 过去 {hours} 小时内无新素材。",
                "entry_count": 0,
                "channel_count": 0,
            }

        name_map = self._get_channel_name_map()

        # Group: channel_id -> category -> [content, ...]
        grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for entry in entries:
            cid = entry.get("channel_id", "unknown")
            cat = entry.get("category", "other")
            content = entry.get("content", "")
            grouped[cid][cat].append(content)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry_count = len(entries)
        channel_count = len(grouped)

        lines: list[str] = []
        lines.append(f"# 每日晨报 — {today}")
        lines.append("")
        lines.append(f"> 数据窗口：过去 {hours} 小时 | 共 {entry_count} 条素材")
        lines.append("")

        # Stable ordering: sort channels alphabetically by display name
        sorted_channels = sorted(
            grouped.keys(),
            key=lambda cid: self._resolve_channel_name(cid, name_map),
        )

        # Define a stable category order
        category_order = ["analysis", "news", "signal", "other"]

        for cid in sorted_channels:
            display_name = self._resolve_channel_name(cid, name_map)
            lines.append("---")
            lines.append("")
            lines.append(f"## 📊 频道: {display_name}")
            lines.append("")

            categories = grouped[cid]
            # Sort categories by predefined order; unknown categories go last
            sorted_cats = sorted(
                categories.keys(),
                key=lambda c: (category_order.index(c) if c in category_order else len(category_order), c),
            )

            for cat in sorted_cats:
                label = self._category_label(cat)
                lines.append(f"### {label}")
                for content in categories[cat]:
                    lines.append(f"- {content}")
                lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("*由 crypto-trader skill 自动生成，待 Claude 进一步整理。*")

        report = "\n".join(lines)

        return {
            "status": "ok",
            "report": report,
            "entry_count": entry_count,
            "channel_count": channel_count,
        }

    def list_briefings(self, hours: int = 24) -> list[dict]:
        """List briefing entries enriched with channel_name.

        Returns a list of briefing dicts, each with an added 'channel_name' field.
        """
        if hours < 1:
            hours = 1
        entries = self._db.list_briefings(hours=hours)
        name_map = self._get_channel_name_map()

        for entry in entries:
            cid = entry.get("channel_id", "unknown")
            entry["channel_name"] = self._resolve_channel_name(cid, name_map)

        return entries

    def get_stats(self, hours: int = 24) -> dict:
        """Return summary statistics for briefing entries in the given window.

        Returns dict with keys: status, total_entries, by_channel, by_category, hours.
        """
        if hours < 1:
            hours = 1
        entries = self._db.list_briefings(hours=hours)
        name_map = self._get_channel_name_map()

        # Count by channel
        channel_counts: dict[str, int] = defaultdict(int)
        category_counts: dict[str, int] = defaultdict(int)

        for entry in entries:
            cid = entry.get("channel_id", "unknown")
            cat = entry.get("category", "other")
            channel_counts[cid] += 1
            category_counts[cat] += 1

        by_channel = sorted(
            [
                {
                    "channel_id": cid,
                    "channel_name": self._resolve_channel_name(cid, name_map),
                    "count": count,
                }
                for cid, count in channel_counts.items()
            ],
            key=lambda x: x["count"],
            reverse=True,
        )

        by_category = sorted(
            [
                {"category": cat, "count": count}
                for cat, count in category_counts.items()
            ],
            key=lambda x: x["count"],
            reverse=True,
        )

        return {
            "status": "ok",
            "total_entries": len(entries),
            "by_channel": by_channel,
            "by_category": by_category,
            "hours": hours,
        }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def _json_out(data) -> None:
    """Print data as compact JSON to stdout."""
    print(json.dumps(data, ensure_ascii=False))


def _error_exit(message: str) -> None:
    """Print a JSON error message and exit with code 1."""
    _json_out({"status": "error", "message": message})
    sys.exit(1)


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate morning briefing reports from collected market analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    sub = parser.add_subparsers(dest="command")

    # generate
    p = sub.add_parser("generate", help="Generate a structured Markdown morning briefing report")
    p.add_argument("--hours", type=int, default=24, help="Look back N hours (default: 24)")

    # list
    p = sub.add_parser("list", help="List raw briefing entries with channel names resolved")
    p.add_argument("--hours", type=int, default=24, help="Look back N hours (default: 24)")

    # stats
    p = sub.add_parser("stats", help="Show summary statistics for briefing entries")
    p.add_argument("--hours", type=int, default=24, help="Look back N hours (default: 24)")

    return parser


# ---------------------------------------------------------------------------
# CLI main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        _error_exit("No command specified. Use --help for usage.")

    try:
        bg = BriefingGenerator(db_path=args.db)

        match args.command:
            case "generate":
                _json_out(bg.generate_report(hours=args.hours))

            case "list":
                _json_out(bg.list_briefings(hours=args.hours))

            case "stats":
                _json_out(bg.get_stats(hours=args.hours))

            case _:
                _error_exit("Unknown command. Use --help for usage.")

    except SystemExit:
        raise
    except Exception as e:
        _error_exit(str(e))


if __name__ == "__main__":
    main()
