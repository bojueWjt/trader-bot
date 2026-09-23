"""OR-05 I07：看板上 OR-01 / OR-05 的 verify 必须能因它要挡的情况失败。

命令一律从 taskList.json 原文读出，在临时副本里执行（只替换审查正文或 frozen 值），不手抄命令。
二审指出上一版 OR-05 门对「只有 pass 没有证据完整性」「证据完整性未完成」「代码块里的示例 pass」都放行；
这里把整张门控矩阵固定下来，并附突变：换回 verifyHistory 里的旧门，同样的输入会被错放。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REVIEW = "docs/adr/review-G0-integration.md"


def _tasks():
    board = json.loads((ROOT / "taskList.json").read_text(encoding="utf-8"))
    return {t["id"]: t for t in board["modules"]["orchestrator"]["tasks"]}, board


@pytest.fixture()
def sandbox(tmp_path):
    for rel in ("contracts", "scripts"):
        shutil.copytree(ROOT / rel, tmp_path / rel)
    shutil.copy(ROOT / "taskList.json", tmp_path / "taskList.json")
    (tmp_path / "docs/adr").mkdir(parents=True)
    return tmp_path


def _run(cmd: str, cwd: Path, prefix: str = "") -> int:
    return subprocess.run(["bash", "-c", prefix + cmd], cwd=cwd, capture_output=True, text=True).returncode


FENCE = "```"
CASES = {
    "none": ("# 审查\n正文，没有终裁。\n", 1),
    "fail": ("证据完整性：完成\n终裁：fail\n", 1),
    "insufficient": ("证据完整性：完成\n终裁：insufficient\n", 1),
    "pass_then_fail": ("证据完整性：完成\n终裁：pass\n## 二审\n证据完整性：完成\n终裁：fail\n", 1),
    "bare_pass": ("终裁：pass\n", 1),
    "incomplete_pass": ("证据完整性：未完成\n终裁：pass\n", 1),
    "two_evidence_marks": ("证据完整性：完成\n证据完整性：未完成\n终裁：pass\n", 1),
    "codeblock_pass": (f"证据完整性：完成\n终裁：fail\n{FENCE}text\n证据完整性：完成\n终裁：pass\n{FENCE}\n", 1),
    "unclosed_fence_pass": (f"证据完整性：完成\n终裁：fail\n{FENCE}\n终裁：pass\n", 1),
    "evidence_from_an_earlier_round": ("证据完整性：完成\n终裁：fail\n## 二审\n终裁：pass\n", 1),
    # 三审 §10.2：围栏须按字符与长度匹配，关闭行只能跟空白
    "four_backticks_inner_three": ("证据完整性：完成\n终裁：fail\n````text\n```\n证据完整性：完成\n终裁：pass\n````\n", 1),
    "four_tildes_inner_three": ("证据完整性：完成\n终裁：fail\n~~~~text\n~~~\n证据完整性：完成\n终裁：pass\n~~~~\n", 1),
    "fence_line_with_trailing_text": ("证据完整性：完成\n终裁：fail\n```text\n```not-a-closing-fence\n证据完整性：完成\n终裁：pass\n```\n", 1),
    "tilde_does_not_close_backticks": ("证据完整性：完成\n终裁：fail\n```\n~~~\n证据完整性：完成\n终裁：pass\n```\n", 1),
    "longer_close_ends_the_block": ("```\n终裁：fail\n`````\n证据完整性：完成\n终裁：pass\n", 0),
    "complete_pass": ("证据完整性：完成\n终裁：pass\n", 0),
    "history_fail_then_complete_pass": ("证据完整性：完成\n终裁：fail\n## 二审\n证据完整性：完成\n终裁：pass\n", 0),
    "example_in_code_then_complete_pass": (f"{FENCE}\n终裁：fail\n{FENCE}\n证据完整性：完成\n终裁：pass\n", 0),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_or05_gate_matrix(sandbox, case):
    body, want = CASES[case]
    (sandbox / REVIEW).write_text(body, encoding="utf-8")
    rc = _run(_tasks()[0]["OR-05"]["verify"], sandbox)
    assert (rc == 0) == (want == 0), f"{case}: rc={rc}"


def test_or05_gate_missing_file_fails(sandbox):
    assert _run(_tasks()[0]["OR-05"]["verify"], sandbox) != 0


@pytest.mark.parametrize("case", ["fail", "bare_pass", "codeblock_pass"])
def test_mutation_the_old_or05_gates_would_let_these_through(sandbox, case):
    """突变：换回 verifyHistory 里的旧门，这些本应失败的输入全被放行——现行门的失败来自门本身。"""
    body, want = CASES[case]
    assert want == 1
    (sandbox / REVIEW).write_text(body, encoding="utf-8")
    olds = [h["old"] for h in _tasks()[0]["OR-05"]["verifyHistory"]]
    assert any(_run(old, sandbox) == 0 for old in olds), f"{case}: 旧门没有一版放行它，突变不成立"


@pytest.mark.parametrize("frozen,want", [(True, 0), (False, 1), ("true", 1), (None, 1)])
def test_or01_gate_reads_the_structured_frozen_value(sandbox, frozen, want):
    board = json.loads((sandbox / "taskList.json").read_text(encoding="utf-8"))
    board["contracts"]["frozen"] = frozen
    (sandbox / "taskList.json").write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    rc = _run(_tasks()[0]["OR-01"]["verify"], sandbox)
    assert (rc == 0) == (want == 0), f"frozen={frozen!r}: rc={rc}"


def test_or01_gate_propagates_an_upstream_failure(sandbox):
    """上游命令失败必须传到最终退出码：用当前实际调用名 python3 注入失败。"""
    assert _run(_tasks()[0]["OR-01"]["verify"], sandbox, prefix="python3() { return 1; }; ") != 0


def test_mutation_the_old_or01_gate_swallowed_the_failure(sandbox):
    """突变：旧门用 ';' 串联，frozen=False 照样 rc=0。"""
    board = json.loads((sandbox / "taskList.json").read_text(encoding="utf-8"))
    board["contracts"]["frozen"] = False
    (sandbox / "taskList.json").write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    old = _tasks()[0]["OR-01"]["verifyHistory"][0]["old"]
    r = subprocess.run(["bash", "-c", 'python() { python3 "$@"; }; ' + old], cwd=sandbox, capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == "", (r.returncode, r.stderr)    # 参数原样转发：旧门确实读了 frozen=False 而放行


def test_mutation_three_char_fence_matching_lets_the_code_example_through(monkeypatch):
    """突变：换回「只记前三个字符、startswith 即关闭」的旧围栏判定，四反引号块里的示例 pass 就被当成正文。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("review_gate", ROOT / "scripts/review_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    body = CASES["four_backticks_inner_three"][0]
    assert gate.check(body)[0] == 1

    def old_formal_lines(text):
        out, fence = [], None
        for line in text.splitlines():
            s = line.lstrip()
            if fence is None and (s.startswith("```") or s.startswith("~~~")):
                fence = s[:3]
                continue
            if fence is not None:
                if s.startswith(fence):
                    fence = None
                continue
            out.append(line)
        return out

    monkeypatch.setattr(gate, "formal_lines", old_formal_lines)
    assert gate.check(body)[0] == 0
