"""S23（G0 新规则）：契约中规定排序/优先级/阈值/标度的具名常量，必须有可追溯调用点 **+ 一条断言其生效的测试**。

只导出不调用即判 open——死常量比缺失更危险，因为它让阅读者以为规则已实现（S21 的 CENSOR_PRIORITY 即如此）。
本文件的每条断言都**从常量推导期望值**，因此改常量而不改行为（或反之）都会立刻失败。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import polars as pl
import pytest

from quant_lab.market import contract as c
from quant_lab.market import execution as x
from quant_lab.market import partition_check as pc
from quant_lab.market import vision as v
from quant_lab.market.kernel_a import KernelA, simulate_a
from tests.market.test_review_p1 import FIX
from tests.market.test_review_p1 import FIX


def test_df_decimal_drives_batch_schema():
    """S23：DF_DECIMAL 曾是死常量——execution.py 硬编码 pl.Decimal(38,12)，改契约常量不会改 schema。"""
    expected = pl.Decimal(*c.DF_DECIMAL)
    for col in ("net_R", "fees", "funding", "filled_qty", "gross_pnl", "risk_budget"):
        assert x.BATCH_SCHEMA[col] == expected, col
    assert x.EVENT_STRUCT.to_schema()["price"] == expected


def test_settlement_and_ratio_quantum_drive_rounding():
    money = c.quantize_money(D("0.1234567891234"))
    assert money.as_tuple().exponent == c.SETTLEMENT_QUANTUM.as_tuple().exponent
    ratio = c.quantize_ratio(D(1) / D(3))
    assert ratio.as_tuple().exponent == c.RATIO_QUANTUM.as_tuple().exponent
    # 标度与 Decimal(38,12) 合同一致：比率标度即 DF_DECIMAL 的 scale
    assert -ratio.as_tuple().exponent == c.DF_DECIMAL[1]


def test_exit_leg_order_drives_output_order_not_event_order():
    """EXIT_LEG_ORDER 规定的是**排序**：E12 先 TP 后 SL，输出必须按常量序而非时间序。"""
    r = simulate_a(FIX["E12"].request, FIX["E12"].market)
    fills = [e.leg for e in r.canonical_events if e.kind in c.FILL_KINDS and e.leg in c.EXIT_LEG_ORDER]
    assert fills[0] == "tp" and "sl" in fills                       # 时间序：TP 在前
    assert r.exit_legs == tuple(l for l in c.EXIT_LEG_ORDER if l in set(fills))
    assert r.exit_legs == ("sl", "tp")                              # 常量序：sl 在前


def test_spike_k_and_min_samples_drive_flagging():
    """SPIKE_K / SPIKE_MIN_SAMPLES 是阈值：flag 必须恒等于 score > SPIKE_K，样本不足时一律不判。"""
    import random
    rnd = random.Random(7)
    closes = [100 + rnd.uniform(-0.05, 0.05) for _ in range(200)]
    closes[150] = 130.0
    s = pl.Series("close", closes)
    gap = pl.Series([False] * len(closes))
    score, flag = pc.spike_scores(s, gap)
    assert flag.to_list() == [(sc is not None and sc > pc.SPIKE_K) for sc in score.to_list()]
    assert flag[150] and score[150] > pc.SPIKE_K
    # 恰在阈值下方不得触发（阈值是 >，不是 >=）
    below = pl.Series([sc for sc in score.to_list()])
    assert not any(f for f, sc in zip(flag.to_list(), below.to_list()) if sc is not None and sc <= pc.SPIKE_K)
    # 样本不足：少于 SPIKE_MIN_SAMPLES 个有效收益 → 全 None/False，不猜
    short = pl.Series("close", closes[: pc.SPIKE_MIN_SAMPLES - 1])
    sc2, fl2 = pc.spike_scores(short, pl.Series([False] * (pc.SPIKE_MIN_SAMPLES - 1)))
    assert set(sc2.to_list()) == {None} and not any(fl2.to_list())


def test_interval_seconds_drives_expected_rows_and_path_points():
    assert v.expected_rows("klines", "1m", "2024-01") == 31 * 86400 // v.INTERVAL_SECONDS["1m"]
    assert v.expected_rows("klines", "15m", "2024-01") == 31 * 86400 // v.INTERVAL_SECONDS["15m"]
    b = c.Bar(open_time=dt.datetime(2024, 1, 1, tzinfo=dt.UTC), o=D(100), h=D(110), l=D(90), c=D(100), interval_s=900)
    pts = KernelA.expand_bar(b, "primary", "long", None, D(1))
    span = (pts[-1].ts - pts[0].ts).total_seconds()
    assert span == b.interval_s - 1e-6                              # 末点 = open + interval − 1µs


def test_censor_priority_has_call_site_and_effect():
    """CENSOR_PRIORITY 曾是死常量（S21）：契约规定优先级、实现却按检查顺序先到先得。"""
    import inspect
    src = inspect.getsource(KernelA.censor_now)
    assert "CENSOR_PRIORITY" in src                                  # 可追溯调用点
    fx = FIX["E03"]
    mk = fx.market.model_copy(update={"rules_known": False, "bars_quality_ok": False, "bars_complete": False})
    r = simulate_a(fx.request, mk)
    applicable = {"RULE_HISTORY_MISSING", "BAR_GAP"}
    assert r.censor_reason == min(applicable, key=c.CENSOR_PRIORITY.index)   # 期望值由常量推导


def test_grid_math_single_source_and_exact_to_microsecond():
    """族A+族B 横扫：网格数学只有一处实现（contract.first_grid_point / grid_points_between），且精确到微秒。

    S28 与其漏网处（loader 期望 bar 数）的共同成因是"同一套网格算术多处实现且精度不一"。
    """
    import inspect
    from quant_lab.market import execution as _x
    from quant_lab.market.kernel_a import KernelA
    base = dt.datetime(2024, 1, 1, 12, 0, tzinfo=dt.UTC)
    # 在网格上 → 返回自身；带亚秒 → 进位到下一个网格点（不是误判成"不在网格上"再乱算）
    assert c.first_grid_point(base, 60) == base
    assert c.first_grid_point(base + dt.timedelta(microseconds=1), 60) == base + dt.timedelta(minutes=1)
    assert c.first_grid_point(base + dt.timedelta(microseconds=999999), 60) == base + dt.timedelta(minutes=1)
    assert c.first_grid_point(base + dt.timedelta(seconds=59, microseconds=999999), 60) == base + dt.timedelta(minutes=1)
    # 半开区间计数：整点对齐、亚秒起点、空区间
    assert c.grid_points_between(base, base + dt.timedelta(minutes=5), 60) == 5
    assert c.grid_points_between(base + dt.timedelta(microseconds=1), base + dt.timedelta(minutes=5), 60) == 4
    assert c.grid_points_between(base, base, 60) == 0 and c.grid_points_between(base + dt.timedelta(minutes=1), base, 60) == 0
    # 与逐点枚举一致（穷举核对，不是同义反复）
    for off_us in (0, 1, 30_000_000, 59_999_999):
        a = base + dt.timedelta(microseconds=off_us)
        b = a + dt.timedelta(minutes=7)
        brute = sum(1 for i in range(20) if a <= base + dt.timedelta(minutes=i) < b)
        assert c.grid_points_between(a, b, 60) == brute, off_us
    # A23(1) 委派证明用**哨兵法**而非源码文本：把单一来源换成哨兵，调用方的可观察输出必须随之改变。
    # （文本出现不等于路径被走——调用可能被短路、结果可能被丢弃，源码断言照样绿。）
    from quant_lab.market import kernel_a as _ka
    import quant_lab.market.contract as _c
    # 探针必须走"首个 bar 晚于 t_start"这条分支，first_grid_point 的返回值才参与判定
    bars = [c.Bar(open_time=base + dt.timedelta(minutes=i), o=D(100), h=D(100), l=D(100), c=D(100), interval_s=60)
            for i in (1, 2, 3)]                                   # t_start=base，首 bar 在 base+1min → 首缺
    fx = FIX["E03"]
    k = KernelA(fx.request, fx.market)
    k.t_start = base
    real = k._first_bar_gap(bars, base + dt.timedelta(minutes=10))
    assert real == base                                            # 真实：首个应有网格点即 base
    SENTINEL = base + dt.timedelta(days=99)
    orig = _ka.first_grid_point
    try:
        _ka.first_grid_point = lambda at, iv: SENTINEL             # 哨兵：若确实委派，起点判定必被带偏
        moved = k._first_bar_gap(bars, base + dt.timedelta(minutes=10))
    finally:
        _ka.first_grid_point = orig
    assert moved != real, "_first_bar_gap 未真正委派给 first_grid_point（换哨兵后行为不变）"


def test_t_start_derivation_single_source():
    """族A 横扫：t_start 推导此前在 validator 与 resolved_t_start 各写一遍。"""
    import inspect
    pol = c.resolve_policy("base-v1")
    t0 = dt.datetime(2024, 1, 1, 1, 0, tzinfo=dt.UTC)
    assert c.derived_t_start(t0, pol) == t0 + dt.timedelta(seconds=pol.latency_s)
    # A23(1) 哨兵法：把 derived_t_start 换成哨兵，请求校验接受的 t_start 必须随之改变
    import quant_lab.market.contract as _c
    fx = FIX["E03"]
    base_req = {k: v for k, v in fx.request.model_dump().items() if k not in ("entry_fractions", "tp_fractions", "entry_ttl_s")}
    orig = _c.derived_t_start
    try:
        _c.derived_t_start = lambda t_dec, policy: t_dec + dt.timedelta(seconds=7)
        with pytest.raises(c.ContractError, match="t_start"):
            c.ExecutionRequest.model_validate({**base_req, "t_start": fx.request.t_dec})      # 原本合法值现在应被拒
    finally:
        _c.derived_t_start = orig
    assert c.ExecutionRequest.model_validate({**base_req, "t_start": fx.request.t_dec}).t_start == fx.request.t_dec
