# 已退役：断言的是 G0 R-10 裁定 §2.2/§2.3 移除的「通用运行状态反射」。
# 保留为历史记录（A43：裁决只关分歧不关发现），不参与测试套。

def test_S17_real_low_power_truthful_fail_accepted():
    assert N.verify_report_text(report_text())['results']
    task = next(t for t in json.loads(Path('taskList.json').read_text())['modules']['research']['tasks'] if t['id'] == 'R-08')
    body = shlex.split(task['verify'].split(' && ')[-1])[-1]
    code = "import io,sys;from unittest.mock import patch;from quant_lab.research.nullmodel import verify_report_text;fake=sys.stdin.read()\nwith patch('builtins.open',return_value=io.StringIO(fake)):\n " + body
    result = subprocess.run([sys.executable, '-c', code], input=report_text(), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr

def test_S17_latest_review_verdict(text, ok):
    task = next(t for t in json.loads(Path('taskList.json').read_text())['modules']['research']['tasks'] if t['id'] == 'R-10')
    command = task['verify'].split(' && ', 1)[1].replace(' docs/adr/review-G3-P1.md', '')
    result = subprocess.run(['bash', '-c', command], input=text, text=True)
    assert (result.returncode == 0) == ok

def test_A31_report_must_be_generated_by_current_code():
    """A31：受门判定的制品必须由不旧于门代码的版本生成，且该关系由 verify 机械断言（内嵌哈希变体）。"""
    import json as _json
    from pathlib import Path as _Path
    from quant_lab.research.nullmodel import research_code_digest, verify_report_text

    text = _Path("docs/adr/report-G3-null-model.md").read_text(encoding="utf-8")
    digest = research_code_digest()
    assert f"研究代码 sha256：`{digest}`" in text, "报告正文未内嵌生成它的代码哈希"
    payload = _json.loads(text[text.rindex("```json") + 7:text.rindex("```")])
    assert payload["meta"]["research_code_sha256"] == digest
    verify_report_text(text)                                   # 新鲜制品：通过

    stale = text.replace(digest, "0" * 64)                     # 篡改为旧版哈希：必须被拒
    with pytest.raises(ValueError, match="制品陈旧"):
        verify_report_text(stale)
    assert research_code_digest() == digest                    # 哈希稳定（同一份源码重复计算一致）

def test_R4V_mc_refuses_to_write_when_source_changed_mid_run(tmp_path, monkeypatch):
    """R4-V：MC 起跑冻结生成身份、收尾确认源码未变——运行期改过源码就不得落盘，
    否则结果由旧代码算出却盖上新哈希，A31 的陈旧检测会被绕过。"""
    from quant_lab.research import nullmodel as NM

    out = tmp_path / "report.md"
    argv = ["--out", str(out), "--n-rep", "2", "--no-ext", "--jobs", "1", "--B", "50",
            "--n-clusters", "200", "--mechanisms", "common_shock", "--power-mechanisms", ""]

    real = NM.research_code_digest
    calls = {"n": 0}

    def drifting():
        calls["n"] += 1
        return real() if calls["n"] == 1 else "f" * 64      # 运行期源码被改动
    monkeypatch.setattr(NM, "research_code_digest", drifting)

    # 六审 R6-H 之后，替换 research_code_digest 本身就改变了"执行修订"，因此 worker 身份门
    # 比父进程收尾比对更早触发。被测性质不变：任一身份漂移都必须阻止落盘。
    with pytest.raises(SystemExit, match="运行期间发生变化|不是被冻结的那一版"):
        NM._main(argv)
    assert not out.exists(), "源码在 MC 运行期间变动，却仍落盘了制品"

    monkeypatch.setattr(NM, "research_code_digest", real)   # 源码稳定：同一组参数正常落盘
    NM._main(argv)
    assert out.exists() and real() in out.read_text(encoding="utf-8")
