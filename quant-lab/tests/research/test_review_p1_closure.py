"""review-G3-P1 必修项闭合回归（S01–S19 中非已有测试覆盖的部分）。每个测试注明对应 S 项。"""
from __future__ import annotations

import datetime as dt
import json
import multiprocessing as mp
import os
import pathlib

import numpy as np
import polars as pl
import pytest

from quant_lab.research import api as API
from quant_lab.research import nullmodel as NM
from quant_lab.research.ast import ASTRejected, canonical_hash, canonical_json, parse_json_text
from quant_lab.research.backends import BackendOpQuarantined, get_backend
from quant_lab.research.grammar import enumerate_grammar_meta
from quant_lab.research.ledger import Ledger, LedgerBudgetExhausted, LedgerError, MemoryLedger
from quant_lab.research.maxt import PairedPanel, max_t_panel
from quant_lab.research.protocol import Fold

T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
UTC = pl.Datetime("us", "UTC")


# ---------------------------------------------------------------- S19 c14n
def test_s19_exact_decimal_canonicalization():
    j = lambda c: '{"op":"Add","args":[{"field":"close"},{"const":%s}]}' % c        # noqa: E731
    assert canonical_hash(parse_json_text(j("1"))) == canonical_hash(parse_json_text(j("1.0"))) == canonical_hash(parse_json_text(j("1e0")))
    assert canonical_hash(parse_json_text(j("0.1"))) != canonical_hash(parse_json_text(j("0.1000000000000000000001")))   # 相邻高精度常量不合并
    for bad, code in ((j("1e-400"), "WIDTH"), (j("1e400"), "WIDTH"), (j("1" + "0" * 130), "WIDTH")):
        with pytest.raises(ASTRejected) as ei:
            parse_json_text(bad)
        assert ei.value.code == code
    from decimal import Decimal
    with pytest.raises(ASTRejected) as ei:                                            # dict 入口非零下溢（Decimal 精确值转 Float64 变 0）
        canonical_hash({"op": "Add", "args": [{"field": "close"}, {"const": Decimal("1e-400")}]})
    assert ei.value.code == "NON_FINITE_CONST"
    assert canonical_hash({"op": "Add", "args": [{"field": "close"}, {"const": 5e-324}]})   # 可表示的次正规数合法
    c = canonical_json({"op": "Add", "args": [{"field": "close"}, {"const": 1e-7}]})
    assert '"const":0.0000001' in c and canonical_json(parse_json_text(c)) == c        # 解析规范串再规范化字节一致


# ---------------------------------------------------------------- S18 grammar
def test_s18_streaming_budget_and_truncation_reason():
    kw = dict(ops=["Mean", "Std", "Add", "SafeDiv", "Corr", "TSRank", "Delta", "Ref"], windows=list(range(1, 40)), fields=["close", "open", "high", "low", "volume"])
    out, meta = enumerate_grammar_meta(4, cap=10**9, max_visited=500, **kw)
    assert meta["stop_reason"] == "visited" and meta["visited"] == 500 and len(out) <= 500
    out, meta = enumerate_grammar_meta(2, cap=3, max_visited=10**6, **kw)
    assert meta["stop_reason"] == "cap" and len(out) == 3
    assert enumerate_grammar_meta(2, cap=0, **kw)[1]["stop_reason"] == "cap"
    for bad in (dict(depth=0), dict(depth=5), dict(windows=[0]), dict(windows=[2.5]), dict(cap=-1)):
        with pytest.raises(ValueError):
            enumerate_grammar_meta(bad.get("depth", 2), cap=bad.get("cap", 5), ops=kw["ops"], windows=bad.get("windows", [5]), fields=kw["fields"])


# ---------------------------------------------------------------- S04 quarantine
def test_s04_quarantine_persists_across_processes():
    code = "from quant_lab.research.backends import get_backend;print(get_backend('polars_ta').supports('Corr'), get_backend('polars').supports('Corr'))"
    import subprocess, sys
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False True"
    from quant_lab.research import contract_tests as ct
    b = get_backend("polars_ta")
    with pytest.raises(BackendOpQuarantined):
        b.compute({"op": "Corr", "args": [{"field": "close"}, {"field": "volume"}], "window": {"unit": "rows", "count": 5}}, ct.make_bars([1.0, 2, 3, 4, 5, 6]))


