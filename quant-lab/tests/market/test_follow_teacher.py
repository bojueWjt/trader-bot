"""Teacher management acceptance: synthetic 1m bars, fixed pre-change identity."""
from datetime import datetime, timedelta, UTC
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

import pytest

from quant_lab.market import contract as c, execution, kernel_a as a
from quant_lab.market.nautilus_adapter import KernelBUnavailable, simulate_b

T0 = datetime(2024, 1, 1, 1, tzinfo=UTC)
BASES = ("base-v1-timeexit", "base-v1-timeexit-live", "base-v1-timeexit-w60", "base-v1-timeexit-w60-live")
BASELINE = Path(__file__).with_name("follow-teacher-legacy-baseline.json")


def command(kind, seconds=75, **kwargs):
    return c.ManagementAction(at=T0 + timedelta(seconds=seconds), kind=kind, source_message_id=17, **kwargs)


def request(side="long", actions=(), *, version=BASES[0] + "-follow", pending=False, qty="4", horizon=180):
    sign = 1 if side == "long" else -1
    entry = D(100 - sign * 10) if pending else D(100)
    plan = c.OrderPlan(instrument_id="BTCUSDT-PERP.BINANCE-UM", side=side,
        entries=[c.Entry(kind="limit" if pending else "market_ref", price_lo=entry, price_hi=entry)],
        stop=c.Stop(price=entry - sign * 10), tps=[c.TakeProfit(level=entry + sign * 10, fraction=D(1))],
        sizing=c.Sizing(mode="fixed_qty", qty=D(qty)), expiry=c.Expiry(entry_ttl_s=300))
    policy = c.resolve_policy(version)
    return c.build_request(dict(episode_id="teacher", graph_version="synthetic", decision_snapshot_hash="decision",
        t_dec=T0, order_plan=plan), policy_version=version, policy_hash=policy.content_hash,
        risk_budget=D(40), market_manifest="synthetic", horizon_end=T0 + timedelta(seconds=horizon), management=list(actions))


def bar(seconds, o, h=None, l=None, close=None, volume="10000"):
    return c.Bar(open_time=T0 + timedelta(seconds=seconds), o=D(o), h=D(o if h is None else h),
                 l=D(o if l is None else l), c=D(o if close is None else close), volume=D(volume))


def market(side="long", offsets=(0, 5, 10), *, volume="10000", step=".001"):
    sign = 1 if side == "long" else -1
    bars = [bar(i * 60, str(100 + sign * offset), volume=volume) for i, offset in enumerate(offsets)]
    return c.MarketView(manifest_id="synthetic", bars_last=bars, bars_mark=bars,
                        rules=c.Rules(tick_size=D(".01"), step_size=D(step)))


def fills(res, leg):
    return [e for e in res.canonical_events if e.leg == leg and e.kind in c.FILL_KINDS]


@pytest.mark.parametrize("side", ["long", "short"])
def test_close_before_tp_is_market_taker_and_cancels_protection(side):
    req = request(side, [command("close_all")])
    res = execution.simulate(req, market=market(side))
    sign = 1 if side == "long" else -1
    assert [(e.ts, e.price, e.qty) for e in fills(res, "close")] == [(T0 + timedelta(seconds=75), D(100 + sign * 5), D(4))]
    assert fills(res, "tp") == [] and fills(res, "sl") == []
    assert res.fees == D(4) * (D(100) + D(100 + sign * 5)) * D(".0005")
    assert D(-1) < res.net_R < D(1)
    assert res.outcome_kind == "filled_closed" and res.exit_legs == ("close",)
    assert {e.leg for e in res.canonical_events if e.kind == "cancelled"} == {"tp", "sl"}
    stats = res.management_stats
    assert stats["n_executed"] == 1 and stats["last_kind"] == "close_all"
    audit = next(e for e in res.canonical_events if e.kind == "management")
    assert json.loads(audit.reason)["source_message_id"] == 17
    assert execution.simulate(req, market=market(side)).model_dump_json() == res.model_dump_json()


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("fraction", [None, D(".5")])
def test_reduce_half_then_remaining_tp(side, fraction):
    req = request(side, [command("reduce", fraction=fraction)])
    res = execution.simulate(req, market=market(side))
    assert [e.qty for e in fills(res, "close")] == [D(2)]
    assert [e.qty for e in fills(res, "tp")] == [D(2)]
    assert res.gross_pnl == D(30) and res.exit_legs == ("tp", "close")
    assert any(e.kind == "amended" and e.leg == "sl" and e.qty == D(2) for e in res.canonical_events)
    assert res.management_stats["n_reduce_fraction_defaulted"] == int(fraction is None)
    assert res.net_R == c.quantize_ratio(res.net_pnl / D(40))
    assert req.order_plan.stop.price == (D(90) if side == "long" else D(110))


