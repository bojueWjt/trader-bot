"""Fabricated close-stop text; no external data or model calls."""

import datetime as dt
from decimal import Decimal as D
import json
import pytest
from quant_lab.data.close_stop import parse_close_stop
from quant_lab.data import cx_v2, lifecycle, validate
from quant_lab.data.market_stub import fixture_registry


@pytest.mark.parametrize(
    "text,tf",
    [
        ("日线收盘低于2.630", "1d"),
        ("D close below 2.630", "1d"),
        ("1D closing below 2.630", "1d"),
        ("日K收于2.630下方", "1d"),
        ("daily close below 2.630", "1d"),
        ("SL: H4 close above 1.25", "4h"),
        ("4H收线低于2.630", "4h"),
        ("4小时收盘低于2.630", "4h"),
        ("四小时收盘低于2.630", "4h"),
        ("H1 close below 2.630", "1h"),
        ("1小时收线低于2.630", "1h"),
        ("小时线收盘低于2.630", "1h"),
        ("周线收盘低于2.630", "1w"),
        ("W close below 2.630", "1w"),
        ("weekly close above 2.630", "1w"),
        ("15m close below 2.630", "15m"),
        ("15分钟收盘低于2.630", "15m"),
        ("30m close below 2.630", "30m"),
        ("三十分钟收盘低于2.630", "30m"),
        ("2h close below 2.630", "2h"),
        ("两小时收盘低于2.630", "2h"),
        ("6h close below 2.630", "6h"),
        ("六小时收盘低于2.630", "6h"),
        ("12h close below 2.630", "12h"),
        ("十二小时收盘低于2.630", "12h"),
        ("H4 / 4H close below 2.630", "4h"),
    ],
)
def test_parse_known_close_timeframes(text, tf):
    assert parse_close_stop(text, D("2.630")) == dict(level=D("2.630"), timeframe=tf)


@pytest.mark.parametrize(
    "text,price",
    [
        ("收盘低于2.630", D("2.630")),
        ("日线跌破2.630", D("2.630")),
        ("H4 or daily close below 2.630", D("2.630")),
        ("日线和周线收盘低于2.630", D("2.630")),
        ("daily close below 2.630", None),
        ("H24 close below 2.630", D("2.630")),
        ("12h and 4H close below 2.630", D("2.630")),
        ("daily close below 2.630", D("NaN")),
        ("daily close below 2.630", D("Infinity")),
        ("daily close below 2.630", D(0)),
        ("daily close below 2.630", D(-1)),
        (None, D("2.630")),
    ],
)
def test_unmapped_conditions_remain_unsupported(text, price):
    assert parse_close_stop(text, price) is None


def canonical(condition="日线收盘低于90", price="90", side="long", chart_stop=77):
    text = "仿写 BTC 入场100做多；止损：" + condition
    action = dict(
        op="open",
        time_ref="now",
        symbol_raw="BTC",
        side=side,
        entry=dict(kind="limit", price=dict(value="100", quote="100")),
        stop=dict(
            kind="condition",
            price=None if price is None else dict(value=price, quote=price),
            condition=condition,
        ),
        tps=[],
        field_issues=[],
    )
    parsed, _ = cx_v2.parse_actions(
        cx_v2.validate_response(dict(schema_version=2, actions=[action]), text), text
    )
    item = parsed[0]
    row = dict(
        symbol_raw="BTC",
        side=side,
        entry=item.entry,
        entries=item.entries,
        stop=chart_stop,
        tps=item.tps,
        available_at=dt.datetime(2024, 7, 1, tzinfo=dt.UTC),
        time_grade="H0",
        kind="entry_proposal",
        entry_mode="price",
        checks=json.dumps(item.checks),
    )
    can, reasons, checks = validate.canonicalize_row(row, registry=fixture_registry())
    root = {**row, **can, "checks": json.dumps({**item.checks, **checks})}
    return (
        can,
        reasons,
        checks,
        lifecycle._order_plan(root, can["stop"], can["tps"], None),
    )


