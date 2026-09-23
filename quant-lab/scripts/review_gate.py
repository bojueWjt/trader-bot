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
#: 围栏行（CommonMark）：至多 3 格缩进，3 个以上同种字符（` 或 ~），其后为信息串
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def formal_lines(text: str) -> list[str]:
    """去掉代码块后的正文行。未闭合的代码块按「到文末都是代码」处理，里面的行一律不算。

    围栏按 CommonMark 判：开围栏记下字符与长度；只有**同字符、长度不短于开围栏、其后只有空白**的行才关闭它。
    所以 ```` 块里的 ``` 行、~~~~ 块里的 ~~~ 行、带尾随文字的 ```xxx 行都不会提前结束代码块（OR-05 三审 §10.2）。
    """
    out, fence = [], None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):    # 反引号围栏的信息串不得含反引号
                fence = (m.group(1)[0], len(m.group(1)))
                continue
            out.append(line)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
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
