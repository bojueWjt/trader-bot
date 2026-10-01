"""Follower fill and holding-period coverage contract; synthetic 1m bars only."""
import datetime as dt
from decimal import Decimal as D
from types import SimpleNamespace

import polars as pl
import pytest

from quant_lab.market import contract as c, execution as x, l0_replay as l0
from quant_lab.market.kernel_a import KernelA, simulate_a
from quant_lab.market.partition_check import check_funding

T = dt.datetime(2024, 1, 1, 7, 58, tzinfo=dt.UTC)


def bar(i, o=100, h=None, l=None, close=None, volume=10000):
    return c.Bar(open_time=T + dt.timedelta(minutes=i), o=o, h=o if h is None else h,
                 l=o if l is None else l, c=o if close is None else close, volume=volume)


def case(side="long", entries=None, stop=None, tps=None, start=None, horizon=5, policy="fixture-zero-v1", qty=2):
    plan = c.OrderPlan(instrument_id="BTCUSDT-PERP.BINANCE-UM", side=side,
        entries=entries or [c.Entry(kind="market_ref", price_lo=100, price_hi=100, tif="IOC")],
        stop=c.Stop(price=(90 if side == "long" else 110) if stop is None else stop),
        tps=tps or [], sizing=c.Sizing(mode="fixed_qty", qty=qty), expiry=c.Expiry(entry_ttl_s=120))
    pol = c.resolve_policy(policy)
    req = c.ExecutionRequest(episode_id="follower", graph_version="g", decision_snapshot_hash="d", t_dec=start or T,
        order_plan=plan, policy_version=pol.version, policy_hash=pol.content_hash, risk_budget=20,
        market_manifest="synthetic", horizon_end=T+dt.timedelta(minutes=horizon), horizon_source="caller")
    return req


def view(bars, marks=None, **kwargs):
    return c.MarketView(manifest_id="synthetic", bars_last=bars, bars_mark=marks if marks is not None else bars, **kwargs)


def fills(result, leg):
    return [e for e in result.canonical_events if e.leg == leg and e.kind in c.FILL_KINDS]


@pytest.mark.parametrize("side,stop,expected", [("long",90,89),("short",110,111)])
def test_stop_intrabar_uses_stop_plus_adverse_tick(side, stop, expected):
    req = case(side=side)
    result = simulate_a(req, view([bar(0), bar(1, h=130,l=70)]))
    assert fills(result,"sl")[0].price == expected
    assert result.net_R == D("-1.1")


@pytest.mark.parametrize("side,opened", [("long",80),("short",120)])
def test_stop_gap_uses_last_open_without_slippage(side,opened):
    req = case(side=side, policy="fixture-tick-v1")
    result = simulate_a(req, view([bar(0),bar(1,o=opened)]))
    assert fills(result,"sl")[0].price == opened


def test_stop_cost_scenario_slippage_and_common_path():
    req = case()
    last = [bar(0),bar(1,h=101,l=70)] # H first
    marks = [bar(0),bar(1,h=140,l=89)] # independently L first
    kernel = KernelA(req, view(last,marks))
    points = kernel.timeline()
    assert all([p.path_step for p in m.marks] == [p.path_step for p in m.lasts] for m in points if m.marks and m.lasts)
    policy = c.resolve_policy(req.policy_version).model_copy(update={"costs":{"base":c.CostSpec(slippage_ticks=2,slippage_bps=D(10))}})
    result = KernelA(req,view(last,marks),policy).run()
    assert fills(result,"sl")[0].price == 87 # 90 - 2 - .09, adverse tick rounding


@pytest.mark.parametrize("side,limit,extreme",[("long",95,80),("short",105,120)])
def test_resting_entry_cross_fills_at_limit(side,limit,extreme):
    entry = c.Entry(kind="limit",price_lo=limit,price_hi=limit)
    bars = [bar(0),bar(1,h=max(100,extreme),l=min(100,extreme))]
    result = simulate_a(case(side=side,entries=[entry]),view(bars,[bar(0),bar(1)]))
    assert fills(result,"entry")[0].price == limit