@pytest.mark.parametrize("side", ["long", "short"])
def test_reduce_uses_current_position_each_time_and_rounds_lots(side):
    req = request(side, [command("reduce", 65, fraction=D(".49")), command("reduce", 75, fraction=D(".5"))], qty="5")
    res = execution.simulate(req, market=market(side, step="1"))
    assert [e.qty for e in fills(res, "close")] == [D(2), D(1)]
    assert [e.qty for e in fills(res, "tp")] == [D(2)]


@pytest.mark.parametrize("side", ["long", "short"])
def test_to_entry_stop_break_even_preserves_original_risk(side):
    req = request(side, [command("move_stop", to_entry=True)])
    res = execution.simulate(req, market=market(side, (0, 5, 0)))
    stop = next(e for e in res.canonical_events if e.kind == "stop_triggered")
    assert stop.ts == T0 + timedelta(seconds=120) and stop.price == D(100) and stop.trigger_basis == "mark"
    assert any(e.kind == "amended" and e.leg == "sl" and e.price == res.entry_avg_price for e in res.canonical_events)
    assert [e.qty for e in fills(res, "sl")] == [D(4)]
    # Existing mark-stop cost path includes one adverse tick even in base costs.
    assert abs(res.net_R) < D(".02") and res.outcome_kind == "stopped"
    assert req.order_plan.stop.price == (D(90) if side == "long" else D(110))
    assert req.risk_budget == D(40) and res.net_R == c.quantize_ratio(res.net_pnl / D(40))


@pytest.mark.parametrize("side", ["long", "short"])
def test_move_stop_past_current_mark_triggers_at_instruction_time(side):
    price = D(106) if side == "long" else D(94)
    req = request(side, [command("move_stop", 60, stop_price=price)])
    res = execution.simulate(req, market=market(side))
    stop = next(e for e in res.canonical_events if e.kind == "stop_triggered")
    assert stop.ts == T0 + timedelta(seconds=60) and stop.price == (D(105) if side == "long" else D(95))
    assert fills(res, "sl")[0].ts == stop.ts and fills(res, "tp") == []
    assert fills(res, "sl")[0].price == (D(105) if side == "long" else D(95))
    assert any(e.kind == "amended" and e.leg == "sl" and e.price == price for e in res.canonical_events)
    between_points = execution.simulate(request(side, [command("move_stop", 75, stop_price=price)]), market=market(side))
    trigger = next(e for e in between_points.canonical_events if e.kind == "stop_triggered")
    assert trigger.ts == T0 + timedelta(seconds=75)
    assert fills(between_points, "sl")[0].ts == T0 + timedelta(seconds=80)


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("kind", ["cancel_pending", "close_all"])
def test_cancel_before_entry_is_unfilled(side, kind):
    req = request(side, [command(kind)], pending=True)
    res = execution.simulate(req, market=market(side, (0, -5, -10)))
    assert res.filled_qty == 0 and res.net_R == 0 and res.fill_status == "none"
    assert res.outcome_kind == "unfilled_expired" and res.censor_reason is None
    assert res.canonical_events[-1].kind == "closed" and res.canonical_events[-1].ts == T0 + timedelta(seconds=75)
    assert any(e.kind == "cancelled" and e.leg == "entry" for e in res.canonical_events)


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("to_entry", [False, True])
def test_move_stop_before_entry_updates_protection_without_changing_sizing(side, to_entry):
    price = D(85) if side == "long" else D(115)
    action = command("move_stop", to_entry=to_entry, stop_price=None if to_entry else price)
    req = request(side, [action], pending=True)
    res = execution.simulate(req, market=market(side, (0, -5, -10)))
    sl = next(e for e in res.canonical_events if e.kind == "submitted" and e.leg == "sl")
    assert sl.price == (res.entry_avg_price if to_entry else price)
    assert res.filled_qty == D(4) and req.order_plan.stop.price == (D(80) if side == "long" else D(120))
    assert res.management_stats["n_executed"] == 1


