"""Close-stop execution using synthetic one-minute bars only."""

import datetime as dt
from decimal import Decimal as D
import pytest
from pydantic import ValidationError
from quant_lab.market import contract as c
from quant_lab.market.kernel_a import simulate_a
from quant_lab.market.nautilus_adapter import simulate_b, KernelBUnavailable

BOUNDARY = dt.datetime(2024, 7, 1, tzinfo=dt.UTC)


def case(boundary=BOUNDARY, timeframe="1d", side="long", close=80, trigger="close"):
    start = boundary - dt.timedelta(minutes=2)
    stop = dict(price=90 if side == "long" else 110, trigger=trigger)
    if trigger == "close":
        stop["timeframe"] = timeframe
    plan = c.OrderPlan(
        instrument_id="BTC-PERP.BINANCE-USDT",
        side=side,
        entries=[c.Entry(kind="limit", price_lo=100, price_hi=100)],
        stop=c.Stop(**stop),
        tps=[],
        sizing=c.Sizing(mode="risk_budget"),
        expiry=c.Expiry(entry_ttl_s=600),
    )
    policy = c.resolve_policy("fixture-zero-v1")
    req = c.ExecutionRequest(
        episode_id="invented-close",
        graph_version="g",
        decision_snapshot_hash="s",
        t_dec=start,
        order_plan=plan,
        policy_version=policy.version,
        policy_hash=policy.content_hash,
        risk_budget=10,
        market_manifest="synthetic",
        horizon_end=boundary + dt.timedelta(minutes=1),
        cost_scenario="base",
        path_scenario="primary",
        seed=0,
        horizon_source="caller",
    )
    bars = [
        c.Bar(open_time=start, o=100, h=120, l=80, c=100),
        c.Bar(
            open_time=boundary - dt.timedelta(minutes=1),
            o=100,
            h=max(120, close),
            l=min(80, close),
            c=close,
        ),
        c.Bar(open_time=boundary, o=close, h=close, l=close, c=close),
    ]
    marks = [c.Bar(open_time=b.open_time, o=100, h=120, l=80, c=100) for b in bars]
    return req, c.MarketView(manifest_id="synthetic", bars_last=bars, bars_mark=marks)


def stops(result):
    return [e for e in result.canonical_events if e.kind == "stop_triggered"]


@pytest.mark.parametrize(
    "side,close", [("long", 100), ("short", 100), ("long", 90), ("short", 110)]
)
def test_wicks_and_equal_close_do_not_stop(side, close):
    result = simulate_a(*case(side=side, close=close))
    assert not stops(result) and result.censor_reason == "LABEL_RIGHT_CENSORED"


@pytest.mark.parametrize("side,close", [("long", 80), ("short", 120)])
def test_daily_close_strict_direction_next_last_and_r(side, close):
    req, market = case(side=side, close=close)
    result = simulate_a(req, market)
    (event,) = stops(result)
    assert event.ts == BOUNDARY - dt.timedelta(microseconds=1)
    assert event.trigger_basis == "close" and event.price == close
    (fill,) = [
        e for e in result.canonical_events if e.leg == "sl" and e.kind == "filled"
    ]
    assert (
        fill.ts == BOUNDARY and fill.path_step == "O" and fill.trigger_basis == "last"
    )
    assert fill.price == close and result.net_R == -2 and result.censor_reason is None
    c.check_invariants(req, result)


@pytest.mark.parametrize(
    "tf,minutes",
    [
        ("15m", 15),
        ("30m", 30),
        ("1h", 60),
        ("2h", 120),
        ("4h", 240),
        ("6h", 360),
        ("12h", 720),
        ("1d", 0),
        ("1w", 0),
    ],
)
def test_all_utc_periods_and_week_monday(tf, minutes):
    boundary = BOUNDARY + dt.timedelta(minutes=minutes)
    assert stops(simulate_a(*case(timeframe=tf, boundary=boundary)))[
        0
    ].ts == boundary - dt.timedelta(microseconds=1)
    assert not stops(
        simulate_a(*case(timeframe=tf, boundary=boundary + dt.timedelta(minutes=1)))
    )