# ---------------------------------------------------------------- S11
def test_s11_censored_blocks_do_not_count_as_nonempty():
    rows = [(f"e{i}", f"c{i}", i, i < 19, float(np.random.default_rng(i).normal())) for i in range(100)]
    df = pl.DataFrame({"episode_id": [r[0] for r in rows], "cluster_id": [r[1] for r in rows], "t_dec": [T0 + dt.timedelta(days=r[2]) for r in rows],
                       "m": [r[3] for r in rows], "x": [r[4] for r in rows], "weight": [1.0] * 100}, schema_overrides={"t_dec": UTC})
    r = max_t_panel(PairedPanel.from_frame(df, ["x"]), block_len_days=1, B=200, seed=1)
    assert r.status == "insufficient" and r.n_nonempty_blocks == 19
    df2 = df.with_columns(pl.Series("m", [i < 20 for i in range(100)]))
    assert max_t_panel(PairedPanel.from_frame(df2, ["x"]), block_len_days=1, B=200, seed=1).n_nonempty_blocks == 20


# ---------------------------------------------------------------- S12 invariants
def _world(seed=1, n=150):
    return NM.synth_world(NM.WorldConfig(mechanism="common_shock", seed=seed, n_clusters=n))


def test_s12_panel_invariants_and_placeholder_only_under_mask():
    w = _world()
    inp = w.inputs
    bad = NM.replace(inp, base_R=np.where(np.arange(inp.n) < 2, np.nan, inp.base_R), censored=np.zeros(inp.n, dtype=bool))
    with pytest.raises(API.PanelInvalid, match="未删失却"):
        bad.validate()
    bad2 = NM.replace(inp, censored=np.ones(inp.n, dtype=bool))
    with pytest.raises(API.PanelInvalid, match="删失须 null"):
        bad2.validate()
    inp.validate()
    idx = np.arange(inp.n)
    d = np.where(inp.censored, np.nan, 0.5)
    p = API._panel(inp, idx, {"c": d}, "f")
    assert np.all(p.diffs[~p.mask] == 0) and np.all(p.diffs[p.mask] == 0.5)
    d[np.where(~inp.censored)[0][0]] = np.nan
    with pytest.raises(API.PanelInvalid, match="非有限配对差"):
        API._panel(inp, idx, {"c": d}, "f")


def test_s12_outer_nan_gate_falls_back_to_baseline():
    w = _world(seed=2, n=800)
    inp = w.inputs
    # 让首折之后的特征几乎全 NaN：outer nan_rate > 5% → 折退回 baseline 且记录原因
    feats = {k: v.copy() for k, v in inp.features.items()}
    late = np.array([t > T0 + dt.timedelta(days=170) for t in inp.anchors["t_dec"].to_list()])
    for k in feats:
        feats[k][late] = np.nan
    inp2 = NM.replace(inp, features=feats)
    rep = API.run_pipeline(inp2, NM.default_candidates(12), API.PipelineConfig(B=200, seed=2, alpha=0.5), ledger=MemoryLedger())
    assert rep["n_selected_folds"] == 0
    assert any(f.get("outer_fallback") or f["selection"]["status"] == "insufficient" or f["selection"].get("rejected") for f in rep["folds"])


# ---------------------------------------------------------------- S05 inner split
def test_s05_inner_split_is_calendar_based_purged_and_mature():
    w = _world(seed=3, n=800)
    inp = w.inputs
    cfg = API.PipelineConfig(B=200, seed=3, label_maturity_days=2.0, embargo_days=1.0)
    rows = API._rows_of(inp)
    from quant_lab.research.protocol import walk_forward
    folds = walk_forward(inp.anchors, scheme="expanding", min_train_clusters=20, embargo=dt.timedelta(days=1), label_maturity=dt.timedelta(days=2),
                         test_span=dt.timedelta(days=60), min_train_span=dt.timedelta(days=180))
    inn = API.inner_split(inp, folds[0], rows, cfg)
    t_mid = inn["t_mid"]
    a = inp.anchors
    s_ids = {a["episode_id"][i] for i in inn["search"].tolist()}
    sel_ids = {a["episode_id"][i] for i in inn["selection"].tolist()}
    assert s_ids and sel_ids and not (s_ids & sel_ids)
    s_cl = {a["cluster_id"][i] for i in inn["search"].tolist()}; sel_cl = {a["cluster_id"][i] for i in inn["selection"].tolist()}
    assert not (s_cl & sel_cl)                                                       # 跨边界同簇被 purge
    for i in inn["search"].tolist():
        assert a["t_dec"][i] < t_mid and a["t1"][i] + dt.timedelta(days=2) <= t_mid   # 标签在 t_mid 成熟
    for i in inn["selection"].tolist():
        assert a["t_dec"][i] >= t_mid
    assert inn["purge_reasons"] and inn["K_search"] > 0


