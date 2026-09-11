"""A15（research-schema §9.10.11）：ExecutionResult → reconstructed_outcome.kind 七值映射 + exit_legs 诊断列。

规格：docs/adr/report-G2-outcome-kind-mapping.md。铁律：
- 两类删失不得互串（证据缺失 ≠ 标签未成熟）；rejected 不得并入 unfilled_expired；
- v0 无政策平仓腿（ADR C06 留 P2）⇒ filled_closed 不可达，断言其不可达而**不是**把 tp_hit 改判过去凑满七值。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import polars as pl
import pytest

from quant_lab.market import contract as c
from quant_lab.market import execution as x
from quant_lab.market.kernel_a import simulate_a
from tests.market.test_review_p1 import T0

FIX = {f.id: f for f in c.load_fixtures("tests/market/fixtures/episodes")}
RESULTS = {fid: simulate_a(f.request, f.market) for fid, f in FIX.items()}


def test_every_result_maps_to_exactly_one_of_seven_values():
    for fid, r in RESULTS.items():
        k = c.outcome_kind(r)
        assert k in c.OUTCOME_KINDS, (fid, k)
        assert c.outcome_kind(r) == r.outcome_kind == k        # 纯函数 + 属性一致
        hits = [
            r.censor_reason == "LABEL_RIGHT_CENSORED",
            r.censor_reason in c.EVIDENCE_CENSORS,
            r.fill_status == "none",
            r.fill_status != "none" and r.censor_reason is None,
        ]
        assert sum(bool(h) for h in hits) == 1, (fid, r.censor_reason, r.fill_status)


def test_two_censor_classes_never_cross():
    """MARK_STALE / BAR_GAP / FUNDING_SCHEDULE_GAP 等 → unevaluable；只有 LABEL_RIGHT_CENSORED → right_censored。"""
    assert c.outcome_kind(RESULTS["E15a"]) == "right_censored" and RESULTS["E15a"].censor_reason == "LABEL_RIGHT_CENSORED"
    assert c.outcome_kind(RESULTS["E15b"]) == "unevaluable" and RESULTS["E15b"].censor_reason == "MARK_STALE"
    assert c.outcome_kind(RESULTS["E16"]) == "unevaluable" and RESULTS["E16"].censor_reason == "FUNDING_SCHEDULE_GAP"
    for reason in c.EVIDENCE_CENSORS:
        faked = RESULTS["E15a"].model_copy(update={"censor_reason": reason})
        assert c.outcome_kind(faked) == "unevaluable", reason      # 证据缺失一律不得伪装成标签未成熟


def test_rejected_not_merged_into_unfilled_expired():
    for fid, reason in (("E14a", "PRICE_FILTER"), ("E14b", "MIN_NOTIONAL"), ("E14c", "MARGIN")):
        r = RESULTS[fid]
        assert c.outcome_kind(r) == "rejected" and r.fill_status == "none"
        assert next(e for e in r.canonical_events if e.kind == "rejected").reason == reason   # 细分读 reason，不扩枚举
    assert c.outcome_kind(RESULTS["E09"]) == "unfilled_expired"     # 挂了但市场没来


def test_stopped_takes_precedence_and_exit_legs_keeps_mixed_information():
    assert c.outcome_kind(RESULTS["E01"]) == "stopped" and RESULTS["E01"].exit_legs == ("sl",)
    r12 = RESULTS["E12"]                                            # 先两档 TP，余仓止损
    assert c.outcome_kind(r12) == "stopped" and r12.exit_legs == ("sl", "tp")
    assert c.outcome_kind(RESULTS["E03"]) == "tp_hit" and RESULTS["E03"].exit_legs == ("tp",)
    for fid in ("E09", "E14a", "E15a"):
        assert RESULTS[fid].exit_legs == ()


def test_six_values_reachable_and_filled_closed_unreachable_in_v0():
    seen = {c.outcome_kind(r) for r in RESULTS.values()}
    assert seen == {"stopped", "tp_hit", "rejected", "unfilled_expired", "right_censored", "unevaluable"}
    # filled_closed 是第 7 条兜底，专留给未来的政策平仓腿（leg="close"，ADR C06）：v0 任何完整平仓都先命中 stopped/tp_hit
    assert "filled_closed" not in seen
    assert not [e for r in RESULTS.values() for e in r.canonical_events if e.leg == "close" and e.kind in c.FILL_KINDS]
    # C06 落地后转正例：构造一条政策平仓腿成交即应得 filled_closed
    r = RESULTS["E03"]
    ev = [e.model_copy(update={"leg": "close"}) if (e.kind in c.FILL_KINDS and e.leg == "tp") else e for e in r.canonical_events]
    assert c.outcome_kind(r.model_copy(update={"canonical_events": ev})) == "filled_closed"


def test_batch_exposes_both_columns():
    markets = {f.market.manifest_id: f.market for f in FIX.values()}
    df = x.simulate_batch([f.request for f in FIX.values()], markets=markets)
    assert df.schema["outcome_kind"] == pl.Utf8 and df.schema["exit_legs"] == pl.List(pl.Utf8)
    assert set(df["outcome_kind"].to_list()) <= set(c.OUTCOME_KINDS) and df["outcome_kind"].null_count() == 0
    row = df.filter(pl.col("episode_id") == "E12")
    assert row["outcome_kind"][0] == "stopped" and row["exit_legs"][0].to_list() == ["sl", "tp"]
    assert "outcome_kind" in c.RESULT_SCALAR_COLS and "exit_legs" in c.RESULT_SCALAR_COLS


def test_no_fill_without_reject_or_expire_is_pending_ruling():
    """规范表空洞（已 block 给 G0）：未成交且无 rejected/expired 可达，却不命中契约 §3 七条规则。

    见 docs/adr/report-G2-outcome-kind-mapping.md §6。恒成立的不变量先钉死；
    末行钉住当前兜底行为（G2 建议 A），G0 若改判此断言会立刻失败并强制同步，不会静默漂移。
    """
    fx = FIX["E03"]
    from tests.market.test_review_p1 import rebuild
    plan = fx.request.order_plan.model_copy(update={
        "entries": [c.Entry(kind="limit", price_lo=D(80), price_hi=D(80), fraction=D(1), tif="IOC")],
        "stop": c.Stop(price=D(70)), "tps": [c.TakeProfit(level=D(105), fraction=D(1))],
        "sizing": c.Sizing(mode="fixed_qty", qty=D(1))})
    r = simulate_a(rebuild(fx.request, order_plan=plan), fx.market)
    kinds = [e.kind for e in r.canonical_events]
    assert r.fill_status == "none" and "rejected" not in kinds and "expired" not in kinds and "cancelled" in kinds
    k = c.outcome_kind(r)
    assert k in c.OUTCOME_KINDS and k not in ("rejected", "right_censored", "unevaluable")   # 恒成立
    assert r.exit_legs == () and r.net_R == 0
    assert k == "unfilled_expired"      # 待 G0 裁定（建议 A：规则④放宽为"未成交且非 rejected"）


def test_s19_explicit_ttl_cannot_bypass_policy_fallback():
    """五审 S19：作者 TTL=null 时，显式 entry_ttl_s 曾可在同一 policy_hash 下任意取值，
    把同一笔交易的标签从 tp_hit 翻成 unfilled_expired（与 S17 同型：显式字段不是独立输入）。"""
    fx = FIX["E03"]
    plan_null_ttl = fx.request.order_plan.model_copy(update={"expiry": c.Expiry(entry_ttl_s=None)})
    base = {k: v for k, v in fx.request.model_dump().items() if k not in ("entry_ttl_s", "entry_fractions", "tp_fractions")}
    r = c.ExecutionRequest.model_validate({**base, "order_plan": plan_null_ttl})
    pol_ttl = c.resolve_policy(fx.request.policy_version).entry_ttl_s
    assert r.entry_ttl_s == pol_ttl and r.entry_ttl_source == "policy"
    for bogus in (1, 60, pol_ttl - 1, pol_ttl + 1):
        with pytest.raises(c.ContractError, match="entry_ttl_s"):
            c.ExecutionRequest.model_validate({**base, "order_plan": plan_null_ttl, "entry_ttl_s": bogus})
    # 作者给值时同样只能逐值相等
    plan_given = fx.request.order_plan.model_copy(update={"expiry": c.Expiry(entry_ttl_s=600)})
    assert c.ExecutionRequest.model_validate({**base, "order_plan": plan_given}).entry_ttl_s == 600
    with pytest.raises(c.ContractError, match="entry_ttl_s"):
        c.ExecutionRequest.model_validate({**base, "order_plan": plan_given, "entry_ttl_s": pol_ttl})


def test_s18_b10_enumerated_rule4_and_no_fallback_labelling():
    """契约 v1.8 §5.12 B10：规则④枚举 expired|cancelled；未命中任何规则一律 raise，禁止兜底贴值。"""
    from tests.market.test_review_p1 import rebuild
    fx = FIX["E03"]
    plan = fx.request.order_plan.model_copy(update={
        "entries": [c.Entry(kind="limit", price_lo=D(80), price_hi=D(80), fraction=D(1), tif="IOC")],
        "stop": c.Stop(price=D(70)), "tps": [c.TakeProfit(level=D(105), fraction=D(1))],
        "sizing": c.Sizing(mode="fixed_qty", qty=D(1))})
    r = simulate_a(rebuild(fx.request, order_plan=plan), fx.market)
    kinds = {e.kind for e in r.canonical_events}
    assert "expired" not in kinds and "cancelled" in kinds            # 零成交 IOC：撤销终止，非到期
    assert c.outcome_kind(r) == "unfilled_expired"                    # 规则④ 枚举命中 cancelled
    # 未命中任何规则 → 必须抛错并回报实际事件集合，不得返回"看起来合理"的值
    doctored = r.model_copy(update={"canonical_events": [e for e in r.canonical_events if e.kind != "cancelled"]})
    with pytest.raises(c.ContractError, match="未命中契约|B10"):
        c.outcome_kind(doctored)
    closed_no_exit = FIX["E03"], simulate_a(FIX["E03"].request, FIX["E03"].market)
    res = closed_no_exit[1]
    no_exit = res.model_copy(update={"canonical_events": [e for e in res.canonical_events
                                                         if not (e.kind in c.FILL_KINDS and e.leg == "tp")]})
    with pytest.raises(c.ContractError, match="未命中契约|B10"):
        c.outcome_kind(no_exit)


def test_s21_primary_censor_follows_contract_priority_not_check_order():
    """S21：CENSOR_PRIORITY 此前是死常量，主因取决于代码检查顺序；候选 B 更严重——bars 分支无条件覆盖。"""
    from quant_lab.market import nautilus_adapter as nb
    fx = FIX["E03"]
    mk = fx.market.model_copy(update={"rules_known": False, "bars_quality_ok": False, "bars_complete": False})
    ra, rb = simulate_a(fx.request, mk), nb.simulate_b(fx.request, mk)
    # RULE_HISTORY_MISSING 优先级高于 BAR_GAP：两个内核都必须取前者
    assert c.CENSOR_PRIORITY.index("RULE_HISTORY_MISSING") < c.CENSOR_PRIORITY.index("BAR_GAP")
    assert ra.censor_reason == rb.censor_reason == "RULE_HISTORY_MISSING"
    assert not ra.coverage_mask.rules_ok and not ra.coverage_mask.bars_ok      # 两个 coverage 位都要落下
    assert not rb.coverage_mask.rules_ok and not rb.coverage_mask.bars_ok
    assert c.outcome_kind(ra) == c.outcome_kind(rb) == "unevaluable"


def test_s22_explicit_empty_allocation_is_rejected_not_silently_filled():
    """六审 S22：`entry_fractions=[]` 等合法假值曾被 `or` 兜底静默替换成推导值 (1,)。

    B9 要求不等即拒——"静默修补显式值"与"显式值绕过推导"是同一枚硬币的两面。
    """
    fx = FIX["E03"]
    base = {k: v for k, v in fx.request.model_dump().items() if k not in ("entry_fractions", "tp_fractions")}
    ok = c.ExecutionRequest.model_validate(base)
    assert ok.entry_fractions == (D(1),)                       # 省略 → 推导
    for empty in ([], ()):
        with pytest.raises(c.ContractError, match="长度|推导不一致"):
            c.ExecutionRequest.model_validate({**base, "entry_fractions": empty})
        with pytest.raises(c.ContractError, match="长度|推导不一致"):
            c.ExecutionRequest.model_validate({**base, "entry_fractions": empty, "tp_fractions": empty})
    # tps 非空时显式空 tp_fractions 同样必须被拒，而不是替换成等分
    with pytest.raises(c.ContractError, match="长度|推导不一致"):
        c.ExecutionRequest.model_validate({**base, "tp_fractions": []})


def test_s18_all_non_match_branches_report_actual_event_set():
    """B10 第 2 条：**每条**未命中分支都必须回报实际事件集合，含'有成交却无 closed'那条。"""
    r = simulate_a(FIX["E03"].request, FIX["E03"].market)
    no_closed = r.model_copy(update={"canonical_events": [e for e in r.canonical_events if e.kind != "closed"]})
    with pytest.raises(c.ContractError, match="实际事件集合") as ei:
        c.outcome_kind(no_closed)
    assert "filled" in str(ei.value)                            # 事件集合确实被列出
    no_exit = r.model_copy(update={"canonical_events": [e for e in r.canonical_events
                                                        if not (e.kind in c.FILL_KINDS and e.leg == "tp")]})
    with pytest.raises(c.ContractError, match="实际事件集合"):
        c.outcome_kind(no_exit)


def test_b11_b12_horizon_source_and_policy_split():
    """§5.13 B11 + §5.14 B12：观察窗可自选但必须声明并记账；上限与研究窗职责分离。"""
    row = {"episode_id": "h1", "graph_version": "gv", "decision_snapshot_hash": "d", "t_dec": T0,
           "order_plan": FIX["E03"].request.order_plan.model_dump()}
    pol = c.resolve_policy("base-v1")
    # ① 推导路径：用 research_horizon_s（研究标准窗），不是 max_horizon_s（安全上限）；标 policy
    r = c.build_request(row, policy_version="base-v1", policy_hash=pol.content_hash, risk_budget=D(5), market_manifest="m")
    assert r.horizon_source == "policy"
    assert (r.horizon_end - r.t_dec).total_seconds() == c.derived_window_s(r.order_plan, pol, r.entry_ttl_s) == r.entry_ttl_s + pol.research_horizon_s
    # ② 自选路径：合法但必须声明；未声明即拒（自由要可见，不能是隐形旋钮）
    custom = T0 + dt.timedelta(hours=6)
    r2 = c.build_request(row, policy_version="base-v1", policy_hash=pol.content_hash, risk_budget=D(5),
                         market_manifest="m", horizon_end=custom)
    assert r2.horizon_source == "caller" and r2.horizon_end == custom
    with pytest.raises(c.ContractError, match="horizon_source=policy"):
        c.ExecutionRequest.model_validate({**r2.model_dump(), "horizon_source": "policy"})
    # ③ 显式值必须留在 trace_hash（B11 ②，规范要求：防后续"优化"把它移出）
    rc1 = c.request_canonical(r2, pol, t_start=r2.t_dec, market_manifest_hash="m")
    rc2 = c.request_canonical(r2.model_copy(update={"horizon_end": custom + dt.timedelta(hours=1)}), pol, t_start=r2.t_dec, market_manifest_hash="m")
    assert "horizon_end" in rc1 and rc1["horizon_end"] != rc2["horizon_end"]
    assert c.trace_hash(kernel="A", kernel_version="v", req_canonical=rc1, events=[]) != \
           c.trace_hash(kernel="A", kernel_version="v", req_canonical=rc2, events=[])
    # ④ 安全上限与研究窗职责分离：超过 max_horizon_s 一律拒，两条路径都受封顶
    with pytest.raises(c.ContractError, match="安全上限"):
        c.build_request(row, policy_version="base-v1", policy_hash=pol.content_hash, risk_budget=D(5),
                        market_manifest="m", horizon_end=T0 + dt.timedelta(seconds=pol.max_horizon_s + 1))
    assert "horizon_source" in c.RESULT_SCALAR_COLS


def test_s24_s25_derivation_is_single_source_and_subsecond_cannot_bypass():
    """七审 S24/S25：推导式两处实现只改一处（漏加 ttl）；int() 截断让 +1µs 绕过对账与安全上限。"""
    fx = FIX["E03"]
    pol = c.resolve_policy("base-v1")
    row = {"episode_id": "s24", "graph_version": "gv", "decision_snapshot_hash": "d", "t_dec": T0,
           "order_plan": fx.request.order_plan.model_dump()}
    r = c.build_request(row, policy_version="base-v1", policy_hash=pol.content_hash, risk_budget=D(5), market_manifest="m")
    # S24-a：两条分支同构——有无 max_holding 都含 ttl 段
    with_hold = fx.request.order_plan.model_copy(update={"expiry": c.Expiry(entry_ttl_s=600, max_holding_s=3600)})
    assert c.derived_window_s(with_hold, pol, 600) == 600 + 3600
    assert c.derived_window_s(r.order_plan, pol, r.entry_ttl_s) == r.entry_ttl_s + pol.research_horizon_s
    # S24-b：research_horizon_s 是必填字段，没有默认值可静默补
    assert "research_horizon_s" in c.ExecutionPolicy.model_fields and c.ExecutionPolicy.model_fields["research_horizon_s"].is_required()
    # S25：亚秒偏移既不能绕过对账，也不能绕过安全上限
    for delta in (dt.timedelta(microseconds=1), dt.timedelta(microseconds=999999)):
        with pytest.raises(c.ContractError, match="推导值不一致"):
            c.ExecutionRequest.model_validate({**r.model_dump(), "horizon_end": r.horizon_end + delta})
        cap = r.t_dec + dt.timedelta(seconds=pol.max_horizon_s) + delta
        with pytest.raises(c.ContractError, match="安全上限"):
            c.ExecutionRequest.model_validate({**r.model_dump(), "horizon_end": cap, "horizon_source": "caller"})


def test_lint_forbidden_patterns_do_not_reappear():
    """**lint（禁止模式复现），非行为证据** —— 契约 §A23(2)。

    行为正确性由 test_c2_loader_missing_funding_and_unknown_rules 等行为测试保证；本条只防已删除的错误写法被重新引入。
    之所以不能只靠行为测试：G2-SC-01 的 multiplier 缺陷**今日不可达**（两个 rules 生产者都硬写 "1"），
    重新引入 `or "1"` 时行为测试不会变红，只有文本禁令会。
    """
    import inspect
    from quant_lab.market import execution as _x
    src = inspect.getsource(_x.load_market_from_lake)
    assert 'rr.get("multiplier") is not None' in src        # 纳入 rules_known 校验清单
    assert 'multiplier"] or "1"' not in src                 # 不再有 or 兜底
    assert 'min_notional"] or "0"' not in src


def test_s27_s28_single_source_start_time_and_exact_grid():
    """八审 S27/S28：loader 第三次推导启动时刻（漏 latency）；缺口网格按整秒取模，亚秒 t_start 误判 BAR_GAP。

    注：下面的 `not in src` 是 **lint（禁止模式复现）**，非行为证据；委派的行为证明见
    test_constants_effective.py 的哨兵法测试。
    """
    import inspect
    from quant_lab.market import execution as _x
    from quant_lab.market.kernel_a import KernelA
    src = inspect.getsource(_x.load_market_from_lake)
    assert "(req.t_start or req.t_dec)" not in src        # lint：旧的漏 latency 写法不得复现
    # S28：亚秒 t_start 下，完整行情不得被判 BAR_GAP（网格对齐用精确微秒）
    fx = FIX["E03"]
    base = {k: v for k, v in fx.request.model_dump().items() if k not in ("entry_fractions", "tp_fractions", "entry_ttl_s")}
    bars = [c.Bar(open_time=T0 + dt.timedelta(minutes=i), o=D(100), h=D(106), l=D(99), c=D(100), interval_s=60) for i in range(5)]
    for us in (0, 1, 999999):
        pol = c.POLICIES["fixture-zero-v1"].model_copy(update={"latency_s": 0})
        req = c.ExecutionRequest.model_validate({**base, "horizon_end": T0 + dt.timedelta(minutes=5), "horizon_source": "caller"})
        k = KernelA(req, c.MarketView(manifest_id=fx.market.manifest_id, bars_last=bars, bars_mark=bars), pol)
        k.t_start = T0 + dt.timedelta(microseconds=us)      # 直接置亚秒启动时刻，避开 t_start 对账
        gap = k._first_bar_gap(bars, req.horizon_end)
        assert gap is None or gap >= T0 + dt.timedelta(minutes=5), (us, gap)   # 完整行情不得报缺口


@pytest.mark.parametrize("later_version", [False, True], ids=["first-version", "later-version"])
def test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes(monkeypatch, later_version):
    """真实 result_row 边界产生冲突；必须拒绝最终输出并报告版本与全部 hash。"""
    assert "policy_hash" in c.PAIR_KEY
    fixtures = list(FIX.values())[:4]
    reqs = [f.request for f in fixtures]
    markets = {f.market.manifest_id: f.market for f in fixtures}
    original = x.result_row
    hashes = ["1" * 64, "2" * 64, "3" * 64]
    seen = []

    def injected(req, res):
        row = original(req, res)
        index = reqs.index(req)
        row["policy_version"] = "conflict-version-S31"
        row["policy_hash"] = hashes[max(0, index - 1)]
        if later_version and index == 0:
            row["policy_version"] = "unrelated-first-version"
        seen.append((row["policy_version"], row["policy_hash"]))
        return row

    monkeypatch.setattr(x, "result_row", injected)
    with pytest.raises(c.ContractError, match="policy_hash") as error:
        x.simulate_batch(reqs, markets=markets)
    assert len(seen) == 4
    message = str(error.value)
    assert "conflict-version-S31" in message
    assert all(h in message for h in hashes)
    if later_version:
        assert seen[0][0] == "unrelated-first-version"
        assert "unrelated-first-version" not in message


def test_s27_s28_single_source_start_time_and_exact_grid():
    """八审 S27/S28：loader 第三次推导启动时刻（漏 latency）；缺口网格按整秒取模，亚秒 t_start 误判 BAR_GAP。

    注：下面的 `not in src` 是 **lint（禁止模式复现）**，非行为证据；委派的行为证明见
    test_constants_effective.py 的哨兵法测试。
    """
    import inspect
    from quant_lab.market import execution as _x
    from quant_lab.market.kernel_a import KernelA
    src = inspect.getsource(_x.load_market_from_lake)
    assert "(req.t_start or req.t_dec)" not in src        # lint：旧的漏 latency 写法不得复现
    # S28：亚秒 t_start 下，完整行情不得被判 BAR_GAP（网格对齐用精确微秒）
    fx = FIX["E03"]
    base = {k: v for k, v in fx.request.model_dump().items() if k not in ("entry_fractions", "tp_fractions", "entry_ttl_s")}
    bars = [c.Bar(open_time=T0 + dt.timedelta(minutes=i), o=D(100), h=D(106), l=D(99), c=D(100), interval_s=60) for i in range(5)]
    for us in (0, 1, 999999):
        pol = c.POLICIES["fixture-zero-v1"].model_copy(update={"latency_s": 0})
        req = c.ExecutionRequest.model_validate({**base, "horizon_end": T0 + dt.timedelta(minutes=5), "horizon_source": "caller"})
        k = KernelA(req, c.MarketView(manifest_id=fx.market.manifest_id, bars_last=bars, bars_mark=bars), pol)
        k.t_start = T0 + dt.timedelta(microseconds=us)      # 直接置亚秒启动时刻，避开 t_start 对账
        gap = k._first_bar_gap(bars, req.horizon_end)
        assert gap is None or gap >= T0 + dt.timedelta(minutes=5), (us, gap)   # 完整行情不得报缺口



def test_s30_window_lower_bound_uses_derived_start_not_t_dec(tmp_path, monkeypatch):
    """九审 S30：观察窗下界曾用 (t_start or t_dec)，省略 t_start 时漏 latency——
    同一请求"省略"与"显式写同值"走两条不同边界，公共请求边界随表达方式分叉。"""
    fx = FIX["E03"]
    pol = c.POLICIES["fixture-zero-v1"].model_copy(update={"latency_s": 60})
    import json as _j
    monkeypatch.setitem(c.POLICIES, "probe-lat60-v1", pol.model_copy(update={"version": "probe-lat60-v1"}))
    original = c.POLICY_HASH_REGISTRY
    before = original.stat().st_mtime_ns
    reg = tmp_path / "policy_hashes.json"
    reg.write_text(_j.dumps({**_j.loads(original.read_text()), "probe-lat60-v1": c.POLICIES["probe-lat60-v1"].content_hash}))
    monkeypatch.setattr(c, "POLICY_HASH_REGISTRY", reg)
    base = {k: v for k, v in fx.request.model_dump().items()
            if k not in ("entry_fractions", "tp_fractions", "entry_ttl_s", "t_start")}
    base |= {"policy_version": "probe-lat60-v1", "policy_hash": c.POLICIES["probe-lat60-v1"].content_hash}
    exp_start = fx.request.t_dec + dt.timedelta(seconds=60)
    for delta, label in ((dt.timedelta(microseconds=-1), "推导起点前 1µs"), (dt.timedelta(0), "恰为推导起点")):
        for t_start in (None, exp_start):                  # 省略与显式写同值必须走同一条边界
            payload = {**base, "horizon_end": exp_start + delta}
            if t_start is not None:
                payload["t_start"] = t_start
            with pytest.raises(c.ContractError, match="horizon_end"):
                c.ExecutionRequest.model_validate(payload)
    ok = c.ExecutionRequest.model_validate({**base, "horizon_end": exp_start + dt.timedelta(microseconds=1)})
    assert ok.horizon_source == "caller" or ok.horizon_end > exp_start
    assert original.stat().st_mtime_ns == before


def test_s29_partition_grid_uses_single_source_and_no_empty_gaps():
    """九审 S29：分区体检自带一份整秒取整的网格数学——完整两 bar 却报 gap=True/n=0，右界 +1µs 时 missing=−1。"""
    from quant_lab.market import partition_check as pc
    import polars as pl
    inst = "BTCUSDT-PERP.BINANCE-UM"
    t0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    rows = [{"instrument_id": inst, "interval": "1m", "open_time": t0 + dt.timedelta(minutes=i),
             "close_time": t0 + dt.timedelta(minutes=i + 1), "open": 100.0, "high": 100.0, "low": 100.0,
             "close": 100.0, "volume": 1.0, "available_at": t0, "ingested_at": t0,
             "source_sha256": "h" * 64, "rule_version": "v1"} for i in range(2)]
    df = pl.DataFrame(rows, schema_overrides={c_: pl.Datetime("us", "UTC") for c_ in ("open_time", "close_time", "available_at", "ingested_at")})
    rules = pl.DataFrame([{"instrument_id": inst, "effective_from": t0, "effective_to": t0 + dt.timedelta(minutes=2),
                           "tick_size": "1", "step_size": "1", "min_notional": "0", "multiplier": "1",
                           "funding_interval_hours": 8, "status": "TRADING", "source": "probe"}], schema=pc.RULES_SCHEMA)
    out, qs, rep = pc.check_bars(df, pid="p", data_type="klines", interval="1m", inst=inst, period="2024-01", rules=rules)
    assert rep.expected_rows == 2 and rep.missing == 0            # 完整两 bar
    assert all(g["n"] > 0 for g in rep.gaps), rep.gaps            # 不得出现 n=0 的空缺口
    assert not out["gap_flag"][0], "首行不应标 gap（无缺失网格点）"
    # 右界带亚秒：expected 与 present 必须同窗口口径，missing 不得为负（九审 P29 的 expected3/actual4/missing−1 即此病）
    rules2 = rules.with_columns(pl.lit(t0 + dt.timedelta(minutes=2, microseconds=1)).cast(pl.Datetime("us", "UTC")).alias("effective_to"))
    _, _, rep2 = pc.check_bars(df, pid="p", data_type="klines", interval="1m", inst=inst, period="2024-01", rules=rules2)
    # 窗口 [t0, t0+2min+1µs) 含 3 个网格点，实有 2 根 → expected 3 / missing 1，算术自洽
    assert rep2.expected_rows == 3 and rep2.missing == 1 and rep2.missing >= 0
    rows3 = rows + [{**rows[0], "open_time": t0 + dt.timedelta(minutes=2), "close_time": t0 + dt.timedelta(minutes=3)}]
    df3 = pl.DataFrame(rows3, schema_overrides={c_: pl.Datetime("us", "UTC") for c_ in ("open_time", "close_time", "available_at", "ingested_at")})
    _, _, rep3 = pc.check_bars(df3, pid="p", data_type="klines", interval="1m", inst=inst, period="2024-01", rules=rules2)
    assert rep3.expected_rows == 3 and rep3.missing == 0 and all(g["n"] > 0 for g in rep3.gaps)
    # 多给一根窗口外的 bar：present 只数窗口内，missing 仍不得为负
    rows4 = rows3 + [{**rows[0], "open_time": t0 + dt.timedelta(minutes=3), "close_time": t0 + dt.timedelta(minutes=4)}]
    df4 = pl.DataFrame(rows4, schema_overrides={c_: pl.Datetime("us", "UTC") for c_ in ("open_time", "close_time", "available_at", "ingested_at")})
    _, _, rep4 = pc.check_bars(df4, pid="p", data_type="klines", interval="1m", inst=inst, period="2024-01", rules=rules2)
    assert rep4.missing >= 0, (rep4.expected_rows, rep4.missing)
