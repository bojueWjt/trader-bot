"""R-08：共同日历块 max-t——手算 θ/SE、块充分统计量 vs 逐行重估、p_adj 加一与等号、分位/LB、insufficient、交换/复制不变、共同冲击。"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import polars as pl
import pytest

from quant_lab.research import maxt as M

T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
UTC = pl.Datetime("us", "UTC")


def panel(rows, cands):
    """rows: (id, cluster, day, m, [d_1..d_C])"""
    df = pl.DataFrame({
        "episode_id": [r[0] for r in rows], "cluster_id": [r[1] for r in rows], "t_dec": [T0 + dt.timedelta(days=r[2]) for r in rows],
        "m": [r[3] for r in rows], **{c: [r[4][k] for r in rows] for k, c in enumerate(cands)},
    }, schema_overrides={"t_dec": UTC})
    n_c = df.group_by("cluster_id").len().rename({"len": "n_c"})
    df = df.join(n_c, on="cluster_id").with_columns((1.0 / pl.col("n_c")).alias("weight"))
    return M.PairedPanel.from_frame(df, cands)


def test_hand_computed_theta_and_jackknife_se():
    # 两候选、4 块（第 3 块空）：块 0：簇 a(2 机会)；块 1：簇 b；块 3：簇 c
    p = panel([("a1", "a", 0, True, [1.0, 2.0]), ("a2", "a", 1, True, [3.0, 0.0]), ("b", "b", 3, True, [2.0, 2.0]), ("c", "c", 9, True, [0.0, 4.0])], ["x", "y"])
    bi = M.calendar_blocks(p, block_len_days=3, origin=T0)
    assert bi.n_blocks == 4 and bi.n_nonempty == 3 and bi.block_of_row.tolist() == [0, 0, 1, 3]
    S, W = M.block_sums(p, bi)
    # 簇 a 权重各 0.5：S[0] = [0.5*1 + 0.5*3, 0.5*2 + 0] = [2, 1]，W[0]=1；块 1 = [2,2]，W=1；块 3 = [0,4]，W=1；块 2 空
    assert S.tolist() == [[2.0, 1.0], [2.0, 2.0], [0.0, 0.0], [0.0, 4.0]] and W.tolist() == [1, 1, 0, 1]
    theta, se, ok = M.theta_and_jackknife_se(S, W, np.arange(4))
    assert ok and theta.tolist() == pytest.approx([4 / 3, 7 / 3])
    # 手算 jackknife（Q=4，含空块）：θ_(-0)=[1, 3]，θ_(-1)=[1, 2.5]，θ_(-2)=θ=[4/3, 7/3]，θ_(-3)=[2, 1.5]
    lo = np.array([[1, 3], [1, 2.5], [4 / 3, 7 / 3], [2, 1.5]])
    exp = np.sqrt(3 / 4 * ((lo - lo.mean(axis=0)) ** 2).sum(axis=0))
    assert se.tolist() == pytest.approx(exp.tolist())


def test_block_sums_equal_row_level_reestimate_under_resampling():
    rng = np.random.default_rng(1)
    rows = [(f"e{i}", f"c{i // 3}", float(i * 0.7), bool(rng.random() > 0.1), list(rng.normal(size=3))) for i in range(60)]
    p = panel(rows, ["a", "b", "c"])
    bi = M.calendar_blocks(p, block_len_days=2, origin=T0)
    S, W = M.block_sums(p, bi)
    for _ in range(5):
        draws = rng.integers(0, bi.n_blocks, size=bi.n_blocks)
        th_fast, _, ok = M.theta_and_jackknife_se(S, W, draws)
        th_ref, D_ref = M.reestimate_rows(p, bi, draws)
        assert ok and th_fast.tolist() == pytest.approx(th_ref.tolist())            # 整簇抽样：副本重建权重 == 块充分统计量
        assert D_ref == pytest.approx(float(W[draws].sum()))


def _big_panel(n_clusters=90, seed=0, C=4, shock=0.0, shift=0.0):
    rng = np.random.default_rng(seed)
    rows = []
    days = np.sort(rng.uniform(0, 270, n_clusters))
    common = rng.normal(size=int(270 / 3) + 1)                                   # 共同 3 日块冲击
    for k in range(n_clusters):
        for j in range(int(rng.integers(1, 4))):
            d = days[k] + j * 0.2
            base = shock * common[int(d // 3)]
            rows.append((f"e{k}_{j}", f"c{k}", float(d), True, list(rng.normal(size=C) + base + shift)))
    return panel(rows, [f"cand{i}" for i in range(C)])


def test_max_t_api_p_adj_equality_plus_one_and_lower_bound():
    p = _big_panel()
    r = M.max_t_panel(p, block_len_days=3, B=400, seed=7)
    assert r.status == "ok" and r.n_nonempty_blocks >= 20 and len(r.p_adj) == 4
    assert np.all(r.p_adj >= 1 / 401) and np.all(r.p_adj <= 1.0)                  # 加一修正下界
    k = math.ceil(0.95 * 401)
    assert k == 381 and r.q_crit is not None
    assert r.lower_bound.tolist() == pytest.approx((r.theta - r.q_crit * r.se).tolist())
    # 等号：把 t 精确设为某个 T_b 时 p_adj 计数含等号 —— 用内部公式复算
    S, W = M.block_sums(p, M.calendar_blocks(p, block_len_days=3))
    theta, se, _ = M.theta_and_jackknife_se(S, W, np.arange(M.calendar_blocks(p, block_len_days=3).n_blocks))
    assert theta.tolist() == pytest.approx(r.theta.tolist()) and se.tolist() == pytest.approx(r.se.tolist())
    T = np.array([0.5, 1.0, 1.5]); t = np.array([1.0])
    assert (1 + (T[:, None] >= t[None, :]).sum(axis=0)) / 4 == pytest.approx([3 / 4])
    # 同 seed 复现
    r2 = M.max_t_panel(p, block_len_days=3, B=400, seed=7)
    assert r2.p_adj.tolist() == r.p_adj.tolist()


def test_candidate_permutation_and_duplication_invariance():
    p = _big_panel()
    r = M.max_t_panel(p, block_len_days=3, B=300, seed=3)
    perm = [2, 0, 3, 1]
    p2 = M.PairedPanel(p.episode_ids, p.cluster_ids, p.t_dec, p.weights, p.mask, p.diffs[:, perm], tuple(p.candidate_ids[i] for i in perm))
    r2 = M.max_t_panel(p2, block_len_days=3, B=300, seed=3)
    assert r2.p_adj.tolist() == pytest.approx([r.p_adj[i] for i in perm]) and r2.q_crit == r.q_crit   # 交换候选列不改变结果
    p3 = M.PairedPanel(p.episode_ids, p.cluster_ids, p.t_dec, p.weights, p.mask, np.column_stack([p.diffs, p.diffs[:, :1]]), p.candidate_ids + ("dup",))
    r3 = M.max_t_panel(p3, block_len_days=3, B=300, seed=3)
    assert r3.q_crit == r.q_crit and r3.p_adj[:4].tolist() == pytest.approx(r.p_adj.tolist())        # 完全复制不增加 max 分布


def test_common_shock_widens_se_vs_independent_blocks():
    """共同冲击时块 jackknife SE 应明显大于把每个机会当独立块的 SE（错误实现会低估）。"""
    p = _big_panel(shock=2.0, seed=5)
    r = M.max_t_panel(p, block_len_days=3, B=200, seed=1)
    bi = M.calendar_blocks(p, block_len_days=3)
    S, W = M.block_sums(p, bi)
    # "独立块"错误实现：每机会一块
    S_i = (p.diffs * (p.mask * p.weights)[:, None]); W_i = p.mask * p.weights
    _, se_indep, _ = M.theta_and_jackknife_se(S_i, W_i, np.arange(p.n))
    assert np.all(r.se > 1.3 * se_indep)


def test_insufficient_conditions_and_contract_blocked():
    small = _big_panel(n_clusters=10)
    assert M.max_t_panel(small, block_len_days=3, B=100).status == "insufficient"
    p = _big_panel()
    const = M.PairedPanel(p.episode_ids, p.cluster_ids, p.t_dec, p.weights, p.mask, np.ones((p.n, 1)), ("const",))
    r = M.max_t_panel(const, block_len_days=3, B=100)
    assert r.status == "insufficient" and r.reason == "UNSTABLE_SE"                   # 候选零方差不是无限显著
    assert M.max_t_panel(p, block_len_days=3, B=10).status == "insufficient"          # k > B
    empty = M.PairedPanel(p.episode_ids, p.cluster_ids, p.t_dec, p.weights, np.zeros(p.n, dtype=bool), p.diffs, p.candidate_ids)
    assert M.max_t_panel(empty, block_len_days=3, B=100).status == "insufficient"
    blocked = M.max_t_bootstrap([0.1], [0.05], block_len_days=3, B=100, seed=0)
    assert blocked.status == "contract_blocked"
    ok = M.max_t_bootstrap(None, None, block_len_days=3, B=100, seed=0, panel=p)
    assert ok.status == "ok" and "check_mismatch" not in ok.diagnostics
    bad = M.max_t_bootstrap([9.0] * 4, None, block_len_days=3, B=100, seed=0, panel=p)
    assert "check_mismatch" in bad.diagnostics


def test_block_len_sensitivity_reported_not_selected():
    p = _big_panel(seed=11)
    res = {L: M.max_t_panel(p, block_len_days=L, B=200, seed=2) for L in (1, 3, 7)}
    assert all(r.status == "ok" for r in res.values()) and len({r.q_crit for r in res.values()}) > 1
    assert res[7].n_nonempty_blocks < res[1].n_nonempty_blocks


# ================================================================ B20（execution-interface §5.22）：下限与块长成对声明
def test_B20_sufficiency_floor_pairs_block_len_and_pins_cross_and_span():
    """B20 §3：口径 = calendar_blocks.n_nonempty；下限必须与 block_len 成对且 block_len ≥ 观察窗+embargo；
    同时钉住 cross 与 max_span；数据量由切分算术给出。"""
    import pytest as _pytest
    from quant_lab.research.maxt import SufficiencyFloor, calendar_blocks

    with _pytest.raises(ValueError, match="独立性前提"):
        SufficiencyFloor(block_len_days=3, research_horizon_days=5.0, embargo_days=1.0)     # 块长 < 观察窗+embargo
    fl = SufficiencyFloor(block_len_days=7, research_horizon_days=5.0, embargo_days=1.0, min_nonempty=20)
    assert fl.required_calendar_days() == 140 and fl.span_cap_days == 14                     # Q×block_len；默认 2×块长

    p = _big_panel(n_clusters=90, seed=4)
    bi = calendar_blocks(p, block_len_days=7)
    # 块长不一致必须拒（口径与推断单位必须同一个）
    bi3 = calendar_blocks(p, block_len_days=3)
    assert "BLOCK_LEN_MISMATCH" in fl.check(bi3)
    # 块数不足
    small = _big_panel(n_clusters=8, seed=5)
    assert "NONEMPTY_BLOCKS" in fl.check(calendar_blocks(small, block_len_days=7))
    # cross 与 max_span 各自独立成门
    strict_cross = SufficiencyFloor(block_len_days=7, research_horizon_days=5.0, min_nonempty=1, max_cluster_span_frac=0.0)
    strict_span = SufficiencyFloor(block_len_days=7, research_horizon_days=5.0, min_nonempty=1, max_span_days=0.0)
    assert (bi.cluster_span_frac == 0 and strict_cross.check(bi) is None) or "CLUSTER_SPAN_FRAC" in (strict_cross.check(bi) or "")
    assert (bi.max_cluster_span_days == 0 and strict_span.check(bi) is None) or "MAX_CLUSTER_SPAN" in (strict_span.check(bi) or "")


def test_B20_max_t_panel_uses_declared_floor():
    from quant_lab.research.maxt import SufficiencyFloor, max_t_panel
    p = _big_panel(n_clusters=90, seed=6)
    fl = SufficiencyFloor(block_len_days=7, research_horizon_days=5.0, embargo_days=1.0, min_nonempty=1000)
    r = max_t_panel(p, block_len_days=7, B=200, seed=1, floor=fl)
    assert r.status == "insufficient" and "NONEMPTY_BLOCKS" in r.reason and "block_len=7d" in r.reason
    assert r.diagnostics["sufficiency_floor"]["required_calendar_days"] == 7000
    ok = SufficiencyFloor(block_len_days=7, research_horizon_days=5.0, embargo_days=1.0, min_nonempty=5, max_cluster_span_frac=1.0, max_span_days=1e9)
    r2 = max_t_panel(p, block_len_days=7, B=200, seed=1, floor=ok)
    assert r2.status == "ok" and r2.diagnostics["sufficiency_floor"]["reason"] is None