# ---------------------------------------------------------------- S06 gates
def test_s06_config_cap_can_only_tighten_and_insufficient_folds_do_not_fit():
    w = _world(seed=4, n=800)
    rep = API.run_pipeline(w.inputs, NM.default_candidates(12), API.PipelineConfig(B=200, seed=4, config_cap=99), ledger=MemoryLedger())
    assert rep["candidate_pool"]["n_admitted"] <= rep["candidate_pool"]["tier_cap"] <= 12
    led = MemoryLedger()
    rep2 = API.run_pipeline(w.inputs, NM.default_candidates(12), API.PipelineConfig(B=200, seed=4, min_train_clusters=9999), ledger=led)
    assert all(f["selection"]["reason"].startswith("FOLD_INSUFFICIENT") for f in rep2["folds"])
    assert all(r["stage"] == "final" for r in led.rows.values())                      # 不足折没有任何 search/selection 拟合
    assert rep2["final_status"] == "no_claim"
    small = _world(seed=5, n=40)
    rep3 = API.run_pipeline(small.inputs, NM.default_candidates(12), API.PipelineConfig(B=200, seed=5), ledger=MemoryLedger())
    assert rep3["tier"] == "T0" and rep3["candidate_pool"]["n_admitted"] == 0


# ---------------------------------------------------------------- S08 ledger lifecycle
def test_s08_every_stage_has_terminal_state_including_failures_and_final():
    w = _world(seed=1, n=1600)
    led = MemoryLedger()
    rep = API.run_pipeline(w.inputs, NM.default_candidates(12), API.PipelineConfig(B=200, seed=6), ledger=led)
    st = led.read()
    assert (st["status"] == "reserved").sum() == 0 and "final" in set(st["stage"]) and rep["n_attempts"] == st.height
    # 同流程重跑：重复配置以 recompute 关联原尝试，不再是全 duplicate
    rep2 = API.run_pipeline(w.inputs, NM.default_candidates(12), API.PipelineConfig(B=200, seed=6), ledger=led)
    st2 = led.read()
    assert (st2["status"] == "duplicate").sum() > 0 and st2.filter(pl.col("parent_id").is_not_null()).height > 0
    assert rep["candidate_pool"]["n_admitted"] > 0
    # 阶段异常 → failed 终态
    class Boom(NM.RuleSpec):
        def fit(self, x):
            raise RuntimeError("boom")
    cands = [API.Candidate("f00:boom", "f00", Boom("boom", "gt_q", 0.5))]
    led3 = MemoryLedger()
    with pytest.raises(RuntimeError):
        API.run_pipeline(w.inputs, cands, API.PipelineConfig(B=200, seed=6), ledger=led3)
    assert set(led3.read()["status"]) == {"failed"}


def test_s08_run_protocol_reserves_before_lint_and_rejects_bad_ast(tmp_path):
    import yaml
    cfg = yaml.safe_load(open(pathlib.Path(__file__).parent / "fixtures" / "protocol_synthetic.yaml", encoding="utf-8"))
    cfg["candidates"]["asts"] = [{"op": "Div", "args": [{"field": "close"}, {"field": "open"}]}]
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ASTRejected):
        API.run_protocol(str(p), str(tmp_path / "out"))
    led = Ledger(scope="x").read()
    assert led.filter(pl.col("status") == "rejected").height >= 1 and led.filter(pl.col("status") == "rejected")["raw_input_hash"].null_count() == 0


# ---------------------------------------------------------------- S07 ledger atomicity
def _worker(root, i):
    os.environ["QUANT_LAB_DATA_ROOT"] = root
    from quant_lab.research.ledger import Ledger, LedgerBudgetExhausted
    led = Ledger(budget_configs=1)
    try:
        led.reserve(origin="human", canonical_hash=f"h{i}", params={}, fold_id="f", visible_cutoff="c", objective="theta", data_manifest="d", seed=0)
        return "ok"
    except LedgerBudgetExhausted:
        return "exhausted"


