"""M-07：候选 A 参考实现——夹具全过、不变量全过、trace_hash 重放一致、路径情景、filter 合法/非法对照、随机不变量。"""
from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal
from pathlib import Path

import pytest

from quant_lab.market import contract as c
from quant_lab.market import execution as x
from quant_lab.market.kernel_a import KERNEL_VERSION, KernelA, simulate_a

EP = Path(__file__).parent / "fixtures" / "episodes"
FIX = c.load_fixtures(EP)
T0 = dt.datetime(2024, 1, 1, 1, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize("fx", FIX, ids=[f.id for f in FIX])
def test_fixture_matches_independent_expectation(fx: c.EpisodeFixture):
    res = simulate_a(fx.request, fx.market)
    assert c.diff_result(fx.expected, res) == [], c.diff_result(fx.expected, res)[:3]
    c.check_invariants(fx.request, res)
    assert res.kernel == "A" and res.kernel_version.startswith(KERNEL_VERSION + "+") and len(res.trace_hash) == 64


def test_entry_ttl_source_both_values_in_results():
    fx = next(f for f in FIX if f.id == "E09")
    assert simulate_a(fx.request, fx.market).entry_ttl_source == "plan"
    plan = fx.request.order_plan.model_copy(update={"expiry": c.Expiry(entry_ttl_s=None)})
    req = c.ExecutionRequest(**{**fx.request.model_dump(exclude={"order_plan", "entry_ttl_s"}), "order_plan": plan})
    r = simulate_a(req, fx.market)
    assert r.entry_ttl_source == "policy" and req.entry_ttl_s == 86400 and r.censor_reason == "LABEL_RIGHT_CENSORED"   # TTL 24h：T+60 last=90 成交，窗口结束仍持仓


def test_replay_hash_consistent_across_runs_and_orderings():
    a = {f.id: simulate_a(f.request, f.market).trace_hash for f in FIX}
    b = {f.id: simulate_a(f.request, f.market).trace_hash for f in reversed(FIX)}
    assert a == b and len(set(a.values())) == len(a)                      # 不同 episode 哈希不同


def test_trace_hash_changes_with_policy_seed_market_but_not_ingested_fields():
    fx = next(f for f in FIX if f.id == "E01")
    base = simulate_a(fx.request, fx.market).trace_hash
    assert simulate_a(fx.request.model_copy(update={"seed": 7}), fx.market).trace_hash != base
    tick = fx.request.model_copy(update={"policy_version": "fixture-tick-v1", "policy_hash": c.resolve_policy("fixture-tick-v1").content_hash})
    assert simulate_a(tick, fx.market).trace_hash != base
    m2 = fx.market.model_copy(update={"manifest_id": "fixture-E01", "mark": fx.market.mark + [c.PricePoint(ts=T0 + dt.timedelta(minutes=30), price=Decimal(100))]})
    assert simulate_a(fx.request, m2).trace_hash != base                 # 无实际影响的行情变化也改变 manifest 哈希 → hash 变
    assert simulate_a(fx.request.model_copy(update={"graph_version": "gv-fixture"}), fx.market).trace_hash == base


def test_replay_cli_all_pass(capsys):
    rc = x.main(["replay", "--fixtures", str(EP), "--kernel", "A"])
    out = capsys.readouterr().out
    assert rc == 0 and f"passed={len(FIX)} failed=0" in out


# ---------------- 路径情景 ----------------
def bar(ts, o, h, l, cl, vol="0"):
    return c.Bar(open_time=ts, o=Decimal(o), h=Decimal(h), l=Decimal(l), c=Decimal(cl), volume=Decimal(vol))


def test_path_expansion_rules():
    b = bar(T0, 100, 110, 90, 100)
    steps = lambda sc, side: [p.path_step for p in KernelA.expand_bar(b, sc, side, None, Decimal(1))]  # noqa: E731
    assert steps("primary", "long") == ["O", "H", "L", "C"]                # 等距 → H 先
    assert steps("primary", "short") == ["O", "H", "L", "C"]
    assert steps("adverse", "long") == ["O", "L", "H", "C"] and steps("adverse", "short") == ["O", "H", "L", "C"]
    assert steps("favorable", "long") == ["O", "H", "L", "C"] and steps("favorable", "short") == ["O", "L", "H", "C"]
    b2 = bar(T0, 100, 120, 95, 100)                                          # |O-L|=5 < |H-O|=20 → L 先
    assert [p.path_step for p in KernelA.expand_bar(b2, "primary", "long", None, Decimal(1))] == ["O", "L", "H", "C"]
    pts = KernelA.expand_bar(b, "primary", "long", None, Decimal(1))
    assert [p.ts - T0 for p in pts] == [dt.timedelta(0), dt.timedelta(seconds=20), dt.timedelta(seconds=40), dt.timedelta(seconds=60) - dt.timedelta(microseconds=1)]
    capd = KernelA.expand_bar(bar(T0, 100, 110, 90, 100, vol="1000"), "primary", "long", Decimal("0.1"), Decimal(1))
    assert all(p.capacity == Decimal(25) for p in capd)                     # floor(0.1×1000/4)


def test_scenario_interval_favorable_vs_adverse_same_bars():
    fx = next(f for f in FIX if f.id == "E04a")
    rs = {sc: simulate_a(fx.request.model_copy(update={"path_scenario": sc}), fx.market) for sc in ("primary", "adverse", "favorable")}
    assert rs["favorable"].net_R == Decimal(2) and rs["adverse"].net_R == Decimal(-2) and rs["primary"].net_R == Decimal(2)
    assert rs["favorable"].trace_hash != rs["adverse"].trace_hash


# ---------------- filter 合法 / 非法边界对照 ----------------
def make(req_upd=None, plan_upd=None, rules=None, policy="fixture-zero-v1"):
    fx = next(f for f in FIX if f.id == "E03")
    plan = fx.request.order_plan.model_copy(update=plan_upd or {})
    req = c.ExecutionRequest.model_validate({**fx.request.model_dump(exclude={"entry_fractions", "tp_fractions", "entry_ttl_s"}),
                                             "order_plan": plan, "policy_version": policy, "policy_hash": c.resolve_policy(policy).content_hash, **(req_upd or {})})
    mk = fx.market.model_copy(update={"rules": rules or fx.market.rules})
    return req, mk


def kinds(res):
    return [e.kind for e in res.canonical_events]


def test_filters_accept_at_boundary_and_reject_beyond():
    # MIN_NOTIONAL：刚好 100 接受，101 拒绝
    req, mk = make(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1))}, rules=c.Rules(min_notional=Decimal(100)))
    assert "rejected" not in kinds(simulate_a(req, mk)) and simulate_a(req, mk).fill_status == "filled"
    req, mk = make(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1))}, rules=c.Rules(min_notional=Decimal(101)))
    r = simulate_a(req, mk)
    assert kinds(r) == ["submitted", "rejected", "closed"] and r.canonical_events[1].reason == "MIN_NOTIONAL"
    # LOT_SIZE：step 1 下 qty 0.5 拒绝；step 0.5 接受
    req, mk = make(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal("0.5"))}, rules=c.Rules(step_size=Decimal(1)))
    assert simulate_a(req, mk).canonical_events[1].reason == "LOT_SIZE"
    req, mk = make(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal("0.5"))}, rules=c.Rules(step_size=Decimal("0.5")))
    assert simulate_a(req, mk).fill_status == "filled"
    # PRICE_FILTER：tick 0.5 下 100.5 合法
    req, mk = make(plan_upd={"entries": [c.Entry(kind="limit", price_lo=Decimal("100.5"), price_hi=Decimal("100.5"))],
                             "sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1))}, rules=c.Rules(tick_size=Decimal("0.5")))
    assert "rejected" not in kinds(simulate_a(req, mk))
    req, mk = make(plan_upd={"entries": [c.Entry(kind="limit", price_lo=Decimal("100.5"), price_hi=Decimal("100.5"))],
                             "sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1))}, rules=c.Rules(tick_size=Decimal(1)))
    assert simulate_a(req, mk).canonical_events[1].reason == "PRICE_FILTER"
    # MARGIN：钱包 99 拒绝；杠杆 2 → 预留 50 接受
    req, mk = make(policy="fixture-wallet99-v1")
    assert simulate_a(req, mk).canonical_events[1].reason == "MARGIN"
    pol = c.POLICIES["fixture-wallet99-v1"].model_copy(update={"leverage": Decimal(2)})
    assert KernelA(req, mk, pol).run().fill_status == "filled"


def test_risk_budget_sizing_and_multiplier():
    fx = next(f for f in FIX if f.id == "E03")
    req = fx.request.model_copy(update={"risk_budget": Decimal(12)})       # 12/5 = 2.4 → floor 2
    assert simulate_a(req, fx.market).filled_qty == Decimal(2)
    mk = fx.market.model_copy(update={"rules": c.Rules(multiplier=Decimal(2))})
    assert simulate_a(fx.request.model_copy(update={"risk_budget": Decimal(10)}), mk).filled_qty == Decimal(1)  # 10/(5×2)=1


# ---------------- 删失与覆盖 ----------------
def test_rules_unknown_and_lifecycle_censor():
    fx = next(f for f in FIX if f.id == "E03")
    r = simulate_a(fx.request, fx.market.model_copy(update={"rules_known": False}))
    assert r.censor_reason == "RULE_HISTORY_MISSING" and not r.coverage_mask.rules_ok and r.canonical_events == [] and r.net_R is None
    r = simulate_a(fx.request, fx.market.model_copy(update={"rules": c.Rules(effective_from=T0 + dt.timedelta(days=1))}))
    assert r.censor_reason == "SYMBOL_TIME_INVALID"
    r = simulate_a(fx.request, fx.market.model_copy(update={"bars_complete": False}))
    assert r.censor_reason == "BAR_GAP" and not r.coverage_mask.bars_ok


def test_horizon_expires_working_entries_without_fill():
    fx = next(f for f in FIX if f.id == "E09")
    plan = fx.request.order_plan.model_copy(update={"expiry": c.Expiry(entry_ttl_s=7200), "stop": c.Stop(price=Decimal(70)),
                                                    "sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1)),
                                                    "entries": [c.Entry(kind="limit", price_lo=Decimal(80), price_hi=Decimal(80), tif="GTD")]})
    req = c.ExecutionRequest.model_validate({**fx.request.model_dump(exclude={"entry_fractions", "tp_fractions", "entry_ttl_s"}),
                                             "order_plan": plan, "horizon_end": T0 + dt.timedelta(minutes=30)})   # last 最低 90，永不成交
    r = simulate_a(req, fx.market)
    assert [e.kind for e in r.canonical_events][-2:] == ["expired", "closed"] and r.fill_status == "none" and r.net_R == 0


# ---------------- 随机不变量（I03–I06、I10、I13、I14） ----------------
def random_market(rnd: random.Random, n=40) -> c.MarketView:
    last, mark = [], []
    px = Decimal(100)
    for i in range(n):
        px = max(Decimal(50), px + Decimal(rnd.randint(-4, 4)))
        ts = T0 + dt.timedelta(seconds=15 * i)
        last.append(c.PricePoint(ts=ts, price=px, capacity=None if rnd.random() < 0.5 else Decimal(rnd.randint(0, 3))))
        mark.append(c.PricePoint(ts=ts, price=max(Decimal(50), px + Decimal(rnd.randint(-2, 2)))))
    return c.MarketView(manifest_id="rand", last=last, mark=mark)


@pytest.mark.parametrize("seed", range(40))
def test_random_plans_satisfy_invariants(seed):
    rnd = random.Random(seed)
    side = rnd.choice(["long", "short"])
    entry = Decimal(rnd.randint(96, 104))
    off = Decimal(rnd.randint(2, 6))
    stop, tp1, tp2 = (entry - off, entry + off, entry + 2 * off) if side == "long" else (entry + off, entry - off, entry - 2 * off)
    tps = [c.TakeProfit(level=tp1, fraction=Decimal("0.5")), c.TakeProfit(level=tp2, fraction=Decimal("0.5"))] if rnd.random() < 0.5 else [c.TakeProfit(level=tp1, fraction=Decimal(1))]
    kind = rnd.choice(["limit", "ladder", "market_ref"])
    lo, hi = (entry - 1, entry + 1) if kind == "ladder" else (entry, entry)
    plan = c.OrderPlan(instrument_id="BTCUSDT-PERP.BINANCE-UM", side=side, entries=[c.Entry(kind=kind, price_lo=lo, price_hi=hi, tif=rnd.choice(["GTC", "IOC"]))],
                       stop=c.Stop(price=stop), tps=tps, sizing=c.Sizing(mode="fixed_qty", qty=Decimal(rnd.randint(1, 4))),
                       expiry=c.Expiry(entry_ttl_s=rnd.choice([30, 120, 3600])))
    pv = rnd.choice(["fixture-zero-v1", "fixture-tick-v1"])
    req = c.ExecutionRequest(episode_id=f"r{seed}", graph_version="g", decision_snapshot_hash="h", t_dec=T0, order_plan=plan,
                             policy_version=pv, policy_hash=c.resolve_policy(pv).content_hash, risk_budget=Decimal(10),
                             market_manifest="rand", horizon_end=T0 + dt.timedelta(minutes=rnd.choice([5, 10, 20])), horizon_source="caller",
                             path_scenario=rnd.choice(["primary", "adverse", "favorable"]))
    mk = random_market(rnd)
    r = simulate_a(req, mk)                       # 内部已 check_invariants
    assert simulate_a(req, mk).trace_hash == r.trace_hash
    if r.censor_reason is None and r.fill_status != "none":
        assert r.position_close_at is not None and r.canonical_events[-1].kind == "closed"