def test_no_position_reduce_and_add_are_counted_and_zero_fraction_is_not_defaulted():
    actions = [command("reduce", 30), command("add", 45, fraction=D(2)), command("reduce", 70, fraction=D(0))]
    req = request(actions=actions, pending=True)
    res = execution.simulate(req, market=market(offsets=(0, -5, -10)))
    stats = res.management_stats
    assert stats["n_processed"] == 3 and stats["n_executed"] == 0
    assert stats["ignored_counts"] == {"ignored_no_position": 2, "ignored_add": 1}
    assert stats["n_reduce_fraction_defaulted"] == 1
    filled = execution.simulate(request(actions=[command("reduce", fraction=D(0))]), market=market())
    assert fills(filled, "close") == []
    assert filled.management_stats["ignored_counts"] == {"ignored_zero_quantity": 1}
    assert filled.management_stats["n_reduce_fraction_defaulted"] == 0


def test_partial_entry_cancel_keeps_position_and_reduce_cancels_unfilled_entries():
    req = request(actions=[command("cancel_pending", 30), command("close_all", 75)])
    plan = req.order_plan.model_copy(update={"entries": [c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))]})
    req = c.ExecutionRequest.model_validate({**req.model_dump(), "order_plan": plan})
    bars = [bar(0, "100", volume="40"), bar(60, "105"), bar(120, "110")]
    res = execution.simulate(req, market=market().model_copy(update={"bars_last": bars, "bars_mark": bars}))
    assert res.filled_qty == D(2) and [e.qty for e in fills(res, "close")] == [D(2)]
    assert res.management_stats["n_executed"] == 2 and res.management_stats["last_kind"] == "close_all"
    cancelled = [e for e in res.canonical_events if e.kind == "cancelled" and e.leg == "entry"]
    assert len(cancelled) == 1 and cancelled[0].ts == T0 + timedelta(seconds=30)
    reduced_req = c.ExecutionRequest.model_validate({**req.model_dump(), "management": [command("reduce", 30)]})
    reduced = execution.simulate(reduced_req, market=market().model_copy(update={"bars_last": bars, "bars_mark": bars}))
    assert reduced.filled_qty == D(2) and fills(reduced, "close")[0].qty == D(1)
    assert any(e.kind == "cancelled" and e.leg == "entry" and e.reason == "exit_latch" for e in reduced.canonical_events)


def test_intrabar_management_uses_only_known_point_and_market_capacity_is_unbounded():
    bars = [bar(0, "100"), bar(60, "104", "109", "103", "105", volume="0"), bar(120, "110")]
    req = request(actions=[command("close_all", 65)])
    m = market().model_copy(update={"bars_last": bars, "bars_mark": bars})
    res = execution.simulate(req, market=m)
    assert [(e.ts, e.price, e.qty) for e in fills(res, "close")] == [(T0 + timedelta(seconds=65), D(104), D(4))]
    assert res.position_close_at == T0 + timedelta(seconds=65)
    same_time = execution.simulate(request(actions=[command("close_all", 60)]), market=m)
    assert fills(same_time, "close")[0].price == D(104)
    at_tp = execution.simulate(request(actions=[command("close_all", 120)]), market=market())
    assert fills(at_tp, "tp") == [] and fills(at_tp, "close")[0].ts == T0 + timedelta(seconds=120)


