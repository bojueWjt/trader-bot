"""M-09：执行接口——simulate / simulate_batch DataFrame（配对键、Decimal(38,12)、events 列）、行情湖装载、与 G3 EventEvaluator 对接冒烟（合成 episode）。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from quant_lab.market import contract as c
from quant_lab.market import execution as x

EP = Path(__file__).parent / "fixtures" / "episodes"
FIX = c.load_fixtures(EP)
MARKETS = {f.market.manifest_id: f.market for f in FIX}
T0 = dt.datetime(2024, 1, 1, 1, 0, tzinfo=dt.UTC)


def test_simulate_requires_market_and_checks_manifest():
    f = FIX[0]
    with pytest.raises(c.ContractError):
        x.simulate(f.request)
    with pytest.raises(c.ContractError):
        x.simulate(f.request, market=FIX[1].market)
    r = x.simulate(f.request, market=f.market)
    assert r.kernel == "A"
    rb = x.simulate(f.request, kernel="B", market=f.market)
    assert rb.kernel == "B" and rb.net_R == r.net_R
    assert x.simulate(f.request, resolver=lambda req: MARKETS[req.market_manifest]).trace_hash == r.trace_hash


def test_simulate_batch_schema_and_pairing():
    df = x.simulate_batch([f.request for f in FIX], markets=MARKETS)
    assert df.height == len(FIX)
    for col in c.PAIR_KEY + c.REQUEST_ID_COLS + c.RESULT_SCALAR_COLS + ("risk_budget", "canonical_events", "mark_ok", "liquidation_unmodeled"):
        assert col in df.columns, col
    assert df.schema["net_R"] == pl.Decimal(38, 12) and df.schema["fees"] == pl.Decimal(38, 12)
    assert df.schema["t_dec"] == pl.Datetime("us", "UTC") and isinstance(df.schema["canonical_events"], pl.List)
    assert not df.select(list(c.PAIR_KEY)).is_duplicated().any()
    # null/0 区分：删失 → net_R null；未成交 → 0
    r = df.filter(pl.col("episode_id") == "E15a")
    assert r["censor_reason"][0] == "LABEL_RIGHT_CENSORED" and r["net_R"][0] is None and r["gross_pnl"][0] == 0
    r = df.filter(pl.col("episode_id") == "E09")
    assert r["fill_status"][0] == "none" and r["net_R"][0] == 0 and r["position_open_at"][0] is None
    # events 列可展开回放（seq 连续）
    ev = df.filter(pl.col("episode_id") == "E01").select(pl.col("canonical_events").explode()).unnest("canonical_events")
    assert ev["seq"].to_list() == list(range(ev.height)) and ev["kind"][-1] == "closed"
    # 同键重复 → 拒绝
    with pytest.raises(c.ContractError):
        x.simulate_batch([FIX[0].request, FIX[0].request], markets=MARKETS)


def test_simulate_batch_non_strict_keeps_error_rows():
    bad = FIX[0].request.model_copy(update={"market_manifest": "missing"})
    df = x.simulate_batch([FIX[0].request, bad], markets=MARKETS, strict=False)
    assert df.height == 2 and "error" in df.columns and df.filter(pl.col("error").is_not_null()).height == 1
    with pytest.raises(c.ContractError):
        x.simulate_batch([bad], markets=MARKETS)


# ---------------- 与 G3 EventEvaluator 冒烟（合成 episode） ----------------
def theta_from_execution(df: pl.DataFrame, take: dict[str, bool], weights: dict[str, float]) -> tuple[float, int]:
    """契约 feature-snapshot §4：θ = Σ w_i (R_cand,i − R_base,i) / Σ w_i；候选 skip → 0；删失/证据缺失排除分母。"""
    rows = df.filter(pl.col("censor_reason").is_null())
    num = den = 0.0
    for eid, r in zip(rows["episode_id"], rows["net_R"]):
        w = weights.get(eid, 1.0)
        rb = float(r)
        rc = rb if take.get(eid, True) else 0.0
        num += w * (rc - rb)
        den += w
    return (num / den if den else float("nan")), rows.height


def test_g3_pairing_smoke_on_synthetic_episodes():
    df = x.simulate_batch([f.request for f in FIX], markets=MARKETS)
    n_valid = df.filter(pl.col("censor_reason").is_null()).height
    assert n_valid >= 15 and n_valid < df.height                                   # 有删失样本被排除
    weights = {eid: 1.0 for eid in df["episode_id"]}
    # 同一 take 策略 → θ=0
    theta, n = theta_from_execution(df, {}, weights)
    assert theta == 0 and n == n_valid
    # 全 skip → θ = −mean(R_base)
    theta_skip, _ = theta_from_execution(df, {eid: False for eid in df["episode_id"]}, weights)
    base_mean = float(df.filter(pl.col("censor_reason").is_null())["net_R"].cast(pl.Float64).mean())
    assert abs(theta_skip + base_mean) < 1e-9
    # 只 skip 亏损样本 → θ > 0，且分母不缩（n 不变）
    losers = {eid: False for eid, r in zip(df["episode_id"], df["net_R"]) if r is not None and r < 0}
    theta_l, n_l = theta_from_execution(df, losers, weights)
    assert theta_l > 0 and n_l == n_valid


def test_g3_evaluate_consumes_frozen_contract_output():
    """G3 EventEvaluator（R-06）直接消费 simulate_batch 输出：两臂同 policy → θ=0；删失样本排除并计损耗；契约不变量在 G3 侧校验通过。"""
    try:
        from quant_lab.research.ast import canonical_hash
        from quant_lab.research.evaluator import OpportunitySet, evaluate, pair_arms
    except ImportError:
        pytest.skip("G3 evaluator / ast 不可导入（R-06 未落地）")
    df = x.simulate_batch([f.request for f in FIX], markets=MARKETS).filter(pl.col("policy_version") == "fixture-zero-v1")   # G3 两臂按 policy 区分，冒烟用单臂
    ids = sorted(df["episode_id"].to_list())
    clusters = {e: f"c{i % 5}" for i, e in enumerate(ids)}
    # 直接构造冻结机会集（G3 freeze_opportunity_set 当前在 select 后引用 t_dec 会抛 ColumnNotFound，已在看板告知 G3）
    opp = OpportunitySet(
        episode_ids=ids,
        eligibility=pl.DataFrame({"episode_id": ids, "eligible": [True] * len(ids), "reason": [None] * len(ids)}, schema_overrides={"reason": pl.Utf8}),
        weights=pl.DataFrame({"episode_id": ids, "cluster_id": [clusters[e] for e in ids], "weight": [1.0 / sum(1 for k in ids if clusters[k] == clusters[e]) for e in ids]}),
    )
    ast = {"field": "close"}
    h = canonical_hash(ast)
    feats = pl.DataFrame({"episode_id": list(df["episode_id"]), "t_dec": list(df["t_dec"]), f"f_{h}": [1.0] * df.height, f"validity_{h}": [True] * df.height})
    # G3 两臂配对 + 契约校验（EvalContractError/EvalProtocolError 任一抛出即 G2 输出不合格）
    paired = pair_arms(df, ids)
    n_cens = df.filter(pl.col("censor_reason").is_not_null()).height
    assert paired.height == len(ids) and paired.filter(pl.col("base_R") != pl.col("cand_R")).height == 0
    assert paired.filter(pl.col("base_censor").is_not_null()).height == n_cens
    res = evaluate(ast, opp, features=feats, rule=lambda d: pl.Series([True] * d.height), execution=df, fold_id="f0", attempt_id="a0")
    assert res.status == "ok" and res.theta == 0 and res.n_opportunities == df.height
    # G2 侧契约：删失机会不进分母（n_censored_excluded 是 G3 内部归因计数器，不在此断言）
    assert res.n_evaluated == df.height - n_cens and res.n_censored_excluded >= 1
    # skip 亏损样本 → θ>0，分母不缩
    losers = set(df.filter(pl.col("net_R").cast(pl.Float64) < 0)["episode_id"])
    res2 = evaluate(ast, opp, features=feats, rule=lambda d: pl.Series([e not in losers for e in d["episode_id"]]), execution=df, fold_id="f0", attempt_id="a1")
    assert res2.theta > 0 and res2.n_opportunities == df.height


# ---------------- 行情湖装载（真实分区存在时） ----------------
REAL = Path("data/lake/market/silver/binance/um/klines/1m/instrument=BTCUSDT-PERP.BINANCE-UM/date=2024-01-15/part.parquet")


@pytest.mark.skipif(not REAL.exists(), reason="真实 2024-01 分区未入湖")
def test_load_market_from_lake_and_simulate_real_bars():
    t_dec = dt.datetime(2024, 1, 15, 12, 0, tzinfo=dt.UTC)
    plan = c.OrderPlan(instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
                       entries=[c.Entry(kind="market_ref", price_lo=Decimal(42000), price_hi=Decimal(42000))],
                       stop=c.Stop(price=Decimal(41000)), tps=[c.TakeProfit(level=Decimal(43000), fraction=Decimal(1))],
                       sizing=c.Sizing(mode="fixed_qty", qty=Decimal("0.01")), expiry=c.Expiry(entry_ttl_s=600))
    req = c.ExecutionRequest(episode_id="real-1", graph_version="g", decision_snapshot_hash="h", t_dec=t_dec, order_plan=plan,
                             policy_version="base-v1", policy_hash=c.resolve_policy("base-v1").content_hash, risk_budget=Decimal(10),
                             market_manifest="lake-2024-01", horizon_end=t_dec + dt.timedelta(hours=6), horizon_source="caller")
    mk = x.load_market_from_lake(req)
    assert mk.rules_known and mk.bars_complete and len(mk.bars_last) == 360 and len(mk.bars_mark) == 360 and mk.rules.tick_size == Decimal("0.1")
    r = x.simulate(req, market=mk)
    assert r.fill_status == "filled" and r.canonical_events[0].kind == "submitted"
    fills = [e for e in r.canonical_events if e.kind in ("filled", "partial_fill")]
    assert fills and (fills[0].price / Decimal("0.1")) % 1 == 0                    # 成交价落在 tick 网格
    assert r.fees > 0 and (r.censor_reason in (None, "LABEL_RIGHT_CENSORED"))
    df = x.simulate_batch([req], resolver=lambda rq: x.load_market_from_lake(rq))
    assert df.height == 1 and df["trace_hash"][0] == r.trace_hash
