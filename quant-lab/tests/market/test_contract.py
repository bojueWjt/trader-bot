"""M-06：执行合同 schema、规范 JSON/trace_hash、不变量断言、夹具集（≥10 且期望自洽）。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from quant_lab.market import contract as c

EP = Path(__file__).parent / "fixtures" / "episodes"
T0 = dt.datetime(2024, 1, 1, 1, 0, tzinfo=dt.UTC)


def plan(**kw):
    base = dict(instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long", entries=[c.Entry(kind="limit", price_lo=100, price_hi=100, fraction=Decimal(1))], stop=c.Stop(price=95),
                tps=[c.TakeProfit(level=105, fraction=1)], sizing=c.Sizing(mode="risk_budget"), expiry=c.Expiry(entry_ttl_s=60))
    base.update(kw)
    return c.OrderPlan(**base)


def req(**kw):
    base = dict(episode_id="e", graph_version="g", decision_snapshot_hash="h", t_dec=T0, order_plan=plan(), policy_version="fixture-zero-v1",
                policy_hash=c.resolve_policy("fixture-zero-v1").content_hash, risk_budget=5, market_manifest="m", horizon_end=T0 + dt.timedelta(hours=1),
                horizon_source="caller")
    base.update(kw)
    return c.ExecutionRequest(**base)


# ---------------- schema ----------------
@pytest.mark.parametrize("bad", [
    dict(entries=[c.Entry(kind="limit", price_lo=100, price_hi=100, fraction=Decimal("0.5"))]),        # fractions != 1
    dict(stop=c.Stop(price=100)),                                                                    # long stop >= entry
    dict(tps=[c.TakeProfit(level=100, fraction=1)]),                                                # long tp <= entry
    dict(tps=[c.TakeProfit(level=105, fraction=Decimal("0.6")), c.TakeProfit(level=110, fraction=Decimal("0.6"))]),  # tp sum > 1
    dict(side="short", stop=c.Stop(price=95), tps=[c.TakeProfit(level=90, fraction=1)]),             # short stop below entry
])
def test_order_plan_rejects(bad):
    with pytest.raises(c.ContractError):
        plan(**bad)


def test_entry_limit_requires_single_price_and_positive():
    with pytest.raises(c.ContractError):
        c.Entry(kind="limit", price_lo=98, price_hi=100)
    with pytest.raises(c.ContractError):
        c.Entry(kind="ladder", price_lo=0, price_hi=100)
    assert c.Entry(kind="ladder", price_lo=98, price_hi=100).fraction is None      # §5.10 B8：无默认 1


def test_request_validation():
    with pytest.raises(c.ContractError):
        req(t_dec=dt.datetime(2024, 1, 1))                                   # naive
    with pytest.raises(c.ContractError):
        req(horizon_end=T0)                                                  # horizon <= t_start
    with pytest.raises(c.ContractError):
        req(execution_contract_version="g2-exec-v9")
    with pytest.raises(Exception):
        req(risk_budget=0)
    r = req()
    assert r.resolved_t_start(c.resolve_policy("fixture-zero-v1")) == T0
    # S20（B9 同族）：t_start 是"policy 决定"的解析记录——只接受等于 t_dec + policy.latency_s 的显式值
    assert req(t_start=T0).t_start == T0 and c.resolve_policy("fixture-zero-v1").latency_s == 0
    for bogus in (T0 + dt.timedelta(seconds=5), T0 + dt.timedelta(minutes=1)):
        with pytest.raises(c.ContractError, match="t_start"):
            req(t_start=bogus)
    with pytest.raises(c.ContractError):
        req(t_start=T0 - dt.timedelta(seconds=1))


def test_policy_hash_is_content_addressed_and_required():
    hashes = {v: p.content_hash for v, p in c.POLICIES.items()}
    assert len(set(hashes.values())) == len(hashes)                                  # (policy_version, policy_hash) 一一对应
    with pytest.raises(c.ContractError, match="policy_hash"):
        req(policy_hash="0" * 64)
    with pytest.raises(c.ContractError, match="policy_hash"):
        req(policy_version="fixture-tick-v1")                                        # 版本换了、哈希没换
    assert req(policy_version="fixture-tick-v1", policy_hash=hashes["fixture-tick-v1"]).policy_hash == hashes["fixture-tick-v1"]
    with pytest.raises(Exception):
        plan(instrument_id="BTCUSDT")                                                 # 裁定 B1 格式


def test_build_request_from_episode_row():
    row = {"episode_id": "ep-1", "graph_version": "gv-3", "decision_snapshot_hash": "dsh", "t_dec": "2024-01-01T01:00:00Z",
           "order_plan": plan().model_dump()}
    h = c.resolve_policy("base-v1").content_hash
    r = c.build_request(row, policy_version="base-v1", policy_hash=h, risk_budget=Decimal(50), market_manifest="m1", seed=3)
    assert r.episode_id == "ep-1" and r.t_dec == T0 and r.policy_hash == h and r.seed == 3
    # §5.14 B12：推导窗口用 research_horizon_s（研究标准观察窗），不是 max_horizon_s（安全上限）
    # §5.14 B12 + S24：推导窗口 = ttl + 持仓段（两条分支同构，单一来源 derived_window_s）
    pol_b = c.resolve_policy("base-v1")
    assert r.horizon_end == T0 + dt.timedelta(seconds=c.derived_window_s(r.order_plan, pol_b, r.entry_ttl_s)) and r.horizon_source == "policy"
    assert c.derived_window_s(r.order_plan, pol_b, r.entry_ttl_s) == r.entry_ttl_s + pol_b.research_horizon_s
    plan2 = plan(expiry=c.Expiry(entry_ttl_s=60, max_holding_s=3600))
    r2 = c.build_request({**row, "order_plan": plan2}, policy_version="base-v1", policy_hash=h, risk_budget=Decimal(50), market_manifest="m1")
    assert r2.horizon_end == T0 + dt.timedelta(seconds=3660) and r2.horizon_source == "policy"   # 计划自带 max_holding → ttl+hold
    with pytest.raises(c.ContractError, match="缺字段"):
        c.build_request({"episode_id": "x"}, policy_version="base-v1", policy_hash=h, risk_budget=Decimal(1), market_manifest="m")
    with pytest.raises(c.ContractError, match="policy_hash"):
        c.build_request(row, policy_version="base-v1", policy_hash="bad", risk_budget=Decimal(1), market_manifest="m")


def test_b5_entry_ttl_nullable_resolved_by_policy_and_hashed():
    """契约 §5.9 B5：计划 TTL 为空 → policy 兜底；解析值进显式字段与 trace_hash；只差 entry_ttl_s 的两条 policy 哈希不同。"""
    row = {"episode_id": "ep-2", "graph_version": "gv", "decision_snapshot_hash": "d", "t_dec": T0,
           "order_plan": plan(expiry=c.Expiry(entry_ttl_s=None)).model_dump()}          # 真实 G1 行：25/26 为 null
    h = c.resolve_policy("base-v1").content_hash
    r = c.build_request(row, policy_version="base-v1", policy_hash=h, risk_budget=Decimal(5), market_manifest="m")
    assert r.entry_ttl_s == c.resolve_policy("base-v1").entry_ttl_s == 86400 and r.entry_ttl_source == "policy"
    r2 = c.build_request({**row, "order_plan": plan(expiry=c.Expiry(entry_ttl_s=60)).model_dump()}, policy_version="base-v1", policy_hash=h, risk_budget=Decimal(5), market_manifest="m")
    assert r2.entry_ttl_s == 60 and r2.entry_ttl_source == "plan"
    assert "entry_ttl_s" in c.REQUEST_ID_COLS
    p1 = c.resolve_policy("base-v1")
    p2 = p1.model_copy(update={"entry_ttl_s": 3600})
    assert p1.content_hash != p2.content_hash
    pol = c.resolve_policy("fixture-zero-v1")
    rc1 = c.request_canonical(r, pol, t_start=T0, market_manifest_hash="m")
    rc2 = c.request_canonical(r.model_copy(update={"entry_ttl_s": 3600}), pol, t_start=T0, market_manifest_hash="m")
    assert c.trace_hash(kernel="A", kernel_version="v", req_canonical=rc1, events=[]) != c.trace_hash(kernel="A", kernel_version="v", req_canonical=rc2, events=[])
    with pytest.raises(c.ContractError, match="不一致"):
        req(entry_ttl_s=99)                                                          # 计划给 60、显式 99 → 拒


def test_b8_fractions_nullable_policy_split_and_hashed():
    """契约 §5.10 B8 验收 (a)–(e)。"""
    # (a) 真实 G1 行：entries/tps fraction 全 null → 成功构造，policy 等分
    plan_null = {"instrument_id": "BTCUSDT-PERP.BINANCE-UM", "side": "long",
                 "entries": [{"kind": "ladder", "price_lo": "98", "price_hi": "100"}, {"kind": "limit", "price_lo": "97", "price_hi": "97"}, {"kind": "limit", "price_lo": "96", "price_hi": "96"}],
                 "stop": {"price": "90"}, "tps": [{"level": "105"}, {"level": "110"}, {"level": "120"}],
                 "sizing": {"mode": "fixed_qty", "qty": "3"}, "expiry": {"entry_ttl_s": None}}
    row = {"episode_id": "g1-null", "graph_version": "gv", "decision_snapshot_hash": "d", "t_dec": T0, "order_plan": plan_null}
    h = c.resolve_policy("base-v1").content_hash
    r = c.build_request(row, policy_version="base-v1", policy_hash=h, risk_budget=Decimal(5), market_manifest="m")
    # (d) 3 腿等分精确和为 1（标度 12，余量末腿），禁浮点
    assert sum(r.entry_fractions) == Decimal(1) and r.entry_fractions[0] == Decimal("0.333333333333") and r.entry_fractions[-1] == Decimal("0.333333333334")
    assert sum(r.tp_fractions) == c.resolve_policy("base-v1").tp_total_fraction == Decimal(1)
    assert r.fraction_source == "policy" and "entry_fractions" in c.REQUEST_ID_COLS and "tp_fractions" in c.REQUEST_ID_COLS
    # (c) plan 给出 → plan；entries 给 tps 不给 → policy（保守）
    r_plan = req()
    assert r_plan.fraction_source == "plan" and r_plan.entry_fractions == (Decimal(1),) and r_plan.tp_fractions == (Decimal(1),)
    mixed = plan(tps=[c.TakeProfit(level=105)])
    assert c.ExecutionRequest.model_validate({**r_plan.model_dump(exclude={"entry_fractions", "tp_fractions"}), "order_plan": mixed}).fraction_source == "policy"
    # (b) 只差 tp_total_fraction 的两条**已登记** policy → policy_hash 与 trace_hash 都不同
    p1, p2 = c.resolve_policy("fixture-zero-v1"), c.resolve_policy("fixture-halftp-v1")
    assert p1.content_hash != p2.content_hash and p2.tp_total_fraction == Decimal("0.5")
    r1 = c.build_request(row, policy_version="fixture-zero-v1", policy_hash=p1.content_hash, risk_budget=Decimal(5), market_manifest="m")
    r2 = c.build_request(row, policy_version="fixture-halftp-v1", policy_hash=p2.content_hash, risk_budget=Decimal(5), market_manifest="m")
    assert sum(r1.tp_fractions) == Decimal(1) and sum(r2.tp_fractions) == Decimal("0.5")
    rc1 = c.request_canonical(r1, p1, t_start=T0, market_manifest_hash="m")
    rc2 = c.request_canonical(r2, p2, t_start=T0, market_manifest_hash="m")
    assert c.trace_hash(kernel="A", kernel_version="v", req_canonical=rc1, events=[]) != c.trace_hash(kernel="A", kernel_version="v", req_canonical=rc2, events=[])
    # (e) 部分给出 → ContractError
    with pytest.raises(c.ContractError, match="全部给出"):
        c.OrderPlan.model_validate({**plan_null, "entries": [{**plan_null["entries"][0], "fraction": "0.6"}, plan_null["entries"][1], plan_null["entries"][2]]})
    with pytest.raises(c.ContractError, match="全部给出"):
        c.OrderPlan.model_validate({**plan_null, "tps": [{**plan_null["tps"][0], "fraction": "0.5"}, plan_null["tps"][1], plan_null["tps"][2]]})
    # 显式字段与计划给值不一致 → 拒
    with pytest.raises(c.ContractError, match="不一致|和为 1"):
        c.ExecutionRequest.model_validate({**r_plan.model_dump(), "entry_fractions": (Decimal("0.5"),)})
    two = plan(entries=[c.Entry(kind="limit", price_lo=100, price_hi=100, fraction=Decimal("0.6")), c.Entry(kind="limit", price_lo=99, price_hi=99, fraction=Decimal("0.4"))])
    r_two = c.ExecutionRequest.model_validate({**r_plan.model_dump(exclude={"entry_fractions", "tp_fractions"}), "order_plan": two})
    assert r_two.entry_fractions == (Decimal("0.6"), Decimal("0.4"))
    with pytest.raises(c.ContractError, match="不一致"):
        c.ExecutionRequest.model_validate({**r_two.model_dump(), "entry_fractions": (Decimal("0.5"), Decimal("0.5"))})
    assert c.Entry(kind="limit", price_lo=Decimal(1), price_hi=Decimal(1)).fraction is None      # 无默认 1


def test_policy_registry():
    with pytest.raises(c.ContractError):
        c.resolve_policy("nope")
    for v, p in c.POLICIES.items():
        assert p.version == v and set(p.costs) >= {"base", "stress"} and len(p.content_hash) == 64
    assert c.resolve_policy("base-v1").content_hash == c.resolve_policy("base-v1").content_hash
    assert c.resolve_policy("base-v1").content_hash != c.resolve_policy("fixture-zero-v1").content_hash
    with pytest.raises(c.ContractError):
        c.resolve_policy("base-v1").cost("weird")


# ---------------- canonical json / trace_hash ----------------
def test_canonical_json_rules():
    s = c.canonical_json({"b": Decimal("1.500"), "a": [Decimal("-0.0"), Decimal("10")], "t": T0, "n": None})
    assert s == '{"a":["0","10"],"b":"1.5","n":null,"t":"2024-01-01T01:00:00.000000Z"}'
    with pytest.raises(c.ContractError):
        c.canonical_json({"x": 1.5})
    with pytest.raises(c.ContractError):
        c.canonical_json({"x": Decimal("NaN")})
    assert c.canonical_json({"x": Decimal("1E+2")}) == '{"x":"100"}'


def test_trace_hash_sensitivity():
    r = req()
    pol = c.resolve_policy(r.policy_version)
    ev = [c.CanonicalEvent(seq=0, ts=T0, kind="submitted", order_id="entry-0", leg="entry", price=100, qty=1)]
    rc = c.request_canonical(r, pol, t_start=T0, market_manifest_hash="mh")
    h1 = c.trace_hash(kernel="A", kernel_version="v", req_canonical=rc, events=ev)
    assert h1 == c.trace_hash(kernel="A", kernel_version="v", req_canonical=rc, events=list(ev))
    assert h1 != c.trace_hash(kernel="B", kernel_version="v", req_canonical=rc, events=ev)
    assert h1 != c.trace_hash(kernel="A", kernel_version="v", req_canonical=c.request_canonical(r, pol, t_start=T0, market_manifest_hash="mh2"), events=ev)
    ev2 = [ev[0].model_copy(update={"price": Decimal(101)})]
    assert h1 != c.trace_hash(kernel="A", kernel_version="v", req_canonical=rc, events=ev2)
    assert rc["policy"]["content_hash"] == pol.content_hash and rc["t_start"] == T0


# ---------------- 不变量 ----------------
def load(eid):
    return c.load_fixture(EP / f"{eid}.json")


def mutate(exp: c.ExpectedResult, i: int, **upd) -> c.ExecutionResult:
    evs = list(exp.canonical_events)
    evs[i] = evs[i].model_copy(update=upd)
    return c.expected_as_result(exp.model_copy(update={"canonical_events": evs}))


def test_invariants_pass_on_gold_and_catch_violations():
    fx = load("E01")
    c.check_invariants(fx.request, c.expected_as_result(fx.expected))
    exp = fx.expected
    with pytest.raises(c.ExecutionInvariantError, match="seq"):
        c.check_invariants(fx.request, mutate(exp, 3, seq=99))
    with pytest.raises(c.ExecutionInvariantError, match="trigger_basis=mark"):
        c.check_invariants(fx.request, mutate(exp, 12, trigger_basis="mark"))          # sl fill 标成 mark
    with pytest.raises(c.ExecutionInvariantError, match="累计成交"):
        c.check_invariants(fx.request, mutate(exp, 3, qty=Decimal(2)))                  # entry fill 2 > 订单 1
    with pytest.raises(c.ExecutionInvariantError, match="reduce-only"):
        evs = list(exp.canonical_events)
        evs[4] = evs[4].model_copy(update={"qty": Decimal(2)})      # sl-0 订单量 2
        evs[12] = evs[12].model_copy(update={"qty": Decimal(2)})    # 平 2 > 仓位 1 → 翻转
        c.check_invariants(fx.request, c.expected_as_result(exp.model_copy(update={"canonical_events": evs})))
    with pytest.raises(c.ExecutionInvariantError, match="censor_reason"):
        c.check_invariants(fx.request, c.expected_as_result(exp.model_copy(update={"censor_reason": "MARK_STALE"})))
    with pytest.raises(c.ExecutionInvariantError, match="net_pnl"):
        c.check_invariants(fx.request, c.expected_as_result(exp.model_copy(update={"net_pnl": Decimal(-9)})))
    with pytest.raises(c.ExecutionInvariantError, match="filled_qty"):
        c.check_invariants(fx.request, c.expected_as_result(exp.model_copy(update={"filled_qty": Decimal(2)})))
    with pytest.raises(c.ExecutionInvariantError, match="closed 时仓位非零"):
        evs = [e for e in exp.canonical_events if not (e.kind == "filled" and e.leg == "sl")]
        evs = [e.model_copy(update={"seq": i}) for i, e in enumerate(evs)]
        c.check_invariants(fx.request, c.expected_as_result(exp.model_copy(update={"canonical_events": evs, "exit_avg_price": None})))


def test_tp_before_fill_rejected():
    fx = load("E03")
    evs = list(fx.expected.canonical_events)
    tp = next(e for e in evs if e.kind == "tp_triggered")
    evs = [tp.model_copy(update={"seq": 0, "ts": evs[0].ts})] + [e.model_copy(update={"seq": i + 1}) for i, e in enumerate(evs)]
    with pytest.raises(c.ExecutionInvariantError, match="无 entry fill"):
        c.check_invariants(fx.request, c.expected_as_result(fx.expected.model_copy(update={"canonical_events": evs})))


# ---------------- 夹具集 ----------------
def test_fixture_count_and_integrity():
    files = sorted(EP.glob("*.json"))
    assert len(files) >= 10
    fx = c.load_fixtures(EP)
    ids = [f.id for f in fx]
    assert len(set(ids)) == len(ids)
    for f in fx:
        assert f.id == Path(EP / f"{f.id}.json").stem and len(f.derivation) > 20 and {"A", "B"} <= set(f.kernels)
        c.check_invariants(f.request, c.expected_as_result(f.expected))       # 手工期望必须自洽
        assert f.request.market_manifest == f.market.manifest_id
        # 期望与自身 diff 为空
        assert c.diff_result(f.expected, c.expected_as_result(f.expected)) == []
    # 契约 §4 覆盖清单
    titles = " ".join(f.title for f in fx)
    for kw in ("跳空", "异步", "mark", "funding", "部分成交", "同 bar 双触", "到期", "reduce-only", "区间入场梯", "多档止盈"):
        assert kw in titles, kw


def test_diff_result_reports_first_event_diff_and_scalars():
    fx = load("E01")
    act = mutate(fx.expected, 12, price=Decimal(91))
    d = c.diff_result(fx.expected, act)
    assert d and d[0].startswith("event[12].price") and "expected=Decimal('90')" in d[0]
    act2 = c.expected_as_result(fx.expected.model_copy(update={"net_R": Decimal("-2.000000000000")}))
    assert c.diff_result(fx.expected, act2) == []                                     # Decimal 数值相等即一致
    short = fx.expected.model_copy(update={"canonical_events": fx.expected.canonical_events[:-1]})
    assert any("events length" in x for x in c.diff_result(fx.expected, c.expected_as_result(short)))


def test_floor_step_and_quantize():
    assert c.floor_step(Decimal("2.999"), Decimal("0.01")) == Decimal("2.99")
    assert c.floor_step(Decimal("7"), Decimal("1")) == 7
    assert c.quantize_money(Decimal("0.123456789")) == Decimal("0.12345679")
    assert c.quantize_ratio(Decimal(5) / Decimal(15)) == Decimal("0.333333333333")