def test_management_uses_market_cost_model_and_fee_multiplier():
    req = request(actions=[command("close_all")])
    req = c.ExecutionRequest.model_validate({**req.model_dump(), "cost_scenario": "stress"})
    m = market().model_copy(update={"rules": c.Rules(tick_size=D(".01"), step_size=D(".001"), multiplier=D(2))})
    res = execution.simulate(req, market=m)
    assert fills(res, "entry")[0].price == D("100.03")
    assert fills(res, "close")[0].price == D("104.96")
    assert res.fees == D(".81996") and res.slippage == D(".56")
    assert res.gross_pnl == D("39.44") and res.net_R == D(".965501")


@pytest.mark.parametrize("missing", ["mark", "last"])
def test_management_at_non_price_time_requires_fresh_quotes(missing):
    m = c.MarketView(manifest_id="synthetic", last=[c.PricePoint(ts=T0, price=D(100))],
        mark=[c.PricePoint(ts=T0, price=D(100))], rules=c.Rules(tick_size=D(".01"), step_size=D(".001")))
    if missing == "mark":
        m = m.model_copy(update={"last": m.last + [c.PricePoint(ts=T0 + timedelta(seconds=150), price=D(105))]})
    else:
        m = m.model_copy(update={"mark": m.mark + [c.PricePoint(ts=T0 + timedelta(seconds=150), price=D(105))]})
    res = execution.simulate(request(actions=[command("close_all", 150)], horizon=240), market=m)
    assert res.censor_reason == ("MARK_STALE" if missing == "mark" else "BAR_GAP")
    assert res.net_R is None and fills(res, "close") == []


def test_follow_policy_off_does_not_execute_management():
    req = request(actions=[command("close_all")], version=BASES[0])
    res = execution.simulate(req, market=market())
    assert res.management_stats["n_processed"] == 0 and res.outcome_kind == "tp_hit"


@pytest.mark.parametrize("side", ["long", "short"])
def test_move_stop_preserves_close_timeframe_until_crossed_at_instruction(side):
    sign = 1 if side == "long" else -1
    req = request(side, [command("move_stop", stop_price=D(100 - sign * 5))], horizon=960)
    plan = req.order_plan.model_copy(update={"stop": c.Stop(price=D(100 - sign * 10), trigger="close", timeframe="15m"), "tps": []})
    req = c.ExecutionRequest.model_validate({**req.model_dump(), "order_plan": plan, "tp_fractions": None})
    bars = [bar(i * 60, "100") for i in range(14)]
    last = D(100 - sign * 6)
    bars += [bar(840, "100", str(max(D(100), last)), str(min(D(100), last)), str(last)), bar(900, str(100 - sign * 4))]
    m = market(side).model_copy(update={"bars_last": bars, "bars_mark": bars})
    res = execution.simulate(req, market=m)
    trigger = next(e for e in res.canonical_events if e.kind == "stop_triggered")
    assert trigger.ts == T0 + timedelta(seconds=900, microseconds=-1) and trigger.trigger_basis == "close"
    assert trigger.price == last and fills(res, "sl")[0].ts == T0 + timedelta(seconds=900)
    crossed = command("move_stop", 60, stop_price=D(100 + sign))
    immediate = execution.simulate(c.ExecutionRequest.model_validate({**req.model_dump(), "management": [crossed]}), market=m)
    trigger = next(e for e in immediate.canonical_events if e.kind == "stop_triggered")
    assert trigger.ts == crossed.at and trigger.trigger_basis == "mark"