def test_v2_canonical_and_lifecycle_preserve_condition_over_chart():
    can, reasons, checks, plan = canonical()
    assert can["stop"] == 90 and not reasons
    assert checks["stop_trigger"] == dict(
        basis="close", timeframe="1d", condition="日线收盘低于90"
    )
    assert "mapping_issues" not in checks and checks["eligibility"]["execution"]
    assert plan["stop"] == dict(price=D(90), trigger="close", timeframe="1d")


@pytest.mark.parametrize(
    "condition,price",
    [
        ("跌破颈线就走", None),
        ("跌破90", "90"),
        ("收盘低于90", "90"),
        ("日线收盘低于90", None),
        ("H4和日线收盘低于90", "90"),
    ],
)
def test_v2_unsupported_does_not_fall_back_to_chart(condition, price):
    can, _, checks, plan = canonical(condition=condition, price=price)
    assert can["stop"] is None and plan["stop"] is None
    assert checks["mapping_issues"] == [
        dict(field="stop", reason="condition_not_supported_by_order_plan")
    ]
    assert not checks["eligibility"]["execution"]


def test_direction_uses_plan_side_instead_of_condition_words():
    can, reasons, checks, _ = canonical(condition="H4 close above 90")
    assert can["stop"] == 90 and not reasons and checks["sl_direction"]
    _, reasons, checks, _ = canonical(side="short")
    assert "INTENT_AMBIGUOUS" in reasons and not checks["eligibility"]["execution"]


def test_v2_validate_lifecycle_kernel_end_to_end():
    from quant_lab.market import contract as c
    from quant_lab.market.kernel_a import simulate_a

    can, _, _, plan = canonical()
    boundary = dt.datetime(2024, 7, 1, tzinfo=dt.UTC)
    start = boundary - dt.timedelta(minutes=1)
    policy = c.resolve_policy("fixture-zero-v1")
    req = c.ExecutionRequest(
        episode_id="fabricated-v2",
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
        c.Bar(open_time=start, o=100, h=100, l=80, c=80),
        c.Bar(open_time=boundary, o=80, h=80, l=80, c=80),
    ]
    marks = [c.Bar(open_time=b.open_time, o=100, h=100, l=100, c=100) for b in bars]
    result = simulate_a(
        req, c.MarketView(manifest_id="synthetic", bars_last=bars, bars_mark=marks)
    )
    assert (
        req.order_plan.stop.price == can["stop"]
        and result.net_R == -2
        and result.position_close_at == boundary
    )
    assert (
        next(
            e for e in result.canonical_events if e.kind == "stop_triggered"
        ).trigger_basis
        == "close"
    )


def test_condition_level_reaches_scale_gate():
    from quant_lab.data.market_stub import SyntheticMarks

    can, _, _, _ = canonical(condition="日线收盘低于1", price="1")
    at = dt.datetime(2024, 7, 1, tzinfo=dt.UTC)
    marks = SyntheticMarks({can["instrument_id"]: [(at, 100)]})
    market, reasons, _ = validate.market_check_row(
        can, t_a=at, marks=marks, t_plaus=None, channel_id=1
    )
    assert market["delta_stop"] > 1 and "UNIT_SCALE_CONFLICT" in reasons


def test_canonical_and_graph_rules_invalidate_previous_artifacts():
    assert validate.RULE_VERSION != "tg45-validate-v0.5"
    assert lifecycle.RULE_VERSION != "tg-lifecycle-v0.5"


