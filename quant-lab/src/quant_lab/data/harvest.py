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
from .sources import discover, read_all


class HarvestRefusal(ValueError):
    """稳定、无原文的拒绝码；不把导出内容或底层异常写进报告。"""


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
    source = pathlib.Path(export_dir).expanduser().resolve() if export_dir is not None else root / "import" / "telegram"
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
        messages = list(read_all(source, tdesktop_only=True))
    except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError):
        raise HarvestRefusal("TDESKTOP_EXPORT_INVALID") from None
    ordinary = [m for m in messages if m.message_type == "message"]
    if not ordinary:
        raise HarvestRefusal("TDESKTOP_EXPORT_EMPTY")

    # 分母是本次导出的普通消息（含重复快照），不是湖中历次导入的累计行。
    n = len(ordinary)
    candidates = sum(extract.parse_message(m.text).kind not in ("analysis", "chatter", "result_post", "undecidable") for m in ordinary)
    years = Counter(str(m.message_date.year) if m.message_date else "unknown" for m in ordinary)
    files = sorted({(m.channel_id, m.raw_hash) for m in messages})
    report = {
        "poc": "1a", "source": "tdesktop", "status": "ok",
        "n_messages": n, "n_service_messages": len(messages) - n,
        "denominator": "ordinary_messages_in_this_export",
        "edit_ratio": sum(m.edited_raw is not None or m.edited_unixtime is not None or m.edit_time_problem is not None for m in ordinary) / n,
        "image_ratio": sum(any(ref.kind == "photo" for ref in m.media) for m in ordinary) / n,
        "reply_ratio": sum(m.reply_to_message_id is not None for m in ordinary) / n,
        "year_distribution": dict(sorted(years.items())),
        "signal_rate": {"value": candidates / n, "n_candidates": candidates, "method": "deterministic_parser_candidate_rate", "verified": False},
        "channels": sorted({m.channel_id for m in messages}),
        "exports": [{"channel_id": peer, "sha256": digest} for peer, digest in files],
        "manual_review": {"status": "not_run", "reason": "用户决定暂不人工抽查"},
        "telethon_comparison": {"status": "not_run", "reason": "需要独立账号凭据；严禁使用生产 watcher 会话"},
        "ocr": {"status": "not_run", "reason": "图片暂无 OCR"},
    }
    layout = Layout.from_root(root)
    normalized = normalize.run(source, layout, tdesktop_only=True)
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