def test_four_hour_and_week_are_utc_aligned():
    boundary = (BOUNDARY + dt.timedelta(hours=4)).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    )
    assert stops(simulate_a(*case(timeframe="4h", boundary=boundary)))[
        0
    ].ts == boundary - dt.timedelta(microseconds=1)
    assert not stops(
        simulate_a(*case(timeframe="4h", boundary=BOUNDARY + dt.timedelta(hours=2)))
    )
    assert not stops(
        simulate_a(*case(timeframe="1w", boundary=BOUNDARY + dt.timedelta(days=1)))
    )


def test_pre_entry_and_future_close_are_not_consumed():
    req, market = case(side="short", close=120)
    plan = req.order_plan.model_dump()
    plan["entries"] = [dict(kind="limit", price_lo=105, price_hi=105)]
    req = c.ExecutionRequest.model_validate({**req.model_dump(), "order_plan": plan})
    # Bad period close has no capacity; entry starts only at the next bar.
    policy = c.resolve_policy("fixture-zero-v1").model_copy(
        update={"participation": D("0.1")}
    )
    market.bars_last[0] = c.Bar(
        open_time=req.t_dec, o=100, h=100, l=100, c=100, volume=0
    )
    market.bars_last[1] = c.Bar(
        open_time=BOUNDARY - dt.timedelta(minutes=1),
        o=100,
        h=120,
        l=100,
        c=120,
        volume=0,
    )
    market.bars_last[2] = c.Bar(
        open_time=BOUNDARY, o=120, h=120, l=120, c=120, volume=80
    )
    result = simulate_a(req, market, policy)
    assert result.position_open_at == BOUNDARY and not stops(result)
    req, market = case()
    req = c.ExecutionRequest.model_validate(
        {**req.model_dump(), "horizon_end": BOUNDARY - dt.timedelta(seconds=10)}
    )
    assert not stops(simulate_a(req, market))


def test_close_trigger_cancels_pending_entry_and_tp():
    req, market = case()
    plan = req.order_plan.model_dump()
    plan["entries"] = [
        dict(kind="limit", price_lo=100, price_hi=100),
        dict(kind="limit", price_lo=95, price_hi=95),
    ]
    plan["tps"] = [dict(level=130)]
    req = c.ExecutionRequest.model_validate(
        {
            **req.model_dump(exclude={"entry_fractions", "tp_fractions"}),
            "order_plan": plan,
            "risk_budget": 30,
        }
    )
    # Zero last capacity below 95 keeps the second entry pending until trigger.
    policy = c.resolve_policy("fixture-zero-v1").model_copy(
        update={"participation": D("0.1")}
    )
    market.bars_last[0] = c.Bar(
        open_time=req.t_dec, o=100, h=100, l=100, c=100, volume=40
    )
    market.bars_last[1] = c.Bar(
        open_time=BOUNDARY - dt.timedelta(minutes=1), o=100, h=100, l=80, c=80, volume=0
    )
    market.bars_last[2] = c.Bar(open_time=BOUNDARY, o=80, h=80, l=80, c=80, volume=400)
    result = simulate_a(req, market, policy)
    (event,) = stops(result)
    for leg in ("entry", "tp"):
        assert any(
            e.kind == "cancelled" and e.leg == leg and e.ts == event.ts
            for e in result.canonical_events
        )
    assert result.position_close_at == BOUNDARY


