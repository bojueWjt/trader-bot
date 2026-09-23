#!/usr/bin/env python3
"""审查文件的终裁门（OR-05 verify 用）。

只认**正文**里的正式终裁行：代码块（``` 或 ~~~ 围起来的部分）里出现的「终裁：pass」是示例，不是裁定。
允许保留历史轮次；判定只看最后一轮：
- 最后一条正式终裁必须是 pass；
- 上一条正式终裁之后、最后一条之前，必须恰有一条「证据完整性：完成」。
无终裁、fail、insufficient、只有 pass 没有证据完整性、证据完整性未完成，一律非零退出。

用法：python3 scripts/review_gate.py <审查文件>
"""
from __future__ import annotations

import re
import sys

_VERDICT = re.compile(r"^终裁：(\S+)\s*$")
_EVIDENCE = re.compile(r"^证据完整性：(\S+)\s*$")
#: 围栏行（CommonMark 0.31 §4.5）：至多 3 个 ASCII 空格缩进，3 个以上同种字符（` 或 ~），其后为信息串
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
#: 渲染后不可见或原样输出的 HTML 块（CommonMark §4.6 第 1、2 类）：开头与各自的结束标记
_HTML_RAW = (
    (re.compile(r"^ {0,3}<(pre|script|style|textarea)(\s|>|$)", re.I), re.compile(r"</(pre|script|style|textarea)>", re.I)),
    (re.compile(r"^ {0,3}<!--"), re.compile(r"-->")),
)


def _only_spaces_or_tabs(rest: str) -> bool:
    """关闭围栏之后只允许 ASCII 空格与制表符（§4.5）；str.strip() 会把 U+00A0、U+3000 也当空白，不能用。"""
    return rest.strip(" \t") == ""


def formal_lines(text: str) -> list[str]:
    """去掉代码块与原样 HTML 块后的正文行。未闭合的块按「到文末都是非正文」处理。

    围栏按 CommonMark 判：开围栏记下字符与长度；反引号围栏的信息串不得含反引号；
    只有**同字符、长度不短于开围栏、其后只有 ASCII 空格或制表符**的行才关闭它。
    HTML 注释与 <pre>/<script>/<style>/<textarea> 块里的内容渲染后不可见或原样输出，同样不算正文。
    （OR-05 三审 §10.2、四审 §11）
    """
    out, fence, html_end = [], None, None
    for line in text.splitlines():
        if html_end is not None:
            if html_end.search(line):
                html_end = None
            continue
        m = _FENCE.match(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = (m.group(1)[0], len(m.group(1)))
                continue
            opened = next(((start, end) for start, end in _HTML_RAW if start.match(line)), None)
            if opened is not None:
                if not opened[1].search(line[opened[0].match(line).end():]):
                    html_end = opened[1]
                continue
            out.append(line)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and _only_spaces_or_tabs(m.group(2)):
            fence = None
    return out


def check(text: str) -> tuple[int, str]:
    lines = formal_lines(text)
    idx = [i for i, ln in enumerate(lines) if _VERDICT.match(ln)]
    if not idx:
        return 1, "没有正式终裁行"
    last = _VERDICT.match(lines[idx[-1]]).group(1)
    if last != "pass":
        return 1, f"最后一轮终裁为 {last}"
    start = idx[-2] + 1 if len(idx) > 1 else 0
    marks = [m.group(1) for ln in lines[start:idx[-1]] if (m := _EVIDENCE.match(ln))]
    if marks != ["完成"]:
        return 1, f"最后一轮的证据完整性标记为 {marks}，须恰为一条「完成」"
    return 0, f"最后一轮终裁 pass，证据完整性完成（共 {len(idx)} 轮）"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        text = open(argv[1], encoding="utf-8").read()
    except OSError as exc:
        print(f"读不到审查文件：{exc}", file=sys.stderr)
        return 1
    rc, why = check(text)
    print(why)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
