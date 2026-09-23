"""空模型与 FPR/功效验收（契约 feature-snapshot §5；ADR-G3 §10；合并稿 D.5）。

空模型（用于真实与合成）：只用训练窗拟合——把训练窗机会的残差 e_i = R_i − mean_train 分解为
日历格点冲击 g[day, instrument]（同日同品种残差均值）与簇内特异残差 u_i = e_i − g[day_i, inst_i]；
生成时对**整段 L 日块**的格点向量有放回重采样（所有品种同块联动 → 保留块内时间顺序、横截面相关、波动聚集），
特异残差按**整簇**重采样（保留簇内复制），再经暴露映射 (day_i, inst_i) 累到固定的 episode 时间/簇布局上。
禁止逐 episode 独立洗牌（assert_not_episode_shuffle 结构性断言 + 诊断）。整块循环位移作压力夹具。

合成世界（synth_world）：日历格点上共同冲击（GARCH 型波动聚集）+ 品种冲击 + 簇冲击 + t 尾特异；因果特征来自独立随机流；
机制：common_shock / cluster_heavy_tail / nonuniform_density（事件密度随月变化 + 更高删失）/ circular_shift（压力）。
功效注入：预注册 skip 集合 S = {f_0 < q_π}，S 内基线均值 −δ/π，其余 0 → 规则 lt_q(π) 的总体每种子配对净增益 = δ。

验收（ADR §10.2）：T1/T2 每机制 1000 次完整流程（生成 → search → selection → 外折 → final 合成声明），
FPR = 任一允许主声明为阳性的比例；Clopper–Pearson 双侧 95% 精确区间；FPR 上界 ≤ 7%、功效下界 ≥ 80%；
运行失败/insufficient 单列并给最坏界（FPR 把失败视为阳性；功效视为未检出）。结果 synthetic_validation，不替代 G-STAT-CLAIM。
"""
from __future__ import annotations

import datetime as dt
import json
import dataclasses
import math
import os
import platform
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import polars as pl
from scipy.stats import beta

from quant_lab.research import paths
from quant_lab.research.api import Candidate, PanelInputs, PipelineConfig, RuleSpec, run_pipeline
from quant_lab.research.ledger import MemoryLedger

UTC_US = pl.Datetime("us", "UTC")
T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
MECHANISMS = ("common_shock", "cluster_heavy_tail", "nonuniform_density", "circular_shift")
INSTRUMENTS = ("BTCUSDT-PERP.BINANCE-UM", "ETHUSDT-PERP.BINANCE-UM", "SOLUSDT-PERP.BINANCE-UM")


@dataclass(frozen=True)
class WorldConfig:
    mechanism: str = "common_shock"
    n_clusters: int = 1600
    span_days: int = 360
    n_candidates: int = 12
    delta: float = 0.0             # 功效注入（R/种子）
    pi: float = 0.3                # 预注册 skip 集合比例
    censor_frac: float = 0.05
    cluster_size_max: int = 4
    noise_scale: float = 1.0       # 全部冲击幅度乘子（敏感性用；验收用 1.0）
    seed: int = 0


@dataclass
class World:
    inputs: PanelInputs
    day: np.ndarray                # 每机会所在日（int）
    inst: np.ndarray               # 品种索引
    grid_common: np.ndarray        # 真值（诊断用，不进流水线）
    cfg: WorldConfig


FEATURE_MARGINAL_SD = math.sqrt(1.0 / (1.0 - 0.7 ** 2) + 0.5 ** 2)   # 合成特征流的总体边际标准差（AR(1) φ=0.7 + N(0,0.5²)）


def feature_population_quantile(pi: float) -> float:
    """预注册总体 π 分位（正态边际），功效注入用；不依赖任何样本。"""
    from scipy.stats import norm
    return float(norm.ppf(pi) * FEATURE_MARGINAL_SD)


def _garch_path(rng, n, omega=0.05, a=0.10, b=0.85):
    s2, out = omega / (1 - a - b), np.empty(n)
    for t in range(n):
        e = rng.normal() * math.sqrt(s2)
        out[t] = e
        s2 = omega + a * e * e + b * s2
    return out


