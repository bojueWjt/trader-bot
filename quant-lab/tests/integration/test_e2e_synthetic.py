"""OR-04 端到端合成冒烟（G0 集成，只读三个模块的公共 API，不改模块代码）。

链路：G1 load_episodes（合成夹具湖） → G2 build_request + simulate_batch（合成行情）
      → G3 freeze_opportunity_set + feature_snapshot + evaluate。

断言（GOAL-0 §5.1）：θ 非空、账本 ≥1 行、损耗表 ≥5 层、quarantine 可读。
本文件是 OR-04 骨架：每个接缝独立成 test，失败即定位到具体接缝，
G0 据此判 P1 缺项；**不得**为了让它变绿去改 src/quant_lab/*。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest

POLICY = "base-v1"
RISK_BUDGET = Decimal("100")
MANIFEST = "mm-e2e-0001"


# --------------------------------------------------------------- G1 接缝
@pytest.fixture(scope="module")
def episodes() -> pl.DataFrame:
    from quant_lab.data.api import load_episodes
    return load_episodes("fixture-v1")


def test_seam_g1_decision_view(episodes):
    """G1 决策视图提供 G2/G3 需要的全部契约列（research-schema §9.1）。"""
    assert episodes.height > 0, "load_episodes 返回空 —— 静默为空算 fail"
    required = ("episode_id", "graph_version", "decision_snapshot_hash", "t_dec", "order_plan",
                "instrument_id", "cluster_id", "is_tombstone", "right_censored")
    missing = [c for c in required if c not in episodes.columns]
    assert not missing, f"gold/episode 缺契约列 {missing}"
    assert episodes["t_dec"].null_count() < episodes.height, "t_dec 全空"


# --------------------------------------------------------------- G2 接缝
def _resolver(req):
    """合成行情：从 t_dec 起每分钟一个价点，由入场中值走向首个 TP（保证有成交与出场）。"""
    from quant_lab.market.contract import MarketView, PricePoint
    plan = req.order_plan
    lo = min(e.price_lo for e in plan.entries)
    hi = max(e.price_hi for e in plan.entries)
    mid = (lo + hi) / 2
    target = plan.tps[0].level if plan.tps else (mid * Decimal("1.02") if plan.side == "long" else mid * Decimal("0.98"))
    n = 120
    pts = []
    t = req.t_start or req.t_dec
    for i in range(n + 1):
        ts = t + dt.timedelta(minutes=i)
        if ts > req.horizon_end:
            break
        price = mid + (target - mid) * Decimal(i) / Decimal(n)
        pts.append(PricePoint(ts=ts, price=price))
    return MarketView(manifest_id=req.market_manifest, last=list(pts), mark=list(pts))


@pytest.fixture(scope="module")
def execution(episodes) -> pl.DataFrame:
    from quant_lab.market.contract import build_request, resolve_policy
    from quant_lab.market.execution import simulate_batch
    ph = resolve_policy(POLICY).content_hash
    reqs = []
    for row in episodes.filter(pl.col("t_dec").is_not_null()).iter_rows(named=True):
        if row.get("order_plan") is None:
            continue
        reqs.append(build_request(row, policy_version=POLICY, policy_hash=ph, risk_budget=RISK_BUDGET,
                                 market_manifest=MANIFEST))
    assert reqs, "build_request 未能从 G1 episode 构造出任何请求"
    return simulate_batch(reqs, kernel="A", resolver=_resolver, strict=False)


def test_seam_g2_build_request_is_sole_entrypoint(execution):
    """G2 侧：请求只经 build_request 构造（research-schema §9.4 裁定 A3），输出一行一 episode。"""
    assert execution.height > 0, "simulate_batch 返回空表"
    assert "policy_hash" in execution.columns, "配对键缺 policy_hash（execution-interface §5.5）"
    assert execution["policy_hash"].n_unique() == 1
    if "error" in execution.columns:
        errs = execution.filter(pl.col("error").is_not_null())
        assert errs.height == 0, f"内核报错 {errs.height} 行：{errs['error'].to_list()[:3]}"


# --------------------------------------------------------------- G3 接缝
@pytest.fixture(scope="module")
def opportunity(episodes):
    from quant_lab.research.evaluator import freeze_opportunity_set
    return freeze_opportunity_set(episodes)


def test_seam_g3_opportunity_set_frozen(opportunity):
    assert opportunity.n_clusters > 0, "机会集为空"
    assert len(opportunity.episode_ids) > 0


def test_seam_g3_pair_arms_consumes_g2_output(execution, opportunity):
    """G3 的两臂配对能直接消费 G2 simulate_batch 输出（不经 fake_execution）。"""
    from quant_lab.research.evaluator import pair_arms
    ids = [i for i in opportunity.episode_ids if i in set(execution["episode_id"].to_list())]
    pairs = pair_arms(execution.filter(pl.col("episode_id").is_in(ids)), ids)
    assert pairs.height == len(ids)
    for c in ("base_R", "cand_R", "base_censor", "cand_censor"):
        assert c in pairs.columns


def test_seam_g3_feature_snapshot_respects_t_dec(episodes):
    """feature_snapshot 只用 close_time + latency <= t_dec 的 bar（feature-snapshot §3）。"""
    from quant_lab.research import synthetic
    from quant_lab.research.ast import canonical_hash
    from quant_lab.research.features import feature_snapshot

    anchors = episodes.filter(pl.col("t_dec").is_not_null()).select("episode_id", "instrument_id", "t_dec")
    insts = tuple(anchors["instrument_id"].unique().sort().to_list())
    t0 = anchors["t_dec"].min() - dt.timedelta(days=30)
    bars = synthetic.fake_bars(insts, start=t0, n_bars=4000, interval="15m", seed=7)
    ast = {"op": "Ref", "args": [{"field": "close"}], "params": {"lag": 1}}
    # contracts/feature-snapshot.md §3 签名：feature_snapshot(asts, anchors, *, bars, ...)；无 interval 参数
    snap = feature_snapshot([ast], anchors, bars=bars)
    assert snap.height == anchors.height, "feature_snapshot 行数与 anchors 不一致"
    h = canonical_hash(ast)
    assert f"validity_{h}" in snap.columns or any(c.startswith("validity_") for c in snap.columns)


def test_e2e_theta_ledger_loss_quarantine(episodes, execution, opportunity, tmp_path):
    """OR-04 主断言：θ 非空、账本 ≥1 行（本次 run 有 completed）、损耗表 ≥5 层、quarantine 可读。

    真链路：G1 决策视图 → G2 build_request/simulate_batch（合成行情）→ G3 feature_snapshot + evaluate。
    账本用临时 lockbox（research-schema §9.8 A6 / G0 R7 裁定：测试不得读写持久 data/lockbox）。
    两臂同 policy（base-v1），θ 期望为 0 但必须非 None——"非空"是接缝断言，不是效应声明。
    """
    import glob

    from quant_lab.data.api import loss_table
    from quant_lab.research import synthetic
    from quant_lab.research.ast import canonical_hash
    from quant_lab.research.evaluator import evaluate
    from quant_lab.research.features import feature_snapshot
    from quant_lab.research.ledger import Ledger

    # --- θ：特征快照 + 评估
    ids = [i for i in opportunity.episode_ids if i in set(execution["episode_id"].to_list())]
    assert ids, "机会集与执行结果无交集"
    anchors = episodes.filter(pl.col("episode_id").is_in(ids)).select("episode_id", "instrument_id", "t_dec")
    insts = tuple(anchors["instrument_id"].unique().sort().to_list())
    t0 = anchors["t_dec"].min() - dt.timedelta(days=30)
    # bars 必须覆盖全部 anchors 的 t_dec（否则 validity=False 触发 NAN_RATE 拒评，那是夹具问题不是模块问题）
    span = anchors["t_dec"].max() - t0
    n_bars = int(span / dt.timedelta(minutes=15)) + 200
    bars = synthetic.fake_bars(insts, start=t0, n_bars=n_bars, interval="15m", seed=11)
    ast = {"op": "Ref", "args": [{"field": "close"}], "params": {"lag": 1}}
    feats = feature_snapshot([ast], anchors, bars=bars)
    h = canonical_hash(ast)

    ledger = Ledger(root=tmp_path / "lockbox", scope="or04-e2e")
    attempt = ledger.reserve(origin="human", canonical_hash=h, params={"lag": 1}, fold_id="e2e-f0",
                             visible_cutoff=anchors["t_dec"].max(), objective="theta",
                             data_manifest=str(episodes["graph_version"][0]), seed=11, stage="outer",
                             market_manifest=MANIFEST, graph_version=str(episodes["graph_version"][0]))
    res = evaluate(ast, opportunity, features=feats, rule=lambda f: pl.Series([True] * f.height),
                   execution=execution.filter(pl.col("episode_id").is_in(ids)), fold_id="e2e-f0",
                   attempt_id=attempt, ledger=ledger)
    assert res.status == "ok", f"evaluate 非 ok：status={res.status} reason={res.reason}"
    assert res.theta is not None, "θ 为空 —— 静默为空算 fail"
    assert res.n_evaluated > 0, "共同可评机会数为 0"

    # --- 账本：本次 run 至少 1 行 completed
    led = ledger.read()
    assert led.height >= 1, "账本为空"
    assert (led["status"] == "completed").sum() >= 1, f"账本无 completed 行：{led['status'].to_list()}"

    # --- 损耗表 ≥5 层
    loss = loss_table("latest")
    assert loss.height > 0 and len(loss["layer"].unique()) >= 5, "损耗表层数 < 5"

    # --- quarantine 可读
    qs = sorted(glob.glob("data/quarantine/*.parquet"))
    assert qs and all(len(pl.read_parquet(q).columns) > 0 for q in qs), "quarantine 不可读"


def test_e2e_loss_table_and_quarantine_readable():
    """OR-04 主断言的两项独立子条件（不经 G2 执行链，故不受 B8 阻断）：
    损耗表 ≥5 层、quarantine 可读。θ 与账本两项仍在主断言里等 B8 闭合。"""
    import glob

    from quant_lab.data.api import loss_table

    loss = loss_table("latest")
    assert loss.height > 0, "损耗表为空 —— 静默为空算 fail"
    layers = sorted(loss["layer"].unique().to_list())
    assert len(layers) >= 5, f"损耗表层数 {layers} < 5（research-schema §7）"

    qs = sorted(glob.glob("data/quarantine/*.parquet"))
    assert qs, "quarantine 目录无 parquet"
    for q in qs:
        d = pl.read_parquet(q)
        assert d.height >= 0 and len(d.columns) > 0, f"{q} 不可读"
