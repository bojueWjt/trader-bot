"""G2 强平估值与 A24 机制的消费方夹具证据。

本模块只证明函数五项记账、三条硬约束及登记/禁令生效，不证明 G3 调用而非自算；
G3 尚未接入，须在接入时以哨兵证明其 θ 随返回值改变。
"""
import datetime as dt
import inspect
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D

import pytest

from quant_lab.market import contract as c
from quant_lab.market import single_source as gate
from quant_lab.market.kernel_a import KernelA, simulate_a

T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def sample():
    events = [c.CanonicalEvent(seq=i, ts=T0, kind=kind, leg=leg, order_id=str(i), qty=D(qty))
              for i, (kind, leg, qty) in enumerate([
                  ("filled", "entry", "10"), ("filled", "tp", "1"),
                  ("partial_fill", "tp", "2"), ("filled", "sl", "0.5"),
                  ("partial_fill", "close", "0.5"), ("working", "tp", "6"),
                  ("cancelled", "sl", "6"), ("funding", "funding", "6"),
              ])]
    return c.ExecutionResult(
        canonical_events=events, fill_status="filled", filled_qty=D(10),
        fees=D(3), funding=D(2), slippage=D(0), gross_pnl=D(17),
        net_pnl=None, net_R=None, censor_reason="LABEL_RIGHT_CENSORED",
        coverage_mask=c.CoverageMask(mark_ok=True, funding_ok=True, rules_ok=True, bars_ok=True),
        trace_hash="fixture", kernel="A", kernel_version="fixture", entry_avg_price=D(100),
    )


def inputs():
    base = c.POLICIES["fixture-zero-v1"]
    costs = {name: cost.model_copy(update={"taker_fee": fee})
             for name, fee in (("base", D(".001")), ("stress", D(".002")))
             for cost in [base.cost("base")]}
    return dict(mark=D(110), mark_at=T0, mark_source="fixture-mark-manifest",
                policy=base.model_copy(update={"costs": costs}), risk_budget=D(7),
                cost_scenario="base", side="long", multiplier=D(2))


@pytest.mark.parametrize("side,scenario,expected,fee", [
    ("long", "base", "19.240000000000", "1.320"),
    ("long", "stress", "19.051428571429", "2.640"),
    ("short", "base", "-15.045714285714", "1.320"),
    ("short", "stress", "-15.234285714286", "2.640"),
])
def test_accounting_and_read_only(side, scenario, expected, fee):
    res = sample()
    before = res.model_dump()
    events = res.canonical_events
    count = len(events)
    state = (res.fill_status, res.outcome_kind, res.censor_reason)
    assert c.outcome_kind(res) == "right_censored"
    args = {**inputs(), "side": side, "cost_scenario": scenario}
    val = c.force_close_net_R(res, **args)
    assert val.net_R_forced == D(expected)
    assert val.net_R_forced.as_tuple().exponent == -12
    assert val.residual_qty == D(6)
    assert val.close_fee == D(fee)
    assert (val.mark, val.mark_at, val.mark_source) == (args["mark"], T0, args["mark_source"])
    assert val.estimand == "forced_close"
    with pytest.raises(FrozenInstanceError):
        val.net_R_forced = D(0)
    assert res.canonical_events is events and len(events) == count
    assert (res.fill_status, res.outcome_kind, res.censor_reason) == state
    assert c.outcome_kind(res) == "right_censored"
    assert res.net_R is None and val.net_R_forced is not None
    assert res.model_dump() == before
    assert {"ForceCloseValuation", "force_close_net_R"} <= set(c.__all__)


@pytest.mark.parametrize("empty", ["closed", "unfilled"])
def test_no_residual_is_none(empty):
    res = sample().model_copy(update={"filled_qty": D(4)})
    if empty == "unfilled":
        res = res.model_copy(update={"filled_qty": D(0), "canonical_events": [], "entry_avg_price": None})
    assert c.force_close_net_R(res, **inputs()) is None


@pytest.mark.parametrize("field,message", [("gross_pnl", "内核未提供已实现损益"),
                                            ("entry_avg_price", "entry_avg_price")], ids=["gross", "entry"])
def test_missing_kernel_value_is_error(field, message):
    # 事件有成交和价格也不能成为第二份已实现损益来源。
    res = sample().model_copy(update={field: None})
    with pytest.raises(c.ContractError, match=message):
        c.force_close_net_R(res, **inputs())


def fixture_consumer(res, bars, horizon_end):
    """仅测试侧消费方：取已闭合 bar，并消费返回的估值；不是 G3 θ 实现。"""
    eligible = [bar for bar in bars if bar.open_time + dt.timedelta(seconds=bar.interval_s) <= horizon_end]
    bar = max(eligible, key=lambda b: b.open_time)
    args = {**inputs(), "mark": bar.c, "mark_at": bar.open_time, "mark_source": "fixture-bars"}
    return c.force_close_net_R(res, **args)


