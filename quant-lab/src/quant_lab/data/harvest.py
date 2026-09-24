"""D-08 / PoC 1a：独立研究账号的 TDesktop JSON + 媒体 → 本地湖与无原文报告。

仅导入用户提供的文件；不读取 watcher 会话、不拉取 Telethon、不调用 LLM。
真实导出必须使用仓库外的 QUANT_LAB_DATA_ROOT（home-mini 外挂硬盘）。
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import pathlib
import sys
from typing import Any

from . import dedup, extract, normalize
from .lake import Layout
from .sources import ChannelListing, RawMessage, discover, ingest_tdesktop_dir, load_channel_whitelist


class HarvestRefusal(ValueError):
    """稳定、无原文的拒绝码；不把导出内容或底层异常写进报告。"""


def _ratio(n: int, count: int) -> float:
    return count / n if n else 0.0


def _channel_metrics(msgs: list[RawMessage]) -> dict[str, Any]:
    n = len(msgs)
    years = Counter(str(m.message_date.year) if m.message_date else "unknown" for m in msgs)
    return {
        "n_messages": n,
        "edit_ratio": _ratio(n, sum(m.edited_raw is not None or m.edited_unixtime is not None or m.edit_time_problem is not None for m in msgs)),
        "image_ratio": _ratio(n, sum(any(ref.kind == "photo" for ref in m.media) for m in msgs)),
        "reply_ratio": _ratio(n, sum(m.reply_to_message_id is not None for m in msgs)),
        "year_distribution": dict(sorted(years.items())),
    }


def _merged_channel_reports(listings: list[ChannelListing], ordinary: list[RawMessage], messages: list[RawMessage]) -> list[dict[str, Any]]:
    groups: dict[int, list[ChannelListing]] = {}
    for listing in listings:
        if listing.ingested:
            groups.setdefault(listing.channel_id, []).append(listing)
    rows: list[dict[str, Any]] = []
    for channel_id in sorted(groups):
        group = groups[channel_id]
        rec: dict[str, Any] = {"id": channel_id, **_channel_metrics([m for m in ordinary if m.channel_id == channel_id])}
        account_names = [c.name for c in group if c.from_account and c.name]
        if any(c.from_account for c in group):
            rec["name"] = account_names[0] if account_names else ""
            dates = [m.message_date for m in messages if m.channel_id == channel_id and m.message_date]
            rec["start"] = min(dates).isoformat() if dates else None
            rec["end"] = max(dates).isoformat() if dates else None
        rows.append(rec)
    return rows


def _repository_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[4]


def _data_root() -> pathlib.Path:
    value = os.environ.get("QUANT_LAB_DATA_ROOT")
    if not value or not value.strip():
        raise HarvestRefusal("DATA_ROOT_UNSET")
    root = pathlib.Path(value).expanduser().resolve()
    if root.is_relative_to(_repository_root()):
        raise HarvestRefusal("DATA_ROOT_IN_REPOSITORY")
    # 已存在的湖子目录/文件也不能通过符号链接把原文写到根外。
    for target in (root / "lake", root / "quarantine"):
        if any(not p.resolve().is_relative_to(root) for p in [target, *target.rglob("*")]):
            raise HarvestRefusal("DATA_ROOT_SYMLINK_ESCAPE")
    return root


def run(*, export_dir: str | os.PathLike | None = None) -> dict[str, Any]:
    root = _data_root()
    default_export = root / "import" / "telegram"
    source = pathlib.Path(export_dir).expanduser().resolve() if export_dir is not None else default_export
    if not source.is_dir():
        raise HarvestRefusal("EXPORT_DIR_NOT_FOUND")
    if not any(source.iterdir()):
        raise HarvestRefusal("EXPORT_DIR_EMPTY")
    exports = discover(source, tdesktop_only=True)
    if not exports:
        raise HarvestRefusal("TDESKTOP_EXPORT_NOT_FOUND")
    for _, directory in exports:
        for path in (directory / "result.json", directory / "export_manifest.json"):
            if not path.resolve().is_relative_to(source.resolve()):
                raise HarvestRefusal("EXPORT_SYMLINK_ESCAPE")
    try:
        allowed = load_channel_whitelist(default_export / "channels.txt")
        scan = ingest_tdesktop_dir(source, allowed_peer_ids=allowed)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError):
        raise HarvestRefusal("TDESKTOP_EXPORT_INVALID") from None
    messages = scan.messages
    ordinary = [m for m in messages if m.message_type == "message"]
    if not ordinary and allowed is None and not scan.whole_account:
        raise HarvestRefusal("TDESKTOP_EXPORT_EMPTY")

    # 分母是本次导出的普通消息（含重复快照），不是湖中历次导入的累计行。
    n = len(ordinary)
    candidates = sum(extract.parse_message(m.text).kind not in ("analysis", "chatter", "result_post", "undecidable") for m in ordinary)
    years = Counter(str(m.message_date.year) if m.message_date else "unknown" for m in ordinary)
    files = sorted({(m.channel_id, m.raw_hash) for m in messages})
    in_scope = [c for c in scan.listings if c.ingested]
    present = {c.channel_id for c in scan.listings}
    missing = sorted(allowed - present) if allowed is not None else []
    channel_ids = sorted({c.channel_id for c in in_scope} | {m.channel_id for m in messages})
    report = {
        "poc": "1a", "source": "tdesktop", "status": "ok",
        "n_messages": n, "n_service_messages": len(messages) - n,
        "denominator": "ordinary_messages_in_this_export",
        "edit_ratio": (sum(m.edited_raw is not None or m.edited_unixtime is not None or m.edit_time_problem is not None for m in ordinary) / n) if n else 0.0,
        "image_ratio": (sum(any(ref.kind == "photo" for ref in m.media) for m in ordinary) / n) if n else 0.0,
        "reply_ratio": (sum(m.reply_to_message_id is not None for m in ordinary) / n) if n else 0.0,
        "year_distribution": dict(sorted(years.items())),
        "signal_rate": {"value": (candidates / n) if n else 0.0, "n_candidates": candidates, "method": "deterministic_parser_candidate_rate", "verified": False},
        "channels": channel_ids,
        "exports": [{"channel_id": peer, "sha256": digest} for peer, digest in files],
        "manual_review": {"status": "not_run", "reason": "用户决定暂不人工抽查"},
        "telethon_comparison": {"status": "not_run", "reason": "需要独立账号凭据；严禁使用生产 watcher 会话"},
        "ocr": {"status": "not_run", "reason": "图片暂无 OCR"},
        "skipped_by_type": dict(scan.skipped_by_type),
        "whitelist": {
            "enabled": allowed is not None,
            "ids": sorted(allowed) if allowed is not None else [],
            "missing": missing,
        },
        "channel_reports": _merged_channel_reports(in_scope, ordinary, messages),
    }
    layout = Layout.from_root(root)
    normalized = normalize.run(source, layout, tdesktop_only=True, allowed_peer_ids=allowed)
    dedup.run(layout)
    extract.run(layout)
    report["batch_id"] = normalized["batch_id"]
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="quant_lab.data.harvest", description=__doc__)
    p.add_argument("--report", required=True)
    p.add_argument("--export-dir")
    a = p.parse_args(argv)
    try:
        report = run(export_dir=a.export_dir)
        target = pathlib.Path(a.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except HarvestRefusal as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, OverflowError):
        print(json.dumps({"status": "refused", "reason": "HARVEST_FAILED"}), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