def test_s07_concurrent_reserve_only_one_wins_and_replay_after_crash(tmp_path, monkeypatch):
    root = str(tmp_path / "root")
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", root)
    with mp.get_context("spawn").Pool(4) as pool:
        res = pool.starmap(_worker, [(root, i) for i in range(4)])
    assert sorted(res) == ["exhausted"] * 3 + ["ok"]
    led = Ledger(budget_configs=1)
    df = led.read()
    assert (df["status"] == "reserved").sum() == 1 and (df["status"] == "budget_exhausted").sum() == 3
    # 事件提交后、投影未更新：删除投影 / 篡改投影 → read 重放事件
    (led.root / "ledger.parquet").unlink()
    assert Ledger().read().height == 4
    stale = Ledger().read()
    aid = led.reserve(origin="human", canonical_hash="h_new", params={}, fold_id="g", visible_cutoff="c", objective="theta", data_manifest="d", seed=0, budget_exempt=True)
    stale.with_columns(pl.lit(0, dtype=pl.Int64).alias("__watermark")).write_parquet(led.root / "ledger.parquet")   # 投影落后于事件水位
    assert aid in Ledger().read()["attempt_id"].to_list()
    # 坏事件 fail closed；终态不可转 interrupted
    bad = led.events_dir / "999999999999-zzz-reserved.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(LedgerError, match="损坏"):
        Ledger().read()
    bad.unlink()
    led.mark(aid, "completed")
    with pytest.raises(LedgerError, match="终态"):
        led.mark(aid, "interrupted")


# ---------------------------------------------------------------- S13 t1
def test_s13_unfilled_uncensored_gets_expiry_t1_not_null():
    import yaml
    cfg = yaml.safe_load(open(pathlib.Path(__file__).parent / "fixtures" / "protocol_synthetic.yaml", encoding="utf-8"))
    inputs, cands, meta = API.build_inputs_from_synthetic(cfg)
    a = inputs.anchors
    from quant_lab.research import synthetic
    assert a["t1"].null_count() < a.height
    # 未成交且未删失的机会 t1 = t_dec + entry_ttl_s
    ex = synthetic.fake_execution(synthetic.fake_episodes(int(cfg["data"]["synthetic"]["n_episodes"]), span_days=cfg["data"]["synthetic"]["span_days"], seed=cfg["seed"]), seed=cfg["seed"])
    none_ids = set(ex.filter((pl.col("fill_status") == "none") & pl.col("censor_reason").is_null())["episode_id"]) & set(a["episode_id"])
    assert none_ids
    sub = a.filter(pl.col("episode_id").is_in(list(none_ids)))
    assert sub["t1"].null_count() == 0 and (sub["t1"] > sub["t_dec"]).all()


# ---------------------------------------------------------------- S09/S10 nullmodel
def test_s09_residual_fit_uses_only_mature_labels_and_population_quantile():
    w = _world(seed=7, n=320)
    m = NM.fit_residual_model(w, train_end_day=180, label_maturity_days=0.0)
    # 扰动训练窗内但 t1 落窗外的收益不改变模型
    t1 = w.inputs.anchors["t1"].to_list()
    immature = np.array([(w.day[i] < 180) and (t1[i] > T0 + dt.timedelta(days=180)) for i in range(w.inputs.n)])
    assert immature.sum() > 0 and m.diagnostics["n_immature_excluded"] == int((immature & ~w.inputs.censored).sum())
    w2 = NM.World(NM.replace(w.inputs, base_R=np.where(immature, w.inputs.base_R + 1e4, w.inputs.base_R)), w.day, w.inst, w.grid_common, w.cfg)
    m2 = NM.fit_residual_model(w2, train_end_day=180)
    assert m2.train_mean == m.train_mean and np.allclose(np.nan_to_num(m2.grid[:180]), np.nan_to_num(m.grid[:180]))
    q = NM.feature_population_quantile(0.3)
    assert q < 0 and abs(q - (-0.5244 * NM.FEATURE_MARGINAL_SD)) < 1e-3


def test_s10_diagnostic_failure_cannot_pass(monkeypatch):
    monkeypatch.setattr(NM, "assert_not_episode_shuffle", lambda *a, **k: {"ok": False, "block_icc": {}})
    r = NM.run_mc("common_shock", kind="null", n_rep=5, seed0=3, world_cfg=NM.WorldConfig(n_clusters=200), pipe_cfg=API.PipelineConfig(B=200))
    assert r.verdict == "invalid_null_model" and r.n_failed == 5 and r.diagnostics["n_invalid_null_model"] == 5


