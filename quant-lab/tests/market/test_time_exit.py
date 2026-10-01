"""到期市价平仓只属于 base-v1-timeexit；旧策略的交易结果保留，构建身份随源码变化。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from decimal import Decimal as D

import pytest

from quant_lab.market import contract as c, execution
from quant_lab.market.kernel_a import simulate_a


T0 = dt.datetime(2024, 1, 1, 1, tzinfo=dt.UTC)
POLICY = "base-v1-timeexit"


def point(seconds: int, price: str) -> c.PricePoint:
    return c.PricePoint(ts=T0 + dt.timedelta(seconds=seconds), price=D(price))


def scenario(*, tps: list[c.TakeProfit] | None = None, max_holding_s: int | None = None,
             last: list[c.PricePoint] | None = None, mark: list[c.PricePoint] | None = None,
             policy: str = POLICY, side: str = "long", cost_scenario: str = "base") -> tuple[c.ExecutionRequest, c.MarketView]:
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side=side,
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))],
        stop=c.Stop(price=D(95) if side == "long" else D(105)), tps=[] if tps is None else tps,
        sizing=c.Sizing(mode="fixed_qty", qty=D(2)),
        expiry=c.Expiry(entry_ttl_s=3600, max_holding_s=max_holding_s),
    )
    req = c.build_request(
        {"order_plan": plan, "episode_id": "time-exit", "graph_version": "g", "decision_snapshot_hash": "d", "t_dec": T0},
        policy_version=policy, policy_hash=c.resolve_policy(policy).content_hash,
        risk_budget=D(10), cost_scenario=cost_scenario, market_manifest="synthetic",
        horizon_end=T0 + dt.timedelta(seconds=120),
    )
    market = c.MarketView(
        manifest_id="synthetic", last=last if last is not None else [point(0, "100"), point(60, "102")],
        mark=mark if mark is not None else [point(0, "100"), point(60, "102")],
        rules=c.Rules(tick_size=D(1), step_size=D(1)),
    )
    return req, market


def test_no_tp_exits_at_horizon_with_realized_net_r():
    req, market = scenario()
    res = simulate_a(req, market)
    # 2 × (102−100) − entry maker 0.04 − exit taker 0.102 = 3.858
    assert res.outcome_kind == "time_exit" and res.censor_reason is None
    assert res.gross_pnl == D(4) and res.fees == D("0.142")
    assert res.net_pnl == D("3.858") and res.net_R == D("0.385800000000")
    assert res.position_close_at == req.horizon_end
    assert [(e.leg, e.price, e.qty) for e in res.canonical_events if e.kind in c.FILL_KINDS and e.leg == "close"] == [
        ("close", D(102), D(2))]
    assert res.canonical_events[-1].reason == "time_exit"
    row = execution.simulate_batch([req], markets={market.manifest_id: market}).to_dicts()[0]
    assert row["outcome_kind"] == "time_exit" and row["net_R"] == res.net_R


def test_short_direction_and_stress_slippage_use_market_exit_model():
    short_req, short_market = scenario(side="short", last=[point(0, "100"), point(60, "98")],
                                       mark=[point(0, "100"), point(60, "98")])
    short = simulate_a(short_req, short_market)
    # short: 2 × (100−98) − 0.04 maker − 0.098 taker
    assert short.outcome_kind == "time_exit" and short.exit_avg_price == D(98)
    assert short.net_R == D("0.386200000000")

    stress_req, stress_market = scenario(cost_scenario="stress")
    stress = simulate_a(stress_req, stress_market)
    # sell market: 102 − 1 tick − 2 bps, rounded down to 100; entry/exit taker fees are 0.1 each.
    assert stress.outcome_kind == "time_exit" and stress.exit_avg_price == D(100)
    assert stress.slippage == D(4) and stress.fees == D("0.2")
    assert stress.net_R == D("-0.020000000000")


def test_tp1_then_remaining_position_exits_at_horizon():
    req, market = scenario(
        tps=[c.TakeProfit(level=D(105)), c.TakeProfit(level=D(110))],
        last=[point(0, "100"), point(30, "105"), point(60, "102")],
        mark=[point(0, "100"), point(30, "100"), point(60, "102")],
    )
    res = simulate_a(req, market)
    exits = [(e.leg, e.price, e.qty) for e in res.canonical_events if e.kind in c.FILL_KINDS and e.leg in ("tp", "close")]
    assert exits == [("tp", D(105), D(1)), ("close", D(102), D(1))]
    # TP1: +5; close: +2; maker entry 0.04, TP 0.021; taker close 0.051.
    assert res.outcome_kind == "time_exit" and res.exit_legs == ("tp", "close")
    assert res.censor_reason is None and res.gross_pnl == D(7)
    assert res.fees == D("0.112") and res.net_pnl == D("6.888") and res.net_R == D("0.688800000000")


def test_bar_gap_still_censors_before_time_exit():
    req, _ = scenario()
    bars = [c.Bar(open_time=T0 + dt.timedelta(seconds=s), o=D(100), h=D(100), l=D(100), c=D(100), volume=D(100))
            for s in (0, 120)]
    market = c.MarketView(manifest_id="synthetic", bars_last=bars, bars_mark=bars,
                          rules=c.Rules(tick_size=D(1), step_size=D(1)))
    res = simulate_a(req.model_copy(update={"horizon_end": T0 + dt.timedelta(seconds=180)}), market)
    assert res.censor_reason == "BAR_GAP" and res.outcome_kind == "unevaluable"
    assert res.net_R is None and not [e for e in res.canonical_events if e.leg == "close" and e.kind in c.FILL_KINDS]


def test_missing_mark_or_funding_evidence_remains_unevaluable():
    req, market = scenario(mark=[point(0, "100")])
    stale = simulate_a(req.model_copy(update={"horizon_end": T0 + dt.timedelta(seconds=180)}), market)
    assert stale.censor_reason == "MARK_STALE" and stale.net_R is None
    missing_funding = simulate_a(req, market.model_copy(update={"funding_schedule_complete": False}))
    assert missing_funding.censor_reason == "FUNDING_SCHEDULE_GAP" and missing_funding.net_R is None


def test_base_v1_same_input_remains_right_censored():
    req, market = scenario(policy="base-v1")
    res = simulate_a(req, market)
    assert res.censor_reason == "LABEL_RIGHT_CENSORED" and res.outcome_kind == "right_censored"
    assert res.net_pnl is None and res.net_R is None and res.position_close_at is None


def test_unfilled_entry_still_expires_at_horizon():
    req, market = scenario(last=[point(0, "110"), point(60, "110")],
                           mark=[point(0, "110"), point(60, "110")])
    res = simulate_a(req, market)
    assert res.outcome_kind == "unfilled_expired" and res.net_R == 0
    assert [(e.kind, e.reason) for e in res.canonical_events if e.leg == "entry" and e.kind == "expired"] == [
        ("expired", "horizon_end")]
    assert not [e for e in res.canonical_events if e.leg == "close" and e.kind in c.FILL_KINDS]


def test_unfilled_remainder_expires_before_time_exit_close():
    req, market = scenario(last=[point(0, "100").model_copy(update={"capacity": D(1)}),
                                 point(60, "102").model_copy(update={"capacity": D(0)})])
    res = simulate_a(req, market)
    assert res.fill_status == "partial" and res.outcome_kind == "time_exit"
    expiry = next(e for e in res.canonical_events if e.kind == "expired" and e.leg == "entry")
    close = next(e for e in res.canonical_events if e.kind in c.FILL_KINDS and e.leg == "close")
    assert (expiry.qty, expiry.reason) == (D(1), "horizon_end")
    assert expiry.seq < close.seq and close.qty == D(1)


def test_max_holding_precedes_same_timestamp_price_and_funding():
    req, market = scenario(max_holding_s=90,
                           last=[point(0, "100"), point(60, "102"), point(90, "110")],
                           mark=[point(0, "100"), point(60, "102"), point(90, "110")])
    market = market.model_copy(update={"funding": [c.FundingRow(calc_time=T0 + dt.timedelta(seconds=90), rate=D("0.01"), interval_hours=8)]})
    res = simulate_a(req, market)
    assert res.outcome_kind == "time_exit" and res.position_close_at == T0 + dt.timedelta(seconds=90)
    assert res.exit_avg_price == D(102) and res.net_R == D("0.385800000000")
    assert res.funding == 0 and not [e for e in res.canonical_events if e.kind == "funding"]


def test_policy_identity_and_kernel_b_boundary():
    base, new = c.resolve_policy("base-v1"), c.resolve_policy(POLICY)
    assert base.content_hash == "12f83216444dff123f7cc6e9b4f4752449b8a678555079f033cbb9980f07d5db"
    assert new.content_hash == "0e2a374b36d286f418d350f9159e34d73e4e56bf3280251df3531273450cf424"
    assert not base.time_exit_at_horizon and new.time_exit_at_horizon
    assert "time_exit_at_horizon" not in base.model_dump()
    assert new.model_dump()["time_exit_at_horizon"] is True
    assert {k: v for k, v in base.model_dump().items() if k != "version"} == {
        k: v for k, v in new.model_dump().items() if k not in ("version", "time_exit_at_horizon")}
    req, market = scenario()
    with pytest.raises(c.ContractError, match="内核 B 尚未实现到期平仓"):
        execution.simulate(req, kernel="B", market=market)


def test_kernel_build_id_tracks_source_for_every_policy():
    """S12: the build id is the kernel source digest for legacy and time-exit policies alike, so any later kernel
    edit changes legacy trace_hash too. Legacy outcomes themselves are pinned by the episode fixture suite."""
    import hashlib
    from quant_lab.market import kernel_a
    fixture = next(f for f in c.load_fixtures("tests/market/fixtures/episodes") if f.id == "E15a")
    res = simulate_a(fixture.request, fixture.market)
    h = hashlib.sha256()
    for name in kernel_a._SRC_FILES:
        h.update((Path(kernel_a.__file__).parent / name).read_bytes())
    assert res.kernel_version.endswith(h.hexdigest()[:12]) and res.kernel_version.startswith(kernel_a.KERNEL_VERSION)
    assert kernel_a.kernel_build_id(False) == kernel_a.kernel_build_id(True)


def test_fourteen_day_sensitivity_differs_only_in_horizon():
    from quant_lab.market.contract import resolve_policy
    base, long = resolve_policy("base-v1-timeexit"), resolve_policy("base-v1-timeexit-14d")
    assert long.research_horizon_s == 14 * 86400 and long.time_exit_at_horizon
    assert long.model_dump(exclude={"version", "research_horizon_s"}) == base.model_dump(exclude={"version", "research_horizon_s"})
    assert long.content_hash != base.content_hash
