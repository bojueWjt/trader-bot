"""R-09：分档、封顶枚举、流水线（与 evaluate 一致）、run_protocol 端到端（合成，湖根指向 tmp）。"""
from __future__ import annotations

import json
import os
import pathlib

import numpy as np
import polars as pl
import pytest

from quant_lab.research import api as API
from quant_lab.research import nullmodel as NM
from quant_lab.research.ast import canonical_hash, lint
from quant_lab.research.evaluator import OpportunitySet, evaluate
from quant_lab.research.grammar import enumerate_grammar
from quant_lab.research.ledger import MemoryLedger
from quant_lab.research.tiers import deff_from_panel, tier_of, tier_report

FIX = pathlib.Path(__file__).parent / "fixtures" / "protocol_synthetic.yaml"


# ---------------------------------------------------------------- 分档
def test_tier_thresholds_both_sides_and_downgrade_only():
    assert tier_of(79)[0] == "T0" and tier_of(80)[0] == "T1" and tier_of(199)[0] == "T1" and tier_of(200)[0] == "T2"
    assert tier_of(499)[0] == "T2" and tier_of(500)[0] == "T3a" and tier_of(1499)[0] == "T3a" and tier_of(1500)[0] == "T3b"
    r = tier_report({"a": 150, "b": 300}, DEFF_by_fold={"a": 1.0, "b": 1.2}, span_months=8, n_outer_folds=2, min_fold_test_clusters=25)
    assert r.K == 150 and r.tier == "T1" and r.p_cap == 4 and r.config_cap == 12 and r.p_allowed == min(4, 150 // 20) and r.claim_status == "descriptive_only"
    r2 = tier_report({"a": 150, "b": 300}, DEFF_by_fold={"a": 2.5, "b": 1.0}, span_months=8, n_outer_folds=2, min_fold_test_clusters=25)
    assert r2.K_eff == 60 and r2.tier == "T0" and any("K_EFF" in x for x in r2.downgrade_reasons)                 # K_eff 只降不升
    r3 = tier_report({"a": 250}, DEFF_by_fold={"a": 1.0}, span_months=8, n_outer_folds=1, min_fold_test_clusters=25)
    assert r3.tier_by_K == "T2" and r3.tier == "T1" and r3.downgrade_reasons                                      # 跨度/外折不足降档
    r4 = tier_report({"a": 250}, DEFF_by_fold={"a": None}, span_months=13, n_outer_folds=3, min_fold_test_clusters=40)
    assert r4.K_eff is None and "DEFF_UNDEFINED:a" in r4.downgrade_reasons and r4.tier == "T0" and r4.config_cap == 0 and r4.p_allowed == 0   # fail closed
    assert tier_report({}).tier == "T0"


def test_deff_common_shock_gt_one():
    w = NM.synth_world(NM.WorldConfig(mechanism="common_shock", seed=3, n_clusters=200))
    inp = w.inputs
    from quant_lab.research.maxt import PairedPanel
    df = inp.anchors.select("episode_id", "cluster_id", "t_dec").with_columns(pl.Series("weight", inp.weights), pl.Series("m", ~inp.censored),
                                                                             pl.Series("x", np.nan_to_num(inp.base_R)))
    d = deff_from_panel(PairedPanel.from_frame(df, ["x"]), block_len_days=3)
    assert d is not None and d > 1.0
    const = PairedPanel.from_frame(df.with_columns(pl.lit(1.0).alias("x")), ["x"])
    assert deff_from_panel(const, block_len_days=3) is None                    # 分母方差 0 → undefined


# ---------------------------------------------------------------- 枚举
def test_enumerate_grammar_deterministic_capped_and_lint_clean():
    kw = dict(ops=["Mean", "Std", "Ref", "SafeDiv", "Add", "Gt"], windows=[5, 20], fields=["close", "volume"])
    a = enumerate_grammar(2, cap=50, **kw)
    b = enumerate_grammar(2, cap=50, ops=list(reversed(kw["ops"])), windows=[20, 5], fields=["volume", "close"])
    assert a == b and 0 < len(a) <= 50                                           # 输入重排不改变集合与顺序
    hs = [canonical_hash(x) for x in a]
    assert len(hs) == len(set(hs))
    for x in a:
        lint(x)
    assert enumerate_grammar(2, cap=0, **kw) == [] and len(enumerate_grammar(2, cap=1, **kw)) == 1
    d3 = enumerate_grammar(3, cap=2000, **kw)
    assert len(d3) > len(enumerate_grammar(2, cap=2000, **kw)) and max(lint(x).depth for x in d3) == 3
    with pytest.raises(ValueError):
        enumerate_grammar(2, cap=5, ops=["CSRank"], windows=[5], fields=["close"])
    with pytest.raises(ValueError):
        enumerate_grammar(2, cap=5, ops=["Mean"], windows=[5], fields=["net_R"])


# ---------------------------------------------------------------- 流水线 vs evaluate 一致
def test_pipeline_candidate_diffs_match_evaluate():
    w = NM.synth_world(NM.WorldConfig(mechanism="common_shock", seed=2, n_clusters=120))
    inp = w.inputs
    rule = API.RuleSpec("gt_q70", "gt_q", 0.7)
    cand = API.Candidate("f00:gt_q70", "f00", rule)
    idx = np.arange(inp.n)
    thr = rule.fit(inp.features["f00"][idx])
    d, take, _ = API.candidate_diffs(inp, cand, thr, idx)
    m = ~inp.censored
    theta_pipe = float((inp.weights[m] * d[m]).sum() / inp.weights[m].sum())
    # 同一规则走契约 evaluate()：构造 OpportunitySet / features / execution
    from quant_lab.research import synthetic
    ids = inp.ids()
    elig = pl.DataFrame({"episode_id": ids, "eligible": [True] * inp.n, "reason": [None] * inp.n}, schema_overrides={"reason": pl.Utf8})
    wdf = inp.anchors.select("episode_id", "cluster_id").with_columns(pl.Series("weight", inp.weights))
    opp = OpportunitySet(ids, elig, wdf)
    eps = pl.DataFrame({"episode_id": ids, "graph_version": ["gv"] * inp.n, "decision_snapshot_hash": ["h"] * inp.n, "t_dec": inp.anchors["t_dec"],
                        "right_censored": inp.censored.tolist()})
    ex = synthetic.fake_execution(eps, none_frac=0.0)
    vals = [None if not np.isfinite(r) else synthetic.Decimal(f"{r:.12f}") for r in inp.base_R]
    ex = ex.with_columns(pl.Series("net_R", vals, dtype=synthetic.DEC), pl.Series("net_pnl", vals, dtype=synthetic.DEC),
                         pl.Series("censor_reason", ["LABEL_RIGHT_CENSORED" if c else None for c in inp.censored], dtype=pl.Utf8))
    ast = {"op": "Ref", "args": [{"field": "close"}], "params": {"lag": 0}}
    h = canonical_hash(ast)
    feats = pl.DataFrame({"episode_id": ids, f"f_{h}": inp.features["f00"], f"validity_{h}": [True] * inp.n})
    r = evaluate(ast, opp, features=feats, rule=lambda f: f[f"f_{h}"] > thr, execution=ex, fold_id="f", attempt_id="a")
    assert r.theta == pytest.approx(theta_pipe, abs=1e-9)


def test_run_pipeline_report_shape_and_ledger_before_eval():
    w = NM.synth_world(NM.WorldConfig(mechanism="common_shock", seed=4, n_clusters=1600))
    led = MemoryLedger()
    rep = API.run_pipeline(w.inputs, NM.default_candidates(12), API.PipelineConfig(B=300, seed=4, block_len_days=None), ledger=led)
    assert rep["tier"] == "T1" and rep["candidate_pool"]["n_admitted"] == 12
    assert rep["status"] in ("ok", "insufficient") and rep["tier"] in ("T0", "T1", "T2") and rep["n_attempts"] == led.n_attempts > 0
    assert rep["claim_status"] == "descriptive_only" and rep["gates"]["G-STAT-CLAIM"] == "pending"
    assert rep["candidate_pool"]["n_admitted"] <= rep["tier_report"]["config_cap"] or rep["tier_report"]["config_cap"] == 0
    st = led.read()
    assert set(st["stage"].unique()) >= {"search", "selection"} and (st["status"] == "reserved").sum() == 0   # 全部留终态
    assert len(rep["folds"]) == rep["n_folds"] >= 2 and rep["theta"] is not None
    for f in rep["folds"]:
        assert f["K"] >= 0 and "loss" in f and f["selection"]["status"] in ("ok", "insufficient")


# ---------------------------------------------------------------- run_protocol 端到端
def test_run_protocol_synthetic_end_to_end(lake_root, tmp_path):
    rep = API.run_protocol(str(FIX), str(tmp_path / "out"))
    r = json.loads((tmp_path / "out" / "report.json").read_text())
    assert r["tier"] and r["n_attempts"] > 0 and r["theta"] is not None
    assert r["claim_status"] == "descriptive_only" and r["schema_version"] == "g3-report-v0" and r["protocol_hash"]
    assert r["ledger_path"].startswith(str(lake_root)) and pathlib.Path(r["ledger_path"]).exists()    # 账本落 QUANT_LAB_DATA_ROOT
    led = pl.read_parquet(r["ledger_path"])
    assert led.height == r["n_attempts"] and (led["status"] == "reserved").sum() == 0
    assert r["data"]["n_asts"] == 6 and r["candidate_pool"]["n_submitted"] == 12 and r["candidate_pool"]["n_admitted"] <= 12
    assert set(r["final_by_block_len"]) == {"1", "3", "7"} and r["tier_report"]["K"] == r["K"]
    assert r["block_len_diagnostic"]["chosen"] == r["block_len_days"] in (1, 3, 7) and set(r["block_len_diagnostic"]["ac1"]) == {"1", "3", "7"}
    assert (tmp_path / "out" / "folds.json").exists()
    # 同配置可复现（同 seed → 同 θ）
    rep2 = API.run_protocol(str(FIX), str(tmp_path / "out2"))
    assert rep2["theta"] == rep["theta"] and rep2["protocol_hash"] == rep["protocol_hash"]