def test_persisted_graph_through_l0_request_and_execution(tmp_path, monkeypatch):
    import polars as pl
    from test_cx_v2 import v2_lake, action, number, T0
    from quant_lab.data import api, linker
    from quant_lab.data.market_stub import SyntheticMarks, instrument_id_for
    from quant_lab.market import contract as c, l0_replay as l0

    monkeypatch.setattr(
        validate,
        "fixture_marks",
        lambda: SyntheticMarks({instrument_id_for("BTC"): [(T0, 100)]}),
    )
    condition = "日线收盘低于90"
    layout, mv, _, _, _ = v2_lake(
        tmp_path,
        [
            action(
                stop=dict(kind="condition", price=number(90), condition=condition),
                tps=[],
            )
        ],
        "仿写 BTC 现价100做多；止损：" + condition,
    )
    linker.run(layout, plan_source="llm", ingested_at=T0)
    lifecycle.run(
        layout, graph_version="invented-close", plan_source="llm", ingested_at=T0
    )
    disk = pl.read_parquet(layout.episode("invented-close"))
    assert disk["order_plan"][0]["stop"] == dict(
        price=D(90), trigger="close", timeframe="1d"
    )
    monkeypatch.setattr(api, "_layout", lambda: layout)
    monkeypatch.setattr(l0.Layout, "from_root", classmethod(lambda cls, root: layout))
    episodes = api.load_episodes("invented-close")
    assert (
        episodes.height == 1 and episodes["order_plan"][0]["stop"]["timeframe"] == "1d"
    )
    row = episodes.row(0, named=True)
    boundary = T0 + dt.timedelta(days=1)
    start = c.first_grid_point(row["t_dec"], 60)
    opens = [
        start + dt.timedelta(minutes=i)
        for i in range(int((boundary - start).total_seconds() / 60) + 1)
    ]
    bars = [
        c.Bar(
            open_time=t,
            o=100 if t < boundary else 80,
            h=100,
            l=80,
            c=80 if t >= boundary - dt.timedelta(minutes=1) else 100,
        )
        for t in opens
    ]
    marks = [c.Bar(open_time=t, o=100, h=100, l=100, c=100) for t in opens]
    market = c.MarketView(
        manifest_id="l0-active-silver", bars_last=bars, bars_mark=marks
    )
    seen = []
    batch_results = []
    real_batch = l0.simulate_batch

    def observed_batch(*args, **kwargs):
        result = real_batch(*args, **kwargs)
        batch_results.append(result)
        return result

    monkeypatch.setattr(l0, "simulate_batch", observed_batch)

    def synthetic_market(request, **kwargs):
        seen.append(request)
        return market

    # Replay now requires an as-of mark even for a quoted CMP (independent market task).
    # Supply the same synthetic mark used by canonical validation; assertions stay unchanged.
    from quant_lab.data.market_lake import LakeMarket
    synthetic_marks = SyntheticMarks({instrument_id_for("BTC"): [(T0, 100)]})
    monkeypatch.setattr(LakeMarket, "mark_at", lambda self, inst, at: synthetic_marks.mark_at(inst, at))
    monkeypatch.setattr(l0, "load_market_from_lake", synthetic_market)
    report = l0.replay(
        graph_version="invented-close",
        channel=row["channel_id"],
        out=tmp_path / "replay",
        market_lake=tmp_path / "empty-market",
        policy_version="fixture-zero-v1",
        risk_budget=D(10),
    )
    (request,) = seen
    assert (
        request.order_plan.stop.trigger == "close"
        and request.order_plan.stop.timeframe == "1d"
    )
    assert (
        report["overall"]["n_evaluable"] == 1 and report["overall"]["mean_net_R"] == -2
    )
    trade = pl.read_parquet(tmp_path / "replay/trades.parquet").row(0, named=True)
    assert trade["outcome_kind"] == "stopped" and trade["position_close_at"] == boundary
    events = batch_results[0]["canonical_events"][0]
    event = next(e for e in events if e["kind"] == "stop_triggered")
    assert event["trigger_basis"] == "close" and event["price"] == 80
    assert event["ts"] == boundary - dt.timedelta(microseconds=1)