@pytest.mark.parametrize("side,limit,opened",[("long",95,80),("short",105,120)])
def test_resting_entry_gap_fills_at_open(side,limit,opened):
    result = simulate_a(case(side=side,entries=[c.Entry(kind="limit",price_lo=limit,price_hi=limit)]),
                        view([bar(0),bar(1,o=opened)],[bar(0),bar(1)]))
    assert fills(result,"entry")[0].price == opened


@pytest.mark.parametrize("side,tp,extreme",[("long",110,130),("short",90,70)])
def test_resting_tp_cross_and_gap(side,tp,extreme):
    req = case(side=side,tps=[c.TakeProfit(level=tp,fraction=1)])
    cross = simulate_a(req,view([bar(0),bar(1,h=max(100,extreme),l=min(100,extreme))],[bar(0),bar(1)]))
    gap = simulate_a(req,view([bar(0),bar(1,o=extreme)],[bar(0),bar(1)]))
    assert fills(cross,"tp")[0].price == tp
    assert fills(gap,"tp")[0].price == extreme


@pytest.mark.parametrize("side,limit",[("long",105),("short",95)])
def test_marketable_limit_is_taker_at_current_price(side,limit):
    req = case(side=side,entries=[c.Entry(kind="limit",price_lo=limit,price_hi=limit)],policy="base-v1")
    result = simulate_a(req,view([bar(0)]))
    fill = fills(result,"entry")[0]
    assert fill.price == 100 and fill.qty == 2 and fill.fee == D(".1")


def test_marketable_tp_is_immediate_taker():
    req = case(tps=[c.TakeProfit(level=105,fraction=1)],policy="base-v1")
    result = simulate_a(req,view([bar(0,o=110)],[bar(0,o=100)]))
    fill = fills(result,"tp")[0]
    assert fill.ts == T and fill.price == 110 and fill.fee == D(".11")


def test_ioc_market_ignores_zero_minute_capacity():
    req = case(policy="base-v1")
    result = simulate_a(req,view([bar(0,volume=0),bar(1,o=80,volume=10000)]))
    assert fills(result,"entry")
    assert fills(result,"entry")[0].qty == 2 and result.fill_status == "filled"
    assert not any(e.reason == "ioc_remainder" for e in result.canonical_events)


@pytest.mark.parametrize("side,lo,expected,tp,expected_tp",[("long","95.9",95,"110.9",110),("short","104.1",105,"89.1",90)])
def test_entry_and_tp_tick_rounding_is_passive_and_inward(side,lo,expected,tp,expected_tp):
    req = case(side=side,entries=[c.Entry(kind="limit",price_lo=D(lo),price_hi=D(lo))],tps=[c.TakeProfit(level=D(tp),fraction=1)])
    kernel = KernelA(req,view([bar(0)]))
    kernel.submit_entries(T)
    assert kernel.orders["entry-0"].price == expected
    kernel.pos = kernel.sign * D(2)
    kernel.entry_qty = D(2)
    kernel.protect(T)
    assert kernel.orders["tp-0"].price == expected_tp


def test_illegal_entry_leg_does_not_reject_market_sibling():
    req = case(entries=[c.Entry(kind="limit",price_lo=95,price_hi=95,fraction=D(".5")),
                        c.Entry(kind="market_ref",price_lo=100,price_hi=100,fraction=D(".5"),tif="IOC")])
    result = simulate_a(req,view([bar(0)],rules=c.Rules(min_price=96)))
    assert fills(result,"entry")
    assert fills(result,"entry")[0].order_id == "entry-1"
    assert any(e.reason == "PRICE_FILTER" and e.order_id == "entry-0" for e in result.canonical_events)


class Marks:
    def __init__(self,price):
        self.price=D(str(price))
    def mark_at(self,instrument,at):
        return SimpleNamespace(price=self.price,reason=None)