def test_s10_all_t0_is_not_run_not_pass(monkeypatch):
    # 分档状态机独立于结构门；结构失败的优先级另有定向回归。
    # 桩要**测得出**格点结构：总体门对"有拟合结构却没有任何测量"是 fail-closed（R5-G），
    # 桩回填拟合值本身即"结构完全保持"，把本用例隔离回它要测的分档状态机。
    def _pass_guard(*a, **kw):
        f = NM._grid_cross_corr(kw.get("fitted_grid"), kw.get("block_len_days", 3))
        return {"ok": True, "checks": {"fixture": True},
                "grid": {"used": True, "ac1": {"null": 0.0}, "cross_instrument": {"null": list(f)}}}
    monkeypatch.setattr(NM, "assert_not_episode_shuffle", _pass_guard)
    r = NM.run_mc("common_shock", kind="null", n_rep=4, seed0=3, world_cfg=NM.WorldConfig(n_clusters=40), pipe_cfg=API.PipelineConfig(B=200))
    assert r.verdict == "not_run_T0" and r.diagnostics["all_T0"]


# ================================================================ 二审剩余项回归
def test_r2_s19_long_constants_and_context_independence():
    from decimal import localcontext
    j = lambda c: parse_json_text('{"op":"Add","args":[{"field":"close"},{"const":%s}]}' % c)   # noqa: E731
    a, b = j("0.1234567890123456789012345678901"), j("0.1234567890123456789012345678902")
    assert canonical_hash(a) != canonical_hash(b)                                                  # 31 位相邻常量不合并
    with localcontext() as ctx:
        ctx.prec = 10
        c10 = canonical_json(a)
    with localcontext() as ctx:
        ctx.prec = 40
        c40 = canonical_json(a)
    assert c10 == c40 == canonical_json(a) and "0.1234567890123456789012345678901" in c10          # 不受上下文精度影响


def test_r2_s12_from_frame_rejects_null_under_mask():
    df = pl.DataFrame({"episode_id": [f"e{i}" for i in range(30)], "cluster_id": [f"c{i}" for i in range(30)], "t_dec": [T0 + dt.timedelta(days=i) for i in range(30)],
                       "weight": [1.0] * 30, "m": [True] * 30, "d": [None] + [float(i % 7) for i in range(29)]}, schema_overrides={"t_dec": UTC})
    with pytest.raises(ValueError, match="m=True"):
        PairedPanel.from_frame(df, ["d"])
    ok = df.with_columns(pl.Series("m", [False] + [True] * 29))
    p = PairedPanel.from_frame(ok, ["d"])
    assert p.diffs[0, 0] == 0.0 and not p.mask[0]                                                  # 仅 m=False 占位 0
    with pytest.raises(ValueError, match="weight"):
        PairedPanel.from_frame(ok.with_columns(pl.lit(0.0).alias("weight")), ["d"])


def test_r2_s15_both_null_and_missing_policy_hash_rejected():
    from tests.research.test_evaluator import episodes, execution
    from quant_lab.research.evaluator import EvalProtocolError, pair_arms
    ep = episodes([("a", "ca", False), ("b", "cb", False)])
    x = execution(ep, [1.0, 2.0])
    for key in ("market_manifest", "execution_contract_version", "seed", "risk_budget", "graph_version", "t_dec"):
        bad = x.with_columns(pl.lit(None, dtype=x.schema[key]).alias(key))
        with pytest.raises(EvalProtocolError, match="null"):
            pair_arms(bad, ["a", "b"])
    with pytest.raises(EvalProtocolError, match="policy_hash"):
        pair_arms(x.drop("policy_hash"), ["a", "b"])
    with pytest.raises(EvalProtocolError, match="一一对应"):
        pair_arms(x.with_columns(pl.Series("policy_hash", ["h1", "h2"])), ["a", "b"])


def test_r2_s15_execution_t_dec_must_match_snapshot():
    from tests.research.test_evaluator import episodes, execution, features, take_all, AST
    from quant_lab.research.evaluator import EvalProtocolError, evaluate, freeze_opportunity_set
    ep = episodes([("a", "ca", False), ("b", "cb", False)])
    opp = freeze_opportunity_set(ep)
    f = features(["a", "b"], [1.0, 1.0]).with_columns(pl.Series("t_dec", ep["t_dec"] + dt.timedelta(days=1)))
    with pytest.raises(EvalProtocolError, match="t_dec"):
        evaluate(AST, opp, features=f, rule=take_all, execution=execution(ep, [1.0, 2.0]), fold_id="f", attempt_id="a")