@pytest.mark.parametrize("side", ["long", "short"])
def test_pending_stop_changes_do_not_resize_original_risk_budget(side):
    price = D(85) if side == "long" else D(115)
    req = request(side, [command("move_stop", stop_price=price)], pending=True)
    plan = req.order_plan.model_copy(update={"sizing": c.Sizing(mode="risk_budget")})
    req = c.ExecutionRequest.model_validate({**req.model_dump(), "order_plan": plan})
    res = execution.simulate(req, market=market(side, (0, -5, -10)))
    assert res.filled_qty == D(4)  # 40 / original 10-point stop distance, never 40 / 5.
    assert req.order_plan.stop.price == (D(80) if side == "long" else D(120))


@pytest.mark.parametrize("side", ["long", "short"])
def test_move_to_entry_uses_actual_average_of_multiple_fills(side):
    sign = 1 if side == "long" else -1
    req = request(side, [command("move_stop", to_entry=True)])
    other = D(100 + sign * 2)
    entries = [c.Entry(kind="limit", price_lo=D(100), price_hi=D(100), fraction=D(".5")),
               c.Entry(kind="limit", price_lo=other, price_hi=other, fraction=D(".5"))]
    plan = req.order_plan.model_copy(update={"entries": entries})
    body = req.model_dump()
    body.update(order_plan=plan, entry_fractions=None)
    req = c.ExecutionRequest.model_validate(body)
    bars = [bar(0, str(other)), bar(60, "100"), bar(120, str(100 + sign))]
    m = market(side).model_copy(update={"bars_last": bars, "bars_mark": bars})
    res = execution.simulate(req, market=m)
    assert res.entry_avg_price == D(100 + sign)
    amendment = next(e for e in res.canonical_events if e.kind == "amended" and e.leg == "sl" and e.ts == T0 + timedelta(seconds=75))
    assert amendment.price == res.entry_avg_price
    assert fills(res, "sl") and fills(res, "sl")[0].qty == D(4)


def test_funding_and_expiry_keep_priority_over_same_time_management():
    req = request(actions=[command("close_all", 60)])
    m = market().model_copy(update={"funding": [c.FundingRow(calc_time=T0 + timedelta(seconds=60), rate=D(".001"), interval_hours=8)]})
    res = execution.simulate(req, market=m)
    funding = next(e for e in res.canonical_events if e.kind == "funding")
    action = next(e for e in res.canonical_events if e.kind == "management")
    assert funding.seq < action.seq and funding.qty == D(4) and res.funding == D("-.4")
    req = request(actions=[command("cancel_pending", 60)], pending=True)
    plan = req.order_plan.model_copy(update={"expiry": c.Expiry(entry_ttl_s=60)})
    req = c.ExecutionRequest.model_validate({**req.model_dump(), "order_plan": plan, "entry_ttl_s": None})
    res = execution.simulate(req, market=market(offsets=(0, -5, -10)))
    assert res.management_stats["n_processed"] == 0
    assert any(e.kind == "expired" and e.reason == "entry_ttl" for e in res.canonical_events)


@pytest.mark.parametrize("base", BASES)
def test_follow_registrations_only_change_name_and_follow_flag(base):
    policy = c.resolve_policy(base + "-follow")
    body = policy.model_dump()
    assert body.pop("follow_teacher") is True
    body["version"] = base
    assert c.canonical_json(body) == c.canonical_json(c.resolve_policy(base).model_dump())
    assert c.load_policy_registry()[policy.version] == policy.content_hash
    req = request(version=policy.version, actions=[command("close_all")])
    assert execution.simulate(req, market=market()).management_stats["n_executed"] == 1


@pytest.mark.parametrize("base", BASES)
def test_kernel_b_rejects_follow_before_market_resolver_or_nautilus(base):
    req = request(version=base + "-follow")
    def forbidden(_):
        raise AssertionError("market resolver must not be called")
    with pytest.raises(KernelBUnavailable, match="follow_teacher"):
        execution.simulate(req, kernel="B", resolver=forbidden)
    with pytest.raises(KernelBUnavailable, match="follow_teacher"):
        simulate_b(req, market())