def row(side="long",quote=100):
    return {"instrument_id":"BTC", "t_dec":T, "side":side, "order_plan":{
        "side":side,"stop":{"price":90 if side=="long" else 110},
        "entries":[{"kind":"market_ref","price_lo":quote,"price_hi":quote}]}}


@pytest.mark.parametrize("side,mark",[("long",90),("long",89),("short",110),("short",111)])
def test_plan_stale_includes_equal_stop(side,mark):
    assert l0.resolve_market_refs(row(side),Marks(mark)) == (None,"PLAN_STALE")


@pytest.mark.parametrize("mark,reason",[("102.5",None),("102.50001","PLAN_STALE_QUOTE"),("97.5",None),("97.49999","PLAN_STALE_QUOTE")])
def test_quote_quarter_r_boundary_and_mark_sizing(mark,reason):
    fixed, why = l0.resolve_market_refs(row(),Marks(mark))
    assert why == reason
    if reason is None:
        assert fixed["order_plan"]["entries"][0]["price_lo"] == D(mark)


def test_unpriced_market_and_limit_share_stale_gate():
    for kind in ("market_ref","limit"):
        r=row(quote=None if kind=="market_ref" else 100)
        r["order_plan"]["entries"][0]["kind"]=kind
        assert l0.resolve_market_refs(r,Marks(90)) == (None,"PLAN_STALE")


@pytest.mark.parametrize("close",[True,False])
def test_bar_gap_only_excludes_still_open_trade(close):
    req=case(tps=[c.TakeProfit(level=110,fraction=1)] if close else [])
    result=simulate_a(req,view([bar(0),bar(1,o=110),bar(3)],bars_complete=False))
    assert result.censor_reason == (None if close else "BAR_GAP")
    assert result.coverage_mask.bars_ok is close
    if not close:
        assert fills(result,"entry")[0].ts < T+dt.timedelta(minutes=2)
        assert not any(e.ts > T+dt.timedelta(minutes=2) for e in result.canonical_events)


def test_unfilled_waiting_period_complete_is_zero_despite_later_gap():
    req=case(entries=[c.Entry(kind="limit",price_lo=95,price_hi=95)])
    result=simulate_a(req,view([bar(0),bar(1),bar(2)],bars_complete=False))
    assert result.net_R == 0 and result.censor_reason is None


def test_evaluable_and_coverage_reason_match_summary():
    req=case(tps=[c.TakeProfit(level=110,fraction=1)])
    result=simulate_a(req,view([bar(0),bar(1,o=110)]))
    good=x.result_row(req,result)
    bad=x.result_row(req,result.model_copy(update={"coverage_mask":result.coverage_mask.model_copy(update={"bars_ok":False})}))
    assert good["evaluable"] and not bad["evaluable"] and bad["censor_reason"]=="COVERAGE_BARS_OK"
    assert l0.metrics([good,bad])["n_evaluable"]==sum(r["evaluable"] for r in [good,bad])
    assert l0.metrics([good,bad])["sum_net_R"]==float(good["net_R"])


def test_funding_cycle_switch_and_missing_actual_settlement():
    rows=[c.FundingRow(calc_time=T.replace(hour=0,minute=0)+dt.timedelta(hours=h),rate=D(".001"),interval_hours=iv)
          for h,iv in [(0,8),(8,4),(12,4),(16,4),(20,8),(28,8)]]
    assert c.missing_funding_times(rows,rows[0].calc_time,rows[-1].calc_time)==[]
    missing=c.missing_funding_times([r for r in rows if r.calc_time.hour!=12],rows[0].calc_time,rows[-1].calc_time)
    assert rows[2].calc_time in missing
    df=pl.DataFrame({"calc_time":[r.calc_time for r in rows],"funding_interval_hours":[r.interval_hours for r in rows]})
    _,qs,report=check_funding(df,pid="x",inst="BTC",period="2024-01")
    assert qs==[] and report.status=="ok"


