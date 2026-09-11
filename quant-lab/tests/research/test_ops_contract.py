"""R-04：18 算子 × polars 后端 × 五类契约检查（reference / 截断重算 / 未来免疫 / 过去缺值 / 墙钟缺 bar）。"""
from __future__ import annotations

import polars as pl
import pytest

from quant_lab.research import contract_tests as ct
from quant_lab.research.backends import BarsInvalid, get_backend, prepare_partition
from quant_lab.research.ops import FIRST_BATCH, REGISTRY

BACKEND = get_backend("polars")
CASES = ct.boundary_cases() + ct.random_cases(100, seed=0)
CHECKS = [ct.check_reference, ct.check_truncation, ct.check_future_immunity, ct.check_past_null, ct.check_gap]


def test_registry_has_first_batch():
    assert len(REGISTRY) >= 18 and set(FIRST_BATCH) <= set(REGISTRY)
    for s in REGISTRY.values():
        assert s.lookahead == 0 and s.version and s.backends


def test_boundary_case_count():
    assert len(ct.boundary_cases()) >= 20 and len(ct.random_cases(100)) == 100


@pytest.mark.parametrize("op", FIRST_BATCH)
@pytest.mark.parametrize("check", CHECKS, ids=[c.__name__ for c in CHECKS])
def test_operator_contract(op, check):
    failures = []
    for lbl, ast in ct.op_asts(op):
        for cname, bars in CASES:
            failures += check(BACKEND, ast, bars, f"{lbl}/{cname}")
    assert not failures, "\n".join(failures[:10]) + (f"\n... 共 {len(failures)} 条" if len(failures) > 10 else "")


def test_bars_preconditions_rejected():
    bars = ct.make_bars([1.0, 2.0, 3.0])
    dup = bars.with_columns(bars["close_time"].shift(1).fill_null(bars["close_time"][0]))
    with pytest.raises(BarsInvalid, match="重复"):
        prepare_partition(dup, ("close",))
    import datetime as dt
    off = ct.make_bars([1.0, 2.0, 3.0], times=[ct.T0, ct.T0 + dt.timedelta(minutes=15), ct.T0 + dt.timedelta(minutes=37)])
    with pytest.raises(BarsInvalid, match="槽位"):
        prepare_partition(off, ("close",))
    with pytest.raises(BarsInvalid, match="interval"):
        prepare_partition(bars.with_columns(pl.Series("interval", ["15m", "1m", "15m"])), ("close",))
    with pytest.raises(BarsInvalid, match="缺列"):
        prepare_partition(bars.drop("interval"), ("close",))
    naive = bars.with_columns(pl.col("close_time").dt.replace_time_zone(None))
    with pytest.raises(BarsInvalid, match="UTC"):
        prepare_partition(naive, ("close",))


def test_gap_is_not_bridged_rows_and_wallclock():
    """缺 bar 槽位：rows 窗与 Ref 不跨洞、wallclock 窗按槽位数计。"""
    keep = [0, 1, 2, 3, 5, 6, 7, 8, 9]                       # 丢第 4 根
    bars = ct.make_bars([float(i) for i in keep], times=[ct.T0 + i * ct.STEP for i in keep])
    part = prepare_partition(bars, ("close",))
    ref1 = BACKEND.compute_series({"op": "Ref", "args": [{"field": "close"}], "params": {"lag": 1}}, part).to_list()
    assert ref1 == [None, 0.0, 1.0, 2.0, None, 5.0, 6.0, 7.0, 8.0]        # t=5 的前一槽位缺失 → null，不取 t=3
    m3 = BACKEND.compute_series({"op": "Mean", "args": [{"field": "close"}], "window": {"unit": "rows", "count": 3}}, part).to_list()
    assert m3 == [None, None, 1.0, 2.0, None, None, 6.0, 7.0, 8.0]
    w45 = BACKEND.compute_series({"op": "Mean", "args": [{"field": "close"}], "window": {"unit": "wallclock", "minutes": 45}}, part).to_list()
    assert w45 == m3                                                        # 45 分钟 = 3 槽位
    w40 = BACKEND.compute_series({"op": "Mean", "args": [{"field": "close"}], "window": {"unit": "wallclock", "minutes": 40}}, part).to_list()
    assert w40 == m3                                                        # ceil(40/15) = 3 槽位
    w30 = BACKEND.compute_series({"op": "Mean", "args": [{"field": "close"}], "window": {"unit": "wallclock", "minutes": 30}}, part).to_list()
    assert w30 == [None, 0.5, 1.5, 2.5, None, 5.5, 6.5, 7.5, 8.5]


def test_ema_sma_seed_and_reset():
    bars = ct.make_bars([1.0, 2.0, 3.0, 4.0, None, 6.0, 7.0, 8.0, 9.0])
    part = prepare_partition(bars, ("close",))
    out = BACKEND.compute_series({"op": "EMA", "args": [{"field": "close"}], "window": {"unit": "rows", "count": 3}}, part).to_list()
    assert out[:3] == [None, None, 2.0]                                    # SMA(1,2,3) 种子
    assert abs(out[3] - (0.5 * 4.0 + 0.5 * 2.0)) < 1e-12
    assert out[4] is None and out[5] is None and out[6] is None and out[7] == 7.0   # 重置 warm-up


def test_multi_instrument_isolated(fake_bars):
    """单品种窗口隔离：多品种一起算 == 各自单算。"""
    ast = {"op": "Mean", "args": [{"field": "close"}], "window": {"unit": "wallclock", "minutes": 300}}
    out = BACKEND.compute(ast, fake_bars)
    assert set(out.columns) == {"instrument_id", "close_time", "value"} and out.height == fake_bars.height
    for inst in fake_bars["instrument_id"].unique():
        solo = BACKEND.compute(ast, fake_bars.filter(fake_bars["instrument_id"] == inst))
        assert solo["value"].to_list() == out.filter(out["instrument_id"] == inst)["value"].to_list()
