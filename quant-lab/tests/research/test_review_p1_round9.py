"""九审证据里挖出的五条记账/身份不变式回归。九审本身未写终裁，但它产出的反例是真的。"""
from __future__ import annotations

import copy
import json

import pytest

from quant_lab.research import nullmodel as NM
from quant_lab.research.nullmodel import aggregate_pair_ok, clopper_pearson, verify_report_text
from tests.research.test_review_p1_round3 import _restamped_report


def _render(text, start, end, payload):
    return text[:start] + json.dumps(payload) + text[end:]


def _forge_account(base, x):
    """按九审 R9-ACCOUNT 的构造：T0=499 / T1=2 / error=499，只有 2 个 replicate 有机会产出阳性。
    每一处计数与三分母 CP 都自洽地重算，唯一说不通的是阳性数本身。"""
    q = copy.deepcopy(base)
    r = next(r for r in q["results"] if r["kind"] == "power")
    d = r["diagnostics"]; g = d["grid_dependence"]
    r.update(n_positive=x, n_failed=499, tiers={"T0": 499, "T1": 2, "error": 499},
             n_T0=499, n_searched=501, n_recovered=0)
    d["errors"] = ["RuntimeError: accounting probe"] * 499
    d["chosen_block_len"] = {"L=1": 501}
    d["positive_by_block_len"] = {"L=1": x, "L=3": x, "L=7": x}
    g["n_grid"] = 501; g["n_cross"] = [501] * 3
    g["band"] = [aggregate_pair_ok(f, m, v, 501)[2]
                 for f, m, v in zip(g["fitted_cross"], g["null_mean_cross"], g["null_sd_cross"])]
    for rk, ck, den in [("rate", "ci", 501), ("worst_case_rate", "worst_case_ci", 1000),
                        ("searched_worst_rate", "searched_worst_ci", 501)]:
        r[rk] = x / den
        r[ck] = list(clopper_pearson(x, den))
    r["verdict"] = "pass" if r["searched_worst_ci"][0] >= 0.8 else "fail"
    return q


@pytest.mark.parametrize("x,possible", [(0, True), (2, True), (3, False), (480, False)])
def test_R9_positives_cannot_exceed_eligible_replicates(x, possible):
    """阳性只能来自既非 T0、又没出错、也没判结构无效的 replicate。

    九审反例：报 480 个阳性而实际只有 2 个 replicate 有机会——所有计数与 CP 区间都自洽，
    判读原先照单全收，功效由 fail 翻成 pass。门必须只拒**不可能**的，放行仍然可能的。
    """
    text, base, start, end = _restamped_report()
    payload = _forge_account(base, x)
    if possible:
        verify_report_text(_render(text, start, end, payload))
    else:
        with pytest.raises(ValueError, match="超过可产出阳性的 replicate 数"):
            verify_report_text(_render(text, start, end, payload))


@pytest.mark.parametrize("case", ["error_without_list", "list_without_error"])
def test_R9_error_tier_and_error_list_must_agree(case):
    """error 档与错误清单是同一件事的两种记法，run_mc 每次异常同时写两处。"""
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base)
    r = q["results"][4]; d = r["diagnostics"]
    if case == "error_without_list":
        r["tiers"]["T1"] -= 5; r["tiers"]["error"] = 5
    else:
        d["errors"] = ["RuntimeError: report"] * 5
        d["chosen_block_len"]["L=1"] = d["chosen_block_len"].get("L=1", 0) - 5
    with pytest.raises(ValueError, match="错误清单"):
        verify_report_text(_render(text, start, end, q))


def test_R9_recovered_cannot_exceed_eligible():
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base)
    q["results"][4]["n_recovered"] = 1001
    with pytest.raises(ValueError, match="规则找回数"):
        verify_report_text(_render(text, start, end, q))


def test_R9_six_field_fixed_forgery_contradicts_the_recorded_command():
    """六处全改的协调伪造原先通过。第七处是**命令本身**：本模块 CLI 无条件构造
    block_len_days=None，没有任何选项能设固定 L，所以"命令是本 CLI"与"冻结为 fixed"互相矛盾。"""
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base)
    q["meta"]["pipeline_block_len_days"] = 4
    q["meta"]["L"] = 4
    q["meta"]["pipeline"] = q["meta"]["pipeline"].replace("block_len_days=None", "block_len_days=4")
    for r in q["results"]:
        r["diagnostics"].update(block_len_mode="fixed", block_len_fixed=4, chosen_block_len={})
    # 两道独立不变式都能拦住它：正文渲染没跟着改（同源核对），以及命令与 fixed 互相矛盾。
    # 哪一道先触发是实现顺序，断言接受任一条，不把顺序写死。
    with pytest.raises(ValueError, match="无固定 L 选项|不同源"):
        verify_report_text(_render(text, start, end, q))


def test_R9_cli_really_has_no_fixed_block_len_option():
    """上一条断言依赖"CLI 没有该选项"这个事实——事实本身也要有测试守着，
    将来给 CLI 加了固定 L 选项时，这里会先红，提醒把那条断言一起改。"""
    import argparse
    import inspect
    src = inspect.getsource(NM._main)
    assert "PipelineConfig(B=a.B, block_len_days=None)" in src
    assert "block-len" not in src and "block_len_days=a." not in src


@pytest.mark.parametrize("v", [True, 1, 12, 14, 99999])
def test_R9_receipt_count_must_match_result_rows(v):
    """每个 job 恰好回传一份回执、产出一行结果；回执数原先任意值都接受。"""
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base)
    q["meta"]["worker_receipts_confirmed"] = v
    with pytest.raises(ValueError, match="worker 回执数"):
        verify_report_text(_render(text, start, end, q))


def test_R9_rendered_prose_and_embedded_meta_are_same_source():
    """正文渲染的世界与流水线配置必须与内嵌 meta 同源——否则改 JSON 不改正文即可造出两套说法。"""
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base)
    q["meta"]["world"] = q["meta"]["world"].replace("n_clusters=1600", "n_clusters=9999")
    with pytest.raises(ValueError, match="同源"):
        verify_report_text(_render(text, start, end, q))


# ================================================================ 九审终裁 fail 的两条必修
