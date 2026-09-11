"""四审 S05（未知 OHLC 质量）与 S17（B8 显式解析字段可绕过 policy / 丢精度）反例回归。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import polars as pl
import pytest

from quant_lab.market import contract as c, execution as x
from quant_lab.market import partition_check as pc
from quant_lab.market import vision as v
from quant_lab.market.kernel_a import simulate_a
from tests.market.test_review_p1 import FIX, T0
from tests.market.test_vision import JAN, REL, bar_rows, make_zip, mock_client

INST = "BTCUSDT-PERP.BINANCE-UM"


# S05：ohlc_valid 为 null（未知质量）必须与 false 同样 fail closed —— polars .any() 会跳过 null
def test_s05_null_ohlc_valid_fails_closed(lake_dir):
    lake = v.LakePaths(lake_dir)
    for rel, t in ((REL, "markPriceKlines"), (REL.replace("markPriceKlines", "klines"), "klines")):
        client, _ = mock_client({rel: make_zip(bar_rows(JAN, 31 * 1440), "x.csv")})
        v.fetch("BTCUSDT", t, "1m", "2024-01", lake=lake, client=client)
    pc.write_rules(lake, pc.rules_from_manifests(lake, INST, tick_size="1", step_size="1", min_notional="0"))
    for t in ("markPriceKlines", "klines"):
        pc.check_partition(lake, data_type=t, interval="1m", symbol="BTCUSDT", period="2024-01")
    fx = FIX["E03"]
    t_dec = dt.datetime(2024, 1, 10, 1, 0, tzinfo=dt.UTC)
    req = c.ExecutionRequest.model_validate({**fx.request.model_dump(exclude={"entry_fractions", "tp_fractions", "entry_ttl_s"}),
                                             "t_dec": t_dec, "horizon_end": t_dec + dt.timedelta(minutes=30), "market_manifest": "lk"})
    assert x.load_market_from_lake(req, lake_root=lake_dir).bars_quality_ok      # 基线：质量已知且通过
    day = lake.silver_dir("klines", "1m", "BTCUSDT") / "date=2024-01-10" / "part.parquet"
    df = pl.read_parquet(day)
    for label, variant in (("null", pl.when(pl.arange(0, df.height) == 61).then(None).otherwise(pl.col("ohlc_valid"))),
                           ("false", pl.when(pl.arange(0, df.height) == 61).then(False).otherwise(pl.col("ohlc_valid")))):
        v.atomic_write_parquet(day, df.with_columns(variant.alias("ohlc_valid")))
        mk = x.load_market_from_lake(req, lake_root=lake_dir)
        assert not mk.bars_quality_ok and not mk.bars_complete, label
        r = simulate_a(req, mk)
        assert r.censor_reason == "BAR_GAP" and r.net_R is None and r.fill_status == "none", label


# S17：显式 fractions 只能是 plan/policy 解析结果的记录，且逐值满足 Decimal(38,12)
def test_s17_explicit_fractions_cannot_bypass_policy_or_precision():
    plan_null = {"instrument_id": INST, "side": "long",
                 "entries": [{"kind": "limit", "price_lo": "100", "price_hi": "100"}],
                 "stop": {"price": "90"}, "tps": [{"level": "105"}, {"level": "110"}],
                 "sizing": {"mode": "fixed_qty", "qty": "2"}, "expiry": {"entry_ttl_s": 600}}
    row = {"episode_id": "s17", "graph_version": "gv", "decision_snapshot_hash": "d", "t_dec": T0, "order_plan": plan_null}
    pol = c.resolve_policy("fixture-zero-v1")
    base = c.build_request(row, policy_version="fixture-zero-v1", policy_hash=pol.content_hash, risk_budget=D(5), market_manifest="m")
    assert base.tp_fractions == (D("0.5"), D("0.5")) and base.fraction_source == "policy"
    payload = base.model_dump()
    # 同 policy_hash 下伪造第三种分配（作者留空 → 必须等于 policy 等分）
    for bogus in ((D(1),), (D("0.5"), D("0.4")), (D("0.6"), D("0.4"))):
        with pytest.raises(c.ContractError):
            c.ExecutionRequest.model_validate({**payload, "tp_fractions": bogus})
    with pytest.raises(c.ContractError, match="推导不一致|长度"):
        c.ExecutionRequest.model_validate({**payload, "entry_fractions": (D("0.999999999999"),)})
    # 超 12 位精度一律拒绝（此前被 simulate_batch 截断，trace_hash 却基于原值）
    for bad in ((D("0.5000000000001"), D("0.4999999999999")), (D("0.50000000000001"), D("0.49999999999999"))):
        with pytest.raises(c.ContractError, match="Decimal|推导不一致"):
            c.ExecutionRequest.model_validate({**payload, "tp_fractions": bad})
    # 作者显式给值时，显式字段同样必须与计划逐值相等
    given = {**plan_null, "entries": [{"kind": "limit", "price_lo": "100", "price_hi": "100", "fraction": "1"}],
             "tps": [{"level": "105", "fraction": "0.7"}, {"level": "110", "fraction": "0.3"}]}
    r_given = c.build_request({**row, "order_plan": given}, policy_version="fixture-zero-v1", policy_hash=pol.content_hash,
                              risk_budget=D(5), market_manifest="m")
    assert r_given.tp_fractions == (D("0.7"), D("0.3")) and r_given.fraction_source == "plan"
    # 只有 tps 给出、entries 留空 → 保守标 policy（§5.10 B8 第 5 条）
    half = c.build_request({**row, "order_plan": {**plan_null, "tps": given["tps"]}}, policy_version="fixture-zero-v1",
                           policy_hash=pol.content_hash, risk_budget=D(5), market_manifest="m")
    assert half.fraction_source == "policy" and half.tp_fractions == (D("0.7"), D("0.3"))
    with pytest.raises(c.ContractError, match="推导不一致"):
        c.ExecutionRequest.model_validate({**r_given.model_dump(), "tp_fractions": (D("0.5"), D("0.5"))})
    # batch 往返：输出列逐值等于参与 trace_hash 的请求值
    mk = c.MarketView(manifest_id="m", last=[c.PricePoint(ts=T0, price=D(100)), c.PricePoint(ts=T0 + dt.timedelta(seconds=60), price=D(105))],
                      mark=[c.PricePoint(ts=T0, price=D(100)), c.PricePoint(ts=T0 + dt.timedelta(seconds=60), price=D(100))])
    req = c.ExecutionRequest.model_validate({**payload, "market_manifest": "m", "horizon_end": T0 + dt.timedelta(minutes=5),
                                             "horizon_source": "caller"})   # B11：自选观察窗须显式声明
    df = x.simulate_batch([req], markets={"m": mk})
    assert [D(str(f)) for f in df["tp_fractions"][0]] == list(req.tp_fractions)
    assert [D(str(f)) for f in df["entry_fractions"][0]] == list(req.entry_fractions)
    assert df["fraction_source"][0] == "policy"