@pytest.mark.parametrize("closed",[True,False])
def test_funding_missing_row_checked_only_while_holding(closed):
    req=case(tps=[c.TakeProfit(level=110,fraction=1)] if closed else [])
    bars=[bar(i,o=110 if i==1 and closed else 100) for i in range(5)]
    result=simulate_a(req,view(bars,funding_schedule_complete=False,funding_missing_times=[T+dt.timedelta(minutes=2)]))
    assert result.censor_reason==(None if closed else "FUNDING_SCHEDULE_GAP")


def test_minute_before_funding_has_closed_mark_and_no_position_is_not_stale():
    start=T+dt.timedelta(minutes=1,seconds=30)
    req=case(start=start,tps=[c.TakeProfit(level=110,fraction=1)])
    rows=[c.FundingRow(calc_time=T+dt.timedelta(minutes=2),rate=D(".001"),interval_hours=8)]
    result=simulate_a(req,view([bar(1),bar(2),bar(3,o=110)],funding=rows))
    assert result.censor_reason is None
    # P1 q(t-) is zero: funding requires no settlement mark and creates no spurious censor.
    assert result.funding==0


def test_gap_at_unfilled_expiry_is_outside_waiting_period():
    result=simulate_a(case(entries=[c.Entry(kind="limit",price_lo=95,price_hi=95)]),
                      view([bar(0),bar(1)],bars_complete=False))
    assert result.censor_reason is None and result.net_R == 0


def test_invalid_tp_does_not_reject_other_protection_legs():
    req=case(side="short",tps=[c.TakeProfit(level=D(".1"),fraction=D(".5")),c.TakeProfit(level=90,fraction=D(".5"))])
    result=simulate_a(req,view([bar(0),bar(1,o=90)],rules=c.Rules(min_price=2)))
    assert any(e.order_id=="tp-0" and e.reason=="PRICE_FILTER" for e in result.canonical_events)
    assert fills(result,"tp")[0].order_id=="tp-1"
    assert any(e.order_id=="sl-0" and e.kind=="accepted" for e in result.canonical_events)


def test_kernel_b_rejects_follower_research_policy():
    from quant_lab.market.nautilus_adapter import KernelBUnavailable
    with pytest.raises(KernelBUnavailable,match="follower"):
        x.simulate(case(policy="base-v1"),kernel="B",market=view([bar(0)]))


def test_funding_before_first_fill_does_not_need_a_mark():
    start=T+dt.timedelta(minutes=2)
    rows=[c.FundingRow(calc_time=start,rate=D(".001"),interval_hours=8)]
    result=simulate_a(case(start=start,tps=[c.TakeProfit(level=110,fraction=1)]),
                      view([bar(2),bar(3,o=110)],funding=rows))
    assert result.censor_reason is None and result.funding==0


def test_close_stop_recovery_still_fills_next_open():
    from tests.market.test_close_stop import case as close_case, BOUNDARY
    req, market=close_case()
    bars=list(market.bars_last)
    bars[-1]=c.Bar(open_time=BOUNDARY,o=100,h=100,l=100,c=100)
    result=simulate_a(req,market.model_copy(update={"bars_last":bars}))
    assert fills(result,"sl")[0].ts==BOUNDARY
    assert fills(result,"sl")[0].price==100


def test_limit_submitted_before_first_price_is_resting_maker():
    req=case(entries=[c.Entry(kind="limit",price_lo=95,price_hi=95)],policy="base-v1")
    # Explicit points allow an asynchronous first last; the order has already rested for 30 seconds.
    market=c.MarketView(manifest_id="synthetic",mark=[c.PricePoint(ts=T,price=100)],
                       last=[c.PricePoint(ts=T+dt.timedelta(seconds=30),price=90)])
    result=simulate_a(req,market)
    fill=fills(result,"entry")[0]
    assert fill.price==95 and fill.fee==D(".038")
