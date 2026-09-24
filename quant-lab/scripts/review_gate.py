#!/usr/bin/env python3
"""审查终裁门（OR-05 verify 用）：终裁以**封存记录**为准，不从 Markdown 渲染里推断。

为什么不再解析 Markdown：OR-05 三审到五审反复证明，「哪些行算正文」取决于完整的 CommonMark 渲染规则
（围栏长度、Unicode 空白、HTML 块七类、列表容器……），在门控脚本里逐条追赶，每一轮都会有下一条。
终裁是一个裁定，不该是渲染结果的推论。于是改成：

- 审查者在每轮结束时**封存**：`review_gate.py --seal <审查.md> <记录.json> --round N --verdict V [--evidence-complete]`，
  记录轮次、终裁、证据完整性，以及封存那一刻审查文件的 sha256；
- 门只核三件事，都不需要理解 Markdown：
  1. 记录的字段与类型严格合法（键集固定，verdict 取值封闭，evidence_complete 必须是布尔值）；
  2. 审查文件当前字节的 sha256 等于记录里的值——封存后审查文件改动任何一个字节，门即失败；
  3. 审查文件最后一个非空行恰为 `终裁：<记录中的 verdict>`——人读的结论与机读的记录一致。
- 最后判定：verdict 为 pass 且 evidence_complete 为 true，才退出 0。

用法：
  python3 scripts/review_gate.py <审查.md> <记录.json>
  python3 scripts/review_gate.py --seal <审查.md> <记录.json> --round N --verdict pass|fail|insufficient [--evidence-complete]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

VERDICTS = ("pass", "fail", "insufficient")
KEYS = {"review", "round", "verdict", "evidence_complete", "review_sha256"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _last_nonempty_line(path: Path) -> str:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def check(review: Path, record: Path) -> tuple[int, str]:
    if not review.is_file():
        return 1, f"审查文件不存在：{review}"
    if not record.is_file():
        return 1, f"没有封存记录：{record}（审查者须用 --seal 封存本轮终裁）"
    try:
        rec = json.loads(record.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return 1, f"封存记录不是合法 JSON：{exc}"
    if not isinstance(rec, dict) or set(rec) != KEYS:
        return 1, f"封存记录键集须恰为 {sorted(KEYS)}，得到 {sorted(rec) if isinstance(rec, dict) else type(rec).__name__}"
    if rec["review"] != review.name:
        return 1, f"封存记录指向 {rec['review']!r}，不是 {review.name!r}"
    if type(rec["round"]) is not int or rec["round"] < 1:
        return 1, f"round 须为正整数，得到 {rec['round']!r}"
    if rec["verdict"] not in VERDICTS:
        return 1, f"verdict 须为 {VERDICTS} 之一，得到 {rec['verdict']!r}"
    if type(rec["evidence_complete"]) is not bool:
        return 1, f"evidence_complete 须为布尔值，得到 {rec['evidence_complete']!r}"
    if rec["review_sha256"] != _sha256(review):
        return 1, "审查文件在封存之后被改动过（sha256 不符）：须重新审查并封存"
    last = _last_nonempty_line(review)
    if last != f"终裁：{rec['verdict']}":
        return 1, f"审查文件最后一个非空行 {last!r} 与封存终裁 {rec['verdict']!r} 不一致"
    if rec["verdict"] != "pass":
        return 1, f"第 {rec['round']} 轮终裁为 {rec['verdict']}"
    if rec["evidence_complete"] is not True:
        return 1, f"第 {rec['round']} 轮证据完整性未完成"
    return 0, f"第 {rec['round']} 轮终裁 pass，证据完整性完成，审查文件与封存一致"


def seal(review: Path, record: Path, round_: int, verdict: str, evidence_complete: bool) -> None:
    if verdict not in VERDICTS:
        raise SystemExit(f"verdict 须为 {VERDICTS} 之一")
    if round_ < 1:
        raise SystemExit("round 须为正整数")
    last = _last_nonempty_line(review)
    if last != f"终裁：{verdict}":
        raise SystemExit(f"封存前审查文件最后一个非空行须为「终裁：{verdict}」，现为 {last!r}")
    rec = {"review": review.name, "round": round_, "verdict": verdict,
           "evidence_complete": evidence_complete, "review_sha256": _sha256(review)}
    record.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="review_gate.py", description="审查终裁门")
    ap.add_argument("--seal", action="store_true")
    ap.add_argument("review", type=Path)
    ap.add_argument("record", type=Path)
    ap.add_argument("--round", type=int)
    ap.add_argument("--verdict")
    ap.add_argument("--evidence-complete", action="store_true")
    a = ap.parse_args(argv[1:])
    if a.seal:
        if a.round is None or a.verdict is None:
            ap.error("--seal 需要 --round 与 --verdict")
        seal(a.review, a.record, a.round, a.verdict, a.evidence_complete)
        print(f"已封存第 {a.round} 轮：{a.verdict}，证据完整性 {'完成' if a.evidence_complete else '未完成'}")
        return 0
    rc, why = check(a.review, a.record)
    print(why)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
