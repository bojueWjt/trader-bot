"""分档（契约 feature-snapshot §5；ADR-G3 §11；合并稿 E.1）。

K = min_fold N_cluster_fold（全部实际内层训练折 purge 后可用簇），不能用全样本/外测试簇数或均值替代；
DEFF = max(1, Var_共同块(θ) / Var_独立簇(θ))，两者同机会权重、同 mask（分别用块 jackknife 与簇 jackknife SE²）；
K_eff = min_fold floor(K_fold / DEFF_fold) 只作降档诊断；p ≤ min(档硬顶, floor(K/20), floor(K_eff/20))；只降不升。
未批 G-STAT-CLAIM 时所有档 claim_status = descriptive_only。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from quant_lab.research.maxt import PairedPanel, block_sums, calendar_blocks, theta_and_jackknife_se

#: (档名, K 下限, K 上限, p 硬顶, 全协议唯一配置上限, 额外条件)
TIERS = (
    ("T0", 0, 79, 0, 0, {"min_months": 0, "min_outer_folds": 0, "min_fold_clusters": 0}),
    ("T1", 80, 199, 4, 12, {"min_months": 6, "min_outer_folds": 1, "min_fold_clusters": 20}),
    ("T2", 200, 499, 8, 64, {"min_months": 12, "min_outer_folds": 3, "min_fold_clusters": 30}),
    ("T3a", 500, 1499, 16, 512, {"min_months": 18, "min_outer_folds": 3, "min_fold_clusters": 50}),
    ("T3b", 1500, 10**9, 32, 2048, {"min_months": 24, "min_outer_folds": 3, "min_fold_clusters": 100}),
)


@dataclass(frozen=True)
class TierReport:
    K: int
    K_by_fold: dict
    tier_by_K: str
    tier: str                      # 降档后
    p_cap: int
    config_cap: int
    p_allowed: int
    DEFF_by_fold: dict
    K_eff: int | None
    downgrade_reasons: list[str] = field(default_factory=list)
    claim_status: str = "descriptive_only"
    allowed_search: str = "fixed_policy"

    def to_dict(self) -> dict:
        return {"K": self.K, "K_by_fold": self.K_by_fold, "tier_by_K": self.tier_by_K, "tier": self.tier, "p_cap": self.p_cap,
                "config_cap": self.config_cap, "p_allowed": self.p_allowed, "DEFF_by_fold": self.DEFF_by_fold, "K_eff": self.K_eff,
                "downgrade_reasons": self.downgrade_reasons, "claim_status": self.claim_status, "allowed_search": self.allowed_search}


def tier_of(K: int) -> tuple:
    for t in TIERS:
        if t[1] <= K <= t[2]:
            return t
    return TIERS[0]


def deff_from_panel(panel: PairedPanel, *, block_len_days: int) -> float | None:
    """DEFF = max(1, se_block² / se_cluster²)；分母方差 0/不稳定 → None（undefined，不以 1 替代）。多候选取第一列。"""
    if panel.n_candidates == 0 or int(panel.mask.sum()) < 3:
        return None
    bi = calendar_blocks(panel, block_len_days=block_len_days)
    S, W = block_sums(panel, bi)
    _, se_b, ok_b = theta_and_jackknife_se(S, W, np.arange(bi.n_blocks))
    # 独立簇：每簇一块
    cl = {c: k for k, c in enumerate(dict.fromkeys(panel.cluster_ids))}
    idx = np.array([cl[c] for c in panel.cluster_ids])
    mw = panel.mask.astype(float) * panel.weights
    Sc = np.zeros((len(cl), panel.n_candidates)); Wc = np.zeros(len(cl))
    np.add.at(Sc, idx, panel.diffs * mw[:, None]); np.add.at(Wc, idx, mw)
    _, se_c, ok_c = theta_and_jackknife_se(Sc, Wc, np.arange(len(cl)))
    if not (ok_b and ok_c) or not np.isfinite(se_b[0]) or not np.isfinite(se_c[0]) or se_c[0] <= 1e-12:
        return None
    return float(max(1.0, (se_b[0] ** 2) / (se_c[0] ** 2)))


def tier_report(K_by_fold: dict[str, int], *, DEFF_by_fold: dict[str, float | None] | None = None, span_months: float = 0.0,
                n_outer_folds: int = 0, min_fold_test_clusters: int = 0, claim_gate_approved: bool = False) -> TierReport:
    if not K_by_fold:
        return TierReport(0, {}, "T0", "T0", 0, 0, 0, {}, None, ["NO_FOLDS"], "descriptive_only", "fixed_policy")
    K = min(K_by_fold.values())
    t = tier_of(K)
    reasons = []
    DEFF_by_fold = DEFF_by_fold or {}
    k_eff_vals = []
    for f, k in K_by_fold.items():
        d = DEFF_by_fold.get(f)
        if d is None:
            reasons.append(f"DEFF_UNDEFINED:{f}")
        else:
            k_eff_vals.append(math.floor(k / d))
    K_eff = min(k_eff_vals) if k_eff_vals and len(k_eff_vals) == len(K_by_fold) else None
    # 只降不升：按 K_eff、跨度、外折数、每折簇数逐级降档
    cur = t
    if K_eff is None:                       # DEFF undefined：fail closed → T0（零搜索），不以 1 替代
        reasons.append("DEFF_UNDEFINED → T0 (fail closed)")
        cur = TIERS[0]
    if K_eff is not None and K_eff < cur[1]:
        reasons.append(f"K_EFF {K_eff} < {cur[1]}")
        cur = tier_of(K_eff)
    while cur[0] != "T0":
        need = cur[5]
        bad = []
        if span_months < need["min_months"]:
            bad.append(f"SPAN {span_months:.1f}m < {need['min_months']}m")
        if n_outer_folds < need["min_outer_folds"]:
            bad.append(f"OUTER_FOLDS {n_outer_folds} < {need['min_outer_folds']}")
        if min_fold_test_clusters < need["min_fold_clusters"]:
            bad.append(f"FOLD_CLUSTERS {min_fold_test_clusters} < {need['min_fold_clusters']}")
        if not bad:
            break
        reasons.append(f"{cur[0]}: " + "; ".join(bad))
        cur = TIERS[[x[0] for x in TIERS].index(cur[0]) - 1]
    p_allowed = min(cur[3], K // 20, (K_eff // 20) if K_eff is not None else K // 20)
    search = {"T0": "fixed_policy", "T1": "prelisted<=12", "T2": "bounded_enumeration<=64", "T3a": "enumeration<=512 (GP gated)", "T3b": "enumeration<=2048 (GP gated)"}[cur[0]]
    return TierReport(K, dict(K_by_fold), t[0], cur[0], cur[3], cur[4], max(0, p_allowed), dict(DEFF_by_fold), K_eff, reasons,
                      "claimable_if_all_gates_pass" if claim_gate_approved else "descriptive_only", search)


__all__ = ["TIERS", "TierReport", "deff_from_panel", "tier_of", "tier_report"]
