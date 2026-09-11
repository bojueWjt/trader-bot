"""G3 对外签名与协议编排（契约 feature-snapshot §6；ADR-G3 §1/§7/§9.3/§13）。

run_protocol(config_path, out_dir=None) -> ProtocolReport：冻结 YAML 配置 → 合成/湖数据 → 机会集 → 折 → 分档/预算 → 账本预留 →
特征快照 → 两臂配对 → search(阈值拟合) / selection(max-t 一次) / outer(折外一次预测) / final(全时间共同块 θ、LB) → 报告文件。
enumerate_grammar 见 grammar.py。

铁律：rule 只能是代码内注册的 RuleSpec（配置只引用 rule_id/参数）；账本每次评估前 reserve；未批 G-STAT-CLAIM 时
claim_status=descriptive_only（LB>0 也只描述）；未批 G-LICENSE-SEARCH 只做封顶枚举；湖路径经 QUANT_LAB_DATA_ROOT。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import resource
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import polars as pl

from quant_lab.research import paths
from quant_lab.research.ast import canonical_hash, lint
from quant_lab.research.backends import get_backend
from quant_lab.research.evaluator import NAN_RATE_MAX, freeze_opportunity_set, pair_arms
from quant_lab.research.features import SnapshotContext, feature_snapshot
from quant_lab.research.grammar import enumerate_grammar
from quant_lab.research.ledger import Ledger, LedgerBudgetExhausted, MemoryLedger
from quant_lab.research.maxt import PairedPanel, max_t_panel
from quant_lab.research.protocol import Fold, purge_train, walk_forward
from quant_lab.research.tiers import deff_from_panel, tier_report

UTC_US = pl.Datetime("us", "UTC")
GATES = {"G-STAT-CLAIM": "pending", "G-LICENSE-SEARCH": "pending"}


# ---------------------------------------------------------------- 规则（代码内注册）
@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    kind: str            # gt_q | lt_q | gt_c | lt_c（q：训练 search 子窗分位；c：常量）
    param: float
    version: str = "0.1"

    def fit(self, x_search: np.ndarray) -> float:
        """阈值只在内层 search 子窗拟合（不看 selection / 外测试）。"""
        v = x_search[np.isfinite(x_search)]
        if self.kind in ("gt_q", "lt_q"):
            return float(np.quantile(v, self.param)) if v.size else math.nan
        return float(self.param)

    def apply(self, x: np.ndarray, thr: float) -> np.ndarray:
        """take 布尔；invalid（NaN）→ False（skip）。"""
        ok = np.isfinite(x) & np.isfinite(thr)
        if self.kind.startswith("gt"):
            return ok & (x > thr)
        return ok & (x < thr)

    @property
    def rule_hash(self) -> str:
        return hashlib.sha256(f"{self.rule_id}|{self.kind}|{self.param}|{self.version}".encode()).hexdigest()[:16]


RULE_KINDS = ("gt_q", "lt_q", "gt_c", "lt_c")


def make_rule(d: dict) -> RuleSpec:
    if d.get("kind") not in RULE_KINDS:
        raise ValueError(f"未注册的规则类型 {d.get('kind')!r}，可选 {RULE_KINDS}")
    return RuleSpec(str(d["rule_id"]), d["kind"], float(d["param"]))


@dataclass(frozen=True)
class Candidate:
    cand_id: str
    feature_key: str            # PanelInputs.features 的键（AST 候选 = canonical_hash）
    rule: RuleSpec
    ast: dict | None = None
    canonical_hash: str | None = None


# ---------------------------------------------------------------- 面板输入（run_protocol 与空模型 MC 共用）
@dataclass
class PanelInputs:
    anchors: pl.DataFrame                  # episode_id, cluster_id, t_dec, t1?
    base_R: np.ndarray                     # 基线臂 net_R（NaN = 删失/不可评）
    censored: np.ndarray                   # 两臂排除并集，共同 mask = ~censored
    weights: np.ndarray                    # 簇均权 w_i（冻结）
    features: dict[str, np.ndarray]        # feature_key → 值（NaN = invalid）
    cand_R: np.ndarray | None = None       # 候选臂 net_R（政策变体；None = 纯过滤候选，同基线）
    exclusion_kind: np.ndarray | None = None   # 每机会 None | "censored"（右删失）| "coverage"（unevaluable，A15），与 censored 一致
    data_manifest: str = "synthetic"
    graph_version: str = "gv-synth"

    base_censored: np.ndarray | None = None
    cand_censored: np.ndarray | None = None
    protocol_hash: str = ""
    policy_version: str = "panel-supplied"
    policy_hash: str = ""
    caller_horizon_end: object | None = None   # §5.13 B11：调用方自选观察终点（horizon_source=="caller" 时非空），进尝试配置身份
    backend_version: str | None = None         # R4-L：实际计算后端身份（get_backend(name).version），进账本血缘
    shock_grid: object | None = None           # 空模型生成时实际使用的 (day × instrument) 冲击格点；结构诊断在此测量（不进流水线）

    @property
    def n(self) -> int:
        return self.anchors.height

    def ids(self) -> list[str]:
        return self.anchors["episode_id"].to_list()

    def validate(self) -> None:
        """入口不变量（S12）：逐臂验证删失与非有限；两臂并集须等于 censored；权重正有限。"""
        n = self.n
        for name, arr in (("base_R", self.base_R), ("censored", self.censored), ("weights", self.weights)):
            if len(arr) != n:
                raise PanelInvalid(f"{name} 长度 {len(arr)} != anchors {n}")
        cens = np.asarray(self.censored, dtype=bool)
        arms = (("base_R", self.base_R, self.base_censored),
                ("cand_R", self.base_R if self.cand_R is None else self.cand_R, self.cand_censored))
        masks = []
        for name, values, declared in arms:
            values = np.asarray(values, dtype=float)
            if values.shape != (n,):
                raise PanelInvalid(f"{name} 长度/维度不符")
            missing = ~np.isfinite(values)
            if declared is not None:
                declared = np.asarray(declared, dtype=bool)
                if declared.shape != (n,) or np.any(declared != missing):
                    raise PanelInvalid(f"{name}: 删失须 null，未删失却非有限拒收")
            masks.append(missing)
        common = masks[0] | masks[1]
        if np.any(~cens & common):
            raise PanelInvalid("共同可评行未删失却有非有限收益")
        if np.any(cens & ~common):
            raise PanelInvalid("两臂均有限却标记删失（删失须 null）")
        w = np.asarray(self.weights, dtype=float)
        if np.any(~np.isfinite(w)) or np.any(w <= 0):
            raise PanelInvalid("weights 须为正有限")
        for k, v in self.features.items():
            if len(v) != n:
                raise PanelInvalid(f"feature {k} 长度 {len(v)} != {n}")
        for c in ("episode_id", "cluster_id", "t_dec"):
            if c not in self.anchors.columns or self.anchors[c].null_count():
                raise PanelInvalid(f"anchors.{c} 缺失或含 null")
        if self.anchors["episode_id"].n_unique() != n:
            raise PanelInvalid("episode_id 重复")


class PanelInvalid(ValueError):
    """PanelInputs 不变量失败（证据与数值不一致）。"""


def candidate_diffs(inputs: PanelInputs, cand: Candidate, thr: float, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    """给定行索引 idx：返回 (d_i, take_i, n_invalid)。d = R_cand·take − R_base；invalid 特征 → skip（R_cand=0）；删失行 d=NaN（由 mask 排除）。"""
    x = inputs.features[cand.feature_key][idx]
    take = cand.rule.apply(x, thr)
    n_invalid = int((~np.isfinite(x)).sum())
    base = inputs.base_R[idx]
    cr = base if inputs.cand_R is None else inputs.cand_R[idx]
    d = np.where(take, cr, 0.0) - base
    return np.where(inputs.censored[idx], np.nan, d), take, n_invalid


# ---------------------------------------------------------------- 流水线
@dataclass
class PipelineConfig:
    scheme: str = "expanding"
    test_span_days: int = 60
    min_train_span_days: int = 180
    embargo_days: float = 1.0
    label_maturity_days: float = 0.0
    min_train_clusters: int = 20
    search_frac: float = 0.5               # 训练窗内 search 子窗占比（其余为 selection 子窗）
    block_len_days: int | None = 3         # None → 用首折训练窗残差诊断预注册（diagnose_block_len）
    block_len_sensitivity: tuple[int, ...] = (1, 3, 7)
    B: int = 2000
    alpha: float = 0.05
    config_cap: int | None = None          # None → 按档
    seed: int = 0
    max_folds: int | None = None


def diagnose_block_len(t_dec: list, r: np.ndarray, *, train_end: dt.datetime, candidates: tuple[int, ...] = (1, 3, 7), ac_max: float = 0.10,
                       t1: list | None = None, maturity_days: float = 0.0) -> dict:
    """训练诊断预注册块长（ADR §9：主 L 仅由训练诊断预注册，不按 p 选块）：只用 train_end 前且**标签成熟**
    （t1 + maturity <= train_end；t1 未知不用）的基线残差，对每个候选 L 计算日历块均值的 lag-1 自相关，
    取最小的 |ac1| <= ac_max 的 L；都不满足取最大候选。不看任何候选结果。"""
    ts = np.array([t.timestamp() for t in t_dec]); mask = (ts < train_end.timestamp()) & np.isfinite(r)
    if t1 is not None:
        mat = np.array([(x is not None) and (x + dt.timedelta(days=maturity_days) <= train_end) for x in t1])
        mask = mask & mat
    if mask.sum() < 20:
        return {"chosen": max(candidates), "ac1": {}, "reason": "TOO_FEW_TRAIN_OBS"}
    e = r[mask] - r[mask].mean(); day = (ts[mask] - ts[mask].min()) // 86400
    out = {}
    for L in candidates:
        blk = (day // L).astype(int)
        bm = np.array([e[blk == b].mean() for b in np.unique(blk)])
        out[str(L)] = float(np.corrcoef(bm[:-1], bm[1:])[0, 1]) if bm.size > 3 else float("nan")
    chosen = next((L for L in candidates if np.isfinite(out[str(L)]) and abs(out[str(L)]) <= ac_max), max(candidates))
    return {"chosen": int(chosen), "ac1": out, "reason": f"smallest L with |ac1|<={ac_max}"}


def _close_family(ledger, aids, reason: str) -> None:
    """family 级收尾：把仍为 reserved/running 的尝试全部标 failed（同一异常原因）。"""
    for aid in list(aids):
        if ledger.status_of(aid) in ("reserved", "running"):
            ledger.mark(aid, "failed", reason=reason[:200])


def _backend_identity(name: str) -> str:
    """R4-L：账本里的后端身份必须能认出**是哪个后端**的哪一版，光有版本号分不开 polars / polars_ta。"""
    b = get_backend(name)
    return f"{b.name}/{b.version}"


def _reserve_generic(ledger, *, canonical_hash, params, objective, data_manifest, seed, graph_version, raw_input_hash=None, stage="search", fold_id="snapshot", visible_cutoff="none", recompute_of=None, caller_horizon_end=None, backend_version=None) -> str:
    """非候选阶段（执行批次 / 快照）的预留：预算豁免；重复 → recompute_of 关联原尝试（重算计计算调用）。"""
    kw = dict(origin="enumeration", canonical_hash=canonical_hash, params=params, fold_id=fold_id, visible_cutoff=visible_cutoff, objective=objective,
              data_manifest=data_manifest, seed=seed, stage=stage, raw_input_hash=raw_input_hash, graph_version=graph_version, budget_exempt=True,
              recompute_of=recompute_of, caller_horizon_end=caller_horizon_end, backend_version=backend_version)
    aid = ledger.reserve(**kw)
    if ledger.status_of(aid) == "duplicate":
        aid = ledger.reserve(**(kw | {"recompute_of": aid}))
    return aid


def _mark(ledger, aid: str, status: str, **kw) -> None:
    """reserve 可能直接落 duplicate/budget 终态：终态不再改写。"""
    if ledger.status_of(aid) in ("reserved", "running"):
        ledger.mark(aid, status, **kw)


def _rows_of(inputs: PanelInputs) -> dict[str, int]:
    return {e: i for i, e in enumerate(inputs.ids())}


def _placeholder(d: np.ndarray, m: np.ndarray, label: str) -> np.ndarray:
    """m=True 处必须有限（否则协议错误）；m=False 处写 0 只是存储占位（被 mask 排除，无经济含义）。"""
    if np.any(m & ~np.isfinite(d)):
        raise PanelInvalid(f"{label}: 共同可评行出现非有限配对差（{int((m & ~np.isfinite(d)).sum())} 行）")
    return np.where(m, d, 0.0)


def _panel(inputs: PanelInputs, idx: np.ndarray, cols: dict[str, np.ndarray], fold_id: str) -> PairedPanel:
    a = inputs.anchors[idx.tolist()]
    m = ~inputs.censored[idx]
    df = a.select("episode_id", "cluster_id", "t_dec").with_columns(
        pl.Series("weight", inputs.weights[idx]), pl.Series("m", m), pl.lit(fold_id).alias("fold_id"),
        *[pl.Series(k, _placeholder(v, m, k)) for k, v in cols.items()])
    return PairedPanel.from_frame(df, list(cols))


def _reserve(ledger, c: Candidate, *, fold_id: str, cutoff, stage: str, cfg: PipelineConfig, inputs: PanelInputs, objective: str = "theta") -> str:
    """账本预留：重复配置 → 以 recompute_of 关联原尝试再预留（计计算调用，不耗额度）。"""
    kw = dict(origin="enumeration" if c.ast else "human", canonical_hash=c.canonical_hash or c.feature_key,
              params={"rule": c.rule.rule_id, "param": c.rule.param}, fold_id=fold_id, visible_cutoff=cutoff, objective=objective,
              data_manifest=inputs.data_manifest, seed=cfg.seed, stage=stage, rule_hash=c.rule.rule_hash, graph_version=inputs.graph_version,
              caller_horizon_end=inputs.caller_horizon_end,          # §5.13 B11：caller 观察窗进配置身份
              backend_version=inputs.backend_version)                # R4-L：实际后端身份进账本血缘
    aid = ledger.reserve(**kw)
    if ledger.status_of(aid) == "duplicate":
        dup = aid
        aid = ledger.reserve(**kw, recompute_of=dup)
    return aid


class _Stage:
    """阶段生命周期：异常 → failed 终态；正常 → 调用方 finish()。"""

    def __init__(self, ledger, aid: str):
        self.ledger, self.aid, self.t0, self.done = ledger, aid, time.perf_counter(), False

    def finish(self, status: str = "completed", **kw):
        if not self.done:
            _mark(self.ledger, self.aid, status, cost={"wall_s": round(time.perf_counter() - self.t0, 4)}, **kw)
            self.done = True

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is not None and not self.done:
            _mark(self.ledger, self.aid, "failed", reason=f"{et.__name__}: {ev}"[:200])
            self.done = True
        return False


def inner_split(inputs: PanelInputs, f: Fold, rows: dict[str, int], cfg: PipelineConfig) -> dict:
    """S05：训练窗内按**固定日历**拆 search / selection：t_mid = train_start + (cutoff − train_start)·search_frac；
    search 候选须对 selection 机会做 purge（同簇/重复组、闭区间交集、embargo）并在 t_mid 时标签成熟。"""
    tr_rows = [(e, rows[e]) for e in f.train_ids]
    if not tr_rows:
        return {"search": np.array([], dtype=int), "selection": np.array([], dtype=int), "t_mid": f.cutoff, "purged": {}, "K_search": 0, "K_selection": 0}
    a = inputs.anchors
    t_start = f.train_start or min(a["t_dec"][i] for _, i in tr_rows)
    t_mid = t_start + (f.cutoff - t_start) * cfg.search_frac
    recs = []
    for e, i in tr_rows:
        recs.append({"episode_id": e, "cluster_id": a["cluster_id"][i], "duplicate_group_id": a["duplicate_group_id"][i] if "duplicate_group_id" in a.columns else e,
                     "t_dec": a["t_dec"][i], "t1": a["t1"][i] if "t1" in a.columns else None, "row": i})
    sel = [r for r in recs if r["t_dec"] >= t_mid]
    cands = [r for r in recs if r["t_dec"] < t_mid]
    keep, why = purge_train(cands, sel, embargo=dt.timedelta(days=cfg.embargo_days), label_maturity=dt.timedelta(days=cfg.label_maturity_days), fit_time=t_mid)
    keep_set = set(keep)
    s_idx = np.array([r["row"] for r in cands if r["episode_id"] in keep_set], dtype=int)
    sel_idx = np.array([r["row"] for r in sel], dtype=int)
    return {"search": s_idx, "selection": sel_idx, "t_mid": t_mid, "purged": why,
            "K_search": len({r["cluster_id"] for r in cands if r["episode_id"] in keep_set}), "K_selection": len({r["cluster_id"] for r in sel}),
            "n_search_candidates": len(cands), "purge_reasons": _count_reasons(why)}


def _count_reasons(why: dict) -> dict:
    out: dict[str, int] = {}
    for v in why.values():
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def _baseline_deff(inputs: PanelInputs, idx: np.ndarray, block_len_days: int) -> float | None:
    """计算前冻结的保守 DEFF：训练窗基线收益（不看候选）。"""
    if len(idx) < 3:
        return None
    m = ~inputs.censored[idx]
    if m.sum() < 3:
        return None
    return deff_from_panel(_panel(inputs, idx, {"base": np.where(m, inputs.base_R[idx], 0.0)}, "deff"), block_len_days=block_len_days)


def run_pipeline(inputs: PanelInputs, cands: list[Candidate], cfg: PipelineConfig, *, ledger=None) -> dict:
    """完整流程一次：折 → 内层拆分 → 分档/预算（计算前硬门）→ search/selection/outer（账本全生命周期）→ final。返回可 JSON 的报告字典。"""
    t_start = time.perf_counter()
    ledger = ledger if ledger is not None else MemoryLedger()
    if hasattr(ledger, "recover"):
        ledger.recover()
    inputs.validate()
    protocol_hash = inputs.protocol_hash or hashlib.sha256(json.dumps(cfg.__dict__, sort_keys=True, default=str).encode()).hexdigest()
    ledger.lineage.update(protocol_hash=protocol_hash,
                          opportunity_set_hash=hashlib.sha256(inputs.anchors.write_json().encode() + inputs.weights.tobytes() + inputs.censored.tobytes()).hexdigest(),
                          policy_version=inputs.policy_version,
                          policy_hash=inputs.policy_hash or hashlib.sha256(json.dumps([inputs.policy_version, inputs.data_manifest]).encode()).hexdigest())
    if len({c.cand_id for c in cands}) != len(cands):
        raise PanelInvalid("候选 cand_id 重复")
    if not (0 < cfg.search_frac < 1) or not (0 < cfg.alpha < 1) or cfg.B < 100 or cfg.test_span_days <= 0 or cfg.min_train_span_days <= 0:
        raise ValueError("PipelineConfig 非法：search_frac∈(0,1)、alpha∈(0,1)、B≥100、span>0")
    rows = _rows_of(inputs)
    folds: list[Fold] = walk_forward(
        inputs.anchors, scheme=cfg.scheme, min_train_clusters=cfg.min_train_clusters, embargo=dt.timedelta(days=cfg.embargo_days),
        label_maturity=dt.timedelta(days=cfg.label_maturity_days), test_span=dt.timedelta(days=cfg.test_span_days),
        min_train_span=dt.timedelta(days=cfg.min_train_span_days), max_folds=cfg.max_folds)
    report: dict = {"status": "ok", "n_folds": len(folds), "folds": [], "gates": dict(GATES), "claim_status": "descriptive_only"}
    if not folds:
        report.update(status="insufficient", reason="NO_FOLDS", theta=None, tier="T0", K=0, n_attempts=0, synthetic_claim_positive=False, final_status="insufficient")
        return report
    if cfg.block_len_days is None:
        diag = diagnose_block_len(inputs.anchors["t_dec"].to_list(), inputs.base_R, train_end=folds[0].cutoff, candidates=tuple(cfg.block_len_sensitivity),
                                  t1=inputs.anchors["t1"].to_list() if "t1" in inputs.anchors.columns else None, maturity_days=cfg.label_maturity_days)
        cfg = replace(cfg, block_len_days=diag["chosen"])
        report["block_len_diagnostic"] = diag
    # ---- 内层拆分 + 计算前硬门（S05/S06）
    inner = {f.fold_id: inner_split(inputs, f, rows, cfg) for f in folds}
    K_by_fold = {f.fold_id: min(inner[f.fold_id]["K_search"], inner[f.fold_id]["K_selection"]) for f in folds}

    def _deff_conservative(fid):
        vals = [_baseline_deff(inputs, inner[fid][k], cfg.block_len_days) for k in ("search", "selection")]   # 每个实际内层训练集各估一次
        return None if any(v is None for v in vals) else max(vals)

    DEFF_by_fold = {f.fold_id: _deff_conservative(f.fold_id) for f in folds}
    train_ts = [t for t in inputs.anchors["t_dec"].to_list() if t < folds[-1].cutoff]          # 跨度只按训练可见数据，不含外测试
    span_months = ((max(train_ts) - min(train_ts)).days / 30.4) if train_ts else 0.0
    tr_rep = tier_report(K_by_fold, DEFF_by_fold=DEFF_by_fold, span_months=span_months, n_outer_folds=len(folds), min_fold_test_clusters=min(f.n_test_clusters for f in folds))
    cap = tr_rep.config_cap if cfg.config_cap is None else min(int(cfg.config_cap), tr_rep.config_cap)   # 配置只能收紧
    if tr_rep.p_allowed < 1 or tr_rep.tier == "T0":
        cap = 0
    pool = sorted(cands, key=lambda c: (c.canonical_hash or c.feature_key, c.rule.rule_hash))[:cap] if cap > 0 else []
    if hasattr(ledger, "budget_configs") and ledger.budget_configs is None:
        ledger.budget_configs = max(cap, 0) or None
    report["candidate_pool"] = {"n_submitted": len(cands), "n_admitted": len(pool), "config_cap": cap, "tier_cap": tr_rep.config_cap,
                                "p_allowed": tr_rep.p_allowed, "pool_hash": hashlib.sha256("|".join(c.cand_id for c in pool).encode()).hexdigest()[:16]}
    oof_rows: list[dict] = []
    for f in folds:
        inn = inner[f.fold_id]
        te = np.array([rows[e] for e in f.test_ids], dtype=int)
        s_idx, sel_idx = inn["search"], inn["selection"]
        fr: dict = {"fold_id": f.fold_id, "cutoff": f.cutoff.isoformat(), "n_train": len(f.train_ids), "n_test": len(te), "K": K_by_fold[f.fold_id],
                    "K_outer_train": f.n_train_clusters, "DEFF": DEFF_by_fold[f.fold_id], "insufficient": f.insufficient, "loss": f.loss,
                    "inner": {"t_mid": inn["t_mid"].isoformat(), "n_search": int(len(s_idx)), "n_selection": int(len(sel_idx)), "K_search": inn["K_search"],
                              "K_selection": inn["K_selection"], "purge_reasons": inn.get("purge_reasons", {})},
                    "selected": None, "selection": None}
        gate_fail = None
        if f.insufficient or inn["K_search"] < cfg.min_train_clusters or inn["K_selection"] < cfg.min_train_clusters:
            gate_fail = f"FOLD_INSUFFICIENT K_search={inn['K_search']} K_selection={inn['K_selection']} < {cfg.min_train_clusters}"
        elif DEFF_by_fold[f.fold_id] is None:
            gate_fail = "DEFF_UNDEFINED"
        elif len(te) == 0 or not pool:
            gate_fail = "EMPTY_TEST_OR_POOL"
        if gate_fail:
            fr["selection"] = {"status": "insufficient", "reason": gate_fail}
            oof_rows += _oof(inputs, te, None, f.fold_id)
            report["folds"].append(fr)
            continue
        # search：阈值只在 search 子窗拟合（已 purge、已成熟）
        thr: dict[str, float] = {}
        cols: dict[str, np.ndarray] = {}
        rejected: dict[str, str] = {}
        for c in pool:
            try:
                aid = _reserve(ledger, c, fold_id=f.fold_id, cutoff=f.cutoff, stage="search", cfg=cfg, inputs=inputs)
            except LedgerBudgetExhausted:
                rejected[c.cand_id] = "BUDGET_EXHAUSTED"
                continue
            with _Stage(ledger, aid) as st:
                thr[c.cand_id] = c.rule.fit(inputs.features[c.feature_key][s_idx])
                st.finish(objective=thr[c.cand_id] if math.isfinite(thr[c.cand_id]) else None)
        # selection：nan 门 + 共同块 max-t 一次（family 级收尾：任一候选异常 → 其余未终态尝试一并 failed）
        fr["candidate_dependency_gate"] = {}
        sel_aids: dict[str, str] = {}
        try:
            for c in pool:
                if c.cand_id in rejected:
                    continue
                aid = _reserve(ledger, c, fold_id=f.fold_id, cutoff=f.cutoff, stage="selection", cfg=cfg, inputs=inputs)
                sel_aids[c.cand_id] = aid
                with _Stage(ledger, aid) as st:
                    d, take, n_inv = candidate_diffs(inputs, c, thr[c.cand_id], sel_idx)
                    nan_rate = n_inv / len(sel_idx) if len(sel_idx) else 1.0
                    if nan_rate > NAN_RATE_MAX or not math.isfinite(thr[c.cand_id]):
                        rejected[c.cand_id] = f"NAN_RATE {nan_rate:.3f}" if nan_rate > NAN_RATE_MAX else "THRESHOLD_UNDEFINED"
                        st.finish("rejected", reason=rejected[c.cand_id])
                        continue
                    search_d, _, _ = candidate_diffs(inputs, c, thr[c.cand_id], s_idx)
                    deps = [deff_from_panel(_panel(inputs, idx, {c.cand_id: values}, f.fold_id),
                                            block_len_days=cfg.block_len_days)
                            for idx, values in ((s_idx, search_d), (sel_idx, d))]
                    dep = None
                    if all(v is not None and math.isfinite(v) for v in deps):
                        dep = max(DEFF_by_fold[f.fold_id], *deps)
                    candidate_deffs = dict(DEFF_by_fold)
                    candidate_deffs[f.fold_id] = dep
                    target_tier = tier_report(K_by_fold, DEFF_by_fold=candidate_deffs, span_months=span_months,
                                              n_outer_folds=len(folds), min_fold_test_clusters=min(ff.n_test_clusters for ff in folds))
                    allowed = dep is not None and target_tier.tier != "T0" and target_tier.config_cap >= cap and target_tier.p_allowed >= 1
                    fr["candidate_dependency_gate"][c.cand_id] = {
                        "DEFF_search": deps[0], "DEFF_selection": deps[1], "DEFF": dep,
                        "tier": target_tier.tier, "config_cap": min(cap, target_tier.config_cap),
                        "threshold_dof": int(c.rule.kind in ("gt_q", "lt_q")), "admitted": allowed}
                    if not allowed:
                        rejected[c.cand_id] = "CANDIDATE_DEFF_UNDEFINED_OR_BUDGET_TIGHTENED"
                        st.finish("rejected", reason=rejected[c.cand_id])
                        continue
                    cols[c.cand_id] = d
        except Exception as e:
            _close_family(ledger, sel_aids.values(), f"{type(e).__name__}: {e}")
            raise
        sel = None
        if cols:
            try:
                panel = _panel(inputs, sel_idx, cols, f.fold_id)
                mt = max_t_panel(panel, block_len_days=cfg.block_len_days, B=cfg.B, seed=cfg.seed + 1000 + int(f.fold_id[-3:]), alpha=cfg.alpha)
            except Exception as e:
                _close_family(ledger, sel_aids.values(), f"{type(e).__name__}: {e}")
                raise
            fr["selection"] = {k: v for k, v in mt.to_dict().items() if k != "diagnostics"} | {"rejected": rejected}
            for j, cid in enumerate(mt.candidate_ids):
                _mark(ledger, sel_aids[cid], "completed" if mt.status == "ok" else "insufficient",
                      objective=float(mt.theta[j]) if mt.theta is not None else None, reason=None if mt.status == "ok" else mt.reason,
                      result_hash=hashlib.sha256(json.dumps(mt.to_dict(), sort_keys=True, default=str).encode()).hexdigest())
            if mt.status == "ok":
                win = [(float(mt.p_adj[j]), cid) for j, cid in enumerate(mt.candidate_ids) if mt.p_adj[j] <= cfg.alpha and mt.lower_bound[j] > 0]
                if win:
                    sel = sorted(win)[0][1]
        else:
            fr["selection"] = {"status": "insufficient", "reason": "NO_ADMISSIBLE_CANDIDATE", "rejected": rejected}
        fr["selected"] = sel
        # outer：折外每机会一次预测；候选特征 NaN 率超门 → 记录并退回 baseline（不重新选模）
        cand = next((c for c in pool if c.cand_id == sel), None)
        if cand is not None:
            aid = _reserve(ledger, cand, fold_id=f.fold_id, cutoff=f.cutoff, stage="outer", cfg=cfg, inputs=inputs)
            with _Stage(ledger, aid) as st:
                x = inputs.features[cand.feature_key][te]
                outer_nan = float((~np.isfinite(x)).mean()) if len(te) else 0.0
                fr["outer_nan_rate"] = outer_nan
                if outer_nan > NAN_RATE_MAX:
                    fr["outer_fallback"] = f"OUTER_NAN_RATE {outer_nan:.3f} > {NAN_RATE_MAX}"
                    oof_rows += _oof(inputs, te, None, f.fold_id)
                    fr["selected"] = None
                    st.finish("rejected", reason=fr["outer_fallback"])
                else:
                    oof_rows += _oof(inputs, te, (cand, thr[sel]), f.fold_id)
                    st.finish(result_hash=hashlib.sha256(json.dumps(oof_rows, sort_keys=True, default=str).encode()).hexdigest())
        else:
            oof_rows += _oof(inputs, te, None, f.fold_id)
        report["folds"].append(fr)
    # final：全时间共同块一个 θ / LB（单候选 family = 选定流程），有账
    n_selected = sum(1 for x in report["folds"] if x["selected"])
    final = {}
    faid = ledger.reserve(origin="enumeration", canonical_hash="pipeline", params={"pool_hash": report["candidate_pool"]["pool_hash"]}, fold_id="final",
                          visible_cutoff=folds[-1].test_end, objective="theta_final", data_manifest=inputs.data_manifest, seed=cfg.seed, stage="final",
                          graph_version=inputs.graph_version, budget_exempt=True, caller_horizon_end=inputs.caller_horizon_end,
                          backend_version=inputs.backend_version)
    if ledger.status_of(faid) == "duplicate":
        faid = ledger.reserve(origin="enumeration", canonical_hash="pipeline", params={"pool_hash": report["candidate_pool"]["pool_hash"]}, fold_id="final",
                              visible_cutoff=folds[-1].test_end, objective="theta_final", data_manifest=inputs.data_manifest, seed=cfg.seed, stage="final",
                              graph_version=inputs.graph_version, recompute_of=faid, budget_exempt=True, caller_horizon_end=inputs.caller_horizon_end,
                              backend_version=inputs.backend_version)
    with _Stage(ledger, faid) as st:
        oof = pl.DataFrame(oof_rows, schema={"episode_id": pl.Utf8, "cluster_id": pl.Utf8, "t_dec": UTC_US, "weight": pl.Float64, "m": pl.Boolean,
                                             "pipeline": pl.Float64, "fold_id": pl.Utf8, "take": pl.Boolean})
        final_panel = PairedPanel.from_frame(oof, ["pipeline"])          # 面板构造在 final 生命周期内（异常 → failed）
        if n_selected == 0:
            for L in sorted(set(cfg.block_len_sensitivity) | {cfg.block_len_days}):
                final[str(L)] = {"status": "no_claim", "reason": "NO_CANDIDATE_SELECTED", "theta": [0.0], "se": None, "p_adj": None, "lower_bound": None}
        else:
            for L in sorted(set(cfg.block_len_sensitivity) | {cfg.block_len_days}):
                final[str(L)] = max_t_panel(final_panel, block_len_days=L, B=cfg.B, seed=cfg.seed + 7, alpha=cfg.alpha).to_dict()
        main = final[str(cfg.block_len_days)]
        st.finish("completed" if main["status"] in ("ok", "no_claim") else "insufficient", objective=(main["theta"] or [None])[0], reason=main.get("reason"),
                  result_hash=hashlib.sha256(json.dumps(final, sort_keys=True, default=str).encode()).hexdigest())
    led_df = ledger.read()
    if led_df.height and "run_id" in led_df.columns:
        led_df = led_df.filter(pl.col("run_id") == ledger.run_id)          # 只计本 run 的尝试
    report.update({
        "tier": tr_rep.tier, "tier_report": tr_rep.to_dict(), "K": tr_rep.K,
        "theta": (main["theta"] or [None])[0], "se": (main["se"] or [None])[0], "p_adj": (main["p_adj"] or [None])[0],
        "lower_bound": (main["lower_bound"] or [None])[0], "final_status": main["status"], "final_reason": main["reason"],
        "final_by_block_len": final, "block_len_days": cfg.block_len_days, "B": cfg.B, "alpha": cfg.alpha,
        "synthetic_claim_positive": bool(main["status"] == "ok" and main["lower_bound"] and main["lower_bound"][0] > 0),
        "n_attempts": int(led_df.height) if led_df.height else 0,
        "n_attempts_by_status": (led_df.group_by("status").len().sort("status").to_dict(as_series=False) if led_df.height else {}),
        "n_selected_folds": n_selected,
        "oof": {"n": oof.height, "n_evaluable": int(oof["m"].sum()), "n_excluded": int((~oof["m"]).sum()), "n_take": int(oof["take"].sum()),
                **_oof_exclusion_breakdown(inputs, oof)},
        "resources": {"wall_s": round(time.perf_counter() - t_start, 3), "peak_rss_gib": _rss()},
    })
    if main["status"] not in ("ok", "no_claim"):
        report["status"] = "insufficient"
    return report


def _oof_exclusion_breakdown(inputs: PanelInputs, oof: pl.DataFrame) -> dict:
    """OOF 排除按 A15 互斥拆分：右删失 vs 覆盖失败（PanelInputs.exclusion_kind 缺失时全部计为 censored）。"""
    if inputs.exclusion_kind is None:
        return {"n_censored_excluded": int((~oof["m"]).sum()), "n_coverage_excluded": 0}
    rows = _rows_of(inputs)
    ex = [inputs.exclusion_kind[rows[e]] for e, m in zip(oof["episode_id"].to_list(), oof["m"].to_list()) if not m]
    return {"n_censored_excluded": sum(1 for k in ex if k == "censored"), "n_coverage_excluded": sum(1 for k in ex if k == "coverage")}


def _oof(inputs: PanelInputs, te: np.ndarray, sel: tuple[Candidate, float] | None, fold_id: str) -> list[dict]:
    a = inputs.anchors[te.tolist()]
    if sel is None:
        d, take = np.zeros(len(te)), np.ones(len(te), dtype=bool)
    else:
        d, take, _ = candidate_diffs(inputs, sel[0], sel[1], te)
    m = ~inputs.censored[te]
    d = _placeholder(np.asarray(d, dtype=float), m, "outer")
    return [{"episode_id": e, "cluster_id": c, "t_dec": t, "weight": float(inputs.weights[i]), "m": bool(m[k]),
             "pipeline": float(d[k]), "fold_id": fold_id, "take": bool(take[k])}
            for k, (i, e, c, t) in enumerate(zip(te.tolist(), a["episode_id"], a["cluster_id"], a["t_dec"]))]


def _rss() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1024 ** 3) if sys.platform == "darwin" else r / (1024 ** 2), 3)


# ---------------------------------------------------------------- run_protocol（冻结配置 → 报告）
def load_config(config_path: str) -> dict:
    import yaml
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("配置须为 YAML 对象")
    return cfg


def build_inputs_from_synthetic(cfg: dict, *, ledger=None) -> tuple[PanelInputs, list[Candidate], dict]:
    """合成源：fake_bars / fake_episodes / fake_execution → 机会集 → feature_snapshot → 两臂配对 → PanelInputs。
    ledger 给定时：每个 AST 在 lint / 特征计算前先 reserve（S08），非法 AST 记 rejected 终态并中止。"""
    from quant_lab.research import synthetic
    from quant_lab.research.ast import ASTRejected
    s = cfg["data"]["synthetic"]
    seed = int(cfg.get("seed", 0))
    bars = synthetic.fake_bars(tuple(s.get("instruments", synthetic.DEFAULT_INSTRUMENTS)), n_bars=int(s["n_bars"]), interval=s.get("interval", "15m"), seed=seed,
                               gap_frac=float(s.get("gap_frac", 0.0)))
    eps = synthetic.fake_episodes(int(s["n_episodes"]), instrument_ids=tuple(s.get("instruments", synthetic.DEFAULT_INSTRUMENTS)), span_days=int(s["span_days"]),
                                  seed=seed, censor_frac=float(s.get("censor_frac", 0.05)), bars=bars, graph_version=s.get("graph_version", "gv-synth-0001"),
                                  cluster_window_h=int(s.get("cluster_window_h", 24)))
    ex = cfg.get("execution", {})
    opp = freeze_opportunity_set(eps)                      # 收益前冻结机会集；执行请求只对机会集内的 episode 发（ADR §6.1）
    ids = opp.episode_ids
    opp_diag = dict(opp.diagnostics)                       # A13：非请求键 null 只记诊断进 report
    e = eps.filter(pl.col("episode_id").is_in(ids)).sort("episode_id")
    gv = s.get("graph_version", "gv-synth-0001")
    if ledger is not None:
        ledger.lineage.update(protocol_hash=hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest(),
                              opportunity_set_hash=hashlib.sha256(opp.weights.write_json().encode()).hexdigest(),
                              policy_version=ex.get("policy_version", "policy-synth-base"),
                              policy_hash=synthetic._policy_identity(ex.get("policy_version", "policy-synth-base"))[0])
    exec_aid = None
    if ledger is not None:                                  # 收益访问前先落账（S08）：执行批次是一次"看标签"的统计暴露
        exec_aid = _reserve_generic(ledger, canonical_hash="execution_batch", params={"policy_version": ex.get("policy_version", "policy-synth-base"), "seed": seed, "n": len(ids)},
                                    objective="execution_batch", data_manifest=f"synthetic:{seed}", seed=seed, graph_version=gv,
                                    caller_horizon_end=(str(ex["horizon_end"]) if ex.get("horizon_end") else None))
    try:
        execution = synthetic.fake_execution(e, policy_version=ex.get("policy_version", "policy-synth-base"), seed=seed, none_frac=float(s.get("none_frac", 0.1)),
                                             noise_sd=float(s.get("noise_sd", 1.0)), cost_scenario=ex.get("cost_scenario", "base"),
                                             path_scenario=ex.get("path_scenario", "primary"), market_manifest=ex.get("market_manifest", "mm-synth-0001"),
                                             caller_horizon=bool(ex.get("horizon_end")))
    except Exception as ex_:
        if exec_aid is not None:
            _mark(ledger, exec_aid, "failed", reason=f"{type(ex_).__name__}: {ex_}"[:200])
        raise
    if exec_aid is not None:
        _mark(ledger, exec_aid, "completed", result_hash=hashlib.sha256(execution.write_json().encode()).hexdigest())
    pairs = pair_arms(execution, ids)
    x = e.join(pairs.select("episode_id", "base_R", "cand_R", "base_censor", "cand_censor", "base_kind", "cand_kind", "cand_close", "cand_open"), on="episode_id").join(opp.weights, on="episode_id")
    # t1（标签信息终点，S13）：已平仓 → position_close_at；未成交且无删失 → 入场到期时刻 t_dec + entry_ttl_s（收益已确定为 0）；
    # 删失/未平 → null（不猜 t_dec、不把删失伪造为到期 0）
    x = x.join(execution.select("episode_id", "entry_ttl_s", pl.col("fill_status").alias("__fill")), on="episode_id", how="left")
    x = x.with_columns(
        pl.when(pl.col("cand_close").is_not_null()).then(pl.col("cand_close"))
        .when((pl.col("__fill") == "none") & pl.col("base_censor").is_null() & pl.col("cand_censor").is_null())
        .then(pl.col("t_dec") + pl.duration(seconds=pl.col("entry_ttl_s")))
        .otherwise(None).alias("t1"))
    # 候选：预列 AST × 规则（或封顶枚举）
    cand_cfg = cfg["candidates"]
    if cand_cfg.get("mode", "prelisted") == "enumerate":
        g = cand_cfg["grammar"]
        asts = enumerate_grammar(int(g.get("depth", 2)), ops=list(g["ops"]), windows=list(g["windows"]), fields=list(g["fields"]), cap=int(g["cap"]))
    else:
        asts = list(cand_cfg["asts"])
    rules = [make_rule(r) for r in cand_cfg["rules"]]
    snap_aids = []
    first_attempt = {}
    anchors = x.select("episode_id", "instrument_id", "t_dec")
    ctx = SnapshotContext(market_manifest=ex.get("market_manifest", "mm-synth-0001"), graph_version=gv)
    try:
        for a in asts:
            raw_hash = hashlib.sha256(json.dumps(a, sort_keys=True, default=str).encode()).hexdigest()
            aid = None
            if ledger is not None:
                aid = _reserve_generic(ledger, canonical_hash=None, params={"stage": "snapshot", "raw_input_hash": raw_hash},
                                       objective="feature_snapshot", data_manifest=f"synthetic:{seed}", seed=seed,
                                       graph_version=gv, raw_input_hash=raw_hash, recompute_of=first_attempt.get(raw_hash),
                                       backend_version=_backend_identity(cfg.get("backend", "polars")))    # R4-L：算特征的后端身份进血缘
                snap_aids.append(aid)
                first_attempt.setdefault(raw_hash, aid)
            try:
                lint(a)
                if aid is not None:
                    ledger.mark(aid, "running", canonical_hash=canonical_hash(a))
            except ASTRejected as exc:
                if aid is not None:
                    _mark(ledger, aid, "rejected", reason=f"ASTRejected {exc.code}")
                raise
        snap = feature_snapshot(asts, anchors, bars=bars, backend=cfg.get("backend", "polars"), ctx=ctx)
        for aid in snap_aids:
            _mark(ledger, aid, "completed", result_hash=hashlib.sha256(snap.write_json().encode()).hexdigest())
    except Exception as exc:
        if ledger is not None:
            _close_family(ledger, snap_aids, f"{type(exc).__name__}: {exc}")
        raise
    feats: dict[str, np.ndarray] = {}
    cands: list[Candidate] = []
    for a in asts:
        h = canonical_hash(a)
        v = snap[f"f_{h}"].cast(pl.Float64).to_numpy().astype(float)
        v = np.where(snap[f"validity_{h}"].to_numpy(), v, np.nan)
        feats[h] = v
        for r in rules:
            cands.append(Candidate(f"{h[:12]}:{r.rule_id}", h, r, ast=a, canonical_hash=h))
    base = x["base_R"].to_numpy().astype(float)
    from quant_lab.research.evaluator import exclusion_kind as _ek
    kinds = np.array([(_ek(bc, bk) or _ek(cc_, ck)) or "" for bc, bk, cc_, ck in zip(x["base_censor"].to_list(), x["base_kind"].to_list(), x["cand_censor"].to_list(), x["cand_kind"].to_list())], dtype=object)
    cens = kinds != ""
    inputs = PanelInputs(x.select("episode_id", "cluster_id", "t_dec", "t1"), base, cens, x["weight"].to_numpy().astype(float), feats,
                         cand_R=x["cand_R"].to_numpy().astype(float),
                         base_censored=x["base_censor"].is_not_null().to_numpy(), cand_censored=x["cand_censor"].is_not_null().to_numpy(),
                         protocol_hash=hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest(),
                         policy_version=ex.get("policy_version", "policy-synth-base"),
                         policy_hash=synthetic._policy_identity(ex.get("policy_version", "policy-synth-base"))[0],
                         caller_horizon_end=(str(ex["horizon_end"]) if ex.get("horizon_end") else None),   # §5.13 B11
                         backend_version=_backend_identity(cfg.get("backend", "polars")),                     # R4-L

                         data_manifest=f"synthetic:{seed}", graph_version=s.get("graph_version", "gv-synth-0001"),
                         exclusion_kind=np.array([k if k else None for k in kinds], dtype=object))
    meta = {"n_episodes": eps.height, "n_eligible": len(ids), "n_asts": len(asts), "n_rules": len(rules), "bars_rows": bars.height,
            "n_censored": int(cens.sum()),
            "loss": {"censored_excluded": int((kinds == "censored").sum()), "coverage_excluded": int((kinds == "coverage").sum()),   # A15 互斥计数
                     "not_eligible": eps.height - len(ids)},
            "opportunity_diagnostics": opp_diag}
    # B8（execution-interface §5.10）对接准备：G2 落地后 simulate_batch 带 fraction_source / entry_fractions / tp_fractions；这里只做诊断透传
    for col in ("fraction_source", "entry_ttl_source"):
        if col in execution.columns:
            meta[f"{col}_dist"] = execution.group_by(col).len().sort(col).to_dict(as_series=False)
    return inputs, cands, meta


def run_protocol(config_path: str, out_dir: str | None = None) -> dict:
    cfg = load_config(config_path)
    t0 = time.perf_counter()
    if cfg["data"].get("source", "synthetic") != "synthetic":
        raise NotImplementedError("P1 只支持 source=synthetic；真实湖接入属 P2（需闸门 approved）")
    p = cfg.get("protocol", {})
    pc = PipelineConfig(scheme=p.get("scheme", "expanding"), test_span_days=int(p.get("test_span_days", 60)), min_train_span_days=int(p.get("min_train_span_days", 180)),
                        embargo_days=float(p.get("embargo_days", 1.0)), label_maturity_days=float(p.get("label_maturity_days", 0.0)),
                        min_train_clusters=int(p.get("min_train_clusters", 20)), search_frac=float(p.get("search_frac", 0.5)),
                        block_len_days=(None if str(p.get("block_len_days", 3)) == "auto" else int(p.get("block_len_days", 3))),
                        block_len_sensitivity=tuple(p.get("block_len_sensitivity", [1, 3, 7])),
                        B=int(p.get("B", 2000)), alpha=float(p.get("alpha", 0.05)), config_cap=p.get("config_cap"), seed=int(cfg.get("seed", 0)),
                        max_folds=p.get("max_folds"))
    proto_hash = hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()
    ledger = Ledger(budget_configs=None, scope=f"protocol:{proto_hash[:16]}")
    ledger.recover()
    inputs, cands, meta = build_inputs_from_synthetic(cfg, ledger=ledger)
    rep = run_pipeline(inputs, cands, pc, ledger=ledger)
    rep.update({"protocol_hash": proto_hash, "config_path": str(config_path), "schema_version": "g3-report-v0", "data": meta,
                "ledger_path": str(ledger.projection), "data_root": str(paths.data_root()), "wall_s_total": round(time.perf_counter() - t0, 2),
                "limitations": ["合成数据；不构成研究优势声明", "G-STAT-CLAIM pending → descriptive_only", "G-LICENSE-SEARCH pending → 仅封顶枚举，无 DEAP",
                                "latency=0（H0 乐观假设），P1 合成不受 latency=1s 敏感性约束"]})
    if out_dir:
        od = Path(out_dir)
        od.mkdir(parents=True, exist_ok=True)
        (od / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        (od / "folds.json").write_text(json.dumps(rep["folds"], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        rep["out_dir"] = str(od)
    return rep


def _main(argv=None):  # pragma: no cover - CLI
    import argparse
    ap = argparse.ArgumentParser(prog="quant_lab.research.api")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--config", required=True); r.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        rep = run_protocol(a.config, a.out)
        print(json.dumps({k: rep.get(k) for k in ("status", "tier", "K", "theta", "se", "lower_bound", "p_adj", "n_attempts", "n_folds", "claim_status", "out_dir")}, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    _main()


__all__ = ["Candidate", "PanelInputs", "PanelInvalid", "PipelineConfig", "RuleSpec", "inner_split", "build_inputs_from_synthetic", "candidate_diffs", "diagnose_block_len", "enumerate_grammar",
           "load_config", "make_rule", "run_pipeline", "run_protocol"]
