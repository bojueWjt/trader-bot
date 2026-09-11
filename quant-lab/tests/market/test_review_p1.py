"""review-G2-P1 必修 S01–S13 的反例回归：每条用审查报告给出的实跑反例（或其最小等价）作为验收。"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from quant_lab.market import asof, contract as c, execution as x
from quant_lab.market import nautilus_adapter as nb
from quant_lab.market import partition_check as pc
from quant_lab.market import vision as v
from quant_lab.market.kernel_a import simulate_a
from tests.market.test_vision import JAN, REL, bar_rows, make_zip, mock_client

EP = Path(__file__).parent / "fixtures" / "episodes"
FIX = {f.id: f for f in c.load_fixtures(EP)}
T0 = dt.datetime(2024, 1, 1, 1, 0, tzinfo=dt.UTC)
INST = "BTCUSDT-PERP.BINANCE-UM"


def pt(s, px, cap=None):
    return c.PricePoint(ts=T0 + dt.timedelta(seconds=s), price=Decimal(px), capacity=None if cap is None else Decimal(cap))


def rebuild(req: c.ExecutionRequest, **upd) -> c.ExecutionRequest:
    """改 order_plan/policy 后必须重新构造（重新解析 entry_ttl_s / entry_fractions / tp_fractions），不能 model_copy。"""
    base = req.model_dump(exclude={"entry_fractions", "tp_fractions", "entry_ttl_s"})
    return c.ExecutionRequest.model_validate({**base, **upd})


def e03(plan_upd=None, req_upd=None, market_upd=None, policy=None):
    fx = FIX["E03"]
    plan = fx.request.order_plan.model_copy(update=plan_upd or {})
    upd = {"order_plan": plan, **(req_upd or {})}
    if policy:
        upd.update({"policy_version": policy, "policy_hash": c.resolve_policy(policy).content_hash})
    req = rebuild(fx.request, **upd)
    mk = fx.market.model_copy(update=market_upd or {})
    return req, mk


def kinds(r):
    return [e.kind for e in r.canonical_events]


# S01 —— TP 历史触及不是永久可成交条件
def test_s01_tp_fill_requires_current_last_to_satisfy_limit():
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(2))},
                  market_upd={"last": [pt(0, 100, 2), pt(30, 105, 1), pt(60, 100, 1)], "mark": [pt(0, 100), pt(30, 100), pt(60, 100)]})
    r = simulate_a(req, mk)
    tp_fills = [e for e in r.canonical_events if e.kind in c.FILL_KINDS and e.leg == "tp"]
    assert len(tp_fills) == 1 and tp_fills[0].price == Decimal(105) and tp_fills[0].qty == 1     # T+60 last=100 不能卖 105
    assert r.censor_reason == "LABEL_RIGHT_CENSORED" and r.net_R is None                          # 余仓右删失
    # short 镜像 + 首次触及零容量不设 latch
    fx = FIX["E04c"]
    sp = fx.request.order_plan.model_copy(update={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(2))})
    sreq = rebuild(fx.request, order_plan=sp, path_scenario="primary")
    smk = c.MarketView(manifest_id=fx.market.manifest_id, last=[pt(0, 100, 2), pt(30, 95, 0), pt(60, 100, 5), pt(90, 95, 5)],
                       mark=[pt(0, 100), pt(30, 100), pt(60, 100), pt(90, 100)])
    rs = simulate_a(sreq, smk)
    fills = [e for e in rs.canonical_events if e.kind in c.FILL_KINDS and e.leg == "tp"]
    assert [ (e.ts, e.qty) for e in fills ] == [(T0 + dt.timedelta(seconds=90), Decimal(2))] and rs.net_R == Decimal(2)   # (100-95)×2/5


# S02 —— 见 test_funding.py（闭合 mark、冲突键、schedule 缺证据）；此处只核 B 同步
def test_s02_b_rejects_conflicting_settlement_and_uses_closed_mark():
    fx = FIX["E05"]
    mk = fx.market.model_copy(update={"funding": fx.market.funding + [c.FundingRow(calc_time=fx.market.funding[0].calc_time, rate=Decimal("0.002"), interval_hours=8)]})
    with pytest.raises(c.ContractError):
        nb.simulate_b(fx.request, mk)
    S8 = fx.market.funding[0].calc_time
    b_prev = c.Bar(open_time=S8 - dt.timedelta(minutes=1), o=Decimal(100), h=Decimal(100), l=Decimal(100), c=Decimal(100))
    b_new = c.Bar(open_time=S8, o=Decimal(110), h=Decimal(110), l=Decimal(110), c=Decimal(110))
    mk2 = fx.market.model_copy(update={"mark": [], "bars_mark": [b_prev, b_new], "last": [c.PricePoint(ts=fx.request.t_dec, price=Decimal(100))]})
    rb = nb.simulate_b(fx.request, mk2)
    fe = [e for e in rb.canonical_events if e.kind == "funding"]
    assert fe and fe[0].price == Decimal(100)


# S03 —— 分钟内部启动不消费该 bar 的合成极值
def test_s03_intra_bar_start_begins_at_next_bar_open():
    fx = FIX["E04a"]
    req = fx.request.model_copy(update={"t_start": T0 + dt.timedelta(seconds=10)})
    ra = simulate_a(req, fx.market)
    rb = nb.simulate_b(req, fx.market)
    assert ra.position_open_at == T0 + dt.timedelta(seconds=60) == rb.position_open_at
    # 改动启动时刻所在 bar 的 H/L/C 不影响启动后首个入场
    bars = [fx.market.bars_last[0].model_copy(update={"h": Decimal(150), "l": Decimal(50), "c": Decimal(80)}), fx.market.bars_last[1]]
    mk = fx.market.model_copy(update={"bars_last": bars, "bars_mark": bars})
    assert simulate_a(req, mk).canonical_events[3] == ra.canonical_events[3]     # 首个 entry fill 相同（100 @ T+60）


# S04 —— max_holding 与 TP 舍入余量
def test_s04_max_holding_censors_and_tp_remainder_goes_to_last_leg():
    req, mk = e03(plan_upd={"expiry": c.Expiry(entry_ttl_s=3600, max_holding_s=10)})
    r = simulate_a(req, mk)
    assert r.censor_reason == "LABEL_RIGHT_CENSORED" and r.net_R is None and not [e for e in r.canonical_events if e.leg == "tp" and e.kind in c.FILL_KINDS]
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1)),
                            "tps": [c.TakeProfit(level=Decimal(105), fraction=Decimal("0.5")), c.TakeProfit(level=Decimal(110), fraction=Decimal("0.5"))]},
                  market_upd={"last": [pt(0, 100), pt(60, 105), pt(120, 110)], "mark": [pt(0, 100), pt(60, 100), pt(120, 100)]})
    r = simulate_a(req, mk)
    subs = {e.order_id: e.qty for e in r.canonical_events if e.kind == "submitted"}
    assert "tp-0" not in subs and subs["tp-1"] == 1 and r.exit_avg_price == Decimal(110) and r.net_R == Decimal(2)
    # sum<1 保留余仓：0.5 档、qty 1 → 无 TP，SL 覆盖
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1)), "tps": [c.TakeProfit(level=Decimal(105), fraction=Decimal("0.5"))]})
    r = simulate_a(req, mk)
    assert "tp-0" not in {e.order_id for e in r.canonical_events} and r.censor_reason == "LABEL_RIGHT_CENSORED"


# S05 —— 覆盖/规则隔离落到执行入口
def test_s05_rule_expiry_mid_window_and_mark_required_before_entry():
    req, mk = e03(market_upd={"rules": c.Rules(effective_to=T0 + dt.timedelta(seconds=20))})
    r = simulate_a(req, mk)
    assert r.censor_reason == "SYMBOL_TIME_INVALID" and r.fill_status == "filled" and not [e for e in r.canonical_events if e.leg == "tp" and e.kind in c.FILL_KINDS]
    req, mk = e03(market_upd={"mark": [pt(60, 100)]})          # 首笔 entry 前无 mark
    r = simulate_a(req, mk)
    assert r.censor_reason == "MARK_STALE" and r.fill_status == "none"
    req, mk = e03(market_upd={"rules": c.Rules(tick_size=Decimal(0))})
    assert simulate_a(req, mk).censor_reason == "RULE_HISTORY_MISSING"


def test_s05_loader_rejects_unchecked_or_incomplete_partitions(lake_dir):
    lake = v.LakePaths(lake_dir)
    rows = bar_rows(JAN, 31 * 1440)
    del rows[-1]                                                     # 尾缺一根：无后续行承载 gap_flag
    client, _ = mock_client({REL: make_zip(rows, "x.csv")})
    v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    pc.write_rules(lake, pc.rules_from_manifests(lake, INST, tick_size="1", step_size="1"))
    t_dec = dt.datetime(2024, 1, 31, 23, 0, tzinfo=dt.UTC)
    plan = FIX["E03"].request.order_plan
    req = FIX["E03"].request.model_copy(update={"t_dec": t_dec, "horizon_end": t_dec + dt.timedelta(minutes=30), "market_manifest": "lk"})
    mk = x.load_market_from_lake(req, lake_root=lake_dir)
    assert not mk.bars_complete and any("check_status" in n or "manifest" in n for n in mk.quality_notes)   # 未体检 → 不可用
    r = simulate_a(req, mk)
    assert r.censor_reason in ("BAR_GAP", "RULE_HISTORY_MISSING") and r.net_R is None
    pc.check_partition(lake, data_type="markPriceKlines", interval="1m", symbol="BTCUSDT", period="2024-01")
    mk2 = x.load_market_from_lake(req, lake_root=lake_dir)
    assert not mk2.bars_complete and any("期望" in n for n in mk2.quality_notes)                           # 体检后仍因尾缺不完整
    assert mk2.manifest_refs and mk2.manifest_refs[0]["source_sha256"]


# S06 —— 冲突键隔离 / bronze 不可变 / 隔离 id 绑定源版本
def test_s06_conflicting_keys_quarantined_and_bronze_retained(lake_dir):
    lake = v.LakePaths(lake_dir)
    rows = bar_rows(JAN, 10)
    rows.append(list(rows[3]))                    # 完全相同副本
    bad = list(rows[5]); bad[4] = 999.0           # 同键异值
    rows.append(bad)
    z1 = make_zip(rows, "x.csv")
    client, _ = mock_client({REL: z1})
    m = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    assert m.status == "quarantined" and len(m.conflict_keys) == 1 and len(m.exact_duplicate_keys) == 1 and m.quarantine_n == 2
    part = pl.read_parquet(lake.silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-01-01" / "part.parquet")
    assert part.height == 9                       # 冲突键两候选都不进 silver；副本折叠
    q = pl.read_parquet(pc.quarantine_path(lake))
    assert q.height == 2 and set(q["reason_code"]) == {"KEY_DUPLICATE_OR_ORDER"} and q["raw_hash"][0] == m.source_sha256
    # 同源重跑不增记录；新源版本（修订后的包）→ 新记录，旧 bronze 字节仍可取
    v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client, force=True)
    assert pl.read_parquet(pc.quarantine_path(lake)).height == 2
    rows2 = rows + [list(rows[7])]
    rows2[-1][4] = 888.0
    z2 = make_zip(rows2, "x.csv")
    client2, _ = mock_client({REL: z2})
    m2 = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client2)
    assert m2.source_sha256 != m.source_sha256 and Path(m2.retained_previous).exists()
    import hashlib
    assert hashlib.sha256(Path(m2.retained_previous).read_bytes()).hexdigest() == m.source_sha256
    assert pl.read_parquet(pc.quarantine_path(lake)).height > 2


# S07 —— post-only 穿价拒绝 / 价格优先 / 钱包重检
def test_s07_post_only_cross_rejected_and_price_priority_and_margin_recheck():
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1)),
                            "entries": [c.Entry(kind="limit", price_lo=Decimal(101), price_hi=Decimal(101), post_only=True)]})
    r = simulate_a(req, mk)
    assert [e.kind for e in r.canonical_events if e.order_id == "entry-0"][-1] == "rejected" and r.fill_status == "none"
    assert next(e for e in r.canonical_events if e.kind == "rejected").reason == "POST_ONLY_CROSS"
    # 非穿价 post-only 正常挂单成交
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1)),
                            "entries": [c.Entry(kind="limit", price_lo=Decimal(99), price_hi=Decimal(99), post_only=True)]},
                  market_upd={"last": [pt(0, 100), pt(30, 99), pt(60, 105)], "mark": [pt(0, 100), pt(30, 100), pt(60, 100)]})
    assert simulate_a(req, mk).fill_status == "filled"
    # 价格优先：梯档 [98,100] 容量 1 时先成交 100 档（buy 高价先）
    fx = FIX["E11"]
    mk = fx.market.model_copy(update={"last": [pt(0, 98, 1), pt(60, 105)], "mark": [pt(0, 100), pt(60, 100)]})
    r = simulate_a(fx.request, mk)
    first = next(e for e in r.canonical_events if e.kind in c.FILL_KINDS)
    assert first.order_id == "entry-0" and first.price == Decimal(98)
    # 钱包重检：参考价 100 预留通过，market 跳空 200 时可承担量按实际价重算 → 余量 MARGIN 取消
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(9)), "entries": [c.Entry(kind="market_ref", price_lo=Decimal(100), price_hi=Decimal(100))]},
                  market_upd={"last": [pt(0, 200), pt(60, 205)], "mark": [pt(0, 200), pt(60, 200)]}, policy="fixture-zero-v1")
    r = simulate_a(req, mk)                                         # 钱包 1000：200×q ≤ 1000 → 5
    assert r.filled_qty == Decimal(5) and any(e.kind == "cancelled" and e.reason == "MARGIN" for e in r.canonical_events)


# S08 —— multiplier 进全部金额
def test_s08_multiplier_scales_pnl_fees_exposure():
    req, mk = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1))}, market_upd={"rules": c.Rules(multiplier=Decimal(2))}, policy="fixture-tick-v1")
    ra = simulate_a(req, mk)
    rb = nb.simulate_b(req, mk)
    assert ra.gross_pnl == Decimal(10) and ra.mfe_R == Decimal(2) and ra.fees == 0          # 限价入场 maker 0
    assert rb.gross_pnl == Decimal(10)
    req2, mk2 = e03(plan_upd={"sizing": c.Sizing(mode="fixed_qty", qty=Decimal(1)),
                              "entries": [c.Entry(kind="market_ref", price_lo=Decimal(100), price_hi=Decimal(100))]},
                    market_upd={"rules": c.Rules(multiplier=Decimal(2))}, policy="fixture-tick-v1")
    r2 = simulate_a(req2, mk2)
    assert r2.fees == Decimal("0.101") and r2.slippage == Decimal(2) and r2.gross_pnl == Decimal(8)   # 101 入 105 出 ×2；fee 101×2×0.0005


# S09 —— 等号候选整行采用，null 不被旧值填回
def test_s09_equal_candidate_null_kept():
    U = pl.Datetime("us", "UTC")
    r = pl.DataFrame({"available_at": [T0 - dt.timedelta(seconds=1), T0], "seq": [0, 1], "v": [9, None]}, schema_overrides={"available_at": U, "v": pl.Int64})
    l = pl.DataFrame({"t_dec": [T0], "lseq": [2]}, schema_overrides={"t_dec": U})
    out = asof.asof_join(l, r, strategy="le_with_sequence", sequence_cols=("lseq", "seq"))
    assert out["v"][0] is None and out["asof_matched_at"][0] == T0 and out["asof_reason"][0] is None
    naive = pl.DataFrame({"available_at": [T0.replace(tzinfo=None)], "v": [1]})
    with pytest.raises(asof.TimeUnitInvalid):
        asof.asof_join(l, naive)
    tokyo = r.with_columns(pl.col("available_at").dt.convert_time_zone("Asia/Tokyo"))
    with pytest.raises(asof.TimeUnitInvalid):
        asof.asof_join(l, tokyo)


# S10 —— 解释器不得把未知差异洗成已解释
def test_s10_classifier_exc_first_and_predicate_bound():
    assert nb.classify("E02", ["EXC RuntimeError: unrelated"], None, None) == "NOT_RUN"      # 无结果 = 未运行，不是已解释
    assert nb.classify("E02", [], None, None) == "NOT_RUN"
    ra = simulate_a(FIX["E02"].request, FIX["E02"].market)
    rb = nb.simulate_b(FIX["E02"].request, FIX["E02"].market)
    assert nb.classify("E02", ["EXC RuntimeError: unrelated"], ra, rb) == "UNEXPLAINED"
    d = c.diff_result(FIX["E02"].expected, rb, ignore_reason=False, all_diffs=True)
    assert nb.classify("E02", d, ra, rb) == "B_COMMAND_LATENCY"
    assert nb.classify("E02", ["event[0].price: expected=1 actual=2"], ra, rb) == "UNEXPLAINED"
    rb_bad = rb.model_copy(update={"mae_R": Decimal("-9")})
    assert nb.classify("E02", d, ra, rb_bad) == "UNEXPLAINED"
    assert rb.mae_R == Decimal("-1.2")                                 # B 暴露含 mark-only 时刻
    # 二审 C1：保留已知首差、篡改末尾 closed.reason → 差异键集合变化 → UNEXPLAINED；E17 标量意外差 → UNEXPLAINED
    ev = list(rb.canonical_events); ev[-1] = ev[-1].model_copy(update={"reason": "UNRELATED_CORRUPTION"})
    tampered = rb.model_copy(update={"canonical_events": ev})
    assert nb.classify("E02", c.diff_result(FIX["E02"].expected, tampered, ignore_reason=False, all_diffs=True), ra, tampered) == "UNEXPLAINED"
    ra17 = simulate_a(FIX["E17"].request, FIX["E17"].market); rb17 = nb.simulate_b(FIX["E17"].request, FIX["E17"].market)
    d17 = c.diff_result(FIX["E17"].expected, rb17, ignore_reason=False, all_diffs=True)
    assert nb.classify("E17", d17, ra17, rb17) == "B_LIQUIDITY_MODEL"
    assert nb.classify("E17", d17, ra17, rb17.model_copy(update={"funding": Decimal("-999")})) == "UNEXPLAINED"


# S12 —— 哈希绑定不可变输入
def test_s12_hash_sensitivity_and_stale_policy_hash_rejected():
    fx = FIX["E01"]
    base = simulate_a(fx.request, fx.market).trace_hash
    mk = fx.market.model_copy(update={"manifest_refs": [{"partition_id": "p", "source_sha256": "abc"}]})
    assert simulate_a(fx.request, mk).trace_hash != base
    mk2 = fx.market.model_copy(update={"quality_notes": ["x"]})
    assert simulate_a(fx.request, mk2).trace_hash == base            # 诊断不进哈希
    stale = fx.request.model_copy(update={"policy_hash": "0" * 64})
    with pytest.raises(c.ContractError):
        simulate_a(stale, fx.market)


# S13 —— 事件层不变量
def test_s13_event_level_invariants():
    fx = FIX["E01"]
    exp = fx.expected
    evs = list(exp.canonical_events)
    tp_cancel = next(i for i, e in enumerate(evs) if e.kind == "cancelled" and e.order_id == "tp-0")
    fill_after = evs[tp_cancel].model_copy(update={"kind": "partial_fill", "leg": "tp", "trigger_basis": "last", "price": Decimal(105), "qty": Decimal(1), "fee": Decimal(0)})
    bad = evs[: tp_cancel + 1] + [fill_after] + evs[tp_cancel + 1:]
    bad = [e.model_copy(update={"seq": i}) for i, e in enumerate(bad)]
    with pytest.raises(c.ExecutionInvariantError, match="终态后"):
        c.check_invariants(fx.request, c.expected_as_result(exp.model_copy(update={"canonical_events": bad})))
    no_cancel = [e for e in evs if not (e.kind == "cancelled" and e.order_id == "tp-0")]
    no_cancel = [e.model_copy(update={"seq": i}) for i, e in enumerate(no_cancel)]
    with pytest.raises(c.ExecutionInvariantError, match="兄弟腿"):
        c.check_invariants(fx.request, c.expected_as_result(exp.model_copy(update={"canonical_events": no_cancel})))
    dup_f = FIX["E05"].expected
    fe = next(e for e in dup_f.canonical_events if e.kind == "funding")
    evs2 = list(dup_f.canonical_events)
    i = evs2.index(fe)
    evs2 = evs2[: i + 1] + [fe.model_copy(update={"order_id": "funding-1"})] + evs2[i + 1:]
    evs2 = [e.model_copy(update={"seq": k}) for k, e in enumerate(evs2)]
    with pytest.raises(c.ExecutionInvariantError, match="I11"):
        c.check_invariants(FIX["E05"].request, c.expected_as_result(dup_f.model_copy(update={"canonical_events": evs2, "funding": Decimal("-0.2")})))
    with pytest.raises(Exception):
        c.OrderPlan(**{**FIX["E01"].request.order_plan.model_dump(), "reduce_only_exit": False})
    with pytest.raises(Exception):
        c.Expiry(entry_ttl_s=60, max_holding_s=0)
    with pytest.raises(c.ContractError):
        c.Stop(price=Decimal("1e-20")) and c.OrderPlan(**{**FIX["E01"].request.order_plan.model_dump(), "stop": {"price": "0.0000000000001", "trigger": "mark"}})
