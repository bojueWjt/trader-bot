"""共同日历块 max-t bootstrap（契约 feature-snapshot §5；ADR-G3 §9；合并稿 D.5）。

PairedPanel：逐机会配对差面板（episode / cluster / t_dec / 初始权重 w_i=1/n_c / 共同 mask m_i / 各候选差 d_ij）。
共同日历：UTC 固定起点 origin，长度 L 天不重叠块（含空块）；整簇归最早 t_dec 所在块，保持簇完整；
所有候选共用同一块索引矩阵 I[b, q]；非空块 < 20 → insufficient。
重估器：每次重采样按块实例重建 A_j* = Σ m w d_ij、D* = Σ m w、θ_j* = A_j*/D*（整簇抽样下副本簇权重与原簇相同，
仍按副本重建；快速路径用块充分统计量，与逐行 reference 对拍）。
SE：共同块 delete-one jackknife，se_j² = (Q−1)/Q Σ_q (θ_(−q) − mean)²，空块保留在 Q 中；原样本与每个 bootstrap 样本同一 SE 估计器。
max-t：t_j = θ_j / se_j；T_b = max_j (θ*_bj − θ_j)/se*_bj（单侧，不取绝对值）；p_adj,j = (1 + #{T_b ≥ t_j})/(B+1)；
k = ceil((1−α)(B+1))，q = 升序 T 的第 k 项，LB_j = θ_j − q·se_j（依赖假设下 bootstrap 近似下界，不称有限样本保证）。
insufficient：非空块不足、零/不稳定 SE（se ≤ 1e-12·max(1,|θ|)）、重采样零分母、非有限、k > B、候选零方差。
旧签名 max_t_bootstrap(thetas, ses, ...) 无 panel → status=contract_blocked（ADR C03），不以隐式全局缓存补数据。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass, field

import numpy as np
import polars as pl

MIN_NONEMPTY_BLOCKS = 20


@dataclass(frozen=True)
class SufficiencyFloor:
    """最小有效样本下限的**成对声明**（execution-interface §5.22 B20 第 3 节）。

    口径与 `calendar_blocks` 的 `n_nonempty` 逐字一致：非空 = 共同支持上 `W = mask × weight > 0` 的日历块；
    全删失块保留在重采样矩阵里但不计入准入。

    B20 的四条在此固化：
    1. 口径 = `n_nonempty`（本文件 `calendar_blocks`）；
    2. **下限必须与 `block_len_days` 成对声明**，且 `block_len_days ≥ research_horizon_days + embargo_days`
       ——只写块数不写块长，同一个数字可以对应任意强度；
    3. **同时钉住 `cross`（跨块簇占比）与 `max_span`**：两者为 0 与非 0 含义完全不同，只看块数看不出来；
    4. 数据量由切分算术直接给出：达到 Q 个非空块，每折约需 `Q × block_len_days` 天日历
       （5d 窗口、Q=20 → 100 天）。`required_calendar_days()` 给出该数字。

    `max_cluster_span_frac` / `max_span_days` 的默认值是**提案**（P1 合成用），真实数据须随 G-STAT-CLAIM 确认。
    """
    block_len_days: int
    research_horizon_days: float
    embargo_days: float = 0.0
    min_nonempty: int = MIN_NONEMPTY_BLOCKS
    max_cluster_span_frac: float = 0.10
    max_span_days: float | None = None          # None → 2 × block_len_days

    def __post_init__(self):
        if self.block_len_days < self.research_horizon_days + self.embargo_days:
            raise ValueError(
                f"block_len_days={self.block_len_days} < research_horizon+embargo="
                f"{self.research_horizon_days + self.embargo_days}：块自举的独立性前提不成立（B20 §3.2）")
        if self.min_nonempty < 1:
            raise ValueError("min_nonempty 须 ≥ 1")

    @property
    def span_cap_days(self) -> float:
        return float(self.max_span_days if self.max_span_days is not None else 2 * self.block_len_days)

    def required_calendar_days(self) -> float:
        """达到下限所需的每折日历长度（B20 §3.4 的切分算术）。"""
        return float(self.min_nonempty * self.block_len_days)

    def check(self, bi: "BlockIndex") -> str | None:
        """返回 None（通过）或不足原因。"""
        if bi.block_len_days != self.block_len_days:
            return f"BLOCK_LEN_MISMATCH {bi.block_len_days} != 声明的 {self.block_len_days}"
        if bi.n_nonempty < self.min_nonempty:
            return f"NONEMPTY_BLOCKS {bi.n_nonempty} < {self.min_nonempty}（block_len={self.block_len_days}d）"
        if bi.cluster_span_frac > self.max_cluster_span_frac:
            return f"CLUSTER_SPAN_FRAC {bi.cluster_span_frac:.3f} > {self.max_cluster_span_frac}"
        if bi.max_cluster_span_days > self.span_cap_days:
            return f"MAX_CLUSTER_SPAN {bi.max_cluster_span_days:.2f}d > {self.span_cap_days}d"
        return None
SE_FLOOR_REL = 1e-12


@dataclass(frozen=True)
class PairedPanel:
    episode_ids: tuple[str, ...]
    cluster_ids: tuple[str, ...]
    t_dec: tuple[dt.datetime, ...]
    weights: np.ndarray            # 初始 w_i = 1/n_c
    mask: np.ndarray               # 共同可评 m_i（bool）
    diffs: np.ndarray              # (N, C) 配对差 d_ij；被 mask 掉的行值忽略
    candidate_ids: tuple[str, ...]
    fold_ids: tuple[str, ...] = ()
    family_hash: str = ""

    @property
    def n(self) -> int:
        return len(self.episode_ids)

    @property
    def n_candidates(self) -> int:
        return len(self.candidate_ids)

    @staticmethod
    def from_frame(df: pl.DataFrame, candidate_cols: list[str], *, weight_col="weight", mask_col="m") -> "PairedPanel":
        d = df.sort("t_dec", "episode_id")
        for c in ("episode_id", "cluster_id", "t_dec", weight_col, mask_col):
            if c not in d.columns or d[c].null_count():
                raise ValueError(f"PairedPanel: 列 {c} 缺失或含 null")
        if d["episode_id"].n_unique() != d.height:
            raise ValueError("PairedPanel: episode_id 重复")
        w = d[weight_col].cast(pl.Float64)
        if (~w.is_finite()).any() or (w <= 0).any():
            raise ValueError("PairedPanel: weight 须为正有限")
        m = d[mask_col].cast(pl.Boolean).to_numpy()
        cols = []
        for c in candidate_cols:
            v = d[c].cast(pl.Float64).to_numpy().astype(float)
            bad = m & ~np.isfinite(v)
            if bad.any():
                raise ValueError(f"PairedPanel: 候选 {c} 在 {int(bad.sum())} 个共同可评行（m=True）上为 null/NaN/Inf，拒收（不得静默补 0）")
            cols.append(np.where(m, v, 0.0))          # m=False 处的存储占位 0，无经济含义
        diffs = np.column_stack(cols) if candidate_cols else np.zeros((d.height, 0))
        fam = hashlib.sha256(("|".join(candidate_cols) + "|" + "|".join(d["episode_id"].to_list())).encode()).hexdigest()[:16]
        return PairedPanel(tuple(d["episode_id"]), tuple(d["cluster_id"]), tuple(d["t_dec"]), w.to_numpy().copy(),
                           m.copy(), np.ascontiguousarray(diffs, dtype=float), tuple(candidate_cols),
                           tuple(d["fold_id"]) if "fold_id" in d.columns else (), fam)


@dataclass(frozen=True)
class BlockIndex:
    origin: dt.datetime
    block_len_days: int
    n_blocks: int                  # Q（含空块）
    block_of_row: np.ndarray       # 每行所属块（按簇最早 t_dec）
    n_nonempty: int
    cluster_span_frac: float       # 跨块（成员 t_dec 落在不同块）的簇比例
    max_cluster_span_days: float


def calendar_blocks(panel: PairedPanel, *, block_len_days: int, origin: dt.datetime | None = None) -> BlockIndex:
    """块索引：整簇归最早 t_dec 所在块；n_nonempty 只数**共同支持上 W_b > 0**（有 m=True 且权重>0 的行）的块，
    全删失块保留在重采样矩阵里但不计入准入门。"""
    ts = list(panel.t_dec)
    if not ts:
        raise ValueError("空面板")
    origin = origin or min(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    L = dt.timedelta(days=block_len_days)
    raw = np.array([int((t - origin) / L) for t in ts])
    first: dict[str, int] = {}
    span: dict[str, list] = {}
    for c, b, t in zip(panel.cluster_ids, raw, ts):
        first[c] = min(first.get(c, b), b)
        span.setdefault(c, []).append(t)
    block = np.array([first[c] for c in panel.cluster_ids])
    n_blocks = int(raw.max()) + 1
    multi = sum(1 for c, b in zip(panel.cluster_ids, raw) if b != first[c])
    n_cl = len(first)
    cross = len({c for c, b in zip(panel.cluster_ids, raw) if b != first[c]})
    max_span = max(((max(v) - min(v)).total_seconds() / 86400 for v in span.values()), default=0.0)
    mw = panel.mask.astype(float) * panel.weights
    W = np.zeros(n_blocks)
    np.add.at(W, block, mw)
    return BlockIndex(origin, block_len_days, n_blocks, block, int((W > 0).sum()), cross / n_cl if n_cl else 0.0, max_span)


# ---------------------------------------------------------------- 重估器
def block_sums(panel: PairedPanel, bi: BlockIndex) -> tuple[np.ndarray, np.ndarray]:
    """S[b, j] = Σ_{i∈b} m_i w_i d_ij；W[b] = Σ_{i∈b} m_i w_i（空块为 0）。"""
    mw = panel.mask.astype(float) * panel.weights
    S = np.zeros((bi.n_blocks, panel.n_candidates))
    W = np.zeros(bi.n_blocks)
    np.add.at(S, bi.block_of_row, panel.diffs * mw[:, None])
    np.add.at(W, bi.block_of_row, mw)
    return S, W


def reestimate_rows(panel: PairedPanel, bi: BlockIndex, draws: np.ndarray) -> tuple[np.ndarray, float]:
    """逐行 reference 重估：按块实例复制机会行（副本簇独立身份，重建 n_c* 与 w*），返回 (θ*, D*)。"""
    rows_d, rows_m, rows_c = [], [], []
    for copy_id, b in enumerate(draws.tolist()):
        idx = np.where(bi.block_of_row == b)[0]
        for i in idx:
            rows_d.append(panel.diffs[i]); rows_m.append(panel.mask[i]); rows_c.append((copy_id, panel.cluster_ids[i]))
    if not rows_d:
        return np.full(panel.n_candidates, np.nan), 0.0
    n_c: dict = {}
    for c in rows_c:
        n_c[c] = n_c.get(c, 0) + 1
    w = np.array([1.0 / n_c[c] for c in rows_c])
    m = np.array(rows_m, dtype=float)
    D = float((m * w).sum())
    A = (np.array(rows_d) * (m * w)[:, None]).sum(axis=0)
    return (A / D if D > 0 else np.full(panel.n_candidates, np.nan)), D


def theta_and_jackknife_se(S: np.ndarray, W: np.ndarray, instances: np.ndarray) -> tuple[np.ndarray, np.ndarray, bool]:
    """给定块实例（索引数组，可重复），返回 (θ, se_jackknife, ok)。instances 形状 (Q,) 或 (B, Q)。"""
    inst = np.atleast_2d(instances)
    Si, Wi = S[inst], W[inst]                       # (B, Q, C), (B, Q)
    A = Si.sum(axis=1)                              # (B, C)
    D = Wi.sum(axis=1)                              # (B,)
    ok = np.all(D > 0)
    theta = A / np.where(D > 0, D, np.nan)[:, None]
    Dl = D[:, None] - Wi                            # (B, Q)
    ok = ok and bool(np.all(Dl > 0))
    th_l = (A[:, None, :] - Si) / np.where(Dl > 0, Dl, np.nan)[:, :, None]   # (B, Q, C)
    Q = inst.shape[1]
    se = np.sqrt((Q - 1) / Q * ((th_l - th_l.mean(axis=1, keepdims=True)) ** 2).sum(axis=1))
    if instances.ndim == 1:
        return theta[0], se[0], ok
    return theta, se, ok


@dataclass(frozen=True)
class MaxTResult:
    status: str                                   # ok | insufficient | contract_blocked
    reason: str | None
    candidate_ids: tuple[str, ...]
    theta: np.ndarray | None = None
    se: np.ndarray | None = None
    t: np.ndarray | None = None
    p_adj: np.ndarray | None = None
    lower_bound: np.ndarray | None = None
    q_crit: float | None = None
    B: int = 0
    alpha: float = 0.05
    n_blocks: int = 0
    n_nonempty_blocks: int = 0
    block_len_days: int = 0
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        f = lambda a: None if a is None else [None if (isinstance(x, float) and not math.isfinite(x)) else float(x) for x in np.atleast_1d(a)]   # noqa: E731
        return {"status": self.status, "reason": self.reason, "candidate_ids": list(self.candidate_ids), "theta": f(self.theta), "se": f(self.se),
                "t": f(self.t), "p_adj": f(self.p_adj), "lower_bound": f(self.lower_bound), "q_crit": self.q_crit, "B": self.B, "alpha": self.alpha,
                "n_blocks": self.n_blocks, "n_nonempty_blocks": self.n_nonempty_blocks, "block_len_days": self.block_len_days, "diagnostics": self.diagnostics}


def max_t_panel(panel: PairedPanel, *, block_len_days: int, B: int = 2000, seed: int = 0, alpha: float = 0.05,
                origin: dt.datetime | None = None, min_nonempty: int = MIN_NONEMPTY_BLOCKS,
                floor: "SufficiencyFloor | None" = None) -> MaxTResult:
    ids = panel.candidate_ids
    if panel.n_candidates == 0:
        return MaxTResult("insufficient", "NO_CANDIDATES", ids)
    bi = calendar_blocks(panel, block_len_days=block_len_days, origin=origin)
    diag = {"cluster_span_frac": bi.cluster_span_frac, "max_cluster_span_days": bi.max_cluster_span_days, "n_evaluable": int(panel.mask.sum())}
    base = dict(candidate_ids=ids, B=B, alpha=alpha, n_blocks=bi.n_blocks, n_nonempty_blocks=bi.n_nonempty, block_len_days=block_len_days, diagnostics=diag)
    if floor is not None:                        # B20：成对声明的下限（块长 + 块数 + cross + max_span 一起判）
        why = floor.check(bi)
        diag["sufficiency_floor"] = {"block_len_days": floor.block_len_days, "min_nonempty": floor.min_nonempty,
                                     "max_cluster_span_frac": floor.max_cluster_span_frac, "max_span_days": floor.span_cap_days,
                                     "required_calendar_days": floor.required_calendar_days(), "reason": why}
        if why:
            return MaxTResult("insufficient", why, **base)
    elif bi.n_nonempty < min_nonempty:
        return MaxTResult("insufficient", f"NONEMPTY_BLOCKS {bi.n_nonempty} < {min_nonempty}（未声明 SufficiencyFloor：块长未成对，见 B20 §3.2）", **base)
    S, W = block_sums(panel, bi)
    Q = bi.n_blocks
    theta, se, ok = theta_and_jackknife_se(S, W, np.arange(Q))
    if not ok or not np.all(np.isfinite(theta)) or not np.all(np.isfinite(se)):
        return MaxTResult("insufficient", "ZERO_DENOMINATOR_OR_NONFINITE", theta=theta, se=se, **base)
    floor = SE_FLOOR_REL * np.maximum(1.0, np.abs(theta))
    if np.any(se <= floor):
        return MaxTResult("insufficient", "UNSTABLE_SE", theta=theta, se=se, **base)
    k = math.ceil((1 - alpha) * (B + 1))
    if k > B:
        return MaxTResult("insufficient", f"K {k} > B {B}", theta=theta, se=se, **base)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, Q, size=(B, Q))                        # 共同索引矩阵 I[b, q]，空块可被抽到且占位
    th_b, se_b, ok_b = theta_and_jackknife_se(S, W, draws)
    if not ok_b or not np.all(np.isfinite(th_b)) or not np.all(np.isfinite(se_b)) or np.any(se_b <= 0):
        return MaxTResult("insufficient", "RESAMPLE_ZERO_DENOMINATOR_OR_SE", theta=theta, se=se, **base)
    t = theta / se
    T = ((th_b - theta[None, :]) / se_b).max(axis=1)              # (B,)
    p_adj = (1 + (T[:, None] >= t[None, :]).sum(axis=0)) / (B + 1)
    q = float(np.sort(T)[k - 1])
    lb = theta - q * se
    diag["T_quantiles"] = {"p50": float(np.quantile(T, 0.5)), "p95": float(np.quantile(T, 0.95))}
    return MaxTResult("ok", None, theta=theta, se=se, t=t, p_adj=p_adj, lower_bound=lb, q_crit=q, **base)


def max_t_bootstrap(thetas, ses, *, block_len_days: int, B: int = 2000, seed: int = 0, panel: PairedPanel | None = None,
                    alpha: float = 0.05, **kw) -> MaxTResult:
    """契约 §5 公共签名。无 panel → contract_blocked（ADR C03：仅均值与 SE 不能恢复相关与重采样权重）；
    有 panel 时 thetas/ses 作校验值（与面板重估相差 > 1e-9 记入 diagnostics.check_mismatch）。"""
    if panel is None:
        return MaxTResult("contract_blocked", "C03: max_t_bootstrap 需要 PairedPanel（逐机会配对差 + 簇 + 日历）", tuple())
    res = max_t_panel(panel, block_len_days=block_len_days, B=B, seed=seed, alpha=alpha, **kw)
    for name, given, mine in (("thetas", thetas, res.theta), ("ses", ses, res.se)):
        if given is None or mine is None:
            continue
        g = np.asarray(given, dtype=float)
        if g.shape != mine.shape:
            res.diagnostics[f"check_mismatch_{name}"] = {"reason": "SHAPE", "given_shape": list(g.shape), "reestimated_shape": list(mine.shape)}
        elif not np.allclose(g, mine, atol=1e-9, rtol=1e-6):
            res.diagnostics[f"check_mismatch_{name}"] = {"given": g.tolist(), "reestimated": mine.tolist()}
    if any(k.startswith("check_mismatch") for k in res.diagnostics):
        res.diagnostics["check_mismatch"] = True
    return res


__all__ = ["MIN_NONEMPTY_BLOCKS", "BlockIndex", "SufficiencyFloor", "MaxTResult", "PairedPanel", "block_sums", "calendar_blocks", "max_t_bootstrap",
           "max_t_panel", "reestimate_rows", "theta_and_jackknife_se"]
