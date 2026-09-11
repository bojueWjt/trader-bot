"""R-08：合成世界、训练窗残差整块重采样（禁逐 episode 洗牌）、功效注入、Clopper–Pearson、小规模 MC 冒烟。"""
from __future__ import annotations

import numpy as np
import pytest

from quant_lab.research import nullmodel as NM
from quant_lab.research.api import PipelineConfig


def test_world_mechanisms_shape_and_independence_of_features():
    for m in NM.MECHANISMS:
        w = NM.synth_world(NM.WorldConfig(mechanism=m, seed=1, n_clusters=150))
        inp = w.inputs
        assert inp.n >= 150 and len(inp.features) == 12 and inp.anchors["t1"].null_count() == 0
        ok = np.isfinite(inp.base_R)
        assert 0.0 < (~ok).mean() < 0.25
        # 因果特征与收益独立（相关系数小）
        for k, f in inp.features.items():
            assert abs(np.corrcoef(f[ok], inp.base_R[ok])[0, 1]) < 0.3, (m, k)   # 持久过程间的伪相关允许，结构上独立生成


def test_residual_block_resample_preserves_structure_and_forbids_shuffle():
    w = NM.synth_world(NM.WorldConfig(mechanism="common_shock", seed=2))
    model = NM.fit_residual_model(w, block_len_days=3)
    assert model.train_days == (0, 180) and model.diagnostics["n_clusters_train"] > 50
    assert np.isfinite(model.grid[:180]).all() and not np.isfinite(model.grid[180:]).any()      # 只用训练窗
    rng = np.random.default_rng(3)
    null = NM.resample_null(w, model, rng)
    assert null.n == w.inputs.n and null.anchors.equals(w.inputs.anchors)                       # 固定 episode 布局
    # 零条件均值：多次重采样的总体均值 ≈ 0，随机 skip 的期望增益 ≈ 0
    means = []
    for k in range(40):
        nk = NM.resample_null(w, model, np.random.default_rng(100 + k))
        means.append(np.nanmean(nk.base_R))
    assert abs(np.mean(means)) < 0.03, np.mean(means)
    diag = NM.assert_not_episode_shuffle(w.inputs, null, w.day)
    assert diag["ok"] and diag["block_icc"]["null"] > 3 * abs(diag["block_icc"]["episode_shuffle"])
    # 特征独立重生成
    assert not np.allclose(null.features["f00"], w.inputs.features["f00"])
    # 循环位移压力夹具也保持结构
    shifted = NM.resample_null(w, model, np.random.default_rng(4), circular_shift=True)
    shifted_guard = NM.assert_not_episode_shuffle(w.inputs, shifted, w.day)
    assert not shifted_guard["ok"] and not shifted_guard["checks"]["cross_instrument"]  # 位移也必须逐复制诊断


def test_power_injection_expected_gain():
    w = NM.synth_world(NM.WorldConfig(mechanism="common_shock", seed=5))
    model = NM.fit_residual_model(w)
    rng = np.random.default_rng(6)
    inp = NM.resample_null(w, model, rng, delta=0.2, pi=0.3)
    f = inp.features["f00"]; q = np.quantile(f, 0.3); S = f < q
    ok = np.isfinite(inp.base_R)
    gain = -(inp.base_R[ok & S]).mean() * S[ok].mean()              # skip 集合的期望增益 ≈ δ
    assert gain == pytest.approx(0.2, abs=0.1)


def test_clopper_pearson_known_values():
    lo, hi = NM.clopper_pearson(0, 200)
    assert lo == 0.0 and hi == pytest.approx(0.0183, abs=5e-4)                                    # ADR §10.2 算例
    lo, hi = NM.clopper_pearson(10, 200)
    assert hi == pytest.approx(0.0899, abs=1e-3)
    lo, hi = NM.clopper_pearson(50, 1000)
    assert lo < 0.05 < hi and hi < 0.07
    assert NM.clopper_pearson(5, 5) == (pytest.approx(0.4782, abs=1e-3), 1.0)


def test_small_mc_runs_full_pipeline_and_accounts_failures():
    r = NM.run_mc("common_shock", kind="null", n_rep=6, seed0=1, world_cfg=NM.WorldConfig(n_clusters=200), pipe_cfg=PipelineConfig(B=200))
    assert r.n_done == 6 and r.n_positive + r.n_failed <= 6 and r.verdict in ("pass", "fail", "insufficient", "not_run", "not_run_T0", "invalid_null_model")
    assert r.worst_case_ci is not None and r.worst_case_ci[1] >= (r.ci[1] if r.ci else 0)
    # 校准带生效后，小规模运行的结构门不再必然失败（原断言编码的是未校准阈值的行为）：
    # 这里断言门确实跑了、判定与门/总体检查一致——失败才 invalid，通过则不得 invalid。
    g = r.diagnostics["shuffle_guard"]
    assert set(g["checks"]) >= {"icc", "scale", "tail", "missing"} and "dependence_aggregate" in r.diagnostics
    if r.diagnostics.get("invalid_reason"):
        assert r.verdict == "invalid_null_model"
    else:
        assert r.verdict != "invalid_null_model"
    assert sum(r.tiers.values()) == r.n_done
    assert r.tiers.get("invalid", 0) == r.diagnostics["n_invalid_null_model"]
    p = NM.run_mc("common_shock", kind="power", n_rep=4, seed0=2, world_cfg=NM.WorldConfig(n_clusters=200), pipe_cfg=PipelineConfig(B=200), delta=2.0)
    # 大 δ 冒烟：结构门校准后，小规模 power 运行不再必然 invalid（原断言同样编码了未校准阈值的行为）。
    # 断言：跑满、给出最坏界、判定与诊断一致（有 invalid_reason 才 invalid）。
    assert p.n_done == 4 and p.worst_case_ci is not None
    assert (p.verdict == "invalid_null_model") == bool(p.diagnostics.get("invalid_reason"))