@pytest.mark.parametrize("bad", [
    {"fraction": D("-.1")}, {"fraction": D("1.1")}, {"fraction": D("NaN")},
    {"fraction": D(".0000000000001")}, {"kind": "none"},
    {"kind": "move_stop"}, {"kind": "move_stop", "stop_price": D(0)},
    {"kind": "close_all", "fraction": D(".5")}, {"to_entry": True}, {"at": T0.replace(tzinfo=None)},
])
def test_management_contract_rejects_bad_actions(bad):
    data = dict(at=T0 + timedelta(seconds=75), kind="reduce", source_message_id=17)
    data.update(bad)
    with pytest.raises((c.ContractError, ValueError)):
        c.ManagementAction(**data)


def test_management_request_time_boundaries_and_provenance_change_hash():
    for at in (0, -1, 180):
        with pytest.raises(c.ContractError):
            request(actions=[command("close_all", at)])
    with pytest.raises(c.ContractError, match="ordered"):
        request(actions=[command("reduce", 80), command("close_all", 75)])
    req = request(actions=[command("close_all")])
    serialized = req.model_dump_json()
    assert c.ExecutionRequest.model_validate_json(serialized).model_dump_json() == serialized
    modified = req.model_copy(update={"management": [command("close_all").model_copy(update={"source_message_id": 18})]})
    assert execution.simulate(req, market=market()).trace_hash != execution.simulate(modified, market=market()).trace_hash


def test_all_15_old_hashes_and_112_synthetic_cases_are_byte_identical(monkeypatch):
    frozen = json.loads(BASELINE.read_text())
    assert len(frozen["policy_hashes"]) == 15 and len(frozen["cases"]) == 112
    monkeypatch.setattr(a, "kernel_build_id", lambda *args: frozen["build"])
    for version, old_hash in frozen["policy_hashes"].items():
        policy = c.resolve_policy(version)
        assert policy.content_hash == c.load_policy_registry()[version] == old_hash
        assert "follow_teacher" not in policy.model_dump()
        assert c.canonical_json(policy) == c.canonical_json(policy.model_copy(update={"follow_teacher": False}))
    for case in frozen["cases"]:
        req = c.ExecutionRequest.model_validate(case["request"])
        explicit_empty = c.ExecutionRequest.model_validate({**case["request"], "management": []})
        m = c.MarketView.model_validate(case["market"])
        assert req.model_dump_json().encode() == explicit_empty.model_dump_json().encode()
        assert hashlib.sha256(req.model_dump_json().encode()).hexdigest() == case["request_bytes_sha256"]
        policy = c.resolve_policy(req.policy_version)
        rc = c.request_canonical(req, policy, t_start=req.resolved_t_start(policy), market_manifest_hash=m.manifest_hash)
        assert c.sha256_canonical(rc) == case["request_canonical_sha256"]
        res = execution.simulate(explicit_empty, market=m)
        assert c.sha256_canonical(res.canonical_events) == case["events_sha256"]
        assert (None if res.net_R is None else str(res.net_R)) == case["net_R"]
        assert res.trace_hash == case["trace_hash"]
        assert hashlib.sha256(res.model_dump_json().encode()).hexdigest() == case["result_bytes_sha256"]


def test_new_build_identity_covers_changed_source():
    frozen = json.loads(BASELINE.read_text())
    assert a.KERNEL_VERSION == "kernel-a-v0.7"
    digest = hashlib.sha256()
    for name in a._SRC_FILES:
        digest.update((Path(a.__file__).parent / name).read_bytes())
    assert a.kernel_build_id() == f"kernel-a-v0.7+{digest.hexdigest()[:12]}"
    assert a.kernel_build_id() != frozen["build"]
