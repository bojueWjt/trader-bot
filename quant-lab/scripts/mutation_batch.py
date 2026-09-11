"""G2 突变取证批次的统一执行器——把 A29/A33/A34/A37/A38 从「记得照做」变成「做不到就拒绝出结果」。

立这个模块的直接原因（G0 A38 §判例 6 的一般义务）：
    凡为他人立下的检查性质要求（集合相等、判别性、自证会红、基线先于过程），
    立规者须同期审计自己的同类工具是否满足。

我照做后发现：我四个突变脚本**没有一个内建活性对照**——每次都是我手工把对照当成一个
case 塞进表里，再靠我自己看它有没有变红。**那正是 A24 §12.2 判过走不通的那条路：
依赖人去记得。** 而且我确实漏过一次：用了一条本批选择器根本碰不到的对照
（`vision.expected_rows+1` 配 `test_boundary_*`），四类全绿，我差点据此怀疑一个健康的取证器。

本模块强制：
  A29  真实写盘 + 子进程隔离；基线/注入/还原三次各起新进程；还原以内容 SHA256 为准。
  A33  每批必须有活性对照。
  A38  **对照必须在本批实际使用的选择器下变红**——否则拒绝出结果。
       （A38 自带验证：对照红了，可达性就由那个红本身证明，不需另做可达性论证。
         "同批"以**选择器同一性**定义，不以会话或墙钟分组定义。）
  A34/A37  批前以 tree_guard 声明并断言差异，批后逐文件回到批前快照。
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from tree_guard import G2_PATHS, TreeGuard  # noqa: E402

ROOT = pathlib.Path("/Users/balen/projects/trader-bot/quant-lab")
PY_BIN = ROOT / ".venv-g2/bin/python"
ENV = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(pathlib.Path.home())}


class ControlNotRed(RuntimeError):
    """A38：对照没有在本批选择器下变红 → 本批一切绿结果不得采信。"""


def _run(selectors: list[str]) -> tuple[int, str]:
    r = subprocess.run([str(PY_BIN), "-m", "pytest", *selectors, "-q", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True, env=ENV)
    return r.returncode, (r.stdout + r.stderr).strip().split("\n")[-1][:70]


def _inject(rel: str, old: str, new: str, selectors: list[str]) -> tuple[int, str, bool]:
    tgt = ROOT / rel
    raw = tgt.read_bytes()
    before = hashlib.sha256(raw).hexdigest()
    src = raw.decode()
    if src.count(old) != 1:
        raise ValueError(f"{rel}: 注入点出现 {src.count(old)} 次，必须唯一")
    tgt.write_text(src.replace(old, new, 1))
    try:
        rc, tail = _run(selectors)
    finally:
        tgt.write_bytes(raw)
    return rc, tail, hashlib.sha256(tgt.read_bytes()).hexdigest() == before


def run_batch(*, selectors: list[str], control: tuple[str, str, str, str], cases: list[tuple],
              declared_diff: list[str] | None = None) -> list[dict]:
    """control = (名称, 相对路径, 原文, 替换)；cases 同构。

    **control 是必填参数，不是可选项**——一个可以省略对照的批次执行器，
    等于把 A33 退回成「记得写」。
    """
    guard = TreeGuard(G2_PATHS)
    actual = [guard.path_of(e) for e in guard._status()]
    print(guard.assert_declared(declared_diff if declared_diff is not None else actual, "批前"))
    snap = guard.snapshot()

    base_rc, base_tail = _run(selectors)
    if base_rc != 0:
        raise ControlNotRed(f"批前基线本身不绿（{base_tail}），无法据此判读任何注入结果")

    cname, crel, cold, cnew = control
    crc, ctail, crestored = _inject(crel, cold, cnew, selectors)
    if crc == 0:
        raise ControlNotRed(
            f"A38：活性对照「{cname}」在本批选择器下**没有变红**（{ctail}）。\n"
            f"  本批选择器：{selectors}\n"
            f"  这条绿不携带信息——它由两种情形同样产生：取证器健康但对照不可达、以及取证器失效。\n"
            f"  而 A33 立意要防的正是「失效产出的是绿」，所以唯一可能输出绿的对照恰好把这条规则废掉了。\n"
            f"  请换一条本批选择器**确实会触及**的对照，而不是在别处已知必红的对照。")
    if not crestored:
        raise ControlNotRed(f"对照注入后还原失败（SHA256 不符），本批作废")
    print(f"A38 活性对照「{cname}」：本批选择器下 rc={crc} {ctail} → **RED，可达性由该红自证** ✓")

    out = []
    for name, rel, old, new, *rest in cases:
        rc, tail, restored = _inject(rel, old, new, selectors)
        out.append({"case": name, "rc": rc, "tail": tail, "restored": restored,
                    "expect_red": rest[0] if rest else True})
    print(guard.assert_matches(snap, "批后"))
    return out
