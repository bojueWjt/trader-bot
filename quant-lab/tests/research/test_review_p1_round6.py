"""六审（R-10 round 6）必修项回归：R6-G 相关 sd 的可实现性、R6-H worker 执行修订绑定、
R6-L 空主 L 分布跳过记账门。全部复现审查报告里的工程反例。"""
from __future__ import annotations

import copy
import json
import math
from concurrent.futures import ProcessPoolExecutor

import pytest

from quant_lab.research import nullmodel as NM
from quant_lab.research.api import PipelineConfig
from quant_lab.research.nullmodel import aggregate_pair_ok, max_realizable_sd, verify_report_text
from tests.research.test_review_p1_round3 import _restamped_report


def _render(text, start, end, payload):
    return text[:start] + json.dumps(payload) + text[end:]


def test_R6G_sd_upper_bound_matches_the_math():
    """相关系数恒在 [-1,1]，故 ddof=1 的样本 sd 有硬上界 s² ≤ n/(n−1)·(1−m²)。"""
    assert max_realizable_sd(0.0, 1000) == pytest.approx(1.0005003753127737, abs=1e-12)   # 六审算的同一值
    assert max_realizable_sd(1.0, 1000) == 0.0                                            # 均值贴边 → 只能全同值
    assert max_realizable_sd(0.5, 1000) < max_realizable_sd(0.0, 1000)
    assert math.isinf(max_realizable_sd(0.0, 1))                                          # n<2：不可估，不设限

    import numpy as np                                                                    # 经验佐证：抽样达不到上界
    rng = np.random.default_rng(0)
    for _ in range(20):
        x = rng.choice([-1.0, 1.0], size=200)
        assert float(np.std(x, ddof=1)) <= max_realizable_sd(float(np.mean(x)), 200) + 1e-12


@pytest.mark.parametrize("damage", ["sd_3", "sd_1e6", "band_forged", "n_cross_inflated",
                                    "n_cross_below_two", "zeroed_with_legal_sd"])
def test_R6G_unrealizable_sd_cannot_widen_the_band(damage):
    """R6-G：用不可能的 sd 把带宽撑开，就能吞掉整段真实相关的丢失——必须按可实现性上界拒收。"""
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base)
    g = q["results"][0]["diagnostics"]["grid_dependence"]
    j = 1
    if damage in ("sd_3", "sd_1e6"):
        sd = 3.0 if damage == "sd_3" else 1e6
        g["null_mean_cross"][j] = 0.0; g["null_sd_cross"][j] = sd
        g["band"][j] = aggregate_pair_ok(g["fitted_cross"][j], 0.0, sd, g["n_cross"][j])[2]
        assert g["band"][j] > abs(g["fitted_cross"][j]), "反例前提：带宽确实吞得下整段相关"
    elif damage == "band_forged":
        g["band"] = [b * 20 for b in g["band"]]
    elif damage == "n_cross_inflated":
        g["n_cross"] = [g["n_grid"] * 50] * len(g["n_cross"])
    elif damage == "n_cross_below_two":
        g["n_cross"] = [1] * len(g["n_cross"])
    elif damage == "zeroed_with_legal_sd":                       # sd 合法但相关确实归零 → 判结构破坏
        g["null_mean_cross"][j] = 0.0
        g["band"][j] = aggregate_pair_ok(g["fitted_cross"][j], 0.0, g["null_sd_cross"][j], g["n_cross"][j])[2]
    with pytest.raises(ValueError):
        verify_report_text(_render(text, start, end, q))


@pytest.mark.parametrize("damage", ["cleared", "removed", "mode_removed", "fixed_lie", "illegal_L"])
def test_R6L_empty_block_len_distribution_cannot_skip_the_gate(damage):
    """R6-L：空分布不得兼任免检开关——auto 模式必须给出合法分布并无条件核上下界。"""
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base)
    d = q["results"][0]["diagnostics"]
    if damage == "cleared":
        d["chosen_block_len"] = {}
    elif damage == "removed":
        d.pop("chosen_block_len")
    elif damage == "mode_removed":
        d.pop("block_len_mode")
    elif damage == "fixed_lie":
        d["block_len_mode"] = "fixed"; d["chosen_block_len"] = {}
    elif damage == "illegal_L":
        d["chosen_block_len"] = {"L=4": sum(d["chosen_block_len"].values())}
    with pytest.raises(ValueError):
        verify_report_text(_render(text, start, end, q))


def test_R6L_report_declares_auto_mode():
    _, base, _, _ = _restamped_report()
    for r in base["results"]:
        d = r["diagnostics"]
        assert d["block_len_mode"] == "auto" and d["block_len_fixed"] is None
        assert d["chosen_block_len"] and set(d["chosen_block_len"]) <= {"L=1", "L=3", "L=7"}


def _inject_resident_revision():                 # 子进程侧：磁盘一字未改，只换掉已加载的函数对象
    orig = NM.run_mc

    def cached_revision(*a, **k):
        r = orig(*a, **k); r.label = "R6-cached-revision"; return r
    NM.run_mc = cached_revision
