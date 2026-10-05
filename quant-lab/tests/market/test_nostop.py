"""v8 F1 stopless fixed-notional execution, kernel A v0.7, and the §4 G2 second pass (synthetic data only)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from quant_lab.market import contract as c, execution, kernel_a as a, l0_replay as l0
from quant_lab.market.nautilus_adapter import KernelBUnavailable
from quant_lab.market.partition_check import RULES_SCHEMA

T0 = datetime(2024, 1, 1, 1, tzinfo=UTC)
INST = "BTCUSDT-PERP.BINANCE-UM"
NS = "base-v1-timeexit-w60-live-follow-ns300"      # v8 主口径
NS_PLAIN = "base-v1-timeexit-w60-live-ns300"        # 不跟老师
OLD = "base-v1-timeexit-w60-live-follow"
B = D(180)
MANIFEST = "l0-active-silver"


class Marks:
    """As-of mark stub: one price for every instrument and time; records the times asked."""

    def __init__(self, price="50000"):
        self.price = None if price is None else D(price)
        self.asked = []

    def mark_at(self, inst, at):
        self.asked.append((inst, at))
        return SimpleNamespace(price=self.price, reason=None if self.price is not None else "NO_MARK")


def rules_frame(inst=INST, *, tick="0.1", step="0.001", multiplier="1", status="TRADING", min_notional="0"):
    return pl.DataFrame([{"instrument_id": inst, "effective_from": T0 - timedelta(days=1), "effective_to": None,
                          "tick_size": tick, "step_size": step, "min_notional": min_notional, "multiplier": multiplier,
                          "funding_interval_hours": 8, "status": status, "source": "synthetic"}], schema=RULES_SCHEMA)


def episode(side="long", legs=(("limit", "50000", "50000"),), *, stop=None, tps=(), inst=INST, eid="ns-1", **extra):
    entries = [{"kind": kind, "price_lo": None if lo is None else D(lo), "price_hi": None if hi is None else D(hi),
                "fraction": None, "tif": "IOC" if kind == "market_ref" else "GTC", "post_only": False} for kind, lo, hi in legs]
    plan = {"instrument_id": inst, "side": side, "entries": entries,
            "stop": None if stop is None else {"price": D(stop), "trigger": "mark", "timeframe": None},
            "tps": [{"level": D(level), "fraction": None} for level in tps],
            "sizing": {"mode": "risk_budget", "qty": None},
            "expiry": {"entry_ttl_s": None, "max_holding_s": None}, "reduce_only_exit": True}
    return {"episode_id": eid, "graph_version": "synthetic", "decision_snapshot_hash": "snap",
            "root_source_version_id": "root-1", "t_dec": T0, "instrument_id": inst, "side": side, "order_plan": plan, **extra}


def prepare(row, version=NS, *, mark="50000", rules="default", text=None, stop_text=None, budget=B):
    info = {}
    frame = rules_frame(row["instrument_id"]) if isinstance(rules, str) else rules
    req, audit, why, _, _ = l0.prepare_episode_request(
        row, marks=Marks(mark), lake=Path("/nonexistent-lake"), policy=c.resolve_policy(version), risk_budget=budget,
        text=text, rule_cache={row["instrument_id"]: frame}, stop_text=stop_text, info=info)
    return req, audit, why, info


def short_horizon(req, seconds=180):
    return c.ExecutionRequest.model_validate({**req.model_dump(), "horizon_end": req.t_dec + timedelta(seconds=seconds),
                                              "horizon_source": "caller"})


def bar(seconds, price):
    p = D(price)
    return c.Bar(open_time=T0 + timedelta(seconds=seconds), o=p, h=p, l=p, c=p, volume=D(10000))


def market(prices, *, tick=".1", step=".001", min_notional="0"):
    bars = [bar(i * 60, p) for i, p in enumerate(prices)]
    return c.MarketView(manifest_id=MANIFEST, bars_last=bars, bars_mark=bars,
                        rules=c.Rules(tick_size=D(tick), step_size=D(step), min_notional=D(min_notional)))


def events(res, kind=None, leg=None):
    return [e for e in res.canonical_events if (kind is None or e.kind == kind) and (leg is None or e.leg == leg)]


# ---------------------------------------------------------------- sizing and shape (F1 tests 1–6, 13–18)
def test_fixed_notional_btc_leg_has_no_stop_events():
    req, audit, why, info = prepare(episode())
    assert why is None and audit["status"] == "applied"
    assert req.order_plan.stop is None
    assert req.order_plan.sizing == c.Sizing(mode="fixed_qty", qty=D("0.006000000000"))
    assert info == {"sizing_basis": "nostop", "nostop_notional_U": D("1.666666666667") * B, "legs_n": 1,
                    "legs_gt3": False, "multiplier": D(1)}
    res = execution.simulate(short_horizon(req), market=market(["50100", "49990", "49000"]))
    assert [e.qty for e in events(res, "submitted", "entry")] == [D("0.006")]
    assert events(res, leg="sl") == []
    assert res.filled_qty == D("0.006") and res.outcome_kind == "time_exit"


def test_three_short_legs_each_near_300U_quantity_inverse_to_price():
    row = episode("short", (("limit", "84.5", "84.5"), ("limit", "89", "89"), ("limit", "94", "94")), inst="CLUSDT-PERP.BINANCE-UM")
    req, _, why, info = prepare(row, rules=rules_frame("CLUSDT-PERP.BINANCE-UM", tick="0.01", step="0.01"))
    assert why is None and info["legs_n"] == 3
    prices = [e.price_lo for e in req.order_plan.entries]
    assert prices == [D("84.5"), D(89), D(94)]
    notional = [f * req.order_plan.sizing.qty * p for f, p in zip(req.entry_fractions, prices)]
    assert all(abs(n - D(300)) < D("0.000001") for n in notional)
    res = execution.simulate(short_horizon(req), market=market(["80", "80", "80"], tick=".01", step=".01"))
    submitted = [e.qty * p for e, p in zip(events(res, "submitted", "entry"), prices)]
    assert all(abs(n - D(300)) < D(2) for n in submitted)      # 300U 每腿，差额只来自 0.01 步长取整


@pytest.mark.parametrize("side,near", [("long", "67000"), ("short", "66000")])
def test_zone_becomes_near_end_single_limit(side, near):
    req, _, why, _ = prepare(episode(side, (("ladder", "66000", "67000"),)))
    assert why is None
    assert [(e.kind, e.price_lo, e.price_hi) for e in req.order_plan.entries] == [("limit", D(near), D(near))]
    assert req.order_plan.sizing.qty == (D("1.666666666667") * B / D(near)).quantize(D("1e-12"), rounding="ROUND_DOWN")


def test_market_reference_leg_is_sized_at_the_decision_mark():
    req, _, why, _ = prepare(episode(legs=(("market_ref", None, None),)), mark="60000")
    assert why is None
    assert req.order_plan.entries[0].kind == "market_ref" and req.order_plan.entries[0].price_lo == D(60000)
    assert req.order_plan.sizing.qty == D("0.005000000000")


def test_whole_plan_third_splits_equally_across_legs():
    row = episode(legs=(("limit", "100", "100"), ("limit", "200", "200")))
    req, _, why, info = prepare(row, "base-v1-timeexit-w60-live-follow-ns3rd", rules=rules_frame(step="0.0001"))
    assert why is None and info["nostop_notional_U"] == D("16.666666666667") * B
    per_leg = [f * req.order_plan.sizing.qty * e.price_lo for f, e in zip(req.entry_fractions, req.order_plan.entries)]
    assert all(abs(n - D(1500)) < D("0.000001") for n in per_leg)


def test_four_legs_all_submitted_and_flagged():
    legs = tuple(("limit", p, p) for p in ("100", "99", "98", "97"))
    req, _, why, info = prepare(episode(legs=legs), mark="101", rules=rules_frame(step="0.0001", tick="0.01"))
    assert why is None and info["legs_gt3"] is True and info["legs_n"] == 4
    res = execution.simulate(short_horizon(req), market=market(["101", "101", "101"], tick=".01", step=".0001"))
    assert len(events(res, "accepted", "entry")) == 4 and not events(res, "rejected", "entry")


def test_old_policy_keeps_plan_no_stop():
    for version in (OLD, "base-v1", "base-v1-timeexit-w60-live"):
        req, _, why, info = prepare(episode(), version)
        assert req is None and why == "PLAN_NO_STOP"


def test_ns_policy_stopless_plan_requires_fixed_qty():
    plan = c.OrderPlan(instrument_id=INST, side="long", entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))],
                       sizing=c.Sizing(mode="risk_budget"), expiry=c.Expiry())
    row = dict(episode_id="x", graph_version="g", decision_snapshot_hash="s", t_dec=T0, order_plan=plan)
    with pytest.raises(c.ContractError, match="fixed_qty"):
        c.build_request(row, policy_version=NS, policy_hash=c.resolve_policy(NS).content_hash, risk_budget=B,
                        market_manifest=MANIFEST)
    fixed = plan.model_copy(update={"sizing": c.Sizing(mode="fixed_qty", qty=D(1))})
    with pytest.raises(c.ContractError, match="无止损策略"):
        c.build_request(dict(row, order_plan=fixed), policy_version=OLD, policy_hash=c.resolve_policy(OLD).content_hash,
                        risk_budget=B, market_manifest=MANIFEST)
    req = c.build_request(dict(row, order_plan=fixed), policy_version=NS, policy_hash=c.resolve_policy(NS).content_hash,
                          risk_budget=B, market_manifest=MANIFEST)
    assert req.order_plan.stop is None and c.ExecutionRequest.model_validate_json(req.model_dump_json()) == req


def test_new_policies_registered_and_only_add_their_fields():
    registry = c.load_policy_registry()
    expected = {
        "base-v1-timeexit-w14-live": ("base-v1-timeexit-w14", {"live_execution_profile": True}),
        "base-v1-timeexit-w14-live-follow": ("base-v1-timeexit-w14-live", {"follow_teacher": True}),
        NS: (OLD, {"nostop_leg_notional_k": "1.666666666667"}),
        "base-v1-timeexit-w1-live-follow-ns300": ("base-v1-timeexit-live-follow",
                                                  {"nostop_leg_notional_k": "1.666666666667", "research_horizon_s": 86400}),
        "base-v1-timeexit-live-follow-ns300": ("base-v1-timeexit-live-follow", {"nostop_leg_notional_k": "1.666666666667"}),
        "base-v1-timeexit-w14-live-follow-ns300": ("base-v1-timeexit-w14-live-follow", {"nostop_leg_notional_k": "1.666666666667"}),
        "base-v1-timeexit-w60-live-follow-ns100": (OLD, {"nostop_leg_notional_k": "0.555555555556"}),
        "base-v1-timeexit-w60-live-follow-ns3rd": (OLD, {"nostop_plan_notional_k": "16.666666666667"}),
        NS_PLAIN: ("base-v1-timeexit-w60-live", {"nostop_leg_notional_k": "1.666666666667"}),
    }
    assert set(c.NOSTOP_POLICIES) == {v for v, (_, extra) in expected.items() if any(k.startswith("nostop") for k in extra)}
    for version, (base, extra) in expected.items():
        policy = c.resolve_policy(version)
        assert registry[version] == policy.content_hash
        body = policy.model_dump(mode="json")
        assert body.pop("version") == version
        reference = c.resolve_policy(base).model_dump(mode="json")
        reference.pop("version")
        assert body == {**reference, **extra}
    for version, policy in c.POLICIES.items():
        if not policy.nostop_enabled:
            assert not {"nostop_leg_notional_k", "nostop_plan_notional_k"} & set(policy.model_dump())
    with pytest.raises(c.ContractError, match="互斥"):
        c.ExecutionPolicy.model_validate({**c.resolve_policy(NS).model_dump(), "nostop_plan_notional_k": D(1)})
    with pytest.raises(c.ContractError):
        c.ExecutionPolicy.model_validate({**c.resolve_policy(OLD).model_dump(), "nostop_leg_notional_k": D(0)})


def test_min_notional_and_lot_size_rejections_are_counted():
    req, _, _, _ = prepare(episode())
    small = execution.simulate(short_horizon(req), market=market(["50100"] * 3, min_notional="400"))
    assert [e.reason for e in events(small, "rejected", "entry")] == ["MIN_NOTIONAL"] and small.outcome_kind == "rejected"
    coarse = execution.simulate(short_horizon(req), market=market(["50100"] * 3, step="1"))
    assert [e.reason for e in events(coarse, "rejected", "entry")] == ["LOT_SIZE"] and coarse.outcome_kind == "rejected"


def test_kernel_b_rejects_nostop_policies_before_resolver():
    assert len(c.NOSTOP_POLICIES) == 7
    for version in c.NOSTOP_POLICIES:
        req, _, why, _ = prepare(episode(), version)
        assert why is None

        def forbidden(request):
            raise AssertionError("market resolver must not be called")

        with pytest.raises(KernelBUnavailable):
            execution.simulate(req, kernel="B", resolver=forbidden)
    # The non-follow stopless policy is rejected by the stopless guard itself, not by the follow guard.
    req, _, _, _ = prepare(episode(), NS_PLAIN)
    with pytest.raises(KernelBUnavailable, match="nostop"):
        execution.simulate(req, kernel="B", resolver=lambda request: pytest.fail("resolver called"))


@pytest.mark.parametrize("rules", ["none", "empty", "halted", "no_multiplier", "no_tick"])
def test_missing_rules_are_their_own_exclusion(rules):
    frame = {"none": None, "empty": rules_frame().clear(), "halted": rules_frame(status="BREAK"),
             "no_multiplier": rules_frame(multiplier=None), "no_tick": rules_frame(tick=None)}[rules]
    req, _, why, _ = prepare(episode(), rules=frame)
    assert req is None and why == "NOSTOP_RULES_UNRESOLVED"
    # With a stop the same rules stay on the old path: no new exclusion code, the kernel censors missing rules.
    req, audit, why, _ = prepare(episode(stop="49000"), rules=frame)
    assert why is None and req is not None


# ---------------------------------------------------------------- kernel A v0.7 management (F1 tests 7–12)
def ns_request(actions=(), *, side="long", pending=False, version=NS, qty="4", horizon=240, limit="90"):
    sign = 1 if side == "long" else -1
    entry = D(limit) if pending else D(100)
    plan = c.OrderPlan(instrument_id=INST, side=side,
                       entries=[c.Entry(kind="limit" if pending else "market_ref", price_lo=entry, price_hi=entry)],
                       tps=[c.TakeProfit(level=entry + sign * 30, fraction=D(1))],
                       sizing=c.Sizing(mode="fixed_qty", qty=D(qty)), expiry=c.Expiry(entry_ttl_s=300))
    return c.build_request(dict(episode_id="ns-k", graph_version="synthetic", decision_snapshot_hash="d", t_dec=T0,
                                order_plan=plan), policy_version=version, policy_hash=c.resolve_policy(version).content_hash,
                           risk_budget=B, market_manifest=MANIFEST, horizon_end=T0 + timedelta(seconds=horizon),
                           management=list(actions))


def move(seconds, **kwargs):
    return c.ManagementAction(at=T0 + timedelta(seconds=seconds), kind="move_stop", source_message_id=9, **kwargs)


def test_teacher_stop_on_stopless_position_creates_sl_and_triggers_on_mark():
    res = execution.simulate(ns_request([move(75, stop_price=D(97))]), market=market(["100", "95", "90", "90"]))
    submitted = events(res, "submitted", "sl")
    assert [(e.ts, e.price) for e in submitted] == [(T0 + timedelta(seconds=75), D(97))]
    trigger = events(res, "stop_triggered")
    assert [(e.ts, e.trigger_basis, e.price) for e in trigger] == [(T0 + timedelta(seconds=75), "mark", D(95))]
    assert res.outcome_kind == "stopped" and [e.price for e in events(res, "filled", "sl")] == [D(95)]
    kernel = a.KernelA(ns_request([move(75, stop_price=D(97))]), market(["100", "95", "90", "90"]))
    assert kernel.protective_stop() == (None, "none") and kernel.stop_hit() is False


def test_teacher_stop_below_market_waits_then_triggers():
    res = execution.simulate(ns_request([move(75, stop_price=D(93))]), market=market(["100", "95", "92", "92"]))
    assert [(e.ts, e.price) for e in events(res, "submitted", "sl")] == [(T0 + timedelta(seconds=75), D(93))]
    assert [e.ts for e in events(res, "stop_triggered")] == [T0 + timedelta(seconds=120)]


def test_move_stop_before_entry_places_stop_at_fill():
    res = execution.simulate(ns_request([move(30, stop_price=D(87))], pending=True), market=market(["100", "92", "89", "85"]))
    fill = events(res, "filled", "entry")[0]
    assert [(e.ts, e.price) for e in events(res, "submitted", "sl")] == [(fill.ts, D(87))]
    assert res.exit_legs == ("sl",) and res.outcome_kind == "stopped"


def test_to_entry_before_fill_places_stop_at_average():
    res = execution.simulate(ns_request([move(30, to_entry=True)], pending=True), market=market(["100", "92", "89", "85"]))
    assert [e.price for e in events(res, "submitted", "sl")] == [res.entry_avg_price] == [D(89)]
    assert res.outcome_kind == "stopped"


def test_close_all_on_stopless_position():
    close = c.ManagementAction(at=T0 + timedelta(seconds=75), kind="close_all", source_message_id=9)
    res = execution.simulate(ns_request([close]), market=market(["100", "95", "90", "90"]))
    assert res.exit_legs == ("close",) and res.outcome_kind == "filled_closed" and events(res, leg="sl") == []


@pytest.mark.parametrize("version,hold_days", [(NS, 59), ("base-v1-timeexit-w1-live-follow-ns300", 1)])
def test_horizon_time_exit_for_w60_and_w1(version, hold_days):
    req, _, why, _ = prepare(episode(), version)
    assert why is None and req.horizon_end - req.t_dec == timedelta(days=1 + hold_days)
    near_end = req.horizon_end - timedelta(seconds=60)
    points = [c.PricePoint(ts=t, price=D(p)) for t, p in ((T0, "50100"), (T0 + timedelta(minutes=1), "49990"),
                                                          (near_end, "51000"))]
    view = c.MarketView(manifest_id=MANIFEST, last=points, mark=points, rules=c.Rules(tick_size=D(".1"), step_size=D(".001")))
    res = execution.simulate(req, market=view)
    assert res.outcome_kind == "time_exit" and res.censor_reason is None
    closed = events(res, "closed")[0]
    assert closed.ts == req.horizon_end and closed.reason == "time_exit" and events(res, leg="sl") == []


def test_mae_in_U_is_mae_R_times_budget():
    req, _, _, info = prepare(episode())
    req = short_horizon(req)
    results = execution.simulate_batch([req], markets={MANIFEST: market(["50100", "49990", "49000"])})
    row = results.to_dicts()[0]
    derived = l0.derived_columns(results, {req.episode_id: req}, {req.episode_id: info}, Marks())[req.episode_id]
    assert row["mae_R"] < 0 and derived["mae_U"] == (row["mae_R"] * B).quantize(D("1e-12"))
    assert derived["mae_pct_notional"] == (row["mae_R"] * B * 100 / info["nostop_notional_U"]).quantize(D("1e-12"))
    assert derived["sizing_basis"] == "nostop" and derived["mtm_U_at_censor"] is None and derived["censor_at"] is None


def test_censored_nostop_mark_to_market_uses_censor_time_mark():
    req, _, _, info = prepare(episode())
    req = short_horizon(req, 600)
    lasts = [c.PricePoint(ts=T0 + timedelta(seconds=s), price=D(p)) for s, p in ((0, "50100"), (60, "49990"), (300, "49500"))]
    marks = [p for p in lasts if p.ts <= T0 + timedelta(seconds=60)]      # marks stop → MARK_STALE censor later
    view = c.MarketView(manifest_id=MANIFEST, last=lasts, mark=marks, rules=c.Rules(tick_size=D(".1"), step_size=D(".001")))
    results = execution.simulate_batch([req], markets={MANIFEST: view})
    row = results.to_dicts()[0]
    assert row["censor_reason"] == "MARK_STALE" and row["filled_qty"] == D("0.006")
    stub = Marks("49000")
    derived = l0.derived_columns(results, {req.episode_id: req}, {req.episode_id: info}, stub)[req.episode_id]
    last_event = max(e["ts"] for e in row["canonical_events"])
    assert stub.asked == [(INST, last_event)] and derived["censor_at"] == last_event
    expected = row["gross_pnl"] + (D(49000) - row["entry_avg_price"]) * D("0.006") - row["fees"] + row["funding"]
    assert derived["mtm_U_at_censor"] == expected.quantize(D("1e-12"))
    label = dict(row, censor_reason="LABEL_RIGHT_CENSORED")
    assert l0.censor_time(req, label) == req.horizon_end


# ---------------------------------------------------------------- summary blocks (F1 test 19)
def summary_rows():
    rows = []
    for i, (basis, net, qty) in enumerate([("risk", "2", 1), ("risk", "-1", 1), ("nostop", "0.5", 1), ("nostop", "-0.25", 1),
                                           ("nostop", "0", 0)]):
        rows.append({"episode_id": str(i), "t_dec": T0 + timedelta(minutes=i), "side": "long", "instrument": "BTC",
                     "net_R": D(net), "filled_qty": D(qty), "censor_reason": None, "risk_budget": B,
                     "mark_ok": True, "funding_ok": True, "rules_ok": True, "bars_ok": True, "sizing_basis": basis,
                     "mae_U": D(-10 * i), "mae_pct_notional": D(-i), "mtm_U_at_censor": None, "entry_notional_U": None,
                     "position_open_at": None, "position_close_at": None, "censor_at": None, "venue_hint": "perp"})
    return rows


def test_summary_overall_is_risk_only_and_blocks_split():
    rows = summary_rows()
    table = pl.DataFrame(rows, infer_schema_length=None)
    report = l0.summarize(table)
    risk_only = l0.summarize(pl.DataFrame([dict(r) for r in rows if r["sizing_basis"] == "risk"], infer_schema_length=None))
    legacy = l0.summarize(pl.DataFrame([{k: v for k, v in r.items() if k != "sizing_basis"} for r in rows[:2]],
                                       infer_schema_length=None))
    for key in ("overall", "by", "cumulative_R", "censor_counts"):
        assert report[key] == risk_only[key] == legacy[key]
    assert report["overall"]["n_trades"] == 2 and report["overall"]["mean_net_R"] == pytest.approx(0.5)
    blocks = report["blocks"]
    assert blocks["with_stop"] == report["overall"]
    nostop = blocks["nostop"]
    assert nostop["n_trades"] == 3 and nostop["n_filled"] == 2
    assert nostop["sum_net_U"] == pytest.approx(0.25 * 180) and nostop["mean_net_U"] == pytest.approx(0.25 * 180 / 3)
    assert nostop["win_rate"] == 0.5 and nostop["median_net_U"] == pytest.approx(0)
    assert nostop["adverse_excursion_U"]["max"] == pytest.approx(30)
    assert set(nostop["by"]) == {"venue_hint"} and nostop["by"]["venue_hint"]["perp"]["n_trades"] == 3
    assert blocks["total"] == {"sum_net_U": pytest.approx(180 + 45), "sum_net_U_with_stop": pytest.approx(180),
                               "sum_net_U_nostop": pytest.approx(45)}
    assert "mean_net_R" not in blocks["total"] and "win_rate" not in blocks["total"]


def test_concurrency_peak_closes_before_opening_at_the_same_time():
    def row(open_s, close_s, notional):
        return {"filled_qty": D(1), "entry_notional_U": D(notional), "position_open_at": T0 + timedelta(seconds=open_s),
                "position_close_at": None if close_s is None else T0 + timedelta(seconds=close_s),
                "censor_at": T0 + timedelta(seconds=999)}
    peak = l0.concurrency([row(0, 60, 300), row(60, 120, 300), row(30, None, 200)])
    assert peak["peak_notional_U"] == 500 and peak["peak_at"] == (T0 + timedelta(seconds=30)).isoformat()


# ---------------------------------------------------------------- §4 second pass (F2 test 23)
EVENT_COLS = ("seq", "ts", "kind", "order_id", "leg", "trigger_basis", "price", "qty", "fee", "reason", "bar_open_time",
              "path_step", "cash_delta")
DAY = timedelta(days=1)


def ev(day, kind, leg="entry", qty=None, order="entry-0"):
    return {**dict.fromkeys(EVENT_COLS), "seq": 0, "ts": T0 + day * DAY, "kind": kind, "order_id": order, "leg": leg,
            "trigger_basis": "none", "path_step": "none", "qty": None if qty is None else D(qty)}


def frame(rows):
    return pl.DataFrame({"episode_id": [eid for eid, _ in rows], "canonical_events": [e for _, e in rows]},
                        schema={"episode_id": pl.Utf8, "canonical_events": pl.List(execution.EVENT_STRUCT)})


def life(open_day, close_day):
    """Filled at open_day, closed at close_day (None = still open)."""
    out = [ev(open_day, "submitted", qty="1"), ev(open_day, "accepted"), ev(open_day, "filled", qty="1")]
    if close_day is not None:
        out += [ev(close_day, "filled", "tp", "1", "tp-0"), ev(close_day, "closed", "close", order="bracket-0")]
    return out


def pending_req(eid, day):
    return SimpleNamespace(episode_id=eid, t_dec=T0 + day * DAY)


def runner(lives):
    ran = []

    def run(req):
        ran.append(req.episode_id)
        return frame([(req.episode_id, lives[req.episode_id])])
    return run, ran


def test_alive_at_only_sees_strictly_earlier_events():
    ended = life(0, 2)
    assert l0.alive_at(ended, T0 + DAY) is True
    assert l0.alive_at(ended, T0 + 2 * DAY) is True          # the close at exactly t is not yet known
    assert l0.alive_at(ended, T0 + 2 * DAY + timedelta(microseconds=1)) is False
    working = [ev(0, "submitted", qty="1"), ev(0, "accepted"), ev(1, "expired")]
    assert l0.alive_at(working, T0 + DAY) is True and l0.alive_at(working, T0 + 2 * DAY) is False
    assert l0.alive_at([], T0) is False


def test_second_pass_repost_family():
    # T (day 0) ended day 1 → E1 (day 2) executes and stays live → E2 (day 8) skipped; E3 (day 20), a repost of the
    # skipped E2, walks back to T's family, now ended (E1 closed day 15) → executes.
    first = frame([("T", life(0, 1))])
    lives = {"E1": life(2, 15), "E3": life(20, None)}
    run, ran = runner(lives)
    pending = [(pending_req("E2", 8), "repost", "T"), (pending_req("E1", 2), "repost", "T"),
               (pending_req("E3", 20), "repost", "E2"), (pending_req("E4", 9), "repost", "missing")]
    lives["E4"] = life(9, 10)
    parts, excluded, decisions = l0.second_pass(first, pending, follow=False, run=run)
    assert ran == ["E1", "E4", "E3"]
    assert excluded == {"REPOST_OF_LIVE_PLAN": 1}
    assert decisions["E2"] == {"kind": "repost", "target": "T", "decision": "REPOST_OF_LIVE_PLAN"}
    assert decisions["E1"]["family"] == decisions["E3"]["family"] == "T"
    assert decisions["E3"]["target"] == "T" and decisions["E4"]["decision"] == "independent_no_target_result"
    assert decisions["E4"]["family"] == "E4" and len(parts) == 3


def test_second_pass_amend():
    first = frame([("T", life(1, None)), ("U", [ev(0, "submitted", qty="1"), ev(0, "accepted"), ev(2, "expired")])])
    lives = {"A1": life(3, None), "A2": life(0.5, None)}
    run, ran = runner(lives)
    pending = [(pending_req("A1", 0.5), "amend", "U"), (pending_req("A2", 2), "amend", "T")]
    _, excluded, decisions = l0.second_pass(first, pending, follow=True, run=run)
    assert ran == ["A1"] and excluded == {"AMEND_TARGET_FILLED": 1}
    assert decisions["A1"]["decision"] == "amend_target_unfilled"
    _, excluded, _ = l0.second_pass(first, pending, follow=False, run=runner(lives)[0])
    assert excluded == {"AMEND_REQUIRES_FOLLOW": 2}


def test_link_of_reads_repost_then_amend():
    assert l0.link_of({"repost_of": "T", "amend_of": "U"}) == ("repost", "T")
    assert l0.link_of({"repost_of": "", "amend_of": "U"}) == ("amend", "U")
    assert l0.link_of({"repost_of": None}) == (None, None) and l0.link_of({}) == (None, None)


# ---------------------------------------------------------------- end to end through the G1-built synthetic graph
from tests.market.test_l0_replay import built  # noqa: E402  (G1 builds, G2 consumes)
from tests.data.l0_fixtures import CHANNEL  # noqa: E402


def patch_episodes(monkeypatch, edit):
    original = l0.load_episodes

    def load(graph_version, *args, **kwargs):
        frame_ = original(graph_version, *args, **kwargs)
        if not kwargs.get("decision_graph", False):
            return frame_
        rows = sorted(frame_.filter(pl.col("channel_id") == CHANNEL).to_dicts(), key=lambda r: r["t_dec"])
        rows = [edit(i, dict(r)) for i, r in enumerate(rows)]
        extra = {k: pl.Utf8 for k in ("repost_of", "venue_hint", "stop_rule")} | {"signal_age_s": pl.Int64}
        return pl.DataFrame(rows, schema={**frame_.schema, **extra})
    monkeypatch.setattr(l0, "load_episodes", load)
    monkeypatch.setattr(l0, "load_message_texts", lambda ids: {value: "BTC 做多 入场 100 目标 110" for value in ids})


def stopless_first(i, row):
    if i == 0:
        row["order_plan"] = dict(row["order_plan"], stop=None)
    return dict(row, repost_of=None, venue_hint="perp" if i == 0 else None, stop_rule=None, signal_age_s=61)


def test_replay_executes_stopless_plan_in_its_own_block(built, monkeypatch):
    root, _ = built
    patch_episodes(monkeypatch, stopless_first)
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "ns", policy_version=NS_PLAIN, risk_budget=B)
    table = pl.read_parquet(root / "ns" / "trades.parquet")
    assert table.height == 2 and report["replay_exclusions"]["reason_counts"] == {}
    nostop = table.filter(pl.col("sizing_basis") == "nostop").to_dicts()
    assert len(nostop) == 1 and nostop[0]["filled_qty"] == D(3) and nostop[0]["outcome_kind"] == "tp_hit"
    assert nostop[0]["venue_hint"] == "perp" and nostop[0]["signal_age_s"] == 61 and nostop[0]["legs_n"] == 1
    assert nostop[0]["nostop_notional_U"] == D("1.666666666667") * B
    # 3 × (110 − 100) − taker 0.05% × 300 − maker 0.02% × 330
    assert report["blocks"]["nostop"]["sum_net_U"] == pytest.approx(29.784)
    assert report["overall"]["n_trades"] == 1 and report["blocks"]["with_stop"] == report["overall"]
    assert report["blocks"]["total"]["sum_net_U"] == pytest.approx(29.784)
    assert "blocks" in (root / "ns" / "summary.md").read_text()
    old = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "old", policy_version="base-v1-timeexit-w60-live",
                    risk_budget=B)
    assert old["replay_exclusions"]["reason_counts"] == {"PLAN_NO_STOP": 1}


@pytest.mark.parametrize("minutes,decision", [(1, "REPOST_OF_LIVE_PLAN"), (10, "repost_family_ended")])
def test_replay_second_pass_judges_repost_against_the_live_family(built, monkeypatch, minutes, decision):
    root, _ = built

    def edit(i, row):
        row = dict(row, repost_of=None, venue_hint=None, stop_rule=None, signal_age_s=None)
        if i == 1:
            row["repost_of"] = first_id[0]
            row["t_dec"] = T_FIRST[0] + timedelta(minutes=minutes)
        else:
            first_id[:] = [row["episode_id"]]
            T_FIRST[:] = [row["t_dec"]]
        return row

    first_id, T_FIRST = [], []
    patch_episodes(monkeypatch, edit)
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "rp", policy_version="base-v1-timeexit-w60-live",
                       risk_budget=B)
    assert report["second_pass"]["n_pending"] == 1
    assert report["second_pass"]["decision_counts"] == {decision: 1}
    table = pl.read_parquet(root / "rp" / "trades.parquet")
    if decision == "REPOST_OF_LIVE_PLAN":
        assert table.height == 1 and report["replay_exclusions"]["reason_counts"] == {"REPOST_OF_LIVE_PLAN": 1}
        assert report["replay_exclusions"]["n_replayed"] == 1
    else:
        assert table.height == 2 and table["second_pass_decision"].to_list() == [None, "repost_family_ended"]
