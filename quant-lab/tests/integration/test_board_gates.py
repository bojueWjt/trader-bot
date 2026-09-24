"""OR-05 I07：看板上 OR-01 / OR-05 的 verify 必须能因它要挡的情况失败。

命令一律从 taskList.json 原文读出，在临时副本里执行（只替换审查正文或 frozen 值），不手抄命令。
OR-05 的门在三审到五审里从「解析 Markdown 正文」改成了「核对封存记录」（见 scripts/review_gate.py 文档串）：
这里固定封存协议的全部失败路径、历次审查报出的渲染反例（它们再也翻不了封存的 fail），以及去掉每道核对的突变。
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


RECORD = "docs/adr/review-G0-integration.verdict.json"


def _gate_cmd():
    return _tasks()[0]["OR-05"]["verify"]


def _write(sandbox, body):
    (sandbox / REVIEW).write_text(body, encoding="utf-8")


def _seal(sandbox, round_, verdict, evidence=True):
    args = ["python3", "scripts/review_gate.py", "--seal", REVIEW, RECORD, "--round", str(round_), "--verdict", verdict]
    return subprocess.run(args + (["--evidence-complete"] if evidence else []), cwd=sandbox, capture_output=True, text=True)


def _record(sandbox):
    return json.loads((sandbox / RECORD).read_text(encoding="utf-8"))


def _rewrite_record(sandbox, **changes):
    rec = _record(sandbox)
    for k, v in changes.items():
        if v is _DROP:
            rec.pop(k)
        else:
            rec[k] = v
    (sandbox / RECORD).write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


_DROP = object()
PASS_BODY = "# 审查\n正文。\n证据完整性：完成\n终裁：pass\n"
FAIL_BODY = "# 审查\n正文。\n证据完整性：完成\n终裁：fail\n"


def test_sealed_pass_with_complete_evidence_passes(sandbox):
    _write(sandbox, PASS_BODY)
    assert _seal(sandbox, 6, "pass").returncode == 0
    assert _run(_gate_cmd(), sandbox) == 0


def test_no_sealed_record_fails(sandbox):
    _write(sandbox, PASS_BODY)
    assert _run(_gate_cmd(), sandbox) != 0


def test_missing_review_file_fails(sandbox):
    assert _run(_gate_cmd(), sandbox) != 0


@pytest.mark.parametrize("verdict", ["fail", "insufficient"])
def test_sealed_non_pass_fails(sandbox, verdict):
    _write(sandbox, f"正文。\n证据完整性：完成\n终裁：{verdict}\n")
    assert _seal(sandbox, 6, verdict).returncode == 0
    assert _run(_gate_cmd(), sandbox) != 0


def test_sealed_pass_with_incomplete_evidence_fails(sandbox):
    _write(sandbox, PASS_BODY)
    assert _seal(sandbox, 6, "pass", evidence=False).returncode == 0
    assert _run(_gate_cmd(), sandbox) != 0


@pytest.mark.parametrize("edit", ["append_code_example", "append_one_space", "rewrite_to_pass"])
def test_review_edited_after_sealing_fails(sandbox, edit):
    """封存之后审查文件改动任何字节都失败——包括追加一个代码块示例，或把 fail 改写成 pass。"""
    _write(sandbox, FAIL_BODY if edit == "rewrite_to_pass" else PASS_BODY)
    assert _seal(sandbox, 6, "fail" if edit == "rewrite_to_pass" else "pass").returncode == 0
    text = (sandbox / REVIEW).read_text(encoding="utf-8")
    if edit == "append_code_example":
        text += "```\n终裁：pass\n```\n终裁：pass\n"
    elif edit == "append_one_space":
        text += " "
    else:
        text = text.replace("终裁：fail", "终裁：pass")
        _rewrite_record(sandbox, verdict="pass")
    (sandbox / REVIEW).write_text(text, encoding="utf-8")
    assert _run(_gate_cmd(), sandbox) != 0


def test_record_verdict_tampered_without_touching_review_fails(sandbox):
    """只改记录不改审查文件：哈希仍符，但最后一行「终裁：fail」与记录的 pass 不一致。"""
    _write(sandbox, FAIL_BODY)
    assert _seal(sandbox, 6, "fail").returncode == 0
    _rewrite_record(sandbox, verdict="pass")
    assert _run(_gate_cmd(), sandbox) != 0


@pytest.mark.parametrize("change", [
    {"evidence_complete": "true"}, {"evidence_complete": 1}, {"round": "6"}, {"round": 0}, {"verdict": "PASS"},
    {"review": "other.md"}, {"extra": 1}, {"review_sha256": _DROP}, {"evidence_complete": _DROP},
])
def test_record_fields_are_strict(sandbox, change):
    _write(sandbox, PASS_BODY)
    assert _seal(sandbox, 6, "pass").returncode == 0
    _rewrite_record(sandbox, **change)
    assert _run(_gate_cmd(), sandbox) != 0


@pytest.mark.parametrize("key", ["review", "round", "verdict", "evidence_complete", "review_sha256"])
def test_every_missing_field_fails(sandbox, key):
    """六审应改：每个字段缺失都各自失败，不只抽样几个。"""
    _write(sandbox, PASS_BODY)
    assert _seal(sandbox, 6, "pass").returncode == 0
    _rewrite_record(sandbox, **{key: _DROP})
    assert _run(_gate_cmd(), sandbox) != 0


@pytest.mark.parametrize("key,bad", [
    ("review", None), ("review", 1), ("round", None), ("round", 6.0), ("round", True), ("round", -1),
    ("verdict", None), ("verdict", True), ("verdict", ["pass"]), ("evidence_complete", None), ("evidence_complete", "完成"),
    ("evidence_complete", [True]), ("review_sha256", None), ("review_sha256", 0), ("review_sha256", "0" * 64),
])
def test_every_field_rejects_wrong_types_and_values(sandbox, key, bad):
    """六审应改：每个字段的错误类型或取值各自失败。round=True 虽是 int 子类也要拒（type is int）。"""
    _write(sandbox, PASS_BODY)
    assert _seal(sandbox, 6, "pass").returncode == 0
    _rewrite_record(sandbox, **{key: bad})
    assert _run(_gate_cmd(), sandbox) != 0


def test_record_that_is_not_an_object_fails(sandbox):
    _write(sandbox, PASS_BODY)
    (sandbox / RECORD).write_text("[]", encoding="utf-8")
    assert _run(_gate_cmd(), sandbox) != 0


def test_record_that_is_not_json_fails(sandbox):
    _write(sandbox, PASS_BODY)
    (sandbox / RECORD).write_text("终裁：pass\n", encoding="utf-8")
    assert _run(_gate_cmd(), sandbox) != 0


def test_seal_refuses_a_verdict_the_review_does_not_end_with(sandbox):
    _write(sandbox, FAIL_BODY)
    assert _seal(sandbox, 6, "pass").returncode != 0
    assert not (sandbox / RECORD).exists()


# 三审到五审报出的全部 Markdown 渲染反例：门不再理解 Markdown，裁定只看封存记录，这些正文怎么写都翻不了 fail
RENDERING_BODIES = {
    "codeblock_pass": "终裁：fail\n```text\n证据完整性：完成\n终裁：pass\n```\n",
    "four_backticks_inner_three": "终裁：fail\n````text\n```\n证据完整性：完成\n终裁：pass\n````\n",
    "fence_line_with_trailing_text": "终裁：fail\n```text\n```not-a-closing-fence\n证据完整性：完成\n终裁：pass\n```\n",
    "nbsp_after_closing_fence": "终裁：fail\n```\n```\u00a0\n证据完整性：完成\n终裁：pass\n```\n",
    "html_comment": "终裁：fail\n<!--\n证据完整性：完成\n终裁：pass\n-->\n",
    "list_container_fence": "终裁：fail\n- ```\n  证据完整性：完成\n终裁：pass\n  ```\n",
    "u2028_line_separator": "终裁：fail\n```\u2028证据完整性：完成\n终裁：pass\n```\n",
}


@pytest.mark.parametrize("case", sorted(RENDERING_BODIES))
def test_markdown_rendering_cannot_flip_a_sealed_fail(sandbox, case):
    _write(sandbox, "证据完整性：完成\n" + RENDERING_BODIES[case] + "证据完整性：完成\n终裁：fail\n")
    assert _seal(sandbox, 6, "fail").returncode == 0
    assert _run(_gate_cmd(), sandbox) != 0


def test_mutation_the_old_or05_gates_would_let_a_fail_through(sandbox):
    """突变：换回 verifyHistory 里的旧门，一审式的 fail 审查被放行——现行门的失败来自门本身。"""
    _write(sandbox, FAIL_BODY)
    olds = [h["old"] for h in _tasks()[0]["OR-05"]["verifyHistory"]]
    assert any(_run(old, sandbox) == 0 for old in olds)


def _gate_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("review_gate", ROOT / "scripts/review_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mutation_without_the_hash_check_an_edited_review_would_pass(sandbox, monkeypatch):
    _write(sandbox, PASS_BODY)
    assert _seal(sandbox, 6, "pass").returncode == 0
    (sandbox / REVIEW).write_text(PASS_BODY + "封存后追加的内容\n终裁：pass\n", encoding="utf-8")
    gate = _gate_module()
    assert gate.check(sandbox / REVIEW, sandbox / RECORD)[0] == 1
    monkeypatch.setattr(gate, "_sha256", lambda path: _record(sandbox)["review_sha256"])
    assert gate.check(sandbox / REVIEW, sandbox / RECORD)[0] == 0


def test_mutation_without_the_last_line_check_a_tampered_record_would_pass(sandbox, monkeypatch):
    _write(sandbox, FAIL_BODY)
    assert _seal(sandbox, 6, "fail").returncode == 0
    _rewrite_record(sandbox, verdict="pass")
    gate = _gate_module()
    assert gate.check(sandbox / REVIEW, sandbox / RECORD)[0] == 1
    monkeypatch.setattr(gate, "_last_nonempty_line", lambda path: "终裁：pass")
    assert gate.check(sandbox / REVIEW, sandbox / RECORD)[0] == 0


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