def test_mark_stop_keeps_existing_wick_trigger_and_sequence():
    req, market = case(trigger="mark", close=100)
    result = simulate_a(req, market)
    (event,) = stops(result)
    assert event.trigger_basis == "mark" and event.price == 80
    assert event.ts == req.t_dec + dt.timedelta(seconds=40)
    (fill,) = [
        e for e in result.canonical_events if e.leg == "sl" and e.kind == "filled"
    ]
    assert fill.ts == event.ts and fill.price == 80


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(trigger="close"),
        dict(trigger="mark", timeframe="1d"),
        dict(trigger="close", timeframe="5m"),
    ],
)
def test_stop_timeframe_contract_rejects_invalid_combinations(kwargs):
    with pytest.raises((c.ContractError, ValidationError)):
        c.Stop(price=90, **kwargs)


def test_close_basis_is_only_allowed_on_stop_event():
    req, market = case()
    result = simulate_a(req, market)
    index = next(i for i, e in enumerate(result.canonical_events) if e.kind == "filled")
    result.canonical_events[index] = result.canonical_events[index].model_copy(
        update={"trigger_basis": "close"}
    )
    with pytest.raises(c.ExecutionInvariantError, match="只允许 stop_triggered"):
        c.check_invariants(req, result)


def test_b_explicitly_rejects_close():
    with pytest.raises(KernelBUnavailable, match="close stop trigger is unsupported"):
        simulate_b(*case())


def test_explicit_c_label_is_not_bar_close_evidence():
    req, market = case(close=100)
    market.last.append(
        c.PricePoint(
            ts=BOUNDARY - dt.timedelta(microseconds=1),
            price=80,
            path_step="C",
            bar_open_time=BOUNDARY - dt.timedelta(minutes=1),
        )
    )
    assert not stops(simulate_a(req, market))


def test_close_hit_requires_position_and_exact_one_minute_evidence():
    from quant_lab.market.kernel_a import KernelA

    req, market = case()
    kernel = KernelA(req, market)
    point = KernelA.expand_bar(market.bars_last[1], "primary", "long", None, D(1))[-1]
    assert not kernel.close_stop_hit(point)
    kernel.pos = D(1)
    assert kernel.close_stop_hit(point)
    for update in (
        {"path_step": "L"},
        {"bar_open_time": None},
        {"bar_open_time": point.bar_open_time + dt.timedelta(seconds=1)},
        {"ts": point.ts - dt.timedelta(seconds=1)},
    ):
        assert not kernel.close_stop_hit(point.model_copy(update=update))
    market.bars_last[1] = market.bars_last[1].model_copy(update={"interval_s": 120})
    kernel = KernelA(req, market)
    kernel.pos = D(1)
    assert not kernel.close_stop_hit(point)


def test_zero_capacity_after_trigger_waits_for_next_legal_last():
    req, market = case()
    policy = c.resolve_policy("fixture-zero-v1").model_copy(
        update={"participation": D("0.1")}
    )
    market.bars_last[0] = market.bars_last[0].model_copy(update={"volume": D(40)})
    market.bars_last[1] = market.bars_last[1].model_copy(update={"volume": D(40)})
    market.bars_last[2] = market.bars_last[2].model_copy(update={"volume": D(0)})
    next_open = BOUNDARY + dt.timedelta(minutes=1)
    req = c.ExecutionRequest.model_validate(
        {**req.model_dump(), "horizon_end": next_open + dt.timedelta(minutes=1)}
    )
    market.bars_last.append(
        c.Bar(open_time=next_open, o=70, h=70, l=70, c=70, volume=40)
    )
    market.bars_mark.append(c.Bar(open_time=next_open, o=100, h=100, l=100, c=100))
    result = simulate_a(req, market, policy)
    assert result.position_close_at == next_open and result.net_R == -3
    assert len(stops(result)) == 1


def test_kernel_build_identity_changes_from_pre_close_rules():
    from quant_lab.market.kernel_a import KERNEL_VERSION, kernel_build_id

    assert KERNEL_VERSION != "kernel-a-v0.2"
    assert kernel_build_id().startswith(KERNEL_VERSION + "+")
    close_req, market = case()
    mark_req, _ = case(trigger="mark")
    assert (
        simulate_a(close_req, market).trace_hash
        != simulate_a(mark_req, market).trace_hash
    )