def synth_world(cfg: WorldConfig) -> World:
    rng = np.random.default_rng(cfg.seed)
    m = cfg.mechanism
    n_days = cfg.span_days
    n_inst = len(INSTRUMENTS)
    common = _garch_path(rng, n_days)
    inst_shock = np.column_stack([0.5 * common + 0.7 * _garch_path(rng, n_days) for _ in range(n_inst)])
    # 事件密度：均匀 / 随月变化
    if m == "nonuniform_density":
        month_w = rng.gamma(1.0, 1.0, size=int(n_days / 30) + 1)
        w = np.array([month_w[d // 30] for d in range(n_days)]); w /= w.sum()
    else:
        w = np.full(n_days, 1.0 / n_days)
    c_days = np.sort(rng.choice(n_days, size=cfg.n_clusters, p=w))
    a_c, b_i, c_c, nu = {"common_shock": (1.2, 0.6, 0.4, 6.0), "cluster_heavy_tail": (0.5, 0.4, 1.2, 3.0),
                         "nonuniform_density": (0.9, 0.6, 0.6, 5.0), "circular_shift": (1.0, 0.6, 0.5, 6.0)}[m]
    rows, base, day, inst = [], [], [], []
    for k, d0 in enumerate(c_days):
        size = int(rng.integers(1, cfg.cluster_size_max + 1))
        ci = int(rng.integers(0, n_inst))
        cs = rng.standard_t(nu) * c_c
        for j in range(size):
            d = min(n_days - 1, int(d0) + (1 if rng.random() < 0.2 else 0))
            secs = int(rng.integers(0, 86400))
            t_dec = T0 + dt.timedelta(days=int(d), seconds=secs)
            rows.append({"episode_id": f"e{k:05d}_{j}", "cluster_id": f"c{k:05d}", "instrument_id": INSTRUMENTS[ci], "t_dec": t_dec,
                         "t1": t_dec + dt.timedelta(hours=float(rng.uniform(2, 60)))})
            base.append(cfg.noise_scale * (a_c * common[d] + b_i * inst_shock[d, ci] + cs + rng.standard_t(nu) * 0.8))
            day.append(d); inst.append(ci)
    anchors = pl.DataFrame(rows, schema_overrides={"t_dec": UTC_US, "t1": UTC_US})
    n = anchors.height
    base = np.array(base); day = np.array(day); inst = np.array(inst)
    # 因果特征：独立随机流（AR(1) 日格点 + 特异噪声），与收益独立
    feats = {}
    for k in range(cfg.n_candidates):
        ar = np.empty(n_days); ar[0] = rng.normal()
        for t in range(1, n_days):
            ar[t] = 0.7 * ar[t - 1] + rng.normal()
        feats[f"f{k:02d}"] = ar[day] + 0.5 * rng.normal(size=n)
    # 功效注入：S = {f00 < q_π}，S 内基线均值 −δ/π
    if cfg.delta > 0:
        S = feats["f00"] < feature_population_quantile(cfg.pi)       # 预注册总体分位，不看样本/未来
        base = base + np.where(S, -cfg.delta / cfg.pi, 0.0)
    cens_frac = cfg.censor_frac * (2.0 if m == "nonuniform_density" else 1.0)
    censored = rng.random(n) < cens_frac
    n_c = anchors.group_by("cluster_id").len().rename({"len": "n_c"})
    w = anchors.join(n_c, on="cluster_id", how="left")["n_c"].to_numpy()
    weights = 1.0 / w.astype(float)
    inputs = PanelInputs(anchors.select("episode_id", "cluster_id", "instrument_id", "t_dec", "t1"), np.where(censored, np.nan, base), censored, weights, feats,
                         data_manifest=f"synthetic-null:{m}:{cfg.seed}", graph_version="gv-null")
    return World(inputs, day, inst, common, cfg)


# ---------------------------------------------------------------- 空模型：训练窗残差整块重采样
@dataclass
class ResidualModel:
    grid: np.ndarray               # (n_days, n_inst) 格点冲击（训练窗外为 NaN）
    idio_by_cluster: dict[str, np.ndarray]
    train_mean: float
    block_len_days: int
    train_days: tuple[int, int]
    diagnostics: dict = field(default_factory=dict)


def fit_residual_model(world: World, *, train_frac: float = 0.5, block_len_days: int = 3, label_maturity_days: float = 0.0,
                       train_end_day: int | None = None) -> ResidualModel:
    """只用训练窗拟合：无候选增益的联合残差过程。训练观测 = t_dec 在训练窗内 **且标签成熟**（t1 + maturity <= 训练窗终点；
    t1 未知不用）且未删失；训练窗终点 = train_end_day（与流水线首折 cutoff 对齐）或 span·train_frac。"""
    inp = world.inputs
    n_days = world.cfg.span_days
    t_end = int(train_end_day) if train_end_day is not None else int(n_days * train_frac)
    t_end_ts = T0 + dt.timedelta(days=t_end)
    t1 = inp.anchors["t1"].to_list()
    mature = np.array([(x is not None) and (x + dt.timedelta(days=label_maturity_days) <= t_end_ts) for x in t1])
    tr = (world.day < t_end) & ~inp.censored & mature
    mean = float(np.nanmean(inp.base_R[tr]))
    e = inp.base_R - mean
    n_inst = len(INSTRUMENTS)
    grid = np.full((n_days, n_inst), np.nan)
    cnt = np.zeros((n_days, n_inst))
    for i in np.where(tr)[0]:
        d, k = world.day[i], world.inst[i]
        grid[d, k] = (0.0 if np.isnan(grid[d, k]) else grid[d, k]) + e[i]; cnt[d, k] += 1
    with np.errstate(invalid="ignore"):
        grid = np.where(cnt > 0, grid / np.maximum(cnt, 1), np.nan)
    # 训练窗内无观测的格点：填训练窗内该品种的经验残差（随机抽），保留为格点级
    rng = np.random.default_rng(world.cfg.seed + 99)
    for k in range(n_inst):
        obs = grid[:t_end, k][np.isfinite(grid[:t_end, k])]
        miss = np.where(~np.isfinite(grid[:t_end, k]))[0]
        if obs.size and miss.size:
            grid[miss, k] = rng.choice(obs, size=miss.size)
    idio: dict[str, np.ndarray] = {}
    cl = inp.anchors["cluster_id"].to_list()
    for i in np.where(tr)[0]:
        u = e[i] - grid[world.day[i], world.inst[i]]
        idio.setdefault(cl[i], []).append(u)
    idio = {c: np.array(v) for c, v in idio.items()}
    # 零条件均值（D.5 "过滤主空模型将净基线收益的条件均值设为 0"）：生成器在固定布局上的期望须逐机会为 0——
    # 格点按品种去均值（块重采样下每源日等概率），特异残差按"簇均值的均值"去均值（每源簇等概率）；否则随机 skip 也有非零期望增益。
    grid_mu = np.nanmean(grid[:t_end], axis=0)
    grid[:t_end] = grid[:t_end] - grid_mu[None, :]
    idio_mu = float(np.mean([v.mean() for v in idio.values()])) if idio else 0.0
    idio = {c: v - idio_mu for c, v in idio.items()}
    # 诊断：块内相关（同块格点冲击的横截面相关 / 块均值 lag-1 自相关）
    blk = np.arange(t_end) // block_len_days
    bm = np.array([np.nanmean(grid[:t_end][blk == b, :]) for b in range(blk.max() + 1)])
    ac1 = float(np.corrcoef(bm[:-1], bm[1:])[0, 1]) if bm.size > 3 else float("nan")
    xs = float(np.nanmean(np.corrcoef(grid[:t_end].T)[np.triu_indices(n_inst, 1)]))
    return ResidualModel(grid, idio, mean, block_len_days, (0, t_end),
                         {"train_days": t_end, "n_train_obs": int(tr.sum()), "n_immature_excluded": int(((world.day < t_end) & ~inp.censored & ~mature).sum()),
                          "block_mean_ac1": ac1, "cross_inst_corr": xs, "n_clusters_train": len(idio),
                          "grid_mu_removed": grid_mu.tolist(), "idio_mu_removed": idio_mu})


def resample_null(world: World, model: ResidualModel, rng: np.random.Generator, *, circular_shift: bool = False, delta: float = 0.0, pi: float = 0.3) -> PanelInputs:
    """在固定 episode 布局上生成空面板：整块（L 日、全品种联动）重采样格点冲击 + 整簇重采样特异残差；不逐 episode 洗牌。
    circular_shift=True：整块循环位移压力夹具。delta>0：功效注入（S 内 −δ/π）。"""
    inp = world.inputs
    n_days = world.cfg.span_days
    L = model.block_len_days
    t0, t_end = model.train_days
    n_src = max(1, (t_end - t0) // L)
    n_tgt = math.ceil(n_days / L)
    src_blocks = np.arange(n_src)
    if circular_shift:
        k = int(rng.integers(1, n_src))
        pick = np.roll(np.resize(src_blocks, n_tgt), k)
    else:
        pick = rng.choice(src_blocks, size=n_tgt, replace=True)
    grid_new = np.empty_like(model.grid)
    for b in range(n_tgt):
        src = model.grid[t0 + pick[b] * L: t0 + pick[b] * L + L]
        lo, hi = b * L, min(n_days, (b + 1) * L)
        grid_new[lo:hi] = src[: hi - lo]
    # 特异残差：整簇抽样（簇 → 随机源簇的残差向量，按成员顺序循环）
    cl = inp.anchors["cluster_id"].to_list()
    src_clusters = list(model.idio_by_cluster.keys())
    base = np.empty(inp.n)
    assign: dict[str, np.ndarray] = {}
    for i in range(inp.n):
        c = cl[i]
        if c not in assign:
            assign[c] = model.idio_by_cluster[src_clusters[int(rng.integers(0, len(src_clusters)))]]
        u = assign[c]
        base[i] = model.train_mean * 0.0 + grid_new[world.day[i], world.inst[i]] + u[i % len(u)]    # 条件均值置 0（无增益）
    feats = {k: v.copy() for k, v in inp.features.items()}
    # 因果特征独立重生成（独立随机流）
    for k in feats:
        ar = np.empty(n_days); ar[0] = rng.normal()
        for t in range(1, n_days):
            ar[t] = 0.7 * ar[t - 1] + rng.normal()
        feats[k] = ar[world.day] + 0.5 * rng.normal(size=inp.n)
    if delta > 0:
        base = base + np.where(feats["f00"] < feature_population_quantile(pi), -delta / pi, 0.0)   # 预注册总体分位
    cens_frac = world.cfg.censor_frac * (2.0 if world.cfg.mechanism == "nonuniform_density" else 1.0)   # 保留机制的缺失强度
    censored = inp.censored.copy()  # 固定缺失布局。
    return replace(inp, base_R=np.where(censored, np.nan, base), censored=censored, base_censored=censored.copy(), cand_censored=censored.copy(),
                   features=feats, data_manifest=inp.data_manifest + ":null", shock_grid=grid_new)   # 结构诊断在格点上测（_grid_cross_corr）


def _icc(r: np.ndarray, blk: np.ndarray) -> float:
    """单向 ANOVA 组内相关（块为组）：逐 episode 洗牌 → ≈ 0；块结构保留 → 明显 > 0。"""
    ok = np.isfinite(r)
    r, blk = r[ok], blk[ok]
    groups = [r[blk == b] for b in np.unique(blk) if (blk == b).sum() >= 2]
    if len(groups) < 3:
        return float("nan")
    k = np.array([len(g) for g in groups]); n = int(k.sum()); N = len(groups)
    gm = np.concatenate(groups).mean()
    msb = sum(len(g) * (g.mean() - gm) ** 2 for g in groups) / (N - 1)
    msw = sum(((g - g.mean()) ** 2).sum() for g in groups) / max(1, n - N)
    k0 = (n - (k ** 2).sum() / n) / (N - 1)
    return float((msb - msw) / (msb + (k0 - 1) * msw))


def _kurt(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 8:
        return float("nan")
    z = (x - x.mean()) / (x.std() + 1e-12)
    return float(np.mean(z ** 4))


# 预注册：相关绝对误差至多 0.25；强相关保留符号及至少一半幅度。**只用于逐 replicate 的粗检查**，
# 不再用于总体门——绝对容差会把 0.14–0.18 这类真实但弱的拟合相关整体划进"允许归零"区，弱结构被摧毁也不报（R5-W）。
CORRELATION_TOLERANCE = 0.25
#: 总体门（均值 vs 拟合格点）的带宽：max(AGG_Z×SE(均值), AGG_ABS_FLOOR)。
#: 校准依据（4 机制 × 200 次重采样，实测见 report §4）：正常重采样 |Δ| 最大 0.0096、|Δ|/SE 最大 1.8；
#: 整块重排摧毁跨品种结构后 |Δ| 最小 0.1379（弱相关机制 cluster_heavy_tail，拟合 0.1393/0.1770/0.1795）。
#: 地板 0.03 在两侧各留约 3 倍余量；SE 项只在样本少时**放宽**，避免小样本假警报（MC8/MC9 的教训）。
AGG_Z = 6.0
AGG_ABS_FLOOR = 0.03
#: 逐 replicate 结构门的失败率上限：超过即认定不是估计噪声而是结构破坏（设计误拒率约 1–2%）
GUARD_FAIL_RATE_MAX = 0.05
MISSING_GENERATING_DIGEST = "missing-generating-digest"   # 源报告没有生成哈希时的显式占位：绝不回落到当前源码摘要


def _grid_cross_corr(grid, block_len_days: int) -> list[float]:
    """在**冲击格点**（day × instrument）上测跨品种相关：生成器直接操作的就是这个对象。
    episode 级块均值会被簇内特异噪声与每块样本量稀释，实测无区分力（正常与破坏型完全重叠），故结构门改在此测量。
    先按 L 日块聚合再算列间相关，与块重采样的粒度一致。"""
    if grid is None or not isinstance(grid, np.ndarray) or grid.ndim != 2 or grid.shape[1] < 2:
        return [math.nan]
    n_days, n_inst = grid.shape
    nb = max(1, int(math.ceil(n_days / block_len_days)))
    agg = np.full((nb, n_inst), np.nan)
    for b in range(nb):
        seg = grid[b * block_len_days:(b + 1) * block_len_days]
        if seg.size:
            with np.errstate(invalid="ignore"):
                agg[b] = np.nanmean(seg, axis=0)
    out = []
    for a_ in range(n_inst):
        for b_ in range(a_ + 1, n_inst):
            x, y = agg[:, a_], agg[:, b_]
            good = np.isfinite(x) & np.isfinite(y)
            out.append(float(np.corrcoef(x[good], y[good])[0, 1]) if (good.sum() >= 4 and np.std(x[good]) > 0 and np.std(y[good]) > 0) else math.nan)
    return out or [math.nan]


def _grid_n_blocks(grid, block_len_days: int) -> int:
    if grid is None or not isinstance(grid, np.ndarray) or grid.ndim != 2:
        return 0
    return max(1, int(math.ceil(grid.shape[0] / block_len_days)))


def _corr_preserved_z(rho_f: float, rho_n: float, nb_f: int, nb_n: int, k: float = 4.0) -> bool:
    """相关系数保持判据（Fisher-z 标准化）：|z(ρ_null) − z(ρ_fitted)| ≤ k·SE，SE=sqrt(1/(nb_f−3)+1/(nb_n−3))。
    噪声随块数与 ρ 自动缩放，避免用固定比例阈值在中等相关上制造误拒（实测 ρ≈0.30、nb≈120 时 sd≈0.11，
    原先的 "null ≥ 0.5·fitted" 只有约 1σ）。逐 replicate 取 k=4（每对误拒 ~0.006%）；**系统性**破坏由总体检查捕获。"""
    if not (math.isfinite(rho_f) and math.isfinite(rho_n)):
        return not math.isfinite(rho_f) and not math.isfinite(rho_n)
    if min(nb_f, nb_n) <= 4:
        return abs(rho_f - rho_n) <= CORRELATION_TOLERANCE
    z = lambda r: math.atanh(max(-0.999999, min(0.999999, r)))          # noqa: E731
    se = math.sqrt(1.0 / (nb_f - 3) + 1.0 / (nb_n - 3))
    return abs(z(rho_n) - z(rho_f)) <= k * se


def _grid_ac1(grid, block_len_days: int) -> float:
    """格点上的块间时间依赖：按 L 日块聚合后跨品种取均值，再算 lag-1 自相关。"""
    if grid is None or not isinstance(grid, np.ndarray) or grid.ndim != 2:
        return math.nan
    nb = max(1, int(math.ceil(grid.shape[0] / block_len_days)))
    bm = np.array([np.nanmean(grid[b * block_len_days:(b + 1) * block_len_days]) if grid[b * block_len_days:(b + 1) * block_len_days].size else np.nan for b in range(nb)])
    good = np.isfinite(bm)
    x = bm[good]
    if x.size < 5 or np.std(x[:-1]) == 0 or np.std(x[1:]) == 0:
        return math.nan
    return float(np.corrcoef(x[:-1], x[1:])[0, 1])


def _dependence_diagnostics(inp: PanelInputs, day: np.ndarray, block_len_days: int) -> tuple[float, list[float]]:
    if "instrument_id" not in inp.anchors.columns or len(day) != inp.n:
        return math.nan, [math.nan]
    inst = np.asarray(inp.anchors["instrument_id"].to_list())
    names = sorted(set(inst))
    if len(names) < 2:
        return math.nan, [math.nan]
    blocks = day // block_len_days
    grid = np.full((int(blocks.max()) + 1, len(names)), np.nan)
    means = np.full(len(grid), np.nan)
    for b in range(len(grid)):
        mask = (blocks == b) & np.isfinite(inp.base_R)
        if mask.any():
            means[b] = inp.base_R[mask].mean()
        for j, name in enumerate(names):
            rows = mask & (inst == name)
            if rows.any():
                grid[b, j] = inp.base_R[rows].mean()

    def corr(x, y):
        good = np.isfinite(x) & np.isfinite(y)
        if good.sum() < 4 or np.std(x[good]) == 0 or np.std(y[good]) == 0:
            return math.nan
        return float(np.corrcoef(x[good], y[good])[0, 1])

    cross = [corr(grid[:, a], grid[:, b]) for a in range(len(names)) for b in range(a + 1, len(names))]
    return corr(means[:-1], means[1:]), cross


@dataclass(frozen=True)
class DependenceReference:
    """依赖类诊断（块间自相关、跨品种块相关）的**校准参考带**。

    这两个统计量的逐 replicate 估计噪声很大（块数 ~span/L，每块每品种只有个位数机会），固定绝对容差会把结构完好的
    重采样判成失败——那是在惩罚估计噪声而不是惩罚结构破坏。做法：用生成器自身的 n_calib 次校准抽样（种子与验收
    replicate 不相交）估出各统计量的抽样分布，取 [q_lo, q_hi] 作逐 replicate 带（默认 0.5%/99.5%，期望误拒 ~1%）；
    结构是否真被破坏另由 run_mc 末尾的**总体均值检查**判定（均值偏离原面板超 tol 即整轮 invalid_null_model）。
    """
    ac1_band: tuple[float, float]
    cross_bands: tuple[tuple[float, float], ...]
    orig_ac1: float
    orig_cross: tuple[float, ...]
    n_calib: int
    q: tuple[float, float] = (0.1, 99.9)
    consistent_with_orig: bool = True     # 校准均值与原面板是否一致；False = 生成器本身偏了（带也跟着偏，逐 replicate 带不可信）
    calib_mean_ac1: float = math.nan
    calib_mean_cross: tuple[float, ...] = ()


def calibrate_dependence(world, model, *, n_calib: int = 60, block_len_days: int = 3, seed0: int = 10 ** 7,
                         q: tuple[float, float] = (0.1, 99.9)) -> DependenceReference:
    """用生成器自身抽样估依赖统计量的参考带；种子段 seed0 与验收 replicate（seed0*100000+i）不相交。"""
    o_ac, o_cross = _dependence_diagnostics(world.inputs, world.day, block_len_days)
    acs, crosses = [], []
    for i in range(n_calib):
        x = resample_null(world, model, np.random.default_rng(seed0 + i),
                          circular_shift=(world.cfg.mechanism == "circular_shift"))
        ac, cr = _dependence_diagnostics(x, world.day, block_len_days)
        acs.append(ac); crosses.append(cr)
    def band(vals):
        v = np.array([x for x in vals if math.isfinite(x)], dtype=float)
        if v.size < 5:
            return (-1.0, 1.0)
        lo, hi = np.percentile(v, q[0]), np.percentile(v, q[1])
        pad = 0.05 + 0.5 * float(np.std(v))          # 带宽下限：避免校准样本本身过窄
        return (float(lo) - pad, float(hi) + pad)
    n_cross = max(len(c) for c in crosses) if crosses else 0
    def mean_of(vals):
        v = np.array([x for x in vals if math.isfinite(x)], dtype=float)
        return float(v.mean()) if v.size else math.nan
    m_ac = mean_of(acs)
    m_cross = tuple(mean_of([c[j] for c in crosses if j < len(c)]) for j in range(n_cross))
    # 校准期一致性：带是用生成器自身抽样估的，生成器若偏了带也跟着偏 → 必须另与**原面板**比对。
    # 判据是"原面板的统计量是否是该生成器的一个可信抽样"——即 orig 落在校准带内（两端都不可估时视为无结构可保持）。
    # 用带而不是绝对容差，因为这些统计量的抽样 SE 随块数/每块样本量变化，绝对容差在小面板上会惩罚噪声。
    ac_band = band(acs)
    cross_bands_ = tuple(band([c[j] for c in crosses if j < len(c)]) for j in range(n_cross))
    def _consistent(o, b_, calib_mean):
        if not math.isfinite(o) and not math.isfinite(calib_mean):
            return True
        return _in_band(o, b_)
    consistent = (len(m_cross) == len(o_cross)
                  and _consistent(o_ac, ac_band, m_ac)
                  and all(_consistent(o, cross_bands_[j], m_cross[j]) for j, o in enumerate(o_cross)))
    return DependenceReference(ac1_band=ac_band, cross_bands=cross_bands_,
                               orig_ac1=o_ac, orig_cross=tuple(o_cross), n_calib=n_calib, q=q,
                               consistent_with_orig=bool(consistent), calib_mean_ac1=m_ac, calib_mean_cross=m_cross)


def _in_band(v: float, band: tuple[float, float]) -> bool:
    return bool(math.isfinite(v) and band[0] <= v <= band[1])


def grid_pair_labels(n_inst: int = len(INSTRUMENTS)) -> list[list[str]]:
    """跨品种相关向量的对标识，顺序与 _grid_cross_corr 的双重循环逐项对应（R5-G：报告须能核对齐全集与顺序）。"""
    names = list(INSTRUMENTS[:n_inst])
    return [[names[i], names[j]] for i in range(len(names)) for j in range(i + 1, len(names))]


def aggregate_pair_ok(fitted: float, mean: float, sd: float, n: int) -> tuple[bool, float, float]:
    """总体门的单对判据（R5-W）：比较**均值**与拟合值，带宽 = max(AGG_Z×SE(均值), AGG_ABS_FLOOR)。

    返回 (是否保持, |Δ|, 带宽)。要点：
    - 判的是重复抽样**均值**，其不确定性是 SE=sd/√n，与单次复制噪声 sd 不是一回事——旧的绝对容差
      把两者混为一谈，于是弱相关（拟合 0.14）被摧毁到 0 仍落在容差内（R5-W 反例）。
    - 只有一端不可估 → 显式判失败：结构信息丢失不能当作"无结构可保持"（R5-G）。
    """
    if not math.isfinite(fitted) and not math.isfinite(mean):
        return True, 0.0, math.inf          # 两端都不可估（品种/块太少）：无结构可保持
    if not math.isfinite(fitted) or not math.isfinite(mean):
        return False, math.inf, 0.0
    se = (sd / math.sqrt(n)) if (n >= 2 and math.isfinite(sd)) else math.inf
    tol = max(AGG_Z * se, AGG_ABS_FLOOR)
    d = abs(mean - fitted)
    return bool(d <= tol), d, tol


def aggregate_preserved(fitted: list, mean: list, sd: list, n) -> bool:
    """n 可以是标量或**逐对**样本数（R6-G：某对不可估时它的有效 n 比 n_grid 小）。"""
    if not fitted or len(fitted) != len(mean) or len(fitted) != len(sd):
        return False
    ns = list(n) if isinstance(n, (list, tuple)) else [n] * len(fitted)
    if len(ns) != len(fitted):
        return False
    return all(aggregate_pair_ok(f, m, s, k)[0] for f, m, s, k in zip(fitted, mean, sd, ns))


def max_realizable_sd(mean: float, n: int) -> float:
    """给定 n 次抽样与样本均值 m，ddof=1 样本标准差的**精确**上界（取值域 [-1,1]）。

    R6-G 用的 `s² ≤ n/(n−1)(1−m²)` 只是必要条件，有限 n 时未必可达（七审 R7-G：n=2、m=−0.9 时它给
    0.61644，真实上界是 0.141421）。精确解：在 Σx=n·m、x∈[-1,1] 上最大化 Σx²，最优点是至多一个坐标
    不在端点的顶点——设 a 个 +1、n−1−a 个 −1、余一个 r∈[-1,1]，则 Σx²=(n−1)+r²，
    r = n·m + (n−1) − 2a。枚举使 |r|≤1 的整数 a 取最大 r²，再换算方差。
    """
    if n < 2 or not math.isfinite(mean) or abs(mean) > 1.0:
        return math.inf
    s_sum = n * mean
    base = (s_sum + (n - 1)) / 2.0
    best = None
    for a_ in {math.floor(base) - 1, math.floor(base), math.ceil(base), math.ceil(base) + 1}:
        if 0 <= a_ <= n - 1:
            r = s_sum + (n - 1) - 2 * a_
            if abs(r) <= 1.0 + 1e-12:
                # R8-M：直接对**离差**求和，而不是 Σx² − n·m²。后者在 |m|→1 时相消：
                # n=1000、m=0.9999999999 的真实上界是 3.16e−9，相消写法算出 1.5e−8（偏大 5 倍）。
                ss = a_ * (1.0 - mean) ** 2 + (n - 1 - a_) * (1.0 + mean) ** 2 + (r - mean) ** 2
                best = ss if best is None else max(best, ss)
    if best is None:                      # |mean| ≤ 1 时总有可行点；保守退回必要条件
        return math.sqrt(max(0.0, n / (n - 1) * (1.0 - mean * mean)))
    return math.sqrt(max(0.0, best / (n - 1)))


def _correlation_preserved(a: float, b: float) -> bool:
    if not math.isfinite(a) and not math.isfinite(b):
        return True               # 两端都不可估（品种/块太少）：无结构可保持，不判失败
    if not math.isfinite(a) or not math.isfinite(b) or abs(a - b) > CORRELATION_TOLERANCE:
        return False
    if abs(a) > CORRELATION_TOLERANCE:
        return a * b > 0 and abs(b) >= 0.5 * abs(a)
    return True


def assert_not_episode_shuffle(orig: PanelInputs, null: PanelInputs, day: np.ndarray, *, block_len_days: int = 3,
                               reference: "DependenceReference | None" = None, fitted_grid=None) -> dict:
    """预注册结构保持门（每 replicate）：(1) 块 ICC 与原面板同量级且远高于逐 episode 洗牌（≈0）；(2) 尺度：sd 比 ∈ [0.5, 2]；
    (3) 尾部：峰度比 ∈ [0.4, 2.5]；(4) 缺失率比 ∈ [0.5, 2]（且非有限即失败）。任一失败 → invalid_null_model。"""
    identity_cols = ["episode_id", "cluster_id", "instrument_id", "t_dec"]
    layout = all(c in orig.anchors.columns and c in null.anchors.columns for c in identity_cols)
    if layout:
        layout = orig.anchors.select(identity_cols).equals(null.anchors.select(identity_cols))
    if null.n != orig.n or len(day) != orig.n:
        return {"ok": False, "checks": {"cluster_layout": False, "missing_layout": False}}
    ac_o, cross_o = _dependence_diagnostics(orig, day, block_len_days)
    ac_n, cross_n = _dependence_diagnostics(null, day, block_len_days)
    g_null = getattr(null, "shock_grid", None)
    g_cross_f = _grid_cross_corr(fitted_grid, block_len_days) if fitted_grid is not None else [math.nan]
    g_cross_n = _grid_cross_corr(g_null, block_len_days)
    g_ac_f, g_ac_n = _grid_ac1(fitted_grid, block_len_days), _grid_ac1(g_null, block_len_days)
    nb_f, nb_n = _grid_n_blocks(fitted_grid, block_len_days), _grid_n_blocks(g_null, block_len_days)
    grid_ok = (fitted_grid is not None and g_null is not None and len(g_cross_f) == len(g_cross_n)
               and any(math.isfinite(v) for v in g_cross_f))
    blk = day // block_len_days
    shuffled = np.random.default_rng(0).permutation(orig.base_R)
    i_o, i_n, i_s = _icc(orig.base_R, blk), _icc(null.base_R, blk), _icc(shuffled, blk)
    sd_o, sd_n = float(np.nanstd(orig.base_R)), float(np.nanstd(null.base_R))
    k_o, k_n = _kurt(orig.base_R), _kurt(null.base_R)
    miss_o, miss_n = float(np.mean(~np.isfinite(orig.base_R))), float(np.mean(~np.isfinite(null.base_R)))
    checks = {
        "cluster_layout": bool(layout),
        "missing_layout": bool(np.array_equal(orig.censored, null.censored)
                               and np.array_equal(np.isfinite(orig.base_R), np.isfinite(null.base_R))),
        # 依赖类统计量：**在冲击格点上**与拟合格点比对（生成器直接操作的对象，有区分力：实测正常 97% 通过、
        # 破坏跨品种结构 0% 通过）。无格点（精简 guard / 旧调用）时退回校准带或 episode 级绝对容差——
        # episode 级估计已实测无区分力，仅作报告，不单独作为拒收依据。
        # 块间时间依赖：**有格点时不设门**——整块自助法按定义保留块内相关、破坏块间相关（ADR §10），要求 null 的块间
        # lag-1 自相关等于拟合格点是与方法相矛盾的规格（实测拟合格点 ac1≈−0.34 全来自逐日残差估计噪声，null 按构造 ≈0）；
        # 无格点（精简 guard / 注入面板）时保留 episode 级粗检查，避免完全无门。
        "block_ac1": True if grid_ok else _correlation_preserved(ac_o, ac_n),
        "cross_instrument": (all(_corr_preserved_z(f_, v, nb_f, nb_n) for f_, v in zip(g_cross_f, g_cross_n)) if grid_ok else
                             (len(cross_o) == len(cross_n) and all(
                                 _in_band(v, reference.cross_bands[j]) if (reference and j < len(reference.cross_bands)) else _correlation_preserved(a_, v)
                                 for j, (a_, v) in enumerate(zip(cross_o, cross_n))))),
        "finite_diagnostics": all(math.isfinite(v) for v in (i_o, i_n, i_s, sd_o, sd_n, k_o, k_n, ac_o, ac_n, *cross_o, *cross_n)),
        # 块内聚集：本检查要分辨的是"保留块结构"与"逐 episode 洗牌"（后者 ICC≈0），故锚在**洗牌基线**上：
        # 要求 null 的 ICC 既超过绝对地板，又数倍于洗牌面板。原先的 i_n >= 0.5*i_o 是任意阈值——生成器按构造把
        # 条件均值置零，各机制保留比例本就不同（实测 49%–171%），nonuniform_density 恰卡在 0.5 上导致半数误拒。
        # i_n/i_o 比例仍作报告项（d["block_icc"]）。
        "icc": bool(np.isfinite(i_n) and i_n >= 0.05 and (not np.isfinite(i_s) or abs(i_s) < i_n / 3) and i_n >= 4 * abs(i_s)),
        "scale": bool(np.isfinite(sd_n) and sd_o > 0 and 0.5 <= sd_n / sd_o <= 2.0),
        "tail": bool(np.isfinite(k_n) and np.isfinite(k_o) and k_o > 0 and 0.4 <= k_n / k_o <= 2.5),
        "missing": bool(miss_o > 0 and 0.5 <= miss_n / miss_o <= 2.0) if miss_o > 0 else bool(miss_n <= 0.2),
    }
    d = {"block_icc": {"orig": i_o, "null": i_n, "episode_shuffle": i_s}, "sd": {"orig": sd_o, "null": sd_n}, "kurtosis": {"orig": k_o, "null": k_n},
         "missing_rate": {"orig": miss_o, "null": miss_n}, "checks": checks,
         "block_ac1": {"orig": ac_o, "null": ac_n}, "cross_instrument": {"orig": cross_o, "null": cross_n},
         "grid": {"used": bool(grid_ok), "ac1": {"fitted": g_ac_f, "null": g_ac_n},
                  "cross_instrument": {"fitted": g_cross_f, "null": g_cross_n}}}
    d["ok"] = all(checks.values())
    return d


# ---------------------------------------------------------------- MC 验收
def artifact_identity_digest() -> str:
    """运行制品身份摘要（冻结源码 + 声明依赖清单）。见 paths.artifact_identity_digest。

    G0 R-10 裁定 §2.2/§2.3：通用运行状态反射已移除，改为结构性关闭——
    未申报的状态根本过不去进程边界，而不是事后检测它们。
    """
    from quant_lab.research.paths import artifact_identity_digest as _a
    return _a()


def research_code_digest() -> str:
    """转发到唯一实现（quant_lab.research.paths），使账本血缘与报告内嵌哈希同源（R4-L）。"""
    from quant_lab.research.paths import research_code_digest as _d
    return _d()


def clopper_pearson(x: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    lo = 0.0 if x == 0 else float(beta.ppf(alpha / 2, x, n - x + 1))
    hi = 1.0 if x == n else float(beta.ppf(1 - alpha / 2, x + 1, n - x))
    return lo, hi


def default_candidates(n_feat: int, rules: list[RuleSpec] | None = None) -> list[Candidate]:
    # 植入结构 S={f00 < q30} 均值 −δ/π；能找回它的是 "take if f > q30"（gt_q30，跳过底部 30%）
    rules = rules or [RuleSpec("gt_q30", "gt_q", 0.3), RuleSpec("lt_q30", "lt_q", 0.3), RuleSpec("gt_q70", "gt_q", 0.7)]
    return [Candidate(f"f{k:02d}:{r.rule_id}", f"f{k:02d}", r) for k in range(n_feat) for r in rules]


@dataclass
class MCResult:
    mechanism: str
    kind: str                 # null | power
    n_planned: int
    n_done: int
    n_positive: int
    n_failed: int
    seeds: list[int]
    rate: float | None
    ci: tuple[float, float] | None
    worst_case_rate: float | None
    worst_case_ci: tuple[float, float] | None
    tiers: dict
    wall_s: float
    diagnostics: dict = field(default_factory=dict)
    verdict: str = "not_run"
    n_recovered: int = 0          # 功效：植入规则（f00:gt_q30）在 ≥1 折被 selection 选中的 replicate 数
    label: str = ""

    @property
    def n_T0(self) -> int:
        return int(self.tiers.get("T0", 0))

    @property
    def n_searched(self) -> int:
        """实际进入搜索（非 T0）的 replicate 数；T0 replicate cap=0 → 必然 no_claim，不作 FPR/功效证据。"""
        return self.n_done - self.n_T0

    def searched_rate_ci(self) -> tuple[float | None, tuple[float, float] | None]:
        """条件于有搜索的 replicate：null → (x+fail)/n_searched 最坏界；power → x/n_searched 最坏界。"""
        n = self.n_searched
        if n <= 0:
            return None, None
        x = self.n_positive + (self.n_failed if self.kind.startswith("null") else 0)
        return x / n, clopper_pearson(x, n)

    def to_dict(self) -> dict:
        d = {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}
        r, ci = self.searched_rate_ci()
        d.update({"n_T0": self.n_T0, "n_searched": self.n_searched, "searched_worst_rate": r, "searched_worst_ci": list(ci) if ci else None})
        return d


PLANTED = "f00:gt_q30"


def run_mc(mechanism: str, *, kind: str = "null", n_rep: int = 1000, seed0: int = 1, world_cfg: WorldConfig | None = None,
           pipe_cfg: PipelineConfig | None = None, delta: float = 0.2, pi: float = 0.3, fpr_max: float = 0.07, power_min: float = 0.80,
           progress=None, label: str = "") -> MCResult:
    wc = replace(world_cfg or WorldConfig(), mechanism=mechanism)
    pc = pipe_cfg or PipelineConfig()
    world = synth_world(replace(wc, seed=seed0))
    model = fit_residual_model(world, block_len_days=pc.block_len_days or 3, label_maturity_days=pc.label_maturity_days,
                               train_end_day=pc.min_train_span_days)
    # 逐 replicate 结构门在**冲击格点**上与拟合格点比对（见 assert_not_episode_shuffle / _grid_cross_corr）。
    # 早先的 episode 级校准带（calibrate_dependence）实测无区分力（正常与破坏型完全重叠），已退为只读诊断，不再 gating。
    dep_ref = None
    dep_ac, dep_cross = [], []
    fitted_grid = model.grid[:model.train_days[1]]          # 结构门的比对基准：拟合格点（生成器要保持的正是它的结构）
    grid_ac, grid_cross = [], []
    calib_bad = False      # 训练窗终点 = 首折 cutoff；成熟门与流水线一致；重采样块长固定 3 日（生成机制）
    cands = default_candidates(wc.n_candidates)
    seeds = [seed0 * 100_000 + i for i in range(n_rep)]
    n_pos = n_fail = n_done = n_rec = 0
    tiers: dict = {}
    by_L: dict[str, int] = {}
    chosen_L: dict[str, int] = {}
    guard_fail: dict[str, int] = {}
    t = time.perf_counter()
    ok0 = np.isfinite(world.inputs.base_R)
    n_invalid = 0
    diag = {"residual_model": model.diagnostics, "base_R_sd": float(np.std(world.inputs.base_R[ok0])), "n_episodes": int(world.inputs.n),
            "n_clusters": int(world.inputs.anchors["cluster_id"].n_unique())}
    for i, s in enumerate(seeds):
        rng = np.random.default_rng(s)
        try:
            inp = resample_null(world, model, rng, circular_shift=(mechanism == "circular_shift"), delta=(delta if kind == "power" else 0.0), pi=pi)
            guard = assert_not_episode_shuffle(world.inputs, inp, world.day, block_len_days=pc.block_len_days or 3,
                                               reference=None, fitted_grid=fitted_grid)
            gd = guard.get("grid", {})
            if gd.get("used"):
                grid_ac.append(gd["ac1"]["null"]); grid_cross.append(gd["cross_instrument"]["null"])
            dep_ac.append((guard.get("block_ac1") or {}).get("null", math.nan))      # 容忍精简 guard（故障注入/替身）
            dep_cross.append((guard.get("cross_instrument") or {}).get("null", []))
            if i == 0:
                diag["shuffle_guard"] = dict(guard, sample_index=0)      # 展示样本；R5-O：它失败也只按统一总量规则记账
            for k, ok in guard.get("checks", {}).items():
                guard_fail[k] = guard_fail.get(k, 0) + (0 if ok else 1)
            if not guard["ok"] or not all(guard.get("checks", {}).values()):
                tiers["invalid"] = tiers.get("invalid", 0) + 1
                n_done += 1; n_fail += 1; n_invalid += 1          # 诊断失败 → invalid_null_model：计失败最坏界，不得 pass
                continue
            rep = run_pipeline(inp, cands, replace(pc, seed=s), ledger=MemoryLedger())
            n_done += 1
            if rep.get("status") == "insufficient" or rep.get("final_status") not in ("ok", "no_claim"):
                n_fail += 1
            elif rep["synthetic_claim_positive"]:
                n_pos += 1
            if any(f.get("selected") == PLANTED for f in rep.get("folds", [])):
                n_rec += 1
            if rep.get("block_len_diagnostic"):
                k = f"L={rep['block_len_diagnostic']['chosen']}"
                chosen_L[k] = chosen_L.get(k, 0) + 1
            for L, fb in rep.get("final_by_block_len", {}).items():
                key = f"L={L}"
                by_L[key] = by_L.get(key, 0) + int(fb.get("status") == "ok" and bool(fb.get("lower_bound")) and fb["lower_bound"][0] > 0)
            tiers[rep.get("tier")] = tiers.get(rep.get("tier"), 0) + 1
        except Exception as e:  # noqa: BLE001
            n_done += 1; n_fail += 1
            tiers["error"] = tiers.get("error", 0) + 1
            diag.setdefault("errors", []).append(f"{type(e).__name__}: {e}"[:160])
        if progress and (i + 1) % 100 == 0:
            progress(mechanism, kind, i + 1, n_pos, n_fail)
    diag["positive_by_block_len"] = by_L          # 1/3/7 日敏感性：全部报告，不选最小 p
    # 总体均值检查（结构保持是**生成器**的性质，不是单次抽样的性质）：均值偏离原面板超容差 → 整轮 invalid
    def _agg(vals, orig):
        v = np.array([x for x in vals if math.isfinite(x)], dtype=float)
        return (float(v.mean()) if v.size else math.nan, float(orig))
    o_ac_ep, o_cross_ep = _dependence_diagnostics(world.inputs, world.day, pc.block_len_days or 3)
    agg = {"block_ac1": _agg(dep_ac, o_ac_ep),
           "cross_instrument": [_agg([c[j] for c in dep_cross if j < len(c)], o_cross_ep[j]) for j in range(len(o_cross_ep))]}
    # 与**原面板**的比对由校准带负责（consistent_with_orig：orig 是否是该生成器的可信抽样；破坏型生成器实测 20/20 被捕获）。
    # 这里的总体检查只回答另一个问题：**验收期的抽样是否相对校准期发生漂移**——均值应落在校准带内（带是单次抽样的分布，
    # 均值的 SE 更小，落在带内是很弱的要求，落在带外说明生成过程在验收期变了）。
    # 不再用"均值 vs orig 的相对幅度"判：orig 只是一次带噪估计，用精确均值去比它会再次惩罚噪声（MC9 实测把
    # cluster_heavy_tail / nonuniform_density 误判为结构未保持，而两者的 orig 都落在各自校准带内）。
    # 总体检查：格点量的均值 vs 拟合格点（有区分力的口径；均值比单次更稳，直接用绝对/相对规则即可）
    gf_cross, gf_ac = _grid_cross_corr(fitted_grid, pc.block_len_days or 3), _grid_ac1(fitted_grid, pc.block_len_days or 3)
    gc = np.array(grid_cross, dtype=float) if grid_cross else None
    gm_cross = (np.nanmean(gc, axis=0).tolist() if gc is not None else [math.nan] * len(gf_cross))
    gs_cross = (np.nanstd(gc, axis=0, ddof=1).tolist() if (gc is not None and gc.shape[0] >= 2) else [math.nan] * len(gf_cross))
    n_grid = int(gc.shape[0]) if gc is not None else 0
    # R6-G：**逐对**有效样本数。某一对不可估时 nanmean/nanstd 用的样本比 n_grid 少，
    # 拿共同 n_grid 当分母会高估精度（带宽被算窄），也让 sd 的可实现性上界失去依据。
    n_cross = (np.isfinite(gc).sum(axis=0).astype(int).tolist() if gc is not None else [0] * len(gf_cross))
    gm_ac = float(np.nanmean(grid_ac)) if grid_ac else math.nan
    # R5-W：总体门判"均值 vs 拟合"，带宽按均值的 SE 校准；块间 ac1 只报告不判（见 checks 注释）
    agg_ok = aggregate_preserved(gf_cross, gm_cross, gs_cross, n_cross)
    diag["grid_dependence"] = {"fitted_ac1": gf_ac, "null_mean_ac1": gm_ac, "fitted_cross": gf_cross,
                               "null_mean_cross": gm_cross, "null_sd_cross": gs_cross, "n_grid": n_grid, "n_cross": n_cross,
                               "pairs": grid_pair_labels(len(gf_cross) and len(INSTRUMENTS)),
                               "band": [aggregate_pair_ok(f_, m_, s_, k_)[2] for f_, m_, s_, k_ in zip(gf_cross, gm_cross, gs_cross, n_cross)],
                               "ok": bool(agg_ok)}
    diag["dependence_aggregate"] = {"block_ac1": agg["block_ac1"], "cross_instrument": agg["cross_instrument"], "ok": bool(agg_ok),
                                    "note": "episode 级依赖统计量仅作报告（实测无区分力）；判定以 grid_dependence 为准"}
    # 整轮 invalid 的判定：逐 replicate 诊断门有设计误拒率（校准带 q=0.1/99.9 + padding，实测约 1–2%），
    # 因此"任一失败即整轮 invalid"不成立——那会把估计噪声当成结构破坏。规则：
    #   (a) 总体均值检查失败（结构确实被破坏）→ invalid；(b) 逐 replicate 失败率 > GUARD_FAIL_RATE_MAX（远超设计误拒率）→ invalid。
    # 单次失败仍计入 n_failed，并在最坏界里按保守方向（null 计阳性 / power 计未检出）处理。
    guard_fail_rate = (n_invalid / n_done) if n_done else 0.0
    diag["guard_fail_rate"] = guard_fail_rate
    diag["invalid_reason"] = None
    if not agg_ok:
        diag["invalid_reason"] = "GRID_DEPENDENCE_NOT_PRESERVED"          # 格点级依赖结构未保持（与拟合格点比对）
    elif guard_fail_rate > GUARD_FAIL_RATE_MAX:
        diag["invalid_reason"] = f"GUARD_FAIL_RATE {guard_fail_rate:.3f} > {GUARD_FAIL_RATE_MAX}"
    diag["n_invalid_null_model"] = n_invalid
    diag["guard_failures_by_check"] = guard_fail
    diag["all_T0"] = bool(tiers.get("T0", 0) == n_done and n_done > 0)
    diag["chosen_block_len"] = chosen_L           # 训练诊断预注册的主 L 分布（block_len_days=None 时）
    # R6-L：显式声明 L 模式。原先"空字典"既表示固定 L、也表示分布被清空，于是清空即可跳过整个计数门。
    diag["block_len_mode"] = "auto" if pc.block_len_days is None else "fixed"
    diag["block_len_fixed"] = None if pc.block_len_days is None else int(pc.block_len_days)
    n_ok = n_done - n_fail
    if n_ok == 0:
        v = "invalid_null_model" if n_invalid > 0 else "not_run"
        # 全部失败也给保守最坏界：null 视全失败为阳性（rate 1）、power 视为未检出（rate 0）
        wc = clopper_pearson(n_done if kind == "null" else 0, n_done) if n_done else None
        return MCResult(mechanism, kind, n_rep, n_done, n_pos, n_fail, seeds[:5], None, None, (1.0 if kind == "null" else 0.0) if n_done else None, wc, tiers,
                        round(time.perf_counter() - t, 1), diag, v, n_rec, label)
    rate = n_pos / n_ok
    ci = clopper_pearson(n_pos, n_ok)
    if kind == "null":
        wc_x, wc_n = n_pos + n_fail, n_done                   # 失败（含 invalid_null_model）视为阳性
        wc_rate, wc_ci = wc_x / wc_n, clopper_pearson(wc_x, wc_n)
        verdict = "pass" if wc_ci[1] <= fpr_max else ("fail" if rate > fpr_max else "insufficient")
        if diag["invalid_reason"]:
            verdict = "invalid_null_model"                    # 只有总体检查失败或失败率超设计误拒率才整轮判废
        elif diag["all_T0"]:
            verdict = "not_run_T0"                            # 全部落 T0（cap=0 无搜索）：FPR 平凡为 0，不作 T1 验收替身
    else:
        wc_x, wc_n = n_pos, n_done                            # 失败视为未检出
        wc_rate, wc_ci = wc_x / wc_n, clopper_pearson(wc_x, wc_n)
        verdict = "pass" if wc_ci[0] >= power_min else ("fail" if rate < power_min else "insufficient")
        if diag["invalid_reason"]:
            verdict = "invalid_null_model"                    # 只有总体检查失败或失败率超设计误拒率才整轮判废
    return MCResult(mechanism, kind, n_rep, n_done, n_pos, n_fail, seeds[:5], rate, ci, wc_rate, wc_ci, tiers, round(time.perf_counter() - t, 1), diag, verdict, n_rec, label)


def verify_report_text(text: str) -> dict:
    """R-08 工程门：先核记录、诊断与计数，再独立复算 CP 和如实标志；不以低功效阻断出口 A。"""
    import re
    section = re.search(r"^## 3\.2 限制声明[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if section is None:
        raise ValueError("限制声明缺失")
    content = section.group(1)
    if not re.search(r"不能.{0,30}未检出.{0,30}(解释|等同).{0,20}无增益", content) or not re.search(r"合成[^\n]*不是真实", content):
        raise ValueError("限制声明必须解释未检出与合成范围")
    data = json.loads(text[text.rindex("```json") + 7:text.rindex("```")])
    embedded = (data.get("meta") or {}).get("research_code_sha256")
    current = research_code_digest()
    if embedded != current:
        raise ValueError(f"制品陈旧（A31）：报告由代码 {str(embedded)[:12]} 生成，当前研究代码为 {current[:12]}——"
                         f"门或流水线已变更，必须重跑 MC 再验收，不得用旧制品判定")
    # 生成来源的回执字段本身也必须在场且自洽——否则删掉它们就等于把 R6-H 那道门从制品里摘掉。
    # 父进程的执行修订可被判读方独立重算（覆盖集合由包决定），因此这里是真比对，不是抄录。
    meta = data.get("meta") or {}
    for k in ("worker_receipts_confirmed", "worker_code_sha256", "worker_artifact_identity", "parent_artifact_identity"):
        if k not in meta:
            raise ValueError(f"生成来源回执字段缺失：{k}（R6-H：删字段不得等于摘掉门）")
    if type(meta["worker_receipts_confirmed"]) is not int or meta["worker_receipts_confirmed"] != len(data["results"]):
        raise ValueError(f"worker 回执数 {meta['worker_receipts_confirmed']!r} 与结果行数 {len(data['results'])} 不符："
                         f"每个 job 恰好回传一份回执、产出一行结果")
    if meta["worker_code_sha256"] != [embedded]:
        raise ValueError(f"worker 磁盘身份 {meta['worker_code_sha256']} 与制品生成身份 {str(embedded)[:12]} 不一致")
    cur_exec = artifact_identity_digest()
    if meta["parent_artifact_identity"] != cur_exec:
        raise ValueError(f"制品陈旧：报告由制品身份 {str(meta['parent_artifact_identity'])[:12]} 生成，"
                         f"当前制品身份为 {cur_exec[:12]}——必须重跑 MC 再验收")
    if meta["worker_artifact_identity"] != [meta["parent_artifact_identity"]]:
        raise ValueError(f"worker 制品身份 {meta['worker_artifact_identity']} 与父进程 "
                         f"{str(meta['parent_artifact_identity'])[:12]} 不一致：存在混版")
    rows = data["results"]
    primary = [r for r in rows if r["kind"] in ("null", "power")]
    keys = [(r["kind"], r["mechanism"]) for r in primary]
    if len(keys) != len(set(keys)):
        raise ValueError("重复主机制")
    if {r["mechanism"] for r in primary if r["kind"] == "null"} != set(MECHANISMS):
        raise ValueError("空机制不完整")
    if not any(r["kind"] == "power" for r in primary):
        raise ValueError("缺功效记录")

    def require(ok, reason):
        if not ok:
            raise ValueError(reason)

    def count(x):
        require(type(x) is int and x >= 0, "计数须为非负整数")
        return x

    def finite_tree(value):
        if isinstance(value, dict):
            return all(finite_tree(v) for v in value.values())
        if isinstance(value, list):
            return all(finite_tree(v) for v in value)
        if isinstance(value, float):
            return math.isfinite(value)
        return True

    def close(actual, expected):
        require(np.allclose(actual, expected, rtol=0, atol=1e-12, equal_nan=False), "报告算术不一致")

    for r in rows:
        n, planned, x, failed = [count(r[k]) for k in ("n_done", "n_planned", "n_positive", "n_failed")]
        require(n == planned and n > 0 and x + failed <= n, "完成/失败计数不一致")
        tiers = r["tiers"]
        require(set(tiers) <= {"T0", "T1", "T2", "T3a", "T3b", "invalid", "error"}, "未知档位")
        require(sum(count(v) for v in tiers.values()) == n, "档分布不一致")
        ns = n - tiers.get("T0", 0)
        require(r["n_T0"] == tiers.get("T0", 0) and r["n_searched"] == ns, "搜索计数不一致")
        diag = r["diagnostics"]
        require(finite_tree(diag), "非有限诊断")
        require(count(diag.get("n_invalid", 0)) == 0 and count(r.get("n_invalid", 0)) == 0, "未知 invalid 计数字段")
        # R4-V：结构门判读必须从**原始诊断重算**，并与 invalid_reason / verdict / 各失败计数互相对账。
        # 只核 ok 标志时，篡改任一字段都能让总体门失效而计数、区间、哈希、§3.2 全部照旧。
        failures = diag["guard_failures_by_check"]
        n_invalid = count(diag["n_invalid_null_model"])
        gridd = diag.get("grid_dependence")
        require(isinstance(gridd, dict), "grid_dependence 缺失")
        require(all(k in gridd for k in ("fitted_cross", "null_mean_cross", "null_sd_cross", "n_grid", "n_cross",
                                         "pairs", "band", "fitted_ac1", "null_mean_ac1", "ok")), "grid_dependence 字段不完整")
        fc, mc_, sc_ = gridd["fitted_cross"], gridd["null_mean_cross"], gridd["null_sd_cross"]
        nc_, band_ = gridd["n_cross"], gridd["band"]
        # R5-G：向量必须按冻结世界核齐全集与顺序，并落在相关系数的值域内——只比两边长度相等是不够的
        require(gridd["pairs"] == grid_pair_labels(), "跨品种对标识缺失/顺序不符/含未知或重复对")
        require(all(isinstance(v, list) and len(v) == len(gridd["pairs"]) for v in (fc, mc_, sc_, nc_, band_)),
                "相关向量与品种对数不一致")
        for v in list(fc) + list(mc_):
            require(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and -1.0 <= v <= 1.0,
                    f"跨品种相关取值非法：{v!r}（须为有限实数且落在 [-1,1]）")
        n_grid = count(gridd["n_grid"])
        require(n - len(diag.get("errors", [])) <= n_grid <= n, "格点观测数与完成数不自洽")
        # R6-G：sd 不是随便一个非负数——相关系数恒在 [-1,1]，样本 sd 有硬上界 s² ≤ n/(n−1)·(1−m²)。
        # 超界的 sd 不可能由任何合法抽样产生，用它撑开带宽就能吞掉整段真实相关的丢失。
        sd_for_decision: list[float] = []
        for j, (m_, s_, k_) in enumerate(zip(mc_, sc_, nc_)):
            require(isinstance(s_, (int, float)) and not isinstance(s_, bool) and math.isfinite(s_) and s_ >= 0.0,
                    f"相关标准差非法：{s_!r}")
            require(type(k_) is int and 2 <= k_ <= n_grid, f"第 {j} 对的有效样本数非法：{k_!r}（须为 2..n_grid 的整数）")
            cap = max_realizable_sd(m_, k_)
            # **接收**与**判定**是两件事（九审 R9-M）。接收留 1e-6 相对容差，是为了容忍双精度在
            # |m|→1 时对上界本身的算术误差；但判定绝不能用超过精确上界的 sd——否则只把 sd 抬高
            # 5e-7 相对量就能把带撑宽约 3.8e-8，足以让一个本该失败的临界判定变成通过。
            require(s_ <= cap * (1 + 1e-6) + 1e-12,
                    f"第 {j} 对的相关标准差 {s_} 不可实现：均值 {m_}、n={k_} 时上界为 {cap}")
            sd_for_decision.append(min(s_, cap))
        # R7-G：逐对缺测必须在总账里出现。fitted 有限时，某 replicate 该对不可估会让
        # _corr_preserved_z(finite, nan) 为 False → 该次 cross_instrument 失败并计 invalid。
        # 于是「n_cross 远小于 n_grid」与「cross_instrument 零失败」不能同时成立。
        missing = [n_grid - k_ for k_ in nc_]
        cross_fail = count(failures.get("cross_instrument", 0))
        require(max(missing, default=0) <= cross_fail,
                f"逐对缺测 {missing} 未计入 cross_instrument 失败（记 {cross_fail}）：报告声称了两件不能同时成立的事")
        agg_ok = aggregate_preserved(fc, mc_, sd_for_decision, nc_)      # 与 run_mc 同一判据，用**钳到精确上界**的 sd
        for j, (f_, m_, s_, k_, b_) in enumerate(zip(fc, mc_, sd_for_decision, nc_, band_)):   # 派生带宽必须能被重算
            require(isinstance(b_, (int, float)) and not isinstance(b_, bool), f"第 {j} 对的带宽非数：{b_!r}")
            close(b_, aggregate_pair_ok(f_, m_, s_, k_)[2])
        require(gridd["ok"] is agg_ok, "grid_dependence.ok 与重算不符")
        require((diag.get("dependence_aggregate") or {}).get("ok") is agg_ok, "dependence_aggregate.ok 与重算不符")

        per_check = [count(v) for v in failures.values()]
        # 一次 replicate 可同时命中多个 check，故 max ≤ n_invalid ≤ sum；三处计数必须同源
        require(max(per_check, default=0) <= n_invalid <= sum(per_check), "guard 失败计数与 invalid 数不自洽")
        require(count(tiers.get("invalid", 0)) == n_invalid, "tiers.invalid 与 n_invalid_null_model 不一致")
        # R5-C：结构失败与运行错误都是**失败集合的子集**，必须真正计进 n_failed，否则三分母最坏界会被低估到翻转准入
        require(count(tiers.get("invalid", 0)) + count(tiers.get("error", 0)) <= failed,
                f"invalid({tiers.get('invalid', 0)}) + error({tiers.get('error', 0)}) 未全部计入失败数 {failed}")
        # R5-O：展示样本只是第一个 replicate 的快照，不构成"首个必须零失败"的门；但它失败就必须在总量里有对应记账
        guard = diag["shuffle_guard"]
        require(isinstance(guard, dict) and isinstance(guard.get("checks"), dict) and bool(guard["checks"]), "guard 样本缺 checks")
        require(set(guard["checks"]) <= set(failures), "guard 失败计数缺 check")
        require(all(isinstance(v, bool) for v in guard["checks"].values()), "guard check 非布尔")
        require(guard.get("ok") is all(v is True for v in guard["checks"].values()), "guard 样本 ok 与其 checks 不自洽")
        for k_, v_ in guard["checks"].items():
            require(v_ is True or count(failures.get(k_, 0)) >= 1, f"展示样本 {k_} 失败却未计入 guard_failures_by_check")
        require(guard["ok"] is True or n_invalid >= 1, "展示样本失败却未计入 invalid 数")
        rate = diag.get("guard_fail_rate")
        require(isinstance(rate, (int, float)) and not isinstance(rate, bool) and math.isfinite(rate), "guard_fail_rate 非数")
        close(rate, n_invalid / n)
        # 与 run_mc **同一口径**：≤ GUARD_FAIL_RATE_MAX 是设计误拒率带，不是零失败；单次失败已按保守方向计进最坏界。
        expected_reason = None if agg_ok else "GRID_DEPENDENCE_NOT_PRESERVED"
        if expected_reason is None and rate > GUARD_FAIL_RATE_MAX:
            expected_reason = f"GUARD_FAIL_RATE {rate:.3f} > {GUARD_FAIL_RATE_MAX}"
        require((diag.get("invalid_reason") or None) == expected_reason,
                f"invalid_reason 与重算不符：记录 {diag.get('invalid_reason')!r} vs 重算 {expected_reason!r}")
        require((r["verdict"] == "invalid_null_model") == bool(expected_reason), "verdict 与 invalid 判定不一致")
        require(expected_reason is None, f"空模型无效：{expected_reason}")
        require(not diag.get("all_T0"), "全部 T0")
        require(len(diag.get("errors", [])) <= failed and tiers.get("error", 0) <= failed, "错误计数不一致")
        # R6-L：L 模式必须显式声明——原先"空字典"既表示固定 L、又能表示分布被清空，清空即可跳过整个计数门。
        # R7-L：并且不采信自报——模式必须与 meta 里**结构化的冻结配置**一致，否则自称 fixed 就能跳过 auto 的计数门。
        mode = diag.get("block_len_mode")
        require(mode in ("auto", "fixed"), f"block_len_mode 缺失或非法：{mode!r}")
        require("pipeline_block_len_days" in meta, "meta 缺结构化冻结配置 pipeline_block_len_days（R7-L）")
        frozen_L = meta["pipeline_block_len_days"]
        require(frozen_L is None or (type(frozen_L) is int and frozen_L > 0), f"冻结 L 非法：{frozen_L!r}")
        # 自审：结构化冻结配置本身也是自报的——只改它和逐结果三个字段就能跳过 auto 计数门。
        # 报告里另有两处独立编码同一件事（PipelineConfig 的 repr 与 §1 的 L 字段），要求三者一致，
        # 伪造者必须同时改到三处才谈得上自洽。这不消除溯源边界，只是把"改三个字段"的成本抬掉。
        # 只能拿**正文**比，不能拿整份文档比：整份文档含那段 JSON，被改过的值必然"出现在文档里"，条件恒真。
        body = text[:text.rindex("```json")]
        require(str(meta.get("pipeline", "")) in body and str(meta.get("world", "")) in body,
                "正文渲染的世界/流水线配置与内嵌 meta 不同源")
        # 本模块 CLI 无条件构造 PipelineConfig(..., block_len_days=None)，没有任何选项能设固定 L；
        # 因此"命令是本 CLI"与"冻结为 fixed"互相矛盾。**若将来给 CLI 加了该选项，这条断言必须同步改。**
        if str(meta.get("command", "")).startswith("python -m quant_lab.research.nullmodel"):
            require(meta["pipeline_block_len_days"] is None,
                    f"记录的命令是本模块 CLI（无固定 L 选项），却声称冻结 L={meta['pipeline_block_len_days']!r}")
        mm = re.search(r"block_len_days=([^,)\s]+)", str(meta.get("pipeline", "")))
        require(mm is not None, "meta.pipeline 里读不到 block_len_days（R7-L 交叉核对）")
        repr_L = None if mm.group(1) == "None" else int(mm.group(1))
        require(repr_L == frozen_L, f"meta.pipeline 的 block_len_days={repr_L!r} 与结构化冻结配置 {frozen_L!r} 不符")
        mL = meta.get("L")
        require((isinstance(mL, str) and mL.startswith("auto")) == (frozen_L is None),
                f"meta.L={mL!r} 与冻结配置 block_len_days={frozen_L!r} 不符")
        if frozen_L is not None:
            require(mL == frozen_L, f"meta.L={mL!r} 与冻结 L {frozen_L} 不符")
        require(mode == ("auto" if frozen_L is None else "fixed"),
                f"自报 block_len_mode={mode} 与冻结配置 block_len_days={frozen_L!r} 不符（R7-L）")
        require(diag.get("block_len_fixed") == frozen_L, f"block_len_fixed 与冻结配置不符：{diag.get('block_len_fixed')!r} vs {frozen_L!r}")
        chosen = diag.get("chosen_block_len", {})
        if mode == "auto":
            require(isinstance(chosen, dict) and bool(chosen), "auto 模式必须给出主 L 分布（空分布不得兼任免检开关）")
            require(set(chosen) <= {"L=1", "L=3", "L=7"}, f"主 L 取值非法：{sorted(chosen)}")
            # R5-C：结构早退（invalid）与异常（error）都没进流水线，不该有主 L；其余完成的 replicate 应各有一个
            got = sum(count(v) for v in chosen.values())
            require(n - failed <= got <= n - len(diag.get("errors", [])) - n_invalid,
                    f"块长计数不一致：主 L 共 {got}，但完成 {n}、失败 {failed}、invalid {n_invalid}、error {len(diag.get('errors', []))}")
        else:
            require(not chosen, "fixed 模式不应给出主 L 分布")
            require(type(diag.get("block_len_fixed")) is int and diag["block_len_fixed"] > 0, "fixed 模式须声明固定 L")
            # 固定 L 的合法值域由流水线决定，不擅自缩成 {1,3,7}
        # R9-ACCOUNT（能翻转结论的那条）：阳性只能来自**既非 T0、又没出错、也没判结构无效**的 replicate。
        # 否则可以写成 T0=499 / T1=2 / error=499 却报 480 个阳性——每条计数、每个 CP 区间都自洽，
        # 判读照样接受，而实际只有 2 个 replicate 有机会产出阳性。
        eligible = n - count(tiers.get("T0", 0)) - count(tiers.get("error", 0)) - count(tiers.get("invalid", 0))
        require(0 <= eligible <= n, "可产出阳性的 replicate 数计算越界")
        require(x <= eligible, f"阳性 {x} 超过可产出阳性的 replicate 数 {eligible}"
                               f"（T0={tiers.get('T0', 0)}、error={tiers.get('error', 0)}、invalid={n_invalid}）")
        require(all(count(v) <= eligible for v in diag.get("positive_by_block_len", {}).values()), "阳性计数不一致")
        require(count(r.get("n_recovered", 0)) <= eligible,
                f"规则找回数 {r.get('n_recovered')} 超过可产出阳性的 replicate 数 {eligible}")
        # error 档与错误清单是同一件事的两种记法（run_mc 每次异常同时写两处），必须逐条对上
        require(len(diag.get("errors", [])) == count(tiers.get("error", 0)),
                f"错误清单 {len(diag.get('errors', []))} 条与 error 档 {tiers.get('error', 0)} 不一致")
        require(n > failed and ns > 0, "没有可复算样本")
        worst = x
        if r["kind"].startswith("null"):
            worst += failed
        require(worst <= ns, "搜索分母不足")
        close(r["rate"], x / (n - failed))
        close(r["ci"], clopper_pearson(x, n - failed))
        close(r["worst_case_rate"], worst / n)
        close(r["worst_case_ci"], clopper_pearson(worst, n))
        close(r["searched_worst_rate"], worst / ns)
        interval = clopper_pearson(worst, ns)
        close(r["searched_worst_ci"], interval)
        if r["kind"] not in ("null", "power"):
            continue
        require(n >= 1000 and ns >= 500 and ns > n / 2, "主记录次数/搜索量不足")
        if r["kind"] == "null":
            require(interval[1] <= 0.07 and r["verdict"] == "pass", "FPR 未通过")
        else:
            if interval[0] >= 0.8:
                require(r["verdict"] == "pass", "功效 flag 与复算不符")
            else:
                require(r["verdict"] in ("fail", "insufficient"), "功效不足却伪造 pass")
            print("POWER(reported, limitation statement applies)", r["mechanism"], x, ns, round(interval[0], 4), r["verdict"])
    print("R-08 report verified:", len(primary), "unique primary records; arithmetic and diagnostics consistent")
    return data


def _grid_pairs(gd: dict) -> str:
    """把每个品种对写成「拟合→null 均值（|Δ| vs 带宽）」，判据可直接目视复核（R5-W）。"""
    f, m = gd.get("fitted_cross") or [], gd.get("null_mean_cross") or []
    sd, n = gd.get("null_sd_cross") or [], gd.get("n_grid") or 0
    out = []
    for i, (a_, b_) in enumerate(zip(f, m)):
        s_ = sd[i] if i < len(sd) else float("nan")
        ok_, d_, tol_ = aggregate_pair_ok(a_, b_, s_, n)
        out.append(f"{a_:.3f}→{b_:.3f}（|Δ|={d_:.4f} vs 带 {tol_:.4f}{'' if ok_ else ' **超**'}）")
    return "、".join(out) or "—"


def write_report(results: list[MCResult], out: Path, *, meta: dict, code_digest: str | None = None) -> None:
    """code_digest=None 表示"本次由当前代码生成"；--rebuild 传入**源报告的**哈希，
    因为重排版不重跑 MC，结果的来源仍是旧代码——重新盖章会让旧结果换到新身份（R4-V 反例）。"""
    digest = research_code_digest() if code_digest is None else code_digest
    def ci_s(c):
        return "—" if c is None else f"[{100 * c[0]:.2f}%, {100 * c[1]:.2f}%]"
    nulls = [r for r in results if r.kind == "null"]
    powers = [r for r in results if r.kind == "power"]
    lines = [
        "# report-G3-null-model：空模型 FPR / 功效验收（合成 T1 规模，synthetic_validation）",
        "",
        f"生成本报告的研究代码 sha256：`{digest}`（A31：制品须由不旧于门代码的版本生成，R-08 verify 机械比对）。",
        "",
        f"日期：{dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC。状态：合成数据实测；**claim_status = descriptive_only**（G-STAT-CLAIM pending），本报告不构成任何研究优势声明，也不替代真实数据前的空模型验收。",
        "依据：合并稿 D.5、ADR-G3 §10、Claude 验收 R03（按档分级：T1/T2 各机制 1000 次；T3 200 次预注册更宽精确区间）。",
        f"命令：`{meta.get('command', '')}`",
        "",
        "## 1. 设定",
        "",
        f"- 合成世界：{meta['world']}；候选池：{meta['n_candidates']} 个（{meta['candidates']}）；流水线：{meta['pipeline']}。",
        "- 空模型：训练窗（前 50% 天）拟合联合残差 → 整 L 日块（全品种联动）重采样格点冲击 + 整簇重采样特异残差 → 固定 episode 布局暴露映射；因果特征独立随机流重生成；**禁止逐 episode 洗牌**（结构断言 + 诊断见 §4）。",
        f"- 每次 replicate 完整流程：walk-forward 折 → 分档/预算 → search（阈值只在 search 子窗拟合）→ selection（共同块 max-t 一次，B={meta['B']}，α={meta['alpha']}）→ 外折每机会一次预测 → final 全时间共同块 θ/LB（主 L={meta['L']}：由首折训练窗残差块均值 lag-1 自相关诊断在 1/3/7 中预注册，不看候选结果；1/3/7 日敏感性另报）。",
        "- FPR 事件：final LB > 0（合成声明函数，输出 synthetic_validation）；失败/insufficient 单列并给最坏界。",
        f"- 验收阈值：FPR 双侧 95% Clopper–Pearson 上界 ≤ 7%；功效（δ={meta['delta']} R/种子，π={meta['pi']}）下界 ≥ 80%。",
        f"- 环境：{platform.machine()} / Python {platform.python_version()}；seed 清单：每机制 seed0×100000+i（seed0 见表）。",
        "",
        "## 2. FPR（每种预注册空机制）",
        "",
        "| 机制 | 计划 n | 完成 | 失败/insufficient | 阳性 x | FPR 点估计 | FPR 95% CI（区间） | 最坏界（失败计阳性）FPR / CI | 有搜索 replicate（非 T0）n / 最坏界 FPR / CI | 阳性数按块长 L=1/3/7（敏感性，不选） | 档分布 | 耗时 s | 判定 |",
        "|---|---:|---:|---:|---:|---:|---|---|---|---|---|---:|---|",
    ]
    for r in nulls:
        bl = r.diagnostics.get("positive_by_block_len", {})
        sr, sci = r.searched_rate_ci()
        lines.append(f"| {r.mechanism} | {r.n_planned} | {r.n_done} | {r.n_failed} | {r.n_positive} | {('—' if r.rate is None else f'{100 * r.rate:.2f}%')} | {ci_s(r.ci)} | "
                     f"{('—' if r.worst_case_rate is None else f'{100 * r.worst_case_rate:.2f}%')} / {ci_s(r.worst_case_ci)} | {r.n_searched} / {('—' if sr is None else f'{100 * sr:.2f}%')} / {ci_s(sci)} | "
                     f"{bl.get('L=1', 0)} / {bl.get('L=3', 0)} / {bl.get('L=7', 0)}（主 L 分布 {json.dumps(r.diagnostics.get('chosen_block_len', {}))}） | {json.dumps(r.tiers)} | {r.wall_s} | **{r.verdict}** |")
    worst = max((r.worst_case_ci[1] for r in nulls if r.worst_case_ci), default=None)
    lines += [
        "",
        f"- 最坏机制 FPR CI 上界：{('—' if worst is None else f'{100 * worst:.2f}%')}（阈值 7%）。所有机制均须过门，不混池稀释。",
        "- T0 replicate（基线 DEFF 降档 → cap=0 无搜索）必然 no_claim，不作 FPR 证据；验收以「有搜索 replicate」条件最坏界为准，并要求其占多数且 ≥ 500 次。",
        "",
        "## 3. 功效（注入 δ）",
        "",
        "| 机制 | 计划 n | 完成 | 失败 | 检出 x | 功效点估计 | 功效 95% CI | 最坏界（失败计未检出）功效 / CI | 规则找回率 | base_R sd（噪声） | 耗时 s | 判定 |",
        "|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---|",
    ]
    for r in powers:
        sr, sci = r.searched_rate_ci()
        lines.append(f"| {r.mechanism} | {r.n_planned} | {r.n_done} | {r.n_failed} | {r.n_positive} | {('—' if r.rate is None else f'{100 * r.rate:.2f}%')} | {ci_s(r.ci)} | "
                     f"{('—' if r.worst_case_rate is None else f'{100 * r.worst_case_rate:.2f}%')} / {ci_s(r.worst_case_ci)}（有搜索 {r.n_searched}：{('—' if sr is None else f'{100 * sr:.2f}%')} / {ci_s(sci)}） | {100 * r.n_recovered / max(1, r.n_done):.1f}% | "
                     f"{r.diagnostics.get('base_R_sd', float('nan')):.2f} | {r.wall_s} | **{r.verdict}** |")
    if powers:
        pw = powers[0]
        lines += ["", f"- 功效未达标时按 D.5「未达标限制声明或扩新样本」处理：本档（T1，约 {pw.diagnostics.get('n_clusters')} 簇 / {pw.diagnostics.get('n_episodes')} 机会，噪声 sd≈{pw.diagnostics.get('base_R_sd', float('nan')):.2f}R）对 δ=0.2R 的全流程功效见上表；§3.1 给出效应/噪声/样本规模的适用范围。流程的功效瓶颈在 selection（内层 max-t 只见训练窗一半样本）。"]
    pw = powers[0] if powers else None
    lines += [
        "",
        "## 3.2 限制声明（用户裁定出口 A，2026-09-11；规范性，随本报告发布）",
        "",
        f"> 在 T1 档合成规模（{meta.get('world_summary', '约 1600 经济簇')}）下，本协议对每种子 {meta['delta']}R 的真实过滤增益的全流程检出功效"
        + (f"约 {100 * pw.rate:.1f}%（条件于实际进入搜索的 replicate 为 {100 * (pw.searched_rate_ci()[0] or 0):.1f}%，"
           f"95% 最坏界下界约 {100 * (pw.worst_case_ci[0] if pw.worst_case_ci else 0):.1f}%）"
           if (pw and pw.rate is not None) else "未测")   # 全失败时 rate 为 None：写"未测"而不是让排版崩掉
        + "。因此：",
        "> 1. 本协议在该规模下**不能**把「未检出」解释为「无增益」；未检出只描述为「在该功效下未检出」。",
        "> 2. 任何 P2 真实数据研究若样本规模/噪声与该档相当，只能对更大量级的效应或更大样本作检出声明；本档效应量需预注册扩样本后另验。",
        "> 3. 上述数字均为**合成**世界预注册假设下的结果，**不是真实**频道数据的功效；真实功效须在 G1/G2 真实接缝就绪后按实测 K/DEFF 重估。",
        "> 4. FPR 结论不受功效不足影响：假阳性控制按上表逐机制判定。",
        "",
        "GOAL-3 §7 的 P1 DoD「功效 ≥ 80%」按用户裁定改为「功效报告 + 本限制声明」，原条件未达标的记录保留于 §3。",
    ]
    exts = [r for r in results if r.kind == "null_ext"]
    if exts:
        lines += [
            "",
            "### 2.1 预注册扩展运行（区间不足 → 只增加 replicate，不改流程、阈值或机制；主判定仍以 1000 次登记结果为准）",
            "",
            "| 机制 | 扩展 n | 完成 | 失败 | 阳性 | FPR | 95% CI | 最坏界 CI | 档分布 | 判定（扩展） | 说明 |",
            "|---|---:|---:|---:|---:|---:|---|---|---|---|---|",
        ]
        for r in exts:
            lines.append(f"| {r.mechanism} | {r.n_planned} | {r.n_done} | {r.n_failed} | {r.n_positive} | {('—' if r.rate is None else f'{100 * r.rate:.2f}%')} | {ci_s(r.ci)} | {ci_s(r.worst_case_ci)} | {json.dumps(r.tiers)} | **{r.verdict}** | {r.label} |")
    sens = [r for r in results if r.kind == "power_sens"]
    if sens:
        lines += [
            "",
            "### 3.1 功效敏感性（效应 / 噪声 / 样本规模；每格 n=200，非验收，只描述阈值适用范围）",
            "",
            "| 设定 | 机制 | n | 检出 | 功效 | 95% CI | 规则找回率（植入候选被选中 ≥1 折） | base_R sd | 簇数 | 档分布 |",
            "|---|---|---:|---:|---:|---|---:|---:|---:|---|",
        ]
        for r in sens:
            lines.append(f"| {r.label} | {r.mechanism} | {r.n_done} | {r.n_positive} | {('—' if r.rate is None else f'{100 * r.rate:.1f}%')} | {ci_s(r.ci)} | {100 * r.n_recovered / max(1, r.n_done):.1f}% | "
                         f"{r.diagnostics.get('base_R_sd', float('nan')):.2f} | {r.diagnostics.get('n_clusters')} | {json.dumps(r.tiers)} |")
    lines += [
        "",
        "## 4. 空模型诊断（块内相关是否保留）",
        "",
        "| 机制 | 训练窗天数 / 观测 | 块均值 lag-1 自相关 | 跨品种格点相关 | 块 ICC orig / null / 逐 episode 洗牌 | 逐 replicate 门 | 格点总体门：拟合 → null 均值（判定依据） |",
        "|---|---|---:|---:|---|---|---|",
    ]
    for r in nulls:
        rm = r.diagnostics.get("residual_model", {}); sg = r.diagnostics.get("shuffle_guard", {})
        b = sg.get("block_icc", {})
        lines.append(f"| {r.mechanism} | {rm.get('train_days')} / {rm.get('n_train_obs')}（未成熟排除 {rm.get('n_immature_excluded')}） | {rm.get('block_mean_ac1', float('nan')):.3f} | {rm.get('cross_inst_corr', float('nan')):.3f} | "
                     f"{b.get('orig', float('nan')):.4f} / {b.get('null', float('nan')):.4f} / {b.get('episode_shuffle', float('nan')):.4f} | "
                     f"失败 {r.diagnostics.get('n_invalid_null_model', 0)} / {r.n_done} = {100 * r.diagnostics.get('guard_fail_rate', 0.0):.2f}%（带 ≤ {100 * GUARD_FAIL_RATE_MAX:.0f}%） | "
                     f"{_grid_pairs(r.diagnostics.get('grid_dependence', {}))} → **{r.diagnostics.get('grid_dependence', {}).get('ok')}**（invalid_reason={r.diagnostics.get('invalid_reason')}） |")
    lines += [
        "",
        f"总体门的带宽 = max({AGG_Z:g}×SE(均值), {AGG_ABS_FLOOR}), SE=sd/√n_grid（n_grid 为参与统计的 replicate 数）。"
        f"**为什么不是绝对容差**（R5-W）：绝对容差把 0.14–0.18 这类真实但弱的拟合相关整体划进「允许归零」区，"
        f"结构被完全摧毁也不报。校准依据（4 机制 × 200 次重采样实测）：正常重采样 |Δ| 最大 0.0096、|Δ|/SE 最大 1.8；"
        f"整块重排摧毁跨品种结构后 |Δ| 最小 0.1379（弱相关机制 cluster_heavy_tail）。地板 {AGG_ABS_FLOOR} 两侧各留约 3 倍余量；"
        f"SE 项只在样本少时**放宽**带宽——样本不足时本就无从区分，宁可不报也不制造假警报。",
        "",
        "**两个门，口径不同，不得混称**（R4-V）：",
        "",
        f"1. **逐 replicate 结构门**（冲击格点上的 Fisher-z 判据）有设计误拒率，按校准带约 1–2%。单次失败的 replicate 直接计失败，"
        f"并按保守方向进最坏界（空机制计阳性、功效计未检出），**但不单独使整轮判废**——那等于拿估计噪声当结构破坏。"
        f"只有失败率 > {100 * GUARD_FAIL_RATE_MAX:.0f}%（远超设计误拒率）才判 invalid_null_model。",
        "2. **格点总体门**（上表末列）比较拟合格点与全 replicate 均值的跨品种相关：均值比单次稳，是真正的判定依据。"
        "它失败即整轮 invalid_null_model，与逐 replicate 失败次数是否为零无关。",
        "",
        "R-08 的机器判读（verify_report_text）用**同一套规则从原始诊断重算**这两条，并要求 "
        "`grid_dependence.ok` / `dependence_aggregate.ok` / `invalid_reason` / `verdict` / `tiers.invalid` / "
        "`n_invalid_null_model` / `guard_fail_rate` / `guard_failures_by_check` 八处彼此自洽——单改任意一处即被拒收。",
        "",
        f"**带宽的分母与 sd 都不是自由参数**（R6-G）：带宽用**逐对**有效样本数 `n_cross`（某对不可估时它小于 `n_grid`，"
        f"拿共同 n_grid 当分母会把带宽算窄）；相关系数逐次取值恒在 [-1,1]，故样本 sd 有硬上界 "
        f"精确上界（见 `max_realizable_sd`）——超界的 sd 不可能由任何合法抽样产生，判读按该上界拒收，"
        f"`band` 也必须能由 (fitted, mean, sd, n_cross) 重算出来。这条不属溯源边界："
        f"不必相信 fitted 是真值、也不必重跑 MC 就能否证。用的是**精确**上界而非 `n/(n−1)·(1−m²)`："
        f"后者只是必要条件，有限 n 时未必可达（n=2、m=−0.9 时它给 0.616，真实上界是 0.141，R7-G）。",
        "",
        f"**逐对缺测必须在总账里出现**（R7-G）：fitted 有限时，某次该对不可估会让逐 replicate 的 "
        f"`cross_instrument` 判据为假、该次计 invalid。因此「`n_cross` 远小于 `n_grid`」与「`cross_instrument` 零失败」"
        f"不能同时成立，判读强制 `max(n_grid − n_cross) ≤ guard_failures_by_check['cross_instrument'] ≤ n_invalid`。"
        f"否则把某对的有效样本数报成 2，就能用一个**本身可实现**的 sd 把带宽撑开。",
        "",
        f"**主 L 分布显式声明模式**（R6-L）：`block_len_mode` 为 auto 时必须给出合法分布并无条件核上下界；"
        f"为 fixed 时必须声明固定 L 且不给分布。原先「空字典」既表示固定 L、又能表示分布被清空，于是清空即可跳过整个计数门。",
        "",
        "另有三条记账约束（R5-C / R5-G / R5-O）：**(a)** `tiers.invalid + tiers.error ≤ n_failed`，"
        "且主 L 分布只覆盖真正进入流水线的 replicate——结构早退者不该有主 L；结构失败必须真正计进失败数，"
        "否则三分母最坏界会被低估到足以翻转 7% 准入。**(b)** 跨品种相关向量按冻结世界核对齐全集与顺序"
        f"（{len(INSTRUMENTS)} 品种恰好 {len(grid_pair_labels())} 对，见上表 `pairs`），取值须为有限实数且落在 [-1,1]，"
        "缺项/重复/越界一律拒收。**(c)** 上表展示的 `shuffle_guard` 只是第一个 replicate 的快照，"
        "**不是**「首个必须零失败」的门；它失败时必须在 per-check 与 invalid 总量里有对应记账。",
        "",
        f"生成身份（A31 / R5-H）：MC 起跑冻结父进程源码摘要，**每个 worker 另行回传自己起跑与收尾的摘要**，"
        f"父进程逐份核对，任何不等或缺回执都拒绝落盘。本报告的 worker 回执数："
        f"{meta.get('worker_receipts_confirmed', '—')}，回执摘要集合：{meta.get('worker_code_sha256', '—')}。"
        f"**结构性隔离，不是事后检测**（G0 R-10 裁定 §2.2/§2.3）：本轮起**移除**对任意运行状态的通用反射，"
        f"改为让未申报的状态根本过不去进程边界——整组 worker 由**全新解释器**（显式 spawn）启动、"
        f"配置以**纯数据**过界并在 worker 内重建、每个 job 回传**制品身份**（冻结源码摘要 + 声明依赖清单），"
        f"父进程逐份核对。父进程制品身份：{str(meta.get('parent_artifact_identity', '—'))[:16]}…；"
        f"worker 制品身份集合：{[str(x)[:16] + '…' for x in meta.get('worker_artifact_identity', [])]}；"
        f"依赖清单：{meta.get('artifact_manifest', {}).get('deps', '—')}。",
        "",
        f"**能力边界**（A39，方法边界而非待办）：对支持域内的**事故类**混版——陈旧 `__pycache__`、"
        f"fork 继承父进程模块对象、普通导入顺序——本系统以结构性隔离关闭。对**对抗类**"
        f"（复现必须在 worker 进程内执行代码去绑定 globals、改注册表项、改类属性或默认参数），"
        f"**本系统不声称防护**，且该防护对任意 callable 不可判定。判别一条反例属哪类只问一句："
        f"**能不能在不向 worker 进程内注入代码的前提下复现**。详见 docs/adr/capability-G3-execution-identity.md。",
        "",
        "全部落 T0 的机制标 not_run_T0（cap=0 无搜索，FPR 平凡为 0，不作 T1 验收替身）。",
        "",
        "## 5. 总判定（synthetic_validation）",
        "",
        f"- FPR：{'、'.join(f'{r.mechanism}={r.verdict}' for r in nulls)}；扩展：{'、'.join(f'{r.mechanism}={r.verdict}' for r in exts) or '无'}。",
        f"- 功效（δ={meta['delta']}）：{'、'.join(f'{r.mechanism}={r.verdict}' for r in powers)}。",
        "- 全部结果只描述；任何档位的真实数据声明仍需 G-STAT-CLAIM。",
        "",
        "## 6. 资源与限制",
        "",
        f"- 总耗时 {sum(r.wall_s for r in results):.0f}s；单 replicate 均值 {sum(r.wall_s for r in results) / max(1, sum(r.n_done for r in results)):.3f}s；峰值 RSS 见 report.json。",
        "- 限制：合成世界的相关结构是预注册假设，不等于真实频道数据；T3 档（200 次）未运行；块长敏感性只报告不选择；max-t 是依赖假设下近似，不是有限样本保证。",
        "- 任何真实数据的 θ 声明须另行通过 G-STAT-CLAIM、最终 V 窗口与 latency=1s 敏感性；本报告结果只描述。",
        "",
        "## 附：原始结果 JSON",
        "",
        "```json",
        json.dumps({"meta": meta | {"research_code_sha256": digest}, "results": [r.to_dict() for r in results]},
                   ensure_ascii=False, indent=1, default=str),   # 不截断：本块是机器可读载荷，截断会让 verify/复核无法解析
        "```",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


#: 正式验收入口允许的**纯数据**标量类型（精确类型，不收子类——IntEnum 之类另有取值语义）
_PURE_SCALARS = (bool, int, float, str, type(None))
_PURE_MAX_DEPTH = 6


def as_pure_data(value, path: str = "<config>", depth: int = 0):
    """把配置校验成**纯数据**，不是纯数据就具名拒绝。

    这是顾问建议的窄入口：正式 MC 不再把父进程的活对象（可调用实例、闭包、任意外部类型）
    传给 worker，worker 在自己进程里用校验过的数据重建配置。任意 Python 行为依赖因此
    根本进不到验收路径，而不是靠反射去证明它们已被完整编码。
    """
    if depth > _PURE_MAX_DEPTH:
        raise UnsupportedConfigValue(f"配置嵌套超过 {_PURE_MAX_DEPTH} 层：{path}")
    if type(value) in _PURE_SCALARS:
        if isinstance(value, float) and not math.isfinite(value):
            raise UnsupportedConfigValue(f"配置含非有限浮点：{path}={value!r}")
        return value
    if type(value) in (list, tuple):
        return [as_pure_data(v, f"{path}[{i}]", depth + 1) for i, v in enumerate(value)]
    if type(value) is dict:
        out = {}
        for k, v in value.items():
            if type(k) is not str:
                raise UnsupportedConfigValue(f"配置字典键必须是 str：{path} 的 {k!r}")
            out[k] = as_pure_data(v, f"{path}.{k}", depth + 1)
        return out
    raise UnsupportedConfigValue(f"配置值不是纯数据：{path}（类型 {type(value).__module__}.{type(value).__name__}）")


class UnsupportedConfigValue(TypeError):
    """正式验收入口只接受纯数据配置；活对象在这里被具名拒绝（顾问建议的窄入口）。"""


def _checked_fields(data: dict, cls) -> dict:
    """未申报字段**具名拒绝，不是忽略**（G0 R-10 裁定 §4 的 P1 最小集要求）。

    直接 `cls(**data)` 对多余键会抛 TypeError，但消息里看不出是「schema 外字段」；
    缺字段则会被默认值悄悄补上。这里把两种都变成具名失败。
    """
    declared = {f.name for f in dataclasses.fields(cls)}
    extra = sorted(set(data) - declared)
    missing = sorted(declared - set(data))
    if extra:
        raise UnsupportedConfigValue(f"{cls.__name__} 配置含未申报字段：{extra}（schema 外字段必须拒绝，不得忽略）")
    if missing:
        raise UnsupportedConfigValue(f"{cls.__name__} 配置缺字段：{missing}（不得用默认值悄悄补齐）")
    return dict(data)


def _config_to_data(cfg) -> dict:
    return as_pure_data({f.name: getattr(cfg, f.name) for f in dataclasses.fields(cfg)},
                        f"<{type(cfg).__name__}>")


def _run_mc_job(*, world_cfg_data: dict, pipe_cfg_data: dict, **kw) -> dict:
    """worker 侧从**纯数据**重建配置，再自报生成身份。

    配置以数据过界、在 worker 内构造，父进程的活对象不再跨进程传递（顾问建议的窄入口）；
    身份回执保留：起跑与收尾各取一次磁盘与执行摘要，父进程逐份核对。
    """
    wc = WorldConfig(**_checked_fields(as_pure_data(world_cfg_data, "<WorldConfig>"), WorldConfig))
    pd_ = _checked_fields(as_pure_data(pipe_cfg_data, "<PipelineConfig>"), PipelineConfig)
    if isinstance(pd_.get("block_len_sensitivity"), list):
        pd_["block_len_sensitivity"] = tuple(pd_["block_len_sensitivity"])
    pc = PipelineConfig(**pd_)
    return _run_mc_job_impl(world_cfg=wc, pipe_cfg=pc, **kw)


def _run_mc_job_impl(**kw) -> dict:
    """worker 侧自报生成身份（R5-H）：起跑与收尾各取一次递归源码摘要，随结果回传，父进程逐份核对。

    父进程首尾摘要相等**不能**证明各 worker 见到的源码与它相同——spawn 的延迟导入、
    不同 worker 的源码视图、A→B→A 式往返都不在父进程那两次测量的覆盖范围内。
    """
    d0, e0 = research_code_digest(), artifact_identity_digest()
    r = run_mc(**kw)
    return {"digest_start": d0, "digest_end": research_code_digest(),
            "artifact_start": e0, "artifact_end": artifact_identity_digest(),
            "pid": os.getpid(), "result": r}


def check_worker_receipt(payload, frozen_digest: str, frozen_exec: str | None = None) -> "MCResult":
    """父进程侧核对 worker 回执：缺回执、磁盘摘要或**执行修订**摘要与冻结身份不等，一律拒绝发布。

    磁盘摘要（R5-H）查的是"文件长什么样"，执行摘要（R6-H）查的是"这个 worker 实际跑的是哪一版"——
    导入缓存 / 驻留修订能让两者背离：六审的反例正是首尾磁盘摘要全等、但两个 worker 执行了另一修订。
    """
    if not isinstance(payload, dict) or "result" not in payload:
        raise SystemExit("worker 未回传生成身份回执：结果不得发布（R5-H）")
    if payload["result"] is None or not isinstance(payload["result"], MCResult):
        raise SystemExit(f"worker 回执里的结果不是 MCResult（{type(payload['result']).__name__}）：不得发布")
    for key in ("digest_start", "digest_end"):
        got = payload.get(key)
        if got != frozen_digest:
            raise SystemExit(f"worker 的 {key}={str(got)[:12]} 与父进程冻结磁盘身份 {frozen_digest[:12]} 不一致："
                             f"该结果并非由本次冻结的源码产生，不得发布（R5-H）")
    if frozen_exec is not None:
        for key in ("artifact_start", "artifact_end"):
            got = payload.get(key)
            if got != frozen_exec:
                raise SystemExit(f"worker 的 {key}={str(got)[:12]} 与父进程冻结制品身份 {frozen_exec[:12]} 不一致："
                                 f"该 worker 跑的不是被冻结的那一份制品，不得发布")
    return payload["result"]




def _main(argv=None):  # pragma: no cover - CLI
    """登记运行（4 空机制 × n_rep + 功效 × n_rep）→ 预注册扩展（区间不足的机制加跑 n_ext；多数落 T0 的机制按 n_clusters_t1 重跑）→ 功效敏感性网格。"""
    import argparse
    ap = argparse.ArgumentParser(prog="quant_lab.research.nullmodel")
    ap.add_argument("--out", default="docs/adr/report-G3-null-model.md")
    ap.add_argument("--n-rep", type=int, default=1000)
    ap.add_argument("--n-ext", type=int, default=4000)
    ap.add_argument("--mechanisms", default=",".join(MECHANISMS))
    ap.add_argument("--power-mechanisms", default="common_shock")
    ap.add_argument("--B", type=int, default=2000)
    ap.add_argument("--delta", type=float, default=0.2)
    ap.add_argument("--n-clusters", type=int, default=1600, help="内层折 K 与基线 DEFF 降档后仍落 T1 所需的合成簇数（S06 口径）")
    ap.add_argument("--n-clusters-t1", type=int, default=2400, help="机制多数落 T0 时的预注册重跑簇数")
    ap.add_argument("--no-ext", action="store_true")
    ap.add_argument("--jobs", type=int, default=min(10, (os.cpu_count() or 5)),
                    help="并行进程数（每个 run_mc 一个进程；seed 清单不变，结果与串行一致）")
    ap.add_argument("--rebuild", default=None, help="从既有报告的原始 JSON 重排版（不重跑 MC），用于报告格式/派生列更新")
    a = ap.parse_args(argv)
    if a.rebuild:
        t = Path(a.rebuild).read_text(encoding="utf-8")
        j = json.loads(t[t.rindex(chr(96) * 3 + "json") + 7:t.rindex(chr(96) * 3)])
        fields = {f for f in MCResult.__dataclass_fields__}
        results = [MCResult(**{k: (tuple(v) if isinstance(v, list) and k in ("ci", "worst_case_ci") else v) for k, v in d.items() if k in fields}) for d in j["results"]]
        src_digest = (j.get("meta") or {}).get("research_code_sha256") or MISSING_GENERATING_DIGEST
        meta = dict(j["meta"]); meta["research_code_sha256"] = src_digest
        meta["renderer_code_sha256"] = research_code_digest()                        # 排版器身份另记，不冒充生成身份
        write_report(results, Path(a.out), meta=meta, code_digest=src_digest)        # 保留源哈希：重排版没有重跑 MC
        print("rebuilt", a.out, "from", a.rebuild, "| preserved generating sha256:", str(src_digest)[:12],
              "| renderer:", meta["renderer_code_sha256"][:12])
        return
    frozen_digest = research_code_digest()          # A31：MC 起跑即冻结生成身份，结束时确认源码未在运行期变动
    frozen_exec = artifact_identity_digest()           # 制品身份：冻结源码 + 声明依赖清单
    print("generating code sha256 (frozen at start):", frozen_digest, flush=True)
    print("artifact identity (frozen at start):", frozen_exec, flush=True)
    wc = WorldConfig(n_clusters=a.n_clusters)
    pc = PipelineConfig(B=a.B, block_len_days=None)      # 主 L 由每 replicate 首折训练窗残差诊断预注册（1/3/7）
    results: list[MCResult] = []

    def prog(m, k, i, pos, fail):
        print(f"[{m}/{k}] {i} done  positive={pos} failed={fail}", flush=True)

    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context
    mechs = [m for m in a.mechanisms.split(",") if m]
    jobs = []      # (kind_override, label, kwargs)
    for m in mechs:
        jobs.append(("null", "", dict(mechanism=m, kind="null", n_rep=a.n_rep, seed0=1 + MECHANISMS.index(m), world_cfg=wc, pipe_cfg=pc)))
    for m in [x for x in a.power_mechanisms.split(",") if x]:
        jobs.append(("power", "", dict(mechanism=m, kind="power", n_rep=a.n_rep, seed0=11 + MECHANISMS.index(m), world_cfg=wc, pipe_cfg=pc, delta=a.delta)))

    worker_receipts: list[str] = []
    worker_execs: list[str] = []

    def _as_job_kwargs(kw: dict) -> dict:
        """把活的配置对象换成纯数据后再过界——正式入口不传活对象。"""
        out = dict(kw)
        out["world_cfg_data"] = _config_to_data(out.pop("world_cfg"))
        out["pipe_cfg_data"] = _config_to_data(out.pop("pipe_cfg"))
        return out

    def run_jobs(js):
        # 显式 spawn：不依赖平台默认值，保证每个 worker 都是**全新解释器**、不继承父进程已加载的模块
        with ProcessPoolExecutor(max_workers=max(1, a.jobs), mp_context=get_context("spawn")) as ex:
            futs = [ex.submit(_run_mc_job, **_as_job_kwargs(kw)) for _, _, kw in js]
            out = []
            for (kind_o, label, _), fu in zip(js, futs):
                payload = fu.result()
                r = check_worker_receipt(payload, frozen_digest, frozen_exec)   # 缺回执 / 身份不等 → 拒绝发布
                worker_receipts.append(payload["digest_end"]); worker_execs.append(payload["artifact_end"])
                r.kind = kind_o; r.label = label or r.label
                print(r.to_dict(), flush=True)
                out.append(r)
        return out

    # 固定网格的功效敏感性与主阶段**没有依赖关系**，同批提交即可把 8 个短任务塞进主阶段的空闲核，
    # 墙钟从"主阶段 + 扩展阶段"压到"最长单任务"（实测 45 分钟 → 26 分钟）。
    # 只有下面两条 null_ext 规则依赖主阶段判定，必须留在第二阶段。
    if not a.no_ext:
        grid = [(d, ns, nc) for d in (0.2, 0.4) for ns in (1.0, 0.6) for nc in (a.n_clusters, a.n_clusters * 3 // 2)]
        for d, ns, nc in grid:
            jobs.append(("power_sens", f"δ={d} noise×{ns} clusters={nc}",
                         dict(mechanism="common_shock", kind="power", n_rep=200, seed0=41,
                              world_cfg=replace(wc, noise_scale=ns, n_clusters=nc), pipe_cfg=pc, delta=d)))
    results += run_jobs(jobs)
    if not a.no_ext:
        ext = []
        for r in [x for x in results if x.kind == "null"]:
            if r.verdict == "insufficient":
                ext.append(("null_ext", f"区间不足 → 同流程加跑 {a.n_ext} 次（新 seed 段）",
                            dict(mechanism=r.mechanism, kind="null", n_rep=a.n_ext, seed0=21 + MECHANISMS.index(r.mechanism), world_cfg=wc, pipe_cfg=pc)))
            if r.verdict == "not_run_T0" or r.tiers.get("T0", 0) > r.n_done / 2:
                ext.append(("null_ext", f"多数 replicate 落 T0（cap=0，FPR 平凡为 0）→ 预注册 n_clusters={a.n_clusters_t1} 重跑使其落 T1",
                            dict(mechanism=r.mechanism, kind="null", n_rep=a.n_rep, seed0=31 + MECHANISMS.index(r.mechanism), world_cfg=replace(wc, n_clusters=a.n_clusters_t1), pipe_cfg=pc)))
        if ext:
            results += run_jobs(ext)
    # 只按 kind 归位；Python 的 sort 是稳定的，各 kind 内部保持提交顺序，报告行序与并行化之前一致
    order = {"null": 0, "power": 1, "null_ext": 2, "power_sens": 3}
    results.sort(key=lambda r: order.get(r.kind, 9))
    meta = {"command": " ".join(["python -m quant_lab.research.nullmodel"] + (argv or [])), "world": str(wc), "n_candidates": len(default_candidates(wc.n_candidates)),
            "candidates": "12 独立特征 × 规则 {gt_q30, lt_q30, gt_q70}（36 提交，T1 cap=12 → 按规范顺序前 12 个 = f00..f03 × 3 规则，含植入候选 f00:gt_q30）", "pipeline": str(pc), "B": pc.B, "alpha": pc.alpha,
            "L": "auto(1/3/7 训练诊断)" if pc.block_len_days is None else pc.block_len_days, "delta": a.delta, "pi": wc.pi,
        "pipeline_block_len_days": pc.block_len_days,          # R7-L：结构化冻结配置，判读侧据此核每条记录自报的 L 模式
        "worker_receipts_confirmed": len(worker_receipts), "worker_code_sha256": sorted(set(worker_receipts)),
        "worker_artifact_identity": sorted(set(worker_execs)), "parent_artifact_identity": frozen_exec,
        "artifact_manifest": __import__("quant_lab.research.paths", fromlist=["x"]).artifact_identity()}
    end_exec = artifact_identity_digest()
    if end_exec != frozen_exec:
        raise SystemExit(f"父进程的制品身份在 MC 运行期间发生变化（起 {frozen_exec[:12]} / 止 {end_exec[:12]}）：不得落盘")
    end_digest = research_code_digest()
    if end_digest != frozen_digest:                 # 运行期改过源码 → 结果与任何单一代码身份都不对应，拒绝落盘
        raise SystemExit(f"研究代码在 MC 运行期间发生变化（起 {frozen_digest[:12]} / 止 {end_digest[:12]}）："
                         f"结果与代码身份不再一一对应，必须在稳定源码上重跑，不得落盘")
    write_report(results, Path(a.out), meta=meta, code_digest=frozen_digest)
    print("written", a.out, "| generating code sha256:", frozen_digest[:12])


if __name__ == "__main__":  # pragma: no cover
    _main()


__all__ = ["INSTRUMENTS", "MECHANISMS", "research_code_digest", "MCResult", "ResidualModel", "World", "WorldConfig", "assert_not_episode_shuffle", "artifact_identity_digest", "check_worker_receipt", "clopper_pearson",
           "default_candidates", "fit_residual_model", "resample_null", "run_mc", "synth_world", "write_report"]
