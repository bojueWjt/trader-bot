"""ADR §13 test_funding：正负费率 × 多空、零仓、结算 mark 取 calc_time 之前最后闭合 mark、重复结算键只入账一次、陈旧 mark → MARK_STALE、t_start 前的结算不入账。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from quant_lab.market import contract as c
from quant_lab.market.kernel_a import simulate_a

TF = dt.datetime(2024, 1, 1, 7, 59, tzinfo=dt.UTC)
S8 = TF + dt.timedelta(minutes=1)
INST = "BTCUSDT-PERP.BINANCE-UM"


def req(side="long", horizon_s=3600):
    stop, tp = (Decimal(95), Decimal(105)) if side == "long" else (Decimal(105), Decimal(95))
    plan = c.OrderPlan(instrument_id=INST, side=side, entries=[c.Entry(kind="limit", price_lo=100, price_hi=100)], stop=c.Stop(price=stop),
                       tps=[c.TakeProfit(level=tp, fraction=1)], sizing=c.Sizing(mode="fixed_qty", qty=Decimal(2)), expiry=c.Expiry(entry_ttl_s=3600))
    return c.ExecutionRequest(episode_id="f", graph_version="g", decision_snapshot_hash="h", t_dec=TF, order_plan=plan, policy_version="fixture-zero-v1",
                              policy_hash=c.resolve_policy("fixture-zero-v1").content_hash, risk_budget=Decimal(10), market_manifest="m",
                              horizon_end=TF + dt.timedelta(seconds=horizon_s), horizon_source="caller")


def pt(ts, px):
    return c.PricePoint(ts=ts, price=Decimal(px))


def market(funding, marks=None, last=None):
    return c.MarketView(manifest_id="m", last=last or [pt(TF, 100), pt(TF + dt.timedelta(minutes=30), 100)],
                        mark=marks or [pt(TF, 100), pt(S8, 100), pt(TF + dt.timedelta(minutes=30), 100)],
                        funding=[c.FundingRow(calc_time=t, rate=Decimal(r), interval_hours=h) for t, r, h in funding])


def funding_events(r):
    return [e for e in r.canonical_events if e.kind == "funding"]


@pytest.mark.parametrize("side,rate,expected", [("long", "0.001", "-0.2"), ("long", "-0.001", "0.2"), ("short", "0.001", "0.2"), ("short", "-0.001", "-0.2")])
def test_sign_convention_long_short_positive_negative(side, rate, expected):
    r = simulate_a(req(side), market([(S8, rate, 8)]))
    ev = funding_events(r)
    assert len(ev) == 1 and ev[0].cash_delta == Decimal(expected) and ev[0].qty == (Decimal(2) if side == "long" else Decimal(-2))
    assert r.funding == Decimal(expected) and r.censor_reason == "LABEL_RIGHT_CENSORED"   # 无出场 → 右删失，但 funding 已入账


def test_settlement_mark_is_last_closed_before_calc_time_not_after():
    marks = [pt(TF, 100), pt(TF + dt.timedelta(seconds=30), 110), pt(S8 + dt.timedelta(seconds=1), 200)]
    r = simulate_a(req(), market([(S8, "0.001", 8)], marks=marks))
    ev = funding_events(r)[0]
    assert ev.price == Decimal(110) and ev.cash_delta == Decimal("-0.22")           # 用 07:59:30 的 110，不用 08:00:01 的 200


def test_duplicate_settlement_row_applied_once_and_zero_position_row_recorded():
    r = simulate_a(req(), market([(S8, "0.001", 8), (S8, "0.001", 8)]))
    assert len(funding_events(r)) == 1 and r.funding == Decimal("-0.2")             # 同键重复只入账一次
    from quant_lab.market.nautilus_adapter import simulate_b
    rb = simulate_b(req(), market([(S8, "0.001", 8), (S8, "0.001", 8)]))
    assert rb.funding == Decimal("-0.2")


def test_stale_settlement_mark_censors():
    marks = [pt(TF - dt.timedelta(minutes=5), 100)]                                  # 最后 mark 距 08:00 有 6 分钟
    mk = market([(S8, "0.001", 8)], marks=marks, last=[pt(TF, 100)])
    r = simulate_a(req(), mk)
    assert r.censor_reason == "MARK_STALE" and not r.coverage_mask.mark_ok and funding_events(r) == []


def test_settlement_before_t_start_ignored_and_variable_interval_chain():
    early = TF - dt.timedelta(hours=1)
    rows = [(early, "0.005", 8), (S8, "0.001", 8), (S8 + dt.timedelta(hours=4), "0.002", 4)]
    marks = [pt(TF, 100), pt(S8, 100), pt(S8 + dt.timedelta(hours=4), 100)]
    r = simulate_a(req(horizon_s=6 * 3600), market(rows, marks=marks, last=[pt(TF, 100)]))
    ev = funding_events(r)
    assert [e.ts for e in ev] == [S8, S8 + dt.timedelta(hours=4)] and r.funding == Decimal("-0.6")


def test_incomplete_schedule_censors_even_when_some_rows_present():
    """S02：结算表证据不完整时持仓不能得到完整标签（即使窗口内有行）：FUNDING_SCHEDULE_GAP，funding_ok=false；已入账行保留。"""
    mk = market([(S8, "0.001", 8)]).model_copy(update={"funding_schedule_complete": False})
    r = simulate_a(req(), mk)
    assert r.censor_reason == "FUNDING_SCHEDULE_GAP" and not r.coverage_mask.funding_ok and r.net_R is None
    assert r.fill_status == "filled" and funding_events(r) == []     # 入场后立即删失，不再撮合/结算


def test_conflicting_duplicate_settlement_key_rejected():
    with pytest.raises(c.ContractError, match="冲突"):
        simulate_a(req(), market([(S8, "0.001", 8), (S8, "0.002", 8)]))


def test_settlement_mark_uses_closed_bar_not_new_bar_open():
    """S02：bars 模式下结算 mark = calc_time 前最后已闭合 bar 的 close，不用结算时刻新 bar 的 O。"""
    b_prev = c.Bar(open_time=S8 - dt.timedelta(minutes=1), o=Decimal(100), h=Decimal(100), l=Decimal(100), c=Decimal(100))
    b_new = c.Bar(open_time=S8, o=Decimal(110), h=Decimal(110), l=Decimal(110), c=Decimal(110))
    mk = c.MarketView(manifest_id="m", last=[pt(TF, 100)], bars_mark=[b_prev, b_new], funding=[c.FundingRow(calc_time=S8, rate=Decimal("0.001"), interval_hours=8)])
    r = simulate_a(req(), mk)
    ev = funding_events(r)[0]
    assert ev.price == Decimal(100) and ev.cash_delta == Decimal("-0.2")
