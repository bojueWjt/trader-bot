"""review-G2-P1 二审补充反例 C1–C3 与 S14 的回归（S02/S04/S05/S07/S08/S09/S12/S13/S14）。"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from pathlib import Path

import polars as pl
import pytest

from quant_lab.market import asof, contract as c, execution as x
from quant_lab.market import nautilus_adapter as nb
from quant_lab.market import partition_check as pc
from quant_lab.market import vision as v
from quant_lab.market.kernel_a import simulate_a
from tests.market.test_review_p1 import FIX, T0, e03, pt, rebuild
from tests.market.test_vision import JAN, REL, bar_rows, make_zip, mock_client

INST = "BTCUSDT-PERP.BINANCE-UM"


# C1 S08：公共接缝 simulate 也带 multiplier
def test_c1_s08_public_simulate_multiplier():
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=D(1))}, market_upd={"rules": c.Rules(multiplier=D(2))})
    for k in ("A", "B"):
        assert x.simulate(req, kernel=k, market=mk).gross_pnl == D(10)


# C1 S04：hold_end 先于同刻/其后 funding；B 同步
def test_c1_s04_hold_end_precedes_funding_and_b_truncates():
    req, mk = e03(plan_upd={"expiry": c.Expiry(entry_ttl_s=3600, max_holding_s=10)},
                  market_upd={"funding": [c.FundingRow(calc_time=T0 + dt.timedelta(seconds=30), rate=D("0.001"), interval_hours=8)]})
    r = simulate_a(req, mk)
    assert r.censor_reason == "LABEL_RIGHT_CENSORED" and r.funding == 0 and not [e for e in r.canonical_events if e.kind == "funding"]
    assert max(e.ts for e in r.canonical_events) <= T0 + dt.timedelta(seconds=10)
    rb = nb.simulate_b(req, mk)
    assert rb.censor_reason == "LABEL_RIGHT_CENSORED" and rb.net_pnl is None and max(e.ts for e in rb.canonical_events) < T0 + dt.timedelta(seconds=10)
    # 同刻：hold_end == funding 时刻 → 先删失，不入账
    req2, mk2 = e03(plan_upd={"expiry": c.Expiry(entry_ttl_s=3600, max_holding_s=30)},
                    market_upd={"funding": [c.FundingRow(calc_time=T0 + dt.timedelta(seconds=30), rate=D("0.001"), interval_hours=8)]})
    r2 = simulate_a(req2, mk2)
    assert r2.funding == 0 and r2.censor_reason == "LABEL_RIGHT_CENSORED"


# C1 S09：同名 sequence 列
def test_c1_s09_same_name_sequence_columns():
    U = pl.Datetime("us", "UTC")
    r = pl.DataFrame({"available_at": [T0 - dt.timedelta(seconds=1), T0], "seq": [0, 1], "v": [9, None]}, schema_overrides={"available_at": U, "v": pl.Int64})
    l = pl.DataFrame({"t_dec": [T0], "seq": [2]}, schema_overrides={"t_dec": U})
    out = asof.asof_join(l, r, strategy="le_with_sequence", sequence_cols=("seq", "seq"))
    assert out["v"][0] is None and out["asof_matched_at"][0] == T0
    with pytest.raises(ValueError):
        asof.asof_join(l.with_columns(pl.lit("x").alias("asof_reason")), r)


# C1 S13：closed 当刻兄弟腿终态；数量精度；step 量化
def test_c1_s13_closed_causality_precision_and_step():
    f = FIX["E03"]
    r = simulate_a(f.request, f.market)
    ev = list(r.canonical_events)
    i = next(i for i, e in enumerate(ev) if e.kind == "cancelled")
    ev.append(ev.pop(i))
    ev = [e.model_copy(update={"seq": k}) for k, e in enumerate(ev)]
    with pytest.raises(c.ExecutionInvariantError, match="兄弟腿|closed 之后"):
        c.check_invariants(f.request, r.model_copy(update={"canonical_events": ev}))
    for q in ("1e-13", "1e27"):
        with pytest.raises(c.ContractError):
            c.OrderPlan.model_validate({**f.request.order_plan.model_dump(), "sizing": {"mode": "fixed_qty", "qty": q}})
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=D(1))},
                  market_upd={"last": [pt(0, 100, "0.5"), pt(60, 105)], "mark": [pt(0, 100), pt(60, 100)]})
    r = simulate_a(req, mk)
    assert all(e.qty % D(1) == 0 for e in r.canonical_events if e.kind in c.FILL_KINDS)      # step=1：容量 0.5 不产生 0.5 成交
    assert not [e for e in r.canonical_events if e.kind in c.FILL_KINDS and e.ts == T0]


# C3 S03：B 的 mark bars 也不消费启动 bar 极值
def test_c3_s03_b_mark_bars_filtered_by_t_start():
    f = FIX["E04a"]
    req = f.request.model_copy(update={"t_start": T0 + dt.timedelta(seconds=10)})
    mk = c.MarketView(manifest_id=f.market.manifest_id, last=[pt(45, 100), pt(60, 105)], mark=[pt(-1, 100), pt(60, 100)],
                      bars_mark=[c.Bar(open_time=T0, o=D(100), h=D(101), l=D(90), c=D(100))])
    for sim in (simulate_a, nb.simulate_b):
        r = sim(req, mk)
        assert not [e for e in r.canonical_events if e.kind == "stop_triggered"] and r.net_pnl == D(5)


# C3 S12：B 重验 policy_hash；登记表拦截同名改内容
def test_c3_s12_b_rejects_stale_policy_and_registry_pins_hash():
    f = FIX["E03"]
    old = c.POLICIES[f.request.policy_version]
    try:
        c.POLICIES[f.request.policy_version] = old.model_copy(update={"wallet": D(999)})
        for sim in (simulate_a, nb.simulate_b):
            with pytest.raises(c.ContractError):
                sim(f.request, f.market)
    finally:
        c.POLICIES[f.request.policy_version] = old
    reg = c.load_policy_registry()
    # B20 解除占位；B21 将声明期追溯挂在声明，而非经济内容哈希。
    assert reg == {k: p.content_hash for k, p in c.POLICIES.items()}
    meta = c.policy_registry_meta()
    assert meta["_placeholder_fields"] == []
    assert all(term in meta["_note"] for term in ("432000", "B16", "曲线 v2", "B20", "B21", "contracts.version", "changeLog", "哈希不可区分"))
    assert "最终值定稿时按 B4 bump 到 v2" not in meta["_note"]


# C3 S07：equity 刷新后的额度
def test_c3_s07_equity_based_affordability():
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=D(10)), "stop": c.Stop(price=D(1)),
                            "entries": [c.Entry(kind="limit", price_lo=D(100), price_hi=D(100), fraction=D("0.5")),
                                        c.Entry(kind="limit", price_lo=D(90), price_hi=D(90), fraction=D("0.5"))]},
                  market_upd={"last": [pt(0, 100), pt(30, 90), pt(60, 105)], "mark": [pt(0, 100), pt(30, 20), pt(60, 100)]})
    r = simulate_a(req, mk)
    fills = [(e.order_id, e.qty) for e in r.canonical_events if e.kind in c.FILL_KINDS and e.leg == "entry"]
    assert fills[0] == ("entry-0", D(5))
    assert all(q <= D(1) for oid, q in fills if oid == "entry-1")      # equity 600 − 已用 500 → 只剩 ≈100 额度 → 最多 1@90


# C2 S02/S05：loader 缺 funding 文件 / 非 TRADING / 未知 min_notional 不放行
def test_c2_loader_missing_funding_and_unknown_rules(lake_dir):
    lake = v.LakePaths(lake_dir)
    day = dt.datetime(2024, 1, 1, 7, 0, tzinfo=dt.UTC)
    rows = bar_rows(JAN, 31 * 1440)
    for rel in (REL, REL.replace("markPriceKlines", "klines")):
        client, _ = mock_client({rel: make_zip(rows, "x.csv")})
        v.fetch("BTCUSDT", "markPriceKlines" if "markPrice" in rel else "klines", "1m", "2024-01", lake=lake, client=client)
    pc.write_rules(lake, pc.rules_from_manifests(lake, INST, tick_size="1", step_size="1", min_notional="0"))
    for t in ("markPriceKlines", "klines"):
        pc.check_partition(lake, data_type=t, interval="1m", symbol="BTCUSDT", period="2024-01")
    f = FIX["E05"]
    req = f.request.model_copy(update={"t_dec": day + dt.timedelta(minutes=59), "horizon_end": day + dt.timedelta(minutes=120), "market_manifest": "lk"})
    mk = x.load_market_from_lake(req, lake_root=lake_dir)
    assert mk.bars_complete and not mk.funding_schedule_complete and any("结算行" in n for n in mk.quality_notes)
    r = simulate_a(req, mk)
    assert r.censor_reason == "FUNDING_SCHEDULE_GAP" and r.net_pnl is None
    # 规则 status=BREAK / min_notional 未知 → rules_known False
    for upd in ({"status": "BREAK"}, {"min_notional": None}):
        rules = pc.rules_from_manifests(lake, INST, tick_size="1", step_size="1", min_notional="0").with_columns(**{k: pl.lit(val) for k, val in upd.items()})
        pc.write_rules(lake, rules.cast(pc.RULES_SCHEMA))
        mk2 = x.load_market_from_lake(req, lake_root=lake_dir)
        assert not mk2.rules_known and simulate_a(req, mk2).censor_reason == "RULE_HISTORY_MISSING"


# S14：源包修订后消失的日分区失效；loader 逐行源版本核对
def test_s14_revised_source_supersedes_old_silver_days(lake_dir):
    lake = v.LakePaths(lake_dir)
    rows = bar_rows(JAN, 1)
    client, _ = mock_client({REL: make_zip(rows, "x.csv")})
    m1 = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    day = lake.silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-01-01"
    assert (day / "part.parquet").exists()
    bad = list(rows[0]); bad[4] = 999.0
    client2, _ = mock_client({REL: make_zip(rows + [bad], "x.csv")})
    m2 = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client2)
    assert m2.days == [] and m2.superseded_days == ["2024-01-01"] and not (day / "part.parquet").exists()
    assert list(day.glob("part.*.superseded.parquet"))                  # 旧行作历史保留，不再是可执行 silver
    assert pc.load_partition(lake, "markPriceKlines", "1m", "BTCUSDT", "2024-01").height == 0