def test_fixture_asof_and_sentinel(monkeypatch):
    horizon = T0 + dt.timedelta(minutes=2)
    bars = [c.Bar(open_time=T0 + dt.timedelta(seconds=seconds), interval_s=60,
                  o=D(price), h=D(price), l=D(price), c=D(price))
            for seconds, price in [(0, 100), (60, 110), (61, 120), (180, 999)]]
    res = sample()
    val = fixture_consumer(res, bars, horizon)
    assert val.mark_at == T0 + dt.timedelta(minutes=1)
    assert val.mark == D(110) and val.mark_source == "fixture-bars"
    assert val.net_R_forced == D("19.240000000000")
    sentinel = replace(val, net_R_forced=D("123.456"))
    monkeypatch.setattr(c, "force_close_net_R", lambda *args, **kwargs: sentinel)
    assert fixture_consumer(res, bars, horizon).net_R_forced == D("123.456")


@pytest.mark.parametrize("route", ["hold_end", "horizon"])
def test_right_censor_routes_remain_reachable(route, monkeypatch):
    fx = next(f for f in c.load_fixtures("tests/market/fixtures/episodes") if f.id == "E15a")
    req = fx.request
    if route == "hold_end":
        expiry = req.order_plan.expiry.model_copy(update={"max_holding_s": 30})
        plan = req.order_plan.model_copy(update={"expiry": expiry})
        req = req.model_copy(update={"order_plan": plan})
    reached = []
    original = KernelA.censor_now

    def capture(self, reason, *args):
        if reason == "LABEL_RIGHT_CENSORED":
            reached.append(inspect.currentframe().f_back.f_locals["ts"])
        return original(self, reason, *args)

    monkeypatch.setattr(KernelA, "censor_now", capture)
    res = simulate_a(req, fx.market)
    assert len(reached) == 1
    if route == "hold_end":
        assert reached == [req.resolved_t_start(c.POLICIES[req.policy_version]) + dt.timedelta(seconds=30)]
    else:
        assert reached == [req.horizon_end]
    assert c.outcome_kind(res) == "right_censored"
    assert not any(e.kind == "closed" for e in res.canonical_events)
    val = c.force_close_net_R(res, **inputs())
    assert val is not None and res.net_R is None
    assert c.outcome_kind(res) == "right_censored"


def test_force_close_empty_registration():
    assert gate.ALLOWED_CALLERS["force_close_net_R"] == set()
    assert gate.ALLOWED_CALL_COUNTS["force_close_net_R"] == {}
    assert gate.collect_call_sites(gate.SRC, {"force_close_net_R"}) == {"force_close_net_R": set()}
    assert gate.collect_call_counts(gate.SRC, {"force_close_net_R"}) == {"force_close_net_R": {}}


@pytest.mark.parametrize("deviation", ["extra", "bypass", "internal_bypass"])
def test_force_close_fixture_registration(tmp_path, deviation):
    root = tmp_path / "quant_lab"
    package = root / "market"
    package.mkdir(parents=True)
    path = package / "consumer.py"
    source = "def consumer():\n    first = force_close_net_R(res)\n    return force_close_net_R(res)\n"
    declared = {"force_close_net_R": {"market/consumer.py:consumer"}}
    counts = {"force_close_net_R": {"market/consumer.py:consumer": 2}}
    path.write_text(source)
    assert gate.diff_call_sites(declared, gate.collect_call_sites(root, set(declared))) == []
    assert gate.collect_call_counts(root, set(declared)) == counts
    if deviation == "extra":
        source += "def unregistered():\n    return force_close_net_R(res)\n"
    elif deviation == "bypass":
        source = source.replace("force_close_net_R(res)", "(mark - res.entry_avg_price)")
    else:
        source = source.replace("return force_close_net_R(res)", "return 0")
    path.write_text(source)
    if deviation == "internal_bypass":
        assert gate.diff_call_sites(declared, gate.collect_call_sites(root, set(declared))) == []
    else:
        assert gate.diff_call_sites(declared, gate.collect_call_sites(root, set(declared)))
    assert gate.collect_call_counts(root, set(declared)) != counts
    if deviation == "bypass":
        assert {p for p, _, _, _ in gate.violations(gate.lint_tree(root))} == {"P8_inline_force_close"}


@pytest.mark.parametrize("mark", ["mark", "quote.mark"])
def test_force_close_inline_ban_independent(tmp_path, mark):
    root = tmp_path / "quant_lab"
    package = root / "market"
    package.mkdir(parents=True)
    # 无任何函数调用，单独证明禁令；含不可达区域，属于语法证据而非行为证据。
    (package / "consumer.py").write_text(f"def consumer():\n    return 0\n    return ({mark} - res.entry_avg_price) * qty\n")
    bad = gate.violations(gate.lint_tree(root))
    assert len(bad) == 1 and bad[0][0] == "P8_inline_force_close"
    (package / "consumer.py").unlink()
    (package / "contract.py").write_text(f"def force_close_net_R():\n    return ({mark} - res.entry_avg_price) * qty\n")
    assert gate.violations(gate.lint_tree(root)) == []