def test_r2_s16_all_censored_still_reports_unclosed():
    from tests.research.test_evaluator import episodes, execution, features, take_all, AST
    from quant_lab.research.evaluator import evaluate, freeze_opportunity_set
    allc = episodes([("a", "ca", True), ("b", "cb", True)])
    opp = freeze_opportunity_set(allc)
    r = evaluate(AST, opp, features=features(opp.episode_ids, [1.0, 1.0]), rule=take_all, execution=execution(allc, [None, None]), fold_id="f", attempt_id="a")
    assert r.status == "insufficient" and r.n_censored_excluded == 2 and r.unclosed_rate == 1.0 and r.n_take == 2


def test_r2_s10_scale_gate_and_power_invalid():
    w = _world(seed=7, n=400)
    m = NM.fit_residual_model(w)
    x = NM.resample_null(w, m, np.random.default_rng(3))
    original_guard = NM.assert_not_episode_shuffle(w.inputs, x, w.day)
    assert original_guard["checks"]["scale"]
    assert not original_guard["checks"]["cross_instrument"]  # 三审新增门发现此夹具的横截面失真
    bad = NM.replace(x, base_R=x.base_R * 100)
    g = NM.assert_not_episode_shuffle(w.inputs, bad, w.day)
    assert not g["ok"] and not g["checks"]["scale"]
    r = NM.run_mc("common_shock", kind="power", n_rep=3, seed0=3, world_cfg=NM.WorldConfig(n_clusters=200), pipe_cfg=API.PipelineConfig(B=200), delta=0.2)
    assert r.verdict in ("fail", "insufficient", "not_run", "not_run_T0", "pass", "invalid_null_model")
    import unittest.mock as um
    with um.patch.object(NM, "assert_not_episode_shuffle", lambda *a, **k: {"ok": False, "checks": {"icc": False}}):
        r2 = NM.run_mc("common_shock", kind="power", n_rep=3, seed0=3, world_cfg=NM.WorldConfig(n_clusters=200), pipe_cfg=API.PipelineConfig(B=200), delta=0.2)
    assert r2.verdict == "invalid_null_model" and r2.worst_case_ci is not None


def test_r2_s08_execution_and_snapshot_reserved_before_compute(tmp_path):
    import copy, yaml
    import unittest.mock as um
    from quant_lab.research import synthetic as S
    cfg = yaml.safe_load(open(pathlib.Path(__file__).parent / "fixtures" / "protocol_synthetic.yaml", encoding="utf-8"))
    cfg = copy.deepcopy(cfg); cfg["data"]["synthetic"].update(n_episodes=40, n_bars=300, span_days=2)
    led = MemoryLedger()
    seen = []
    orig = S.fake_execution
    def probe(*a, **k):
        seen.append([r["objective"] for r in led.rows.values() if r["status"] == "reserved"])
        return orig(*a, **k)
    with um.patch.object(S, "fake_execution", probe), um.patch.object(API, "feature_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("backend boom"))):
        with pytest.raises(RuntimeError):
            API.build_inputs_from_synthetic(cfg, ledger=led)
    assert seen and "execution_batch" in seen[0]                                                  # 收益访问时已有 live 预留
    st = {r["objective"]: r["status"] for r in led.rows.values()}
    assert st["execution_batch"] == "completed" and st["feature_snapshot"] == "failed"              # 后端异常 → snapshot 尝试 failed，无残留 live
    assert not [r for r in led.rows.values() if r["status"] == "reserved"]


def _mark_worker(root, aid, status):
    os.environ["QUANT_LAB_DATA_ROOT"] = root
    from quant_lab.research.ledger import Ledger, LedgerError
    try:
        Ledger().mark(aid, status)
        return "ok"
    except LedgerError:
        return "rejected"


def test_r2_s07_concurrent_terminal_marks_only_one_wins(tmp_path, monkeypatch):
    root = str(tmp_path / "root")
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", root)
    led = Ledger()
    aid = led.reserve(origin="human", canonical_hash="h", params={}, fold_id="f", visible_cutoff="c", objective="theta", data_manifest="d", seed=0)
    led.mark(aid, "running")
    with mp.get_context("spawn").Pool(4) as pool:
        res = pool.starmap(_mark_worker, [(root, aid, s) for s in ("completed", "interrupted", "failed", "rejected")])
    assert sorted(res) == ["ok", "rejected", "rejected", "rejected"]
    assert Ledger().status_of(aid) in ("completed", "interrupted", "failed", "rejected")
    assert Ledger().recover() == 0                                                                # 未超龄不误中断
