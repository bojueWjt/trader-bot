"""七审（R-10 round 7）必修项回归：R7-G 逐对缺测记账与精确可实现上界、
R7-H 执行摘要覆盖默认值/闭包/模块级常量。复现审查报告里的工程反例。"""
from __future__ import annotations

import copy
import json
import math
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pytest

from quant_lab.research import nullmodel as NM
from quant_lab.research.api import PipelineConfig
from quant_lab.research.nullmodel import aggregate_pair_ok, max_realizable_sd, verify_report_text
from tests.research.test_review_p1_round3 import _restamped_report


def _render(text, start, end, payload):
    return text[:start] + json.dumps(payload) + text[end:]


# ----------------------------------------------------------------- R7-G 精确上界
def test_R7G_exact_bound_is_tight_and_never_exceeded():
    """`n/(n−1)(1−m²)` 只是必要条件；有限 n 时未必可达，精确上界才挡得住 n=2 的构造。"""
    assert max_realizable_sd(0.0, 1000) == pytest.approx(1.0005003753127737, abs=1e-12)   # 六审值不变
    assert max_realizable_sd(-0.9, 2) == pytest.approx(math.sqrt(2) * 0.1, abs=1e-12)     # 七审值 0.141421…
    assert max_realizable_sd(-0.9, 2) < math.sqrt(2 / 1 * (1 - 0.81))                     # 严格紧于旧式
    assert max_realizable_sd(0.0, 3) == pytest.approx(1.0, abs=1e-12)                     # x=(1,−1,0)
    assert math.isinf(max_realizable_sd(0.0, 1))

    rng = np.random.default_rng(0)                       # 穷举：合法抽样的 sd 不得超过上界
    for n in (2, 3, 5, 8, 50):
        for _ in range(2000):
            x = rng.choice([-1.0, 1.0], size=n) if rng.random() < 0.5 else rng.uniform(-1, 1, size=n)
            m, sd = float(np.mean(x)), float(np.std(x, ddof=1))
            assert sd <= max_realizable_sd(m, n) + 1e-9, (n, m, sd)


@pytest.mark.parametrize("damage", ["n_cross_2_no_charge", "n_cross_2_charge_only_cross",
                                    "missing_one_no_charge", "small_n_loose_bound"])
def test_R7G_per_pair_missing_measurements_must_be_charged(damage):
    """R7-G：把某对有效样本数报成 2，就能用一个**本身可实现**的 sd 撑开带宽——
    但 998 次不可估必然让那 998 次 cross_instrument 判据为假，两件事不能同时成立。"""
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base)
    r = q["results"][0]; d = r["diagnostics"]; g = d["grid_dependence"]
    if damage in ("n_cross_2_no_charge", "n_cross_2_charge_only_cross"):
        g["null_mean_cross"][0] = 0.0; g["null_sd_cross"][0] = 0.15; g["n_cross"][0] = 2
        g["band"][0] = aggregate_pair_ok(g["fitted_cross"][0], 0.0, 0.15, 2)[2]
        assert g["band"][0] > abs(g["fitted_cross"][0])        # 前提：带宽确实吞得下整段相关
        assert 0.15 <= max_realizable_sd(0.0, 2)               # 前提：该 sd 本身可实现
        if damage == "n_cross_2_charge_only_cross":
            d["guard_failures_by_check"]["cross_instrument"] = g["n_grid"] - 2
    elif damage == "missing_one_no_charge":
        g["n_cross"] = [g["n_grid"] - 1] + list(g["n_cross"])[1:]
    elif damage == "small_n_loose_bound":                      # 旧式必要条件会放行，精确上界不放
        g["null_mean_cross"][0] = -0.9; g["null_sd_cross"][0] = 0.6; g["n_cross"][0] = 2
        g["band"][0] = aggregate_pair_ok(-0.9, -0.9, 0.6, 2)[2]
        assert 0.6 < math.sqrt(2 / 1 * (1 - 0.81))
    with pytest.raises(ValueError):
        verify_report_text(_render(text, start, end, q))


def test_R7G_clean_report_has_no_missing_measurements():
    _, base, _, _ = _restamped_report()
    for r in base["results"]:
        g = r["diagnostics"]["grid_dependence"]
        assert g["n_cross"] == [g["n_grid"]] * len(g["n_cross"])
        for m_, s_, k_ in zip(g["null_mean_cross"], g["null_sd_cross"], g["n_cross"]):
            assert s_ <= max_realizable_sd(m_, k_)


# ----------------------------------------------------------------- R7-H 执行状态
def _inject_defaults():
    """只改默认参数：不动任何 code object、不动磁盘、不替换摘要实现。"""
    NM._garch_path.__defaults__ = (.5, .10, .85)


def _inject_module_constant():
    NM.PLANTED = "R7-resident-constant"


# ================================================================ 八审 R8 闭合回归








# ================================================================ 自审（九审并行期）发现的三条
