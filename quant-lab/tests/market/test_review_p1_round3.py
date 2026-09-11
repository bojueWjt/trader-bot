"""三审 S15 / S14 余项 / S16 反例回归。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import polars as pl
import pytest

from quant_lab.market import contract as c, execution as x
from quant_lab.market import nautilus_adapter as nb
from quant_lab.market import partition_check as pc
from quant_lab.market import vision as v
from quant_lab.market.kernel_a import KernelA, simulate_a
from tests.market.test_review_p1 import FIX, T0, e03, pt, rebuild
from tests.market.test_vision import JAN, REL, bar_rows, make_zip, mock_client

INST = "BTCUSDT-PERP.BINANCE-UM"


def bar(s, p=100, h=None, l=None):
    return c.Bar(open_time=T0 + dt.timedelta(seconds=s), o=D(p), h=D(h if h is not None else p), l=D(l if l is not None else p), c=D(p))


def bars_req(last_secs, mark_secs, horizon_s=300, **plan_upd):
    fx = FIX["E03"]
    plan = fx.request.order_plan.model_copy(update=plan_upd)
    req = rebuild(fx.request, order_plan=plan, horizon_end=T0 + dt.timedelta(seconds=horizon_s))
    mk = c.MarketView(manifest_id=fx.market.manifest_id, bars_last=[bar(s, 105 if s >= 120 else 100) for s in last_secs],
                      bars_mark=[bar(s) for s in mark_secs], bars_complete=False)
    return req, mk


# S15：两流取最早缺口；mark 在 T+60 缺、last 在 T+240 缺 → T+60 删失，T+120 的 TP 不得成交
def test_s15_earliest_gap_across_streams():
    req, mk = bars_req(last_secs=[0, 60, 120, 180, 300 - 60], mark_secs=[0, 120, 180, 240])
    r = simulate_a(req, mk)
    assert r.censor_reason == "BAR_GAP" and r.net_R is None and not [e for e in r.canonical_events if e.leg == "tp" and e.kind in c.FILL_KINDS]
    assert max(e.ts for e in r.canonical_events) <= T0 + dt.timedelta(seconds=60)
    # 反序：last 先缺
    req, mk = bars_req(last_secs=[0, 120, 180, 240], mark_secs=[0, 60, 120, 180, 240])
    r = simulate_a(req, mk)
    assert r.censor_reason == "BAR_GAP" and max(e.ts for e in r.canonical_events) <= T0 + dt.timedelta(seconds=60)


def test_s15_head_and_tail_gaps():
    # 首缺：首根 bar 在 T+60 而 t_start=T → 起点删失、无成交
    req, mk = bars_req(last_secs=[60, 120, 180, 240], mark_secs=[60, 120, 180, 240])
    r = simulate_a(req, mk)
    assert r.censor_reason == "BAR_GAP" and r.fill_status == "none"
    # 尾缺：bars 到 T+120 止、horizon T+300 → 缺口前合法事件保留（入场 T、TP T+120 成交后 closed），缺口在 closed 之后不再影响
    req, mk = bars_req(last_secs=[0, 60, 120], mark_secs=[0, 60, 120])
    r = simulate_a(req, mk)
    assert r.censor_reason is None and r.net_R == D(1)      # T+120 bar O=105 TP 成交并 closed，随后尾缺无关
    req, mk = bars_req(last_secs=[0, 60], mark_secs=[0, 60])   # 尾缺发生在持仓中 → T+120 删失，保留入场事件
    r = simulate_a(req, mk)
    assert r.censor_reason == "BAR_GAP" and r.fill_status == "filled" and r.net_R is None


def test_s15_quality_failure_censors_at_start_even_with_locatable_gap():
    req, mk = bars_req(last_secs=[0, 60, 180, 240], mark_secs=[0, 60, 120, 180, 240])
    mk = mk.model_copy(update={"bars_quality_ok": False})
    r = simulate_a(req, mk)
    assert r.censor_reason == "BAR_GAP" and r.fill_status == "none" and r.canonical_events == []


# S14 余项：loader 遇 source_sha256 缺列 / null 视为不可验证 → bars_quality_ok=False → 起点删失
def test_s14_loader_unverifiable_source_rows_fail_closed(lake_dir):
    lake = v.LakePaths(lake_dir)
    for rel, t in ((REL, "markPriceKlines"), (REL.replace("markPriceKlines", "klines"), "klines")):
        client, _ = mock_client({rel: make_zip(bar_rows(JAN, 31 * 1440), "x.csv")})
        v.fetch("BTCUSDT", t, "1m", "2024-01", lake=lake, client=client)
        pc.write_rules(lake, pc.rules_from_manifests(lake, INST, tick_size="1", step_size="1", min_notional="0"))
        pc.check_partition(lake, data_type=t, interval="1m", symbol="BTCUSDT", period="2024-01")
    fx = FIX["E03"]
    t_dec = dt.datetime(2024, 1, 10, 1, 0, tzinfo=dt.UTC)
    req = fx.request.model_copy(update={"t_dec": t_dec, "horizon_end": t_dec + dt.timedelta(minutes=30), "market_manifest": "lk"})
    base = x.load_market_from_lake(req, lake_root=lake_dir)
    assert base.bars_quality_ok
    day = lake.silver_dir("klines", "1m", "BTCUSDT") / "date=2024-01-10" / "part.parquet"
    df = pl.read_parquet(day)
    for variant in (df.with_columns(pl.when(pl.arange(0, df.height) == 61).then(None).otherwise(pl.col("source_sha256")).alias("source_sha256")),
                    df.drop("source_sha256")):
        v.atomic_write_parquet(day, variant)
        mk = x.load_market_from_lake(req, lake_root=lake_dir)
        assert not mk.bars_quality_ok and not mk.bars_complete
        r = simulate_a(req, mk)
        assert r.censor_reason == "BAR_GAP" and r.fill_status == "none"
    # 旧 sha 行 + 后续可定位缺口：仍起点删失（质量失败不能被时间洞覆盖）
    v.atomic_write_parquet(day, df.with_columns(pl.when(pl.arange(0, df.height) == 61).then(pl.lit("0" * 64)).otherwise(pl.col("source_sha256")).alias("source_sha256")).filter(pl.arange(0, df.height) != 80))
    mk = x.load_market_from_lake(req, lake_root=lake_dir)
    assert not mk.bars_quality_ok and simulate_a(req, mk).fill_status == "none"


# S16：B 截止后不得读取窗外 mark 计算暴露
def test_s16_b_exposure_respects_hold_cutoff():
    req, mk = e03(plan_upd={"expiry": c.Expiry(entry_ttl_s=3600, max_holding_s=10)},
                  market_upd={"mark": [pt(0, 100), pt(30, 1000), pt(60, 100)]})
    ra = simulate_a(req, mk)
    rb = nb.simulate_b(req, mk)
    assert ra.mfe_R == 0 and rb.mfe_R == 0 and ra.censor_reason == rb.censor_reason == "LABEL_RIGHT_CENSORED"
    # 未来行情改变不影响既定标签：截止后的 mark 改动，A/B trace 中的暴露与事件不变
    mk2 = mk.model_copy(update={"mark": [pt(0, 100), pt(30, 5), pt(60, 100)]})
    assert nb.simulate_b(req, mk2).mae_R == rb.mae_R == 0 and [e.kind for e in nb.simulate_b(req, mk2).canonical_events] == [e.kind for e in rb.canonical_events]
