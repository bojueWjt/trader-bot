"""G0 R3 要求：真/假执行输出对拍——同一合成 episode 走 G2 build_request → simulate_batch 与 G3 fake_execution，
列集与 dtype 必须一致；两臂配对与 evaluate 能直接消费真实输出。请求只经 build_request 构造（§9.4 A3）。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest

from quant_lab.research import synthetic
from quant_lab.research.ast import canonical_hash
from quant_lab.research.evaluator import EvalProtocolError, OpportunitySet, evaluate, freeze_opportunity_set, pair_arms


def OpportunitySetSub(opp, ids):
    """测试用：把机会集裁到有真实执行结果的子集（G2 §5.10 落地前）。"""
    ids = list(ids)
    return OpportunitySet(ids, opp.eligibility.filter(pl.col("episode_id").is_in(ids)), opp.weights.filter(pl.col("episode_id").is_in(ids)))

POLICY = "base-v1"
MANIFEST = "mm-parity-0001"


def _resolver(req):
    """合成行情：t_start 起每分钟一个价点，从入场中值走到首个 TP（保证有成交与出场）。"""
    from quant_lab.market.contract import MarketView, PricePoint
    plan = req.order_plan
    lo = min(e.price_lo for e in plan.entries); hi = max(e.price_hi for e in plan.entries)
    mid = (lo + hi) / 2
    target = plan.tps[0].level if plan.tps else (mid * Decimal("1.02") if plan.side == "long" else mid * Decimal("0.98"))
    t = req.t_start or req.t_dec
    pts = []
    for i in range(121):
        ts = t + dt.timedelta(minutes=i)
        if ts > req.horizon_end:
            break
        pts.append(PricePoint(ts=ts, price=mid + (target - mid) * Decimal(i) / Decimal(120)))
    return MarketView(manifest_id=req.market_manifest, last=list(pts), mark=list(pts))


@pytest.fixture(scope="module")
def eps():
    bars = synthetic.fake_bars(n_bars=800, seed=3)
    return synthetic.fake_episodes(24, span_days=6, seed=3, bars=bars, censor_frac=0.0)


@pytest.fixture(scope="module")
def real(eps) -> pl.DataFrame:
    from quant_lab.market.contract import build_request, resolve_policy
    from quant_lab.market.execution import simulate_batch
    ph = resolve_policy(POLICY).content_hash
    ids = set(freeze_opportunity_set(eps).episode_ids)
    rows = [row for row in eps.iter_rows(named=True) if row["episode_id"] in ids]        # 含 fraction=null 的计划（G2 §5.10 policy 兜底）
    reqs = [build_request(row, policy_version=POLICY, policy_hash=ph, risk_budget=Decimal("100"), market_manifest=MANIFEST, seed=3) for row in rows]
    assert len(reqs) == len(rows) > 0
    return simulate_batch(reqs, kernel="A", resolver=_resolver, strict=True)


CONTRACT_PENDING_G2: set[str] = set()          # G2 已落地 §5.10（fraction_source / entry_fractions / tp_fractions）；保持为空，任何差异即漂移


def test_batch_schema_mirror_matches_contract_and_g2():
    from quant_lab.market.execution import BATCH_SCHEMA
    mine, g2 = synthetic.BATCH_SCHEMA, BATCH_SCHEMA
    assert set(g2) <= set(mine), "G2 有列而桩没有 → 桩漂移"
    extra = set(mine) - set(g2)
    assert extra <= CONTRACT_PENDING_G2, f"桩多出未在契约中的列 {extra - CONTRACT_PENDING_G2}"
    assert {k: str(mine[k]) for k in g2} == {k: str(v) for k, v in g2.items()}
    assert [c for c in mine if c in g2] == list(g2)                                    # 共同列顺序一致
    # 契约独立断言（不依赖 G2 当前代码）：§5.10 fraction_source ∈ {plan, policy}；§5.9 entry_ttl_source；§7.4 net_R 可空
    assert mine["fraction_source"] == pl.Utf8 and mine["entry_ttl_source"] == pl.Utf8 and str(mine["net_R"]) == str(synthetic.DEC)


def test_order_plan_is_decimal_and_fraction_nullable(eps):
    """§9.10.1 A8：order_plan 数值列 Decimal(38,12)；§5.10：fraction 可空且全有或全无；build_request 三腿等分和恰为 1。"""
    from decimal import Decimal
    from quant_lab.market.contract import build_request, resolve_policy
    dt_ = eps.schema["order_plan"]
    ent = dt_.fields[[f.name for f in dt_.fields].index("entries")].dtype.inner
    assert str(ent.fields[[f.name for f in ent.fields].index("price_lo")].dtype) == str(synthetic.DEC)
    assert str(ent.fields[[f.name for f in ent.fields].index("fraction")].dtype) == str(synthetic.DEC)
    plans = eps["order_plan"].to_list()
    given = [p for p in plans if p["entries"][0]["fraction"] is not None]
    absent = [p for p in plans if p["entries"][0]["fraction"] is None]
    assert given and absent
    for p in absent:
        assert all(t["fraction"] is None for t in p["tps"])                                # 全有或全无
    for p in given:
        assert sum(t["fraction"] for t in p["tps"]) == Decimal(1)


def test_fraction_nullable_via_build_request(eps):
    from decimal import Decimal
    from quant_lab.market.contract import build_request, resolve_policy
    ph = resolve_policy(POLICY).content_hash
    row = next(p for p in eps.iter_rows(named=True) if p["order_plan"]["entries"][0]["fraction"] is None)
    req = build_request(row, policy_version=POLICY, policy_hash=ph, risk_budget=Decimal("100"), market_manifest=MANIFEST)
    assert sum(req.entry_fractions) == Decimal(1) and sum(req.tp_fractions) <= Decimal(1)


def test_real_and_fake_outputs_same_columns_and_dtypes(eps, real):
    ids = freeze_opportunity_set(eps).episode_ids
    sub = eps.filter(pl.col("episode_id").is_in(ids))
    fake = synthetic.fake_execution(sub, policy_version=POLICY, seed=3, market_manifest=MANIFEST)
    assert real.height == len(ids) and "error" not in real.columns
    assert set(real["fraction_source"].unique()) == {"plan", "policy"}                   # §5.10 第 5 条两种来源各 ≥1
    from decimal import Decimal
    assert all(sum(v) == Decimal(1) for v in real["entry_fractions"].to_list())
    assert set(real.columns) <= set(fake.columns) and set(fake.columns) - set(real.columns) <= CONTRACT_PENDING_G2
    assert {k: str(v) for k, v in real.schema.items()} == {k: str(fake.schema[k]) for k in real.columns}
    assert set(fake["fraction_source"].unique()) == {"plan", "policy"}
    # net_R 判定表（§7.4）在真实内核上成立
    assert real.filter(pl.col("censor_reason").is_not_null())["net_R"].null_count() == real.filter(pl.col("censor_reason").is_not_null()).height
    assert (real.filter(pl.col("censor_reason").is_null() & (pl.col("fill_status") == "none"))["net_R"].cast(pl.Float64) == 0).all()
    assert set(real["entry_ttl_source"].unique()) <= {"plan", "policy"} and "policy" in real["entry_ttl_source"].to_list()   # 约 30% 合成 plan 无 TTL


def test_pair_arms_and_evaluate_consume_real_output(eps, real):
    opp = freeze_opportunity_set(eps)
    ids = [e for e in opp.episode_ids if e in set(real["episode_id"].to_list())]
    pairs = pair_arms(real, ids)
    assert pairs.height == len(ids) > 0
    opp = OpportunitySetSub(opp, ids)
    ast = {"op": "Ref", "args": [{"field": "close"}], "params": {"lag": 0}}
    h = canonical_hash(ast)
    feats = pl.DataFrame({"episode_id": opp.episode_ids, f"f_{h}": [1.0] * len(opp.episode_ids), f"validity_{h}": [True] * len(opp.episode_ids)})
    if False:
        pass
    r = evaluate(ast, opp, features=feats, rule=lambda f: pl.Series([True] * f.height), execution=real, fold_id="f0", attempt_id="parity")
    assert r.status == "ok" and r.theta == 0.0 and r.n_evaluated > 0


def test_freeze_rejects_null_cluster_and_inconsistent_set(eps):
    bad = eps.with_columns(pl.when(pl.int_range(pl.len()) == 0).then(None).otherwise(pl.col("cluster_id")).alias("cluster_id"))
    with pytest.raises(EvalProtocolError, match="cluster_id 含 null"):
        freeze_opportunity_set(bad)
    all_null = eps.with_columns(pl.lit(None, dtype=pl.Utf8).alias("cluster_id"))
    with pytest.raises(EvalProtocolError):
        freeze_opportunity_set(all_null)
    opp = freeze_opportunity_set(eps)
    assert len(opp.episode_ids) == opp.weights.height and opp.n_clusters > 0


def test_fake_bars_rejects_null_instrument():
    with pytest.raises(ValueError, match="instrument_id"):
        synthetic.fake_bars(["BTCUSDT-PERP.BINANCE-UM", None], n_bars=10)   # type: ignore[list-item]
