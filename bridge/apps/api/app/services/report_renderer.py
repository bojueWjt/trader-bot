from __future__ import annotations

import json
from typing import Any

from app.services.report_snapshot import DailySnapshot


def render(snapshot: DailySnapshot) -> str:
    lines = [
        f"# Daily Trading Report {snapshot.date}",
        "",
        "## Account",
        *_render_section(snapshot.account),
        "",
        "## Trades",
        *_render_section(snapshot.trades),
        "",
        "## Signals",
        *_render_section(snapshot.signals),
        "",
        "## Risk",
        *_render_section(snapshot.risk),
        "",
        "## Positions",
        *_render_section(snapshot.positions),
    ]
    return "\n".join(lines)


def _render_section(value: Any) -> list[str]:
    if not value:
        return ["No data available."]
    if isinstance(value, dict):
        return [f"- {key}: {_format_value(value[key])}" for key in sorted(value)]
    if isinstance(value, list):
        return [f"- {_format_value(item)}" for item in value]
    return [_format_value(value)]


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)
