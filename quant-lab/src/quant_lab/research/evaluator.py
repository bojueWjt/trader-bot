"""EventEvaluator（契约 feature-snapshot §4 + §7.3/§7.4；ADR-G3 §6；合并稿 D.4）。

OpportunitySet 收益前冻结（簇均权：每经济簇初始总权重 1，簇内均分）；evaluate 入口重算摘要校验未被改动。
两臂配对：execution 为 G2 simulate_batch 输出（不含 candidate 概念），G3 按 policy_version 区分 baseline / candidate 臂，
按 episode_id 一对一配对；缺臂、重复、意外 episode → EvalProtocolError（不 inner join 静默丢行）。
纯过滤候选（只有一个 policy_version）时两臂共用同一执行结果。

缺值优先级（§7.4 / ADR §6.2，自上而下）：
  任一臂 censor_reason 非空 → net_R 必须 null，共同排除并计损耗（不记 0）
  无删失却 net_R null / 非有限、或 fill_status=none 却 net_R≠0 → EvalContractError（不补 0）
  fill_status=none → net_R=0，仍在分母
  规则判 skip → R_cand=0，仍在分母
  候选特征 invalid → 同 skip；nan_rate = 初始合格机会中候选特征 invalid 数 / 初始合格机会数；> 0.05 拒收（恰 0.05 不拒）
θ = Σ m_i w_i (R_cand,i − R_base,i) / Σ m_i w_i；SE 为簇级 sandwich（簇视为独立单元；共同日历块重估见 maxt）。
铁律：账本每次评估前写入（调用方经 ledger 参数或 protocol 层保证）；本函数不读标签以外的任何未来信息。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
import warnings
from typing import Callable

import polars as pl

from quant_lab.research.ast import canonical_hash

NAN_RATE_MAX = 0.05
TAIL_ALPHA = 0.05
#: research-schema §9.10.11 A15：覆盖失败（"我们没有看世界所需的数据"）与右删失（"数据齐全、标签未成熟"）互斥计数
COVERAGE_CENSOR_REASONS = frozenset({"MARK_STALE", "BAR_GAP", "FUNDING_SCHEDULE_GAP", "RULE_HISTORY_MISSING", "SYMBOL_TIME_INVALID"})


def exclusion_kind(censor_reason: str | None, outcome_kind: str | None = None) -> str | None:
    """None（可评）| "coverage"（unevaluable / 覆盖类 censor_reason）| "censored"（右删失等）。outcome_kind 存在时优先。"""
    if outcome_kind == "unevaluable":
        return "coverage"
    if censor_reason is None:
        return None if outcome_kind != "unevaluable" else "coverage"
    return "coverage" if censor_reason in COVERAGE_CENSOR_REASONS else "censored"
#: research-schema §9.10.1 A8 第 5 条 / §9.10.8 冻结的 estimand 六键（"main" 已废止）
ESTIMANDS = ("description", "entry_decision", "execution", "original_entry", "price_check", "outcome")
DEFAULT_ESTIMAND = "entry_decision"


class EvalProtocolError(ValueError):
    """两臂配对/机会集协议错误：缺臂、重复、意外 episode、机会集被改动。"""


class EvalContractError(ValueError):
    """G2 执行结果违反契约：删失与 net_R 可空性不一致、未成交 net_R≠0、非有限。"""


class EvalProtocolWarning(UserWarning):
    """非阻塞协议告警（research-schema §9.10.9 A13）：非请求 estimand 键含 null 等，进 report 诊断不抛。"""


@dataclass(frozen=True)
class OpportunitySet:
    episode_ids: list[str]
    eligibility: pl.DataFrame          # episode_id, eligible: bool, reason: str|null
    weights: pl.DataFrame              # episode_id, cluster_id, weight（簇均权，每簇总权重 1）
    digest: str = ""
    diagnostics: dict = field(default_factory=dict)   # 非阻塞告警（A13：非请求键 null 计数等），进 report

    def verify(self) -> None:
        if self.digest and _digest(self.episode_ids, self.eligibility, self.weights) != self.digest:
            raise EvalProtocolError("OpportunitySet 内容与冻结摘要不一致（被改动）")

    @property
    def n_clusters(self) -> int:
        return int(self.weights["cluster_id"].n_unique())


def _digest(ids, elig: pl.DataFrame, w: pl.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(json_dumps(list(ids)).encode())
    h.update(elig.sort("episode_id").write_json().encode())
    h.update(w.sort("episode_id").write_json().encode())
    return h.hexdigest()


def json_dumps(o):
    import json
    return json.dumps(o, sort_keys=True, default=str)


def freeze_opportunity_set(episodes: pl.DataFrame, *, estimand: str = DEFAULT_ESTIMAND) -> OpportunitySet:
    """从 G1 决策图视图冻结机会集：合格 = eligibility_by_estimand[estimand] 且非 tombstone 且 t_dec 非空且有 cluster_id。
    权重 w_i = 1 / n_c（n_c = 簇内合格机会数，收益前冻结）。estimand 只能取 ESTIMANDS 六键（§9.10.8），默认 entry_decision。"""
    if estimand not in ESTIMANDS:
        raise EvalProtocolError(f"非法 estimand {estimand!r}，合法取值 {ESTIMANDS}（§9.10.8：'main' 已废止）")
    e = episodes
    if "eligibility_by_estimand" not in e.columns:
        raise EvalProtocolError("决策视图缺 eligibility_by_estimand（struct 固定六键），不得默认全部合格")
    if True:
        dt_ = e.schema["eligibility_by_estimand"]
        if not isinstance(dt_, pl.Struct):
            raise EvalProtocolError(f"eligibility_by_estimand 须为 struct 固定键，得到 {dt_}")
        have = {f.name for f in dt_.fields}
        if set(ESTIMANDS) != have:
            raise EvalProtocolError(f"eligibility_by_estimand 键集漂移：缺 {sorted(set(ESTIMANDS) - have)} 多 {sorted(have - set(ESTIMANDS))}")
        diag: dict = {"null_keys": {}}
        for f in dt_.fields:
            if f.dtype != pl.Boolean:
                raise EvalProtocolError(f"eligibility_by_estimand.{f.name} 须为 Boolean，得到 {f.dtype}")
            n_null = int(e.select(pl.col("eligibility_by_estimand").struct.field(f.name).null_count()).item())
            if n_null:
                if f.name == estimand:      # A13：只对被请求的 estimand 键做非空校验
                    raise EvalProtocolError(f"eligibility_by_estimand.{estimand} 含 null（{n_null} 行；被请求键不可空：null 会让'不合格'与'没算'不可分）")
                diag["null_keys"][f.name] = n_null
                warnings.warn(f"eligibility_by_estimand.{f.name} 含 null（{n_null} 行；非请求键，只记诊断，G1 侧 A13 要求六键非空）", EvalProtocolWarning, stacklevel=2)
    elig = pl.lit(True)
    reason = pl.lit(None, dtype=pl.Utf8)
    if "is_tombstone" in e.columns:
        reason = pl.when(pl.col("is_tombstone")).then(pl.lit("TOMBSTONE")).otherwise(reason)
        elig = elig & ~pl.col("is_tombstone").fill_null(False)
    reason = pl.when(pl.col("t_dec").is_null()).then(pl.lit("T_DEC_MISSING")).otherwise(reason)
    elig = elig & pl.col("t_dec").is_not_null()
    est = pl.col("eligibility_by_estimand").struct.field(estimand)
    reason = pl.when(~est).then(pl.lit(f"NOT_ELIGIBLE:{estimand}")).otherwise(reason)
    elig = elig & est
    tab = e.with_columns(elig.alias("eligible"), reason.alias("reason")).select("episode_id", "cluster_id", "eligible", "reason")
    ok = tab.filter(pl.col("eligible"))
    n_c = ok.group_by("cluster_id").len().rename({"len": "n_c"})
    w = ok.join(n_c, on="cluster_id").with_columns((1.0 / pl.col("n_c")).alias("weight")).select("episode_id", "cluster_id", "weight").sort("episode_id")
    ids = ok["episode_id"].sort().to_list()
    elig_df = tab.select("episode_id", "eligible", "reason").sort("episode_id")
    # research-schema §9.7 裁定 A5：机会集自洽——禁止返回"非空但零权重"的 OpportunitySet
    if ok["cluster_id"].null_count() > 0 or w["cluster_id"].null_count() > 0:
        raise EvalProtocolError(f"cluster_id 含 null（{ok['cluster_id'].null_count()} 个合格机会），决策视图不得以 null 混入（§9.7 A5）")
    if len(ids) != w.height:
        raise EvalProtocolError(f"机会集自相矛盾：episode_ids={len(ids)} 而 weights.height={w.height}")
    if ids and w["cluster_id"].n_unique() == 0:
        raise EvalProtocolError("episode_ids 非空但 n_clusters=0")
    return OpportunitySet(ids, elig_df, w, _digest(ids, elig_df, w), diag)


@dataclass(frozen=True)
class EvalResult:
    theta: float | None
    se: float | None
    n_opportunities: int          # 初始合格机会数（冻结分母）
    n_clusters: int
    coverage: float               # 共同可评 / 初始合格
    nan_rate: float
    per_fill_R: float | None
    tail_loss: float | None
    unclosed_rate: float
    n_evaluated: int = 0          # 共同可评（未删失）机会数
    n_censored_excluded: int = 0  # 右删失排除（标签未成熟；损耗）
    n_coverage_excluded: int = 0  # 覆盖失败排除（unevaluable：行情/规则证据缺失；A15，不得并入 n_censored_excluded）
    n_take: int = 0
    n_skip: int = 0
    n_nan_skip: int = 0
    weight_remaining: float = 0.0 # 排除后剩余总权重（初始 = n_clusters）
    status: str = "ok"            # ok | rejected | insufficient
    reason: str | None = None
    canonical_hash: str = ""
    fold_id: str = ""
    attempt_id: str = ""


# ---------------------------------------------------------------- 两臂配对
#: 臂内唯一性键。G2 的 PAIR_KEY 将补入 policy_hash（同名不同内容的政策会静默配错两臂）；本侧先行纳入：
#: 列存在即参与唯一性判定，G2 落地前不改变行为（policy_hash 已由 _IDENTITY_NONNULL 要求非空且与 version 一一对应）。
_ARM_KEYS = ("episode_id", "graph_version", "policy_version", "policy_hash", "cost_scenario", "path_scenario", "kernel")
_CTX_KEYS = ("graph_version", "decision_snapshot_hash", "t_dec", "cost_scenario", "path_scenario", "kernel", "market_manifest",
             "execution_contract_version", "seed", "risk_budget")
_IDENTITY_NONNULL = ("episode_id", "graph_version", "decision_snapshot_hash", "t_dec", "policy_version", "policy_hash", "cost_scenario", "path_scenario",
                     "kernel", "fill_status", "market_manifest", "execution_contract_version", "seed", "risk_budget")
_SCALARS = ("fill_status", "net_R", "censor_reason", "position_open_at", "position_close_at")


def _arm(execution: pl.DataFrame, policy: str, ids: list[str], label: str) -> pl.DataFrame:
    x = execution.filter(pl.col("policy_version") == policy)
    if x.height == 0:
        raise EvalProtocolError(f"{label} 臂 policy_version={policy!r} 无行")
    keys = [k for k in _ARM_KEYS if k in x.columns]
    if x.select(keys).n_unique() != x.height:
        raise EvalProtocolError(f"{label} 臂配对键重复")
    for c in _IDENTITY_NONNULL:
        if c not in x.columns:
            raise EvalProtocolError(f"{label} 臂缺必需列 {c}")
        if x[c].null_count():
            raise EvalProtocolError(f"{label} 臂 {c} 含 null（{x[c].null_count()} 行）")
    if x["policy_hash"].n_unique() != 1 or x["policy_version"].n_unique() != 1:
        raise EvalProtocolError(f"{label} 臂 policy_hash 必须与 policy_version 一一对应（得到 {x['policy_hash'].n_unique()} 个 hash / {x['policy_version'].n_unique()} 个 version）")
    have = set(x["episode_id"].to_list())
    want = set(ids)
    if want - have:
        raise EvalProtocolError(f"{label} 臂缺 {len(want - have)} 个机会（缺臂），例: {sorted(want - have)[:3]}")
    if have - want:
        raise EvalProtocolError(f"{label} 臂含 {len(have - want)} 个机会集外 episode（意外），例: {sorted(have - want)[:3]}")
    return x


def _validate_and_cast(x: pl.DataFrame, label: str) -> pl.DataFrame:
    """契约不变量校验后 Decimal → Float64；禁止 cast 后 fill_null(0)。"""
    r = x.with_columns(pl.col("net_R").cast(pl.Float64).alias("__r"))
    cens = pl.col("censor_reason").is_not_null()
    bad1 = r.filter(cens & pl.col("__r").is_not_null())
    if bad1.height:
        raise EvalContractError(f"{label} 臂 {bad1.height} 行 censor_reason 非空但 net_R 非 null")
    bad2 = r.filter(~cens & (pl.col("__r").is_null() | pl.col("__r").is_nan() | pl.col("__r").is_infinite()))
    if bad2.height:
        raise EvalContractError(f"{label} 臂 {bad2.height} 行无删失却 net_R null/非有限")
    bad3 = r.filter(~cens & (pl.col("fill_status") == "none") & (pl.col("__r") != 0))
    if bad3.height:
        raise EvalContractError(f"{label} 臂 {bad3.height} 行 fill_status=none 但 net_R≠0")
    return r


def pair_arms(execution: pl.DataFrame, ids: list[str], *, baseline_policy: str | None = None, candidate_policy: str | None = None) -> pl.DataFrame:
    """返回按 episode_id 配对的两臂表：episode_id, base_R, cand_R, base_censor, cand_censor, cand_fill, cand_open, cand_close。"""
    pols = execution["policy_version"].unique().sort().to_list()
    if baseline_policy is None and candidate_policy is None:
        if len(pols) == 1:
            baseline_policy = candidate_policy = pols[0]
        else:
            raise EvalProtocolError(f"execution 含多个 policy_version {pols}，须显式指定 baseline/candidate")
    baseline_policy = baseline_policy or candidate_policy
    candidate_policy = candidate_policy or baseline_policy
    b = _validate_and_cast(_arm(execution, baseline_policy, ids, "baseline"), "baseline")
    c = _validate_and_cast(_arm(execution, candidate_policy, ids, "candidate"), "candidate")
    missing = [k for k in _CTX_KEYS if k not in b.columns or k not in c.columns]
    if missing:
        raise EvalProtocolError(f"两臂缺共享上下文列 {missing}")
    ctx = list(_CTX_KEYS)
    bb = b.select("episode_id", *ctx, pl.col("__r").alias("base_R"), pl.col("censor_reason").alias("base_censor"),
                  (pl.col("outcome_kind") if "outcome_kind" in b.columns else pl.lit(None, dtype=pl.Utf8)).alias("base_kind"))
    ok_c = pl.col("outcome_kind") if "outcome_kind" in c.columns else pl.lit(None, dtype=pl.Utf8)
    cc = c.select("episode_id", *[pl.col(k).alias(f"__c_{k}") for k in ctx], pl.col("__r").alias("cand_R"),
                  pl.col("censor_reason").alias("cand_censor"), pl.col("fill_status").alias("cand_fill"),
                  pl.col("position_open_at").alias("cand_open"), pl.col("position_close_at").alias("cand_close"), ok_c.alias("cand_kind"))
    j = bb.join(cc, on="episode_id", how="inner")
    if j.height != len(ids):
        raise EvalProtocolError("两臂配对后行数与机会集不符")
    for k in ctx:
        # 身份列已在 _arm 断言非空（双方同 null 也被拒）；这里 null-safe 比较值
        mism = j.filter(pl.col(k).cast(pl.Utf8).ne_missing(pl.col(f"__c_{k}").cast(pl.Utf8)))
        if mism.height:
            raise EvalProtocolError(f"两臂上下文 {k} 不等（{mism.height} 行）")
    return j.drop([f"__c_{k}" for k in ctx])


# ---------------------------------------------------------------- 评估
def evaluate(
    ast: dict, opp: OpportunitySet, *, features: pl.DataFrame, rule: Callable[[pl.DataFrame], pl.Series],
    execution: pl.DataFrame, fold_id: str, attempt_id: str,
    baseline_policy: str | None = None, candidate_policy: str | None = None, ledger=None, tail_alpha: float = TAIL_ALPHA,
) -> EvalResult:
    res = _evaluate(ast, opp, features=features, rule=rule, execution=execution, fold_id=fold_id, attempt_id=attempt_id,
                    baseline_policy=baseline_policy, candidate_policy=candidate_policy, ledger=ledger, tail_alpha=tail_alpha)
    if ledger is not None:
        ledger.mark(attempt_id, "completed" if res.status == "ok" else res.status, reason=res.reason, objective=res.theta)
    return res


def _evaluate(ast, opp, *, features, rule, execution, fold_id, attempt_id, baseline_policy, candidate_policy, ledger, tail_alpha) -> EvalResult:
    opp.verify()
    h = canonical_hash(ast)
    ids = list(opp.episode_ids)
    n0 = len(ids)
    if ledger is not None:
        ledger.mark(attempt_id, "running")
    try:
        if n0 == 0:
            return EvalResult(None, None, 0, 0, 0.0, 0.0, None, None, 0.0, status="insufficient", reason="EMPTY_OPPORTUNITY_SET", canonical_hash=h, fold_id=fold_id, attempt_id=attempt_id)
        fcol, vcol = f"f_{h}", f"validity_{h}"
        if fcol not in features.columns or vcol not in features.columns:
            raise EvalProtocolError(f"features 缺 {fcol}/{vcol}")
        feat = pl.DataFrame({"episode_id": ids}).join(features.select("episode_id", fcol, vcol), on="episode_id", how="left")
        fv = pl.col(fcol).cast(pl.Float64)
        feat = feat.with_columns((pl.col(vcol).fill_null(False) & fv.is_not_null() & fv.is_finite()).alias(vcol))   # validity 与数值联合判定
        valid = feat[vcol]
        n_nan = int((~valid).sum())
        nan_rate = n_nan / n0
        if nan_rate > NAN_RATE_MAX:
            return EvalResult(None, None, n0, opp.n_clusters, 0.0, nan_rate, None, None, 0.0, n_nan_skip=n_nan, status="rejected",
                              reason=f"NAN_RATE {nan_rate:.4f} > {NAN_RATE_MAX}", canonical_hash=h, fold_id=fold_id, attempt_id=attempt_id)
        take = pl.Series(rule(feat)).cast(pl.Boolean).fill_null(False) & valid   # invalid 特征 → skip
        pairs = pair_arms(execution, ids, baseline_policy=baseline_policy, candidate_policy=candidate_policy)
        tab = (pl.DataFrame({"episode_id": ids, "take": take})
               .join(pairs, on="episode_id", how="left")
               .join(opp.weights.select("episode_id", "cluster_id", "weight"), on="episode_id", how="left"))
        kinds = [exclusion_kind(bc, bk) or exclusion_kind(cc_, ck)
                 for bc, bk, cc_, ck in zip(tab["base_censor"].to_list(), tab["base_kind"].to_list(), tab["cand_censor"].to_list(), tab["cand_kind"].to_list())]
        tab = tab.with_columns(
            pl.Series("__excl", kinds, dtype=pl.Utf8),
            pl.when(pl.col("take")).then(pl.col("cand_R")).otherwise(0.0).alias("R_cand"),
        ).with_columns(pl.col("__excl").is_null().alias("m")).with_columns((pl.col("R_cand") - pl.col("base_R")).alias("d"))
        n_cov = int((tab["__excl"] == "coverage").sum())
        if "t_dec" in features.columns:
            ft = pl.DataFrame({"episode_id": ids}).join(features.select("episode_id", pl.col("t_dec").alias("__ft")), on="episode_id", how="left")
            chk = tab.select("episode_id", "t_dec").join(ft, on="episode_id", how="left")
            bad = chk.filter(pl.col("t_dec").cast(pl.Utf8).ne_missing(pl.col("__ft").cast(pl.Utf8)))
            if bad.height:
                raise EvalProtocolError(f"执行 t_dec 与当前 feature_snapshot 的 t_dec 不一致（{bad.height} 行）：两臂一致但与冻结快照不一致")
        # 未闭合率诊断：在收益删失 mask **之前**、按候选臂规则 take 且已开仓的全部机会（含右删失未平）
        opened_all = tab.filter(pl.col("take") & pl.col("cand_open").is_not_null())
        unclosed = float((opened_all["cand_close"].is_null()).sum() / opened_all.height) if opened_all.height else 0.0
        ev = tab.filter(pl.col("m"))
        n_cens = n0 - ev.height - n_cov                 # 右删失（标签未成熟）；覆盖失败单列 n_cov（A15 互斥）
        denom = float(ev["weight"].sum()) if ev.height else 0.0
        if ev.height == 0 or denom <= 0:
            return EvalResult(None, None, n0, opp.n_clusters, 0.0, nan_rate, None, None, unclosed, n_evaluated=0, n_censored_excluded=n_cens, n_coverage_excluded=n_cov, n_nan_skip=n_nan,
                              n_take=int(take.sum()), n_skip=int((~take).sum()), status="insufficient", reason="NO_EVALUABLE_OPPORTUNITY",
                              canonical_hash=h, fold_id=fold_id, attempt_id=attempt_id)
        theta = float((ev["weight"] * ev["d"]).sum()) / denom
        # 簇级 sandwich SE：u_c = Σ_{i∈c} w_i (d_i − θ)；se = sqrt(Σ_c u_c²) / Σ w
        u = ev.with_columns((pl.col("weight") * (pl.col("d") - theta)).alias("u")).group_by("cluster_id").agg(pl.col("u").sum())
        se = math.sqrt(float((u["u"] ** 2).sum())) / denom if u.height > 1 else None
        taken = ev.filter(pl.col("take"))
        fills = taken.filter(pl.col("cand_fill").is_in(["partial", "filled"]))
        per_fill = float(fills["cand_R"].mean()) if fills.height else None
        tail = float(ev["R_cand"].quantile(tail_alpha, interpolation="linear")) if ev.height >= 2 else None
        return EvalResult(theta, se, n0, opp.n_clusters, ev.height / n0, nan_rate, per_fill, tail, unclosed,
                          n_evaluated=ev.height, n_censored_excluded=n_cens, n_coverage_excluded=n_cov, n_take=int(take.sum()), n_skip=int((~take).sum()), n_nan_skip=n_nan,
                          weight_remaining=denom, status="ok", canonical_hash=h, fold_id=fold_id, attempt_id=attempt_id)
    except Exception as e:
        if ledger is not None:
            ledger.mark(attempt_id, "failed", reason=f"{type(e).__name__}: {e}"[:200])
        raise


__all__ = ["COVERAGE_CENSOR_REASONS", "DEFAULT_ESTIMAND", "ESTIMANDS", "NAN_RATE_MAX", "exclusion_kind", "EvalContractError", "EvalProtocolError", "EvalProtocolWarning", "EvalResult", "OpportunitySet", "evaluate", "freeze_opportunity_set", "pair_arms"]
