"""R-06：OpportunitySet 冻结、簇均权 θ、NaN→skip 保分母、删失排除计损耗、nan_rate 门、两臂配对协议错误、契约错误。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest

from quant_lab.research import synthetic
from quant_lab.research.ast import canonical_hash
from quant_lab.research.evaluator import (ESTIMANDS, NAN_RATE_MAX, EvalContractError, EvalProtocolError, OpportunitySet, evaluate,
                                          freeze_opportunity_set, pair_arms)

AST = {"op": "Ref", "args": [{"field": "close"}], "params": {"lag": 0}}
H = canonical_hash(AST)
UTC = pl.Datetime("us", "UTC")
T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def episodes(rows):
    """rows: (episode_id, cluster_id, censored)"""
    return pl.DataFrame({
        "episode_id": [r[0] for r in rows], "cluster_id": [r[1] for r in rows], "right_censored": [r[2] for r in rows],
        "t_dec": [T0 + dt.timedelta(hours=i) for i in range(len(rows))], "graph_version": ["gv"] * len(rows),
        "decision_snapshot_hash": ["h"] * len(rows), "is_tombstone": [False] * len(rows),
        "eligibility_by_estimand": [dict.fromkeys(ESTIMANDS, True)] * len(rows),
    }, schema_overrides={"t_dec": UTC})


def execution(ep, R, policy="p0", **kw):
    """用 synthetic.fake_execution 造 G2 列，然后把 net_R 覆盖为给定值（None = 删失）。"""
    x = synthetic.fake_execution(ep, policy_version=policy, none_frac=0.0, seed=0, **kw)
    vals = [None if r is None else Decimal(str(r)) for r in R]
    return x.with_columns(pl.Series("net_R", vals, dtype=synthetic.DEC), pl.Series("net_pnl", vals, dtype=synthetic.DEC),
                          pl.Series("censor_reason", [("LABEL_RIGHT_CENSORED" if r is None else None) for r in R], dtype=pl.Utf8),
                          pl.Series("outcome_kind", [("right_censored" if r is None else "filled_closed") for r in R], dtype=pl.Utf8))


def features(ids, vals, valid=None):
    valid = valid if valid is not None else [v is not None for v in vals]
    return pl.DataFrame({"episode_id": ids, f"f_{H}": vals, f"validity_{H}": valid}, schema_overrides={f"f_{H}": pl.Float64})


take_all = lambda f: pl.Series([True] * f.height)                     # noqa: E731
take_pos = lambda f: f[f"f_{H}"] > 0                                  # noqa: E731


def test_freeze_cluster_equal_weights():
    ep = episodes([("a", "c1", False), ("b", "c1", False), ("c", "c2", False), ("d", "c3", False)])
    ep = ep.with_columns(pl.Series("is_tombstone", [False, False, False, True]))
    opp = freeze_opportunity_set(ep)
    assert opp.episode_ids == ["a", "b", "c"] and opp.n_clusters == 2
    w = dict(zip(opp.weights["episode_id"], opp.weights["weight"]))
    assert w == {"a": 0.5, "b": 0.5, "c": 1.0}
    assert opp.eligibility.filter(pl.col("episode_id") == "d")["reason"][0] == "TOMBSTONE"
    opp.verify()
    tampered = OpportunitySet(opp.episode_ids + ["zzz"], opp.eligibility, opp.weights, opp.digest)
    with pytest.raises(EvalProtocolError, match="改动"):
        tampered.verify()


def test_theta_is_cluster_mean_of_paired_differences():
    """ADR §13 最小反例：一簇两机会、另一簇一机会；候选全 take → θ = 0；候选跳过亏损者 → 簇均权。"""
    ep = episodes([("a", "c1", False), ("b", "c1", False), ("c", "c2", False)])
    opp = freeze_opportunity_set(ep)
    x = execution(ep, [1.0, -1.0, 0.5])
    f = features(["a", "b", "c"], [1.0, -1.0, 0.5])
    r = evaluate(AST, opp, features=f, rule=take_all, execution=x, fold_id="f0", attempt_id="t0")
    assert r.status == "ok" and r.theta == 0.0 and r.n_opportunities == 3 and r.coverage == 1.0 and r.n_take == 3
    r = evaluate(AST, opp, features=f, rule=take_pos, execution=x, fold_id="f0", attempt_id="t1")
    # skip b：d = (0 − (−1)) = 1，权重 0.5；a、c d=0 → θ = 0.5 / 2 = 0.25
    assert r.theta == pytest.approx(0.25) and r.n_skip == 1 and r.se is not None and r.per_fill_R == pytest.approx(0.75)
    assert r.weight_remaining == pytest.approx(2.0) and r.tail_loss is not None


def test_nan_feature_is_skip_and_keeps_denominator():
    ep = episodes([("a", "c1", False), ("b", "c2", False), ("c", "c3", False), ("d", "c4", False)] + [(f"e{i}", f"k{i}", False) for i in range(16)])
    opp = freeze_opportunity_set(ep)
    n = ep.height
    x = execution(ep, [-2.0] + [0.1] * (n - 1))
    f = features(opp.episode_ids, [None] + [1.0] * (n - 1))            # 只有亏损机会 NaN（nan_rate = 1/20 = 0.05，恰不拒）
    r = evaluate(AST, opp, features=f, rule=take_all, execution=x, fold_id="f0", attempt_id="t2")
    assert r.status == "ok" and r.nan_rate == pytest.approx(NAN_RATE_MAX) and r.n_nan_skip == 1
    assert r.n_evaluated == n and r.theta == pytest.approx(2.0 / n)     # a 记 0：d_a = 0 − (−2) = 2，仍在分母（n 个簇各权 1）
    f2 = features(opp.episode_ids, [None, None] + [1.0] * (n - 2))       # 2/20 = 0.10 > 0.05 → 拒收
    r2 = evaluate(AST, opp, features=f2, rule=take_all, execution=x, fold_id="f0", attempt_id="t3")
    assert r2.status == "rejected" and r2.reason.startswith("NAN_RATE") and r2.theta is None


def test_censored_excluded_not_zero_and_counted():
    ep = episodes([("a", "c1", True), ("b", "c2", False), ("c", "c3", False)])
    opp = freeze_opportunity_set(ep)
    x = execution(ep, [None, 1.0, -1.0])
    f = features(["a", "b", "c"], [5.0, 1.0, -1.0])
    r = evaluate(AST, opp, features=f, rule=take_pos, execution=x, fold_id="f0", attempt_id="t4")
    assert r.n_censored_excluded == 1 and r.n_evaluated == 2 and r.coverage == pytest.approx(2 / 3)
    assert r.theta == pytest.approx(0.5)                                  # c skip: d=+1；b d=0；分母 2（a 排除，不记 0）
    assert r.weight_remaining == pytest.approx(2.0)


def test_two_arm_pairing_and_protocol_errors():
    ep = episodes([("a", "c1", False), ("b", "c2", False)])
    opp = freeze_opportunity_set(ep)
    base = execution(ep, [1.0, 1.0], policy="base")
    cand = execution(ep, [2.0, 0.0], policy="cand")
    both = pl.concat([base, cand])
    f = features(["a", "b"], [1.0, 1.0])
    with pytest.raises(EvalProtocolError, match="显式指定"):
        evaluate(AST, opp, features=f, rule=take_all, execution=both, fold_id="f", attempt_id="t")
    r = evaluate(AST, opp, features=f, rule=take_all, execution=both, fold_id="f", attempt_id="t", baseline_policy="base", candidate_policy="cand")
    assert r.theta == pytest.approx(((2 - 1) + (0 - 1)) / 2)
    with pytest.raises(EvalProtocolError, match="缺臂"):
        pair_arms(pl.concat([base, cand[:1]]), ["a", "b"], baseline_policy="base", candidate_policy="cand")
    with pytest.raises(EvalProtocolError, match="意外"):
        pair_arms(base, ["a"])
    with pytest.raises(EvalProtocolError, match="重复"):
        pair_arms(pl.concat([base, base]), ["a", "b"])
    with pytest.raises(EvalProtocolError, match="上下文"):
        pair_arms(pl.concat([base, cand.with_columns(pl.lit("stress").alias("cost_scenario"))]), ["a", "b"], baseline_policy="base", candidate_policy="cand")


def test_contract_violations_rejected_not_filled():
    ep = episodes([("a", "c1", False), ("b", "c2", False)])
    x = execution(ep, [1.0, 1.0])
    bad = x.with_columns(pl.Series("censor_reason", ["MARK_STALE", None]))          # 删失却有 net_R
    with pytest.raises(EvalContractError, match="censor_reason 非空"):
        pair_arms(bad, ["a", "b"])
    bad2 = x.with_columns(pl.Series("net_R", [None, Decimal("1")], dtype=synthetic.DEC))
    with pytest.raises(EvalContractError, match="无删失却"):
        pair_arms(bad2, ["a", "b"])
    bad3 = x.with_columns(pl.Series("fill_status", ["none", "filled"]))
    with pytest.raises(EvalContractError, match="fill_status=none"):
        pair_arms(bad3, ["a", "b"])


def test_synthetic_end_to_end_pairs_and_decimal(fake_episodes, fake_execution):
    opp = freeze_opportunity_set(fake_episodes)
    f = features(opp.episode_ids, [1.0] * len(opp.episode_ids))
    r = evaluate(AST, opp, features=f, rule=take_all, execution=fake_execution, fold_id="f0", attempt_id="e2e")
    elig = fake_episodes.filter(pl.col("episode_id").is_in(opp.episode_ids))
    assert r.status == "ok" and r.theta == 0.0 and r.n_censored_excluded + r.n_coverage_excluded == int(elig["right_censored"].sum())
    assert r.n_coverage_excluded > 0 and r.n_censored_excluded > 0                    # 合成删失原因轮换：右删失与覆盖失败互斥拆分（A15）
    assert 0 < r.coverage < 1
    # S16：未闭合率在删失 mask 之前统计——右删失已开未平仓计入分子
    opened = fake_execution.filter(pl.col("position_open_at").is_not_null())
    assert r.unclosed_rate == pytest.approx(opened["position_close_at"].null_count() / opened.height) and r.unclosed_rate > 0


def test_estimand_vocabulary_frozen_and_stub_isomorphic_with_g1():
    """§9.10.8：'main' 废止、非法 estimand 抛 EvalProtocolError 而非 StructFieldNotFoundError；桩键集 == G1 ELIG_KEYS。"""
    import ast as pyast, pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant_lab" / "data" / "lifecycle.py"
    tree = pyast.parse(src.read_text(encoding="utf-8"))
    elig = next(pyast.literal_eval(n.value) for n in pyast.walk(tree) if isinstance(n, pyast.Assign) and any(getattr(t, "id", "") == "ELIG_KEYS" for t in n.targets))
    assert tuple(elig) == ESTIMANDS                                                          # 与 G1 源码常量逐键相同（不 import：G1 依赖不在 .venv-g3）
    ep = episodes([("a", "c1", False), ("b", "c2", False)])
    for bad in ("main", "", "outcome_x"):
        with pytest.raises(EvalProtocolError, match="estimand"):
            freeze_opportunity_set(ep, estimand=bad)
    assert freeze_opportunity_set(ep).episode_ids == ["a", "b"]                            # 默认 entry_decision
    ep2 = ep.with_columns(pl.Series("eligibility_by_estimand", [{**dict.fromkeys(ESTIMANDS, True), "entry_decision": False}, dict.fromkeys(ESTIMANDS, True)]))
    o = freeze_opportunity_set(ep2)
    assert o.episode_ids == ["b"] and o.eligibility.filter(pl.col("episode_id") == "a")["reason"][0] == "NOT_ELIGIBLE:entry_decision"
    drifted = ep.with_columns(pl.Series("eligibility_by_estimand", [{"main": True}] * 2))
    with pytest.raises(EvalProtocolError, match="键集漂移"):
        freeze_opportunity_set(drifted)
    syn = synthetic.fake_episodes(30, span_days=3, seed=9)
    assert {f.name for f in syn.schema["eligibility_by_estimand"].fields} == set(elig)
    assert all(syn.schema["eligibility_by_estimand"].fields[i].dtype == pl.Boolean for i in range(6))


def test_a13_only_requested_estimand_key_must_be_nonnull():
    """research-schema §9.10.9 A13：非请求键含 null → 只记 EvalProtocolWarning + diagnostics；被请求键含 null → EvalProtocolError。"""
    import warnings
    from quant_lab.research.evaluator import EvalProtocolWarning
    ep = episodes([("a", "c1", False), ("b", "c2", False)])
    with_null_outcome = ep.with_columns(pl.Series("eligibility_by_estimand", [{**dict.fromkeys(ESTIMANDS, True), "outcome": None}] * 2,
                                                  dtype=pl.Struct({k: pl.Boolean for k in ESTIMANDS})))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        opp = freeze_opportunity_set(with_null_outcome, estimand="entry_decision")
    assert opp.episode_ids == ["a", "b"] and opp.diagnostics["null_keys"] == {"outcome": 2}
    assert any(issubclass(x.category, EvalProtocolWarning) and "outcome" in str(x.message) for x in w)
    with pytest.raises(EvalProtocolError, match="outcome 含 null"):
        freeze_opportunity_set(with_null_outcome, estimand="outcome")
    clean = freeze_opportunity_set(ep)
    assert clean.diagnostics["null_keys"] == {}


def test_a15_coverage_failure_counted_separately_from_right_censoring():
    """research-schema §9.10.11 A15：unevaluable/覆盖类 censor_reason → n_coverage_excluded；右删失 → n_censored_excluded；二者互斥且都出分母。"""
    from quant_lab.research.evaluator import COVERAGE_CENSOR_REASONS, exclusion_kind
    ep = episodes([("a", "c1", True), ("b", "c2", True), ("c", "c3", False), ("d", "c4", False)])
    x = execution(ep, [None, None, 1.0, -1.0]).with_columns(pl.Series("censor_reason", ["LABEL_RIGHT_CENSORED", "BAR_GAP", None, None], dtype=pl.Utf8))
    opp = freeze_opportunity_set(ep)
    r = evaluate(AST, opp, features=features(opp.episode_ids, [1.0] * 4), rule=take_all, execution=x, fold_id="f", attempt_id="a15")
    assert r.n_censored_excluded == 1 and r.n_coverage_excluded == 1 and r.n_evaluated == 2 and r.coverage == pytest.approx(0.5)
    assert exclusion_kind("MARK_STALE") == "coverage" and exclusion_kind("LABEL_RIGHT_CENSORED") == "censored" and exclusion_kind(None) is None
    assert exclusion_kind(None, "unevaluable") == "coverage" and all(exclusion_kind(k) == "coverage" for k in COVERAGE_CENSOR_REASONS)
    # outcome_kind 列存在时优先（G2 §9.10.11 落地后的路径）
    x2 = x.with_columns(pl.Series("outcome_kind", ["right_censored", "unevaluable", "filled_closed", "stopped"]))
    r2 = evaluate(AST, opp, features=features(opp.episode_ids, [1.0] * 4), rule=take_all, execution=x2, fold_id="f", attempt_id="a15b")
    assert (r2.n_censored_excluded, r2.n_coverage_excluded) == (1, 1)
