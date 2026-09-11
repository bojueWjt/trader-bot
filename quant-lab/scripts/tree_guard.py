"""A34(2) 护栏：真实树注入法的失效面是**残骸**，护栏是批前批后各断言一次工作树与已提交基线一致。

背景：G0 R61 发现 quant-lab 的 src/tests **从未入过 git**（src 48 个 .py 只跟踪 3 个、tests 56 个只跟踪 2 个），
已于 `bef5750` 一次性入库。在那之前，**我的真实树取证器没有可比对的基线**——中途被杀留下的突变无从检出。
现在有了，这个模块就是把那条护栏真正接上。

本文件按契约 §20.3 与 G0 A35 入库：**认证链上的工具与它认证的对象同列**，
否则那条链的最后一环（护栏自己）没有基线。

用法：
    guard = TreeGuard(["quant-lab/src", "quant-lab/tests"])
    guard.assert_clean("批前")      # 有残骸就抛，不要在脏树上开始
    ...                             # 注入 / 还原
    guard.assert_clean("批后")      # 有残骸就抛，并把残骸文件列出来
"""
from __future__ import annotations
import pathlib
import subprocess


class DirtyTree(RuntimeError):
    pass


class TreeGuard:
    def __init__(self, paths: list[str], repo: str = "/Users/balen/projects/trader-bot") -> None:
        self.paths, self.repo = paths, repo

    def _status(self) -> list[str]:
        """porcelain v1：XY + 空格 + 路径。**不要用定长切片取路径**——
        我第一版用 `l[3:]`，在某些状态码下会把路径首字符一起切掉（实测把
        `quant-lab/...` 切成了 `uant-lab/...`），于是一个合法差异会被当成
        「未被归因」而拒绝开工，或者更糟：一个真实残骸因路径对不上而被漏掉。"""
        r = subprocess.run(["git", "status", "--porcelain", "-z", "--", *self.paths],
                           cwd=self.repo, capture_output=True, text=True, check=True)
        return [e for e in r.stdout.split("\0") if e.strip()]

    @staticmethod
    def path_of(entry: str) -> str:
        """从 porcelain 条目取路径：跳过 2 位状态码与其后的空白。"""
        return entry[2:].lstrip()

    def head(self) -> str:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo,
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def assert_clean(self, when: str) -> str:
        """工作树必须与已提交基线一致；否则抛异常并列出残骸。"""
        dirty = self._status()
        if dirty:
            raise DirtyTree(
                f"{when}：工作树与已提交基线不一致，共 {len(dirty)} 个文件。\n"
                + "\n".join(f"    {d}" for d in dirty[:20])
                + "\n  在脏树上做的取证不可采信：无法区分「这是我这批注入留下的」与「这是上一批的残骸」。"
            )
        return f"{when}：工作树 == HEAD({self.head()})，无残骸 ✓"



    def snapshot(self) -> dict:
        """批前快照：当工作树**合法地**不等于 HEAD 时（例如修复已完成、尚未提交），
        用它做本批的基线。

        与 A35 不冲突：A35 禁的是**由被认证过程在自身起点逐案采集**的基线——那种基线
        对早于该起点的污染失明。本方法在**整批开工前采集一次**，且**显式记录它与 HEAD
        的差异**，差异内容随证据一并留存，因此"开始时是什么样"是可复核的，
        不是被悄悄当成了"干净"。
        """
        import hashlib
        root = pathlib.Path(self.repo)
        files = {}
        for rel in self.paths:
            for f in sorted((root / rel).rglob("*.py")):
                files[str(f.relative_to(root))] = hashlib.sha256(f.read_bytes()).hexdigest()
        return {"head": self.head(), "diff_vs_head": self._status(), "files": files}

    def assert_matches(self, snap: dict, when: str) -> str:
        """批后必须逐文件回到批前快照。"""
        import hashlib
        root = pathlib.Path(self.repo)
        bad = []
        for rel, want in snap["files"].items():
            f = root / rel
            got = hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else "<缺失>"
            if got != want:
                bad.append(f"{rel}: {want[:12]} → {got[:12]}")
        # S44（十三审）：**只比对快照里已有的文件，对批中新增的文件完全失明。**
        # 我在 A24 里要求别人的调用点登记必须是**集合相等**（新增调用方与缺失调用方都要红），
        # 然后在自己的护栏里写了一个**单向**检查——只问"已知文件有没有变"，没问"有没有多出文件"。
        # 一个批中新增的文件正是残骸最典型的形态（注入新函数、写临时模块），而它恰好落在盲区里。
        now = {str(f.relative_to(root)) for rel in self.paths for f in (root / rel).rglob("*.py")}
        added = sorted(now - set(snap["files"]))
        if added:
            bad += [f"{a}: <快照中不存在> → 批中新增（未被快照覆盖，疑为残骸）" for a in added]
        if bad:
            raise DirtyTree(f"{when}：{len(bad)} 个文件未回到批前快照\n" + "\n".join("    " + b for b in bad[:20]))
        n = len(snap["diff_vs_head"])
        note = f"（批前基线与 HEAD({snap['head']}) 差异 {n} 个文件，已记录）" if n else f"（批前基线 == HEAD({snap['head']})）"
        return f"{when}：{len(snap['files'])} 个文件逐一回到批前快照 ✓ {note}"

    def assert_declared(self, expected: list[str], when: str) -> str:
        """A37：批前**声明**预期差异清单，断言「实际差异 == 声明差异」。

        为什么"记录差异"不够（G0 A37）：快照把污染**变可见**了，但**看见不等于分辨**——
        **一条上批残留的突变，与一条合法在途修改，在快照差异里长得一模一样。**
        归因才让机器当场能拒，而记录只让人事后能查。

        `expected` 是仓库相对路径清单（顺序无关）。任何**未被归因**的差异即拒绝开工；
        声明了却没出现的，同样报出来——声明与实际必须**相等**，不是包含。
        """
        actual = {self.path_of(e) for e in self._status()}
        want = set(expected)
        unattributed, missing = sorted(actual - want), sorted(want - actual)
        if unattributed or missing:
            msg = [f"{when}：实际差异与声明差异不符。"]
            if unattributed:
                msg.append(f"  **未被归因的差异 {len(unattributed)} 个**（可能是上一批残留的突变，也可能是别人在途的改动——"
                           f"在快照里这两者长得一模一样，所以必须先归因再开工）：")
                msg += [f"    {u}" for u in unattributed[:20]]
            if missing:
                msg.append(f"  声明了但未出现的 {len(missing)} 个（声明过期或写错）：")
                msg += [f"    {m}" for m in missing[:20]]
            raise DirtyTree("\n".join(msg))
        return f"{when}：{len(actual)} 个差异全部已归因 ✓（HEAD={self.head()}）"

    def selftest(self) -> str:
        """A33 活性自检：证明本护栏确实会红。

        **不碰任何既有文件。** 早先我用「追加一行到 vision.py 再 git checkout 还原」做活性探针，
        那是错的——当时后台修复任务正在改这棵树，若它恰好在写同一个文件，我的还原会毁掉它的工作。
        这正是我一直在要求别人不要做的事（测试写生产文件、`finally` 挡不住 SIGKILL）。
        改为新建一个本护栏自己拥有的未跟踪临时文件：它同样能让 `git status` 非空，
        但**不修改任何既有内容**，因此与任何在途任务都不冲突。
        """
        probe = pathlib.Path(self.repo) / self.paths[0] / ".tree_guard_liveness_probe"
        was_clean = not self._status()
        probe.write_text("liveness probe\n", encoding="utf-8")
        try:
            self.assert_clean("活性探针")
        except DirtyTree:
            fired = True
        else:
            fired = False
        finally:
            probe.unlink(missing_ok=True)
        if not fired:
            raise RuntimeError("活性自检失败：护栏在明确脏的树上没有报警，其任何绿结果都不可采信")
        tail = self.assert_clean("探针还原后") if was_clean else "（开工前树本就非干净，不强求还原后为空）"
        return f"活性自检 ✓ 护栏在脏树上确实报警；{tail}"


# 窗口所有权边界：G2 只拥有 market。
# 护栏的监视范围**必须等于所有权边界**——否则别的窗口的合法在途工作，在我的差异清单里
# 与我自己上一批的残骸长得一模一样，而这正是 A37 要防的混淆，只是发生在**所有权轴**而非时间轴上。
# 我无法为 G3 的改动归因（我不知道它在做什么），所以我不应该去看它。
G2_PATHS = ["quant-lab/src/quant_lab/market", "quant-lab/tests/market"]


if __name__ == "__main__":
    import sys
    g = TreeGuard(G2_PATHS)
    if "--selftest" in sys.argv:
        print(g.selftest())
    else:
        print(g.assert_clean("自检"))
