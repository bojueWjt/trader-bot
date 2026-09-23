"""OR-05 I03/I04：必要依赖时间闭包与特征时间参数守域（含内存突变对照）。"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quant_lab.research import features as FT
from quant_lab.research.ast import ASTRejected, canonical_hash

T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
STEP = dt.timedelta(minutes=1)
US = dt.timedelta(microseconds=1)
INST = "BTCUSDT-PERP.BINANCE-UM"
CLOSE = {"field": "close"}
REF = {"op": "Ref", "args": [CLOSE], "params": {"lag": 2}}
EMA = {"op": "EMA", "args": [CLOSE], "window": {"unit": "rows", "count": 3}}


def _window(op, unit="rows", child=CLOSE):
    win = {"unit": "rows", "count": 3}
    if unit == "wallclock":
        win = {"unit": "wallclock", "minutes": 3}
    args = [child]
    if op == "Corr":
        args.append({"field": "volume"})
    return {"op": op, "args": args, "window": win}


def _bars():
    return pl.DataFrame({
        "instrument_id": [INST] * 9, "interval": ["1m"] * 9,
        "close_time": [T0 + i * STEP for i in range(9)],
        "available_at": [T0 + i * STEP for i in range(9)],
        "close": [float(i + 1) for i in range(9)],
        "volume": [float((i + 1) ** 2) for i in range(9)],
    })


def _anchors(times):
    return pl.DataFrame({"episode_id": [f"e{i}" for i in range(len(times))],
                         "instrument_id": [INST] * len(times), "t_dec": times})


def _arrival(bars, index, at):
    return bars.with_columns(pl.when(pl.col("close_time") == T0 + index * STEP)
                             .then(pl.lit(at, dtype=FT.UTC_US)).otherwise(pl.col("available_at")).alias("available_at"))


def _snapshot(ast, bars, at=T0 + 6 * STEP, **kwargs):
    out = FT.feature_snapshot([ast], _anchors([at]), bars=bars, **kwargs)
    h = canonical_hash(ast)
    return out[f"validity_{h}"][0], out[f"f_{h}"][0]


CASES = [("ref", REF, 4, 5), ("ema", EMA, 0, 7)] + [
    (f"{op}-{unit}", _window(op, unit), 4, 3)
    for op in ("Mean", "Std", "Min", "Max", "TSRank", "Corr")
    for unit in ("rows", "wallclock")
]


@pytest.mark.parametrize("name,ast,required,unneeded", CASES, ids=[c[0] for c in CASES])
def test_required_arrival_unknown_ontime_late_and_unneeded(name, ast, required, unneeded):
    bars, at = _bars(), T0 + 6 * STEP
    valid, expected = _snapshot(ast, bars)
    assert valid and expected is not None
    assert _snapshot(ast, _arrival(bars, required, None)) == (False, None)
    # 真实到达的等号可见，晚 1us 仍拒绝；不以 close_time 替代到达证据。
    assert _snapshot(ast, _arrival(bars, required, at)) == (True, expected)
    assert _snapshot(ast, _arrival(bars, required, at + US)) == (False, None)
    for arrival in (None, at + STEP):
        assert _snapshot(ast, _arrival(bars, unneeded, arrival)) == (True, expected)


def test_i03_review_ref_999_reproduction():
    at = T0 + 2 * STEP
    bars = pl.DataFrame({"instrument_id": [INST] * 3, "interval": ["1m"] * 3,
                         "close_time": [at - 2 * STEP, at - STEP, at],
                         "available_at": [at - 2 * STEP, None, at], "close": [100., 999., 100.]})
    ast = {"op": "Ref", "args": [CLOSE], "params": {"lag": 1}}
    assert _snapshot(ast, bars, at) == (False, None)
    assert _snapshot(ast, _arrival(bars, 1, at), at) == (True, 999.)
    assert _snapshot(ast, _arrival(bars, 1, at + US), at) == (False, None)


@pytest.mark.parametrize("ast,required,unneeded", [
    ({"op": "Delta", "args": [CLOSE], "params": {"lag": 3}}, [3, 6], [4, 5]),
    ({"op": "Add", "args": [REF, CLOSE]}, [4, 6], [3, 5]),
    ({"op": "Ref", "args": [_window("Mean")], "params": {"lag": 2}}, [2, 3, 4], [5, 6]),
    (_window("Mean", child=REF), [2, 3, 4], [5, 6]),
    ({"op": "Ref", "args": [EMA], "params": {"lag": 2}}, list(range(5)), [5, 6]),
    ({"op": "EMA", "args": [REF], "window": {"unit": "rows", "count": 3}}, list(range(5)), [5, 6]),
    (_window("Corr", child=REF), [2, 3, 4, 5, 6], [0, 1]),
])
def test_nested_and_branch_dependencies(ast, required, unneeded):
    bars = _bars()
    valid, expected = _snapshot(ast, bars)
    assert valid and expected is not None
    for index in required:
        assert _snapshot(ast, _arrival(bars, index, None)) == (False, None)
        assert _snapshot(ast, _arrival(bars, index, T0 + 7 * STEP)) == (False, None)
    for index in unneeded:
        assert _snapshot(ast, _arrival(bars, index, None)) == (True, expected)


@pytest.mark.parametrize("op", ["Add", "Sub", "Mul", "SafeDiv", "Abs", "LogPositive", "Gt", "Lt", "And"])
def test_pointwise_operators_propagate_each_child(op):
    args = [REF, CLOSE]
    if op in ("Abs", "LogPositive"):
        args = [REF]
    if op == "And":
        args = [{"op": "Gt", "args": [REF, {"const": 0}]},
                {"op": "Lt", "args": [CLOSE, {"const": 100}]}]
    ast = {"op": op, "args": args}
    bars = _bars()
    valid, expected = _snapshot(ast, bars)
    assert valid and expected is not None
    for index in ([4] if len(args) == 1 else [4, 6]):
        assert _snapshot(ast, _arrival(bars, index, None)) == (False, None)
        assert _snapshot(ast, _arrival(bars, index, T0 + 7 * STEP)) == (False, None)
    assert _snapshot(ast, _arrival(bars, 5, None)) == (True, expected)


def test_ref_missing_intermediate_slot_is_not_a_dependency():
    bars = _bars().filter(pl.col("close_time") != T0 + 5 * STEP)
    assert _snapshot(REF, bars) == (True, 5.)
    assert _snapshot(REF, _arrival(bars, 4, None)) == (False, None)


def test_wallclock_window_uses_ceil_slots_on_coarser_grid():
    bars = _bars().with_columns(pl.lit("3m").alias("interval"),
                                (pl.col("close_time") + pl.duration(minutes=2 * (pl.col("close") - 1))).alias("close_time"))
    ast = {"op": "Mean", "args": [CLOSE], "window": {"unit": "wallclock", "minutes": 4}}
    at = T0 + 18 * STEP
    assert _snapshot(ast, bars, at) == (True, 6.5)
    assert _snapshot(ast, _arrival(bars, 15, None), at) == (False, None)
    assert _snapshot(ast, _arrival(bars, 12, None), at) == (True, 6.5)


@pytest.mark.parametrize("gap", [False, True], ids=["null-value", "missing-slot"])
def test_ema_reset_discards_only_pre_reset_dependencies(gap):
    bars = _arrival(_bars(), 0, None)
    if gap:
        bars = bars.filter(pl.col("close_time") != T0 + 3 * STEP)
    else:
        bars = bars.with_columns(pl.when(pl.col("close_time") == T0 + 3 * STEP)
                                 .then(None).otherwise(pl.col("close")).alias("close"))
    # 第4/5/6槽重新形成 SMA 种子；第0槽的未知时间不再参与。
    assert _snapshot(EMA, bars) == (True, 6.)
    assert _snapshot(EMA, _arrival(bars, 4, None)) == (False, None)
    assert _snapshot(EMA, _arrival(bars, 4, T0 + 7 * STEP)) == (False, None)


def test_ema_expression_null_resets_dependency_prefix():
    ast = {"op": "EMA", "args": [{"op": "SafeDiv", "args": [CLOSE, {"field": "volume"}]}],
           "window": {"unit": "rows", "count": 3}}
    bars = _bars().with_columns(pl.when(pl.col("close_time") == T0 + 3 * STEP)
                                .then(0.).otherwise(pl.col("volume")).alias("volume"))
    expected = (1 / 5 + 1 / 6 + 1 / 7) / 3
    valid, value = _snapshot(ast, _arrival(bars, 0, None))
    assert valid and value == pytest.approx(expected)
    assert _snapshot(ast, _arrival(bars, 4, None)) == (False, None)


@pytest.mark.parametrize("ast", [REF, _window("Mean"), EMA])
def test_grid_gaps_and_other_instrument_do_not_pollute_dependencies(ast):
    bars = _bars()
    foreign = bars.with_columns(pl.lit("ETHUSDT-PERP.BINANCE-UM").alias("instrument_id"),
                                pl.lit(None, dtype=FT.UTC_US).alias("available_at"))
    assert _snapshot(ast, pl.concat([foreign, bars]).reverse()) == _snapshot(ast, bars)
    gapped = bars.filter(pl.col("close_time") != T0 + 4 * STEP)
    assert _snapshot(ast, gapped) == (False, None)


@pytest.mark.parametrize("ast", [{"const": 7}, _window("Mean", child={"const": 7}),
                                 {"op": "EMA", "args": [{"const": 7}], "window": {"unit": "rows", "count": 3}}])
def test_constants_have_no_market_availability_dependency(ast):
    bars = _bars().with_columns(pl.lit(None, dtype=FT.UTC_US).alias("available_at"))
    if "const" in ast:
        # 常量只可作子节点；保留原输入并确认已有 lint 硬门，不绕开 EMPTY 错误。
        with pytest.raises(ASTRejected, match="常量根无信息"):
            _snapshot(ast, bars)
        return
    assert _snapshot(ast, bars) == (True, 7.)


@pytest.mark.parametrize("op", ["Ref", "Mean", "Std", "Min", "Max", "TSRank"])
def test_polars_ta_uses_same_availability_gate(op):
    ast = REF if op == "Ref" else _window(op)
    bars = _bars()
    assert _snapshot(ast, bars, backend="polars_ta")[0]
    assert _snapshot(ast, _arrival(bars, 4, None), backend="polars_ta") == (False, None)


def _time_kwargs(path, **kwargs):
    if path == "context":
        return {"ctx": FT.SnapshotContext(**kwargs)}
    return kwargs


@pytest.mark.parametrize("path", ["context", "convenience"])
@pytest.mark.parametrize("latency", [-US, -STEP], ids=["minus-1us", "minus-60s"])
def test_i04_review_negative_latency_rejected(path, latency):
    # 审查原样：无 available_at 的 H0 数据，负60s 会暴露未来的999。
    at = T0 + 2 * STEP
    bars = pl.DataFrame({"instrument_id": [INST] * 2, "interval": ["1m"] * 2,
                         "close_time": [at, at + STEP], "close": [100., 999.]})
    with pytest.raises(FT.SnapshotInvalid, match="latency"):
        _snapshot(CLOSE, bars, at, **_time_kwargs(path, latency=latency))
    assert _snapshot(CLOSE, bars, at) == (True, 100.)


@pytest.mark.parametrize("path", ["context", "convenience"])
@pytest.mark.parametrize("latency", [dt.timedelta(0), US, STEP])
def test_nonnegative_latency_preserves_equality(path, latency):
    bars = _bars().drop("available_at")
    kwargs = _time_kwargs(path, latency=latency)
    at = T0 + 6 * STEP + latency
    assert _snapshot(CLOSE, bars, at - US, **kwargs) == (True, 6.)
    assert _snapshot(CLOSE, bars, at, **kwargs) == (True, 7.)
    assert _snapshot(CLOSE, bars, at + US, **kwargs) == (True, 7.)


def test_latency_and_actual_arrival_use_separate_clocks():
    at = T0 + 6 * STEP + US
    bars = _arrival(_bars(), 6, at)
    assert _snapshot(CLOSE, bars, at, latency=US) == (True, 7.)
    assert _snapshot(CLOSE, _arrival(bars, 6, at + US), at, latency=US) == (False, None)


@pytest.mark.parametrize("ast,required", [(REF, 4), (_window("Mean"), 4), (EMA, 0)])
def test_arrival_precision_does_not_round_late_dependency_down(ast, required):
    at = T0 + 6 * STEP
    ontime = _arrival(_bars(), required, at).with_columns(pl.col("available_at").cast(pl.Datetime("ns", "UTC")))
    late = ontime.with_columns(pl.when(pl.col("close_time") == T0 + required * STEP)
                               .then(pl.col("available_at") + pl.duration(nanoseconds=1))
                               .otherwise(pl.col("available_at")).alias("available_at"))
    assert _snapshot(ast, ontime)[0]
    assert _snapshot(ast, late) == (False, None)


@pytest.mark.parametrize("path", ["context", "convenience"])
@pytest.mark.parametrize("value", [-US, -STEP, dt.timedelta(0), 0, "1m"])
def test_max_staleness_invalid_domain_rejected(path, value):
    with pytest.raises(FT.SnapshotInvalid, match="max_staleness"):
        _snapshot(CLOSE, _bars(), **_time_kwargs(path, max_staleness=value))


@pytest.mark.parametrize("path", ["context", "convenience"])
@pytest.mark.parametrize("stale", [None, US, 2 * STEP])
def test_max_staleness_positive_strict_boundary(path, stale):
    bars = _bars().head(7)
    bound = STEP if stale is None else stale
    kwargs = _time_kwargs(path, max_staleness=stale)
    assert _snapshot(CLOSE, bars, T0 + 6 * STEP + bound - US, **kwargs) == (True, 7.)
    assert _snapshot(CLOSE, bars, T0 + 6 * STEP + bound, **kwargs) == (False, None)


@pytest.mark.parametrize("name,ast,required,unneeded", [CASES[0], CASES[1], CASES[2]], ids=["ref", "ema", "rolling"])
@pytest.mark.parametrize("guard", ["unknown", "late"])
def test_mutation_removing_dependency_guard_is_killed(monkeypatch, name, ast, required, unneeded, guard):
    original = FT._dependency_availability

    def mutant(*args):
        out = original(*args)
        if guard == "unknown":
            return out.with_columns(pl.lit(False).alias("__avail_unknown"))
        return out.with_columns(pl.lit(None, dtype=FT.UTC_US).alias("__avail_max"))

    monkeypatch.setattr(FT, "_dependency_availability", mutant)
    # 复用同一行为断言；若突变未被杀死，外层 pytest.raises 本身会失败。
    with pytest.raises(AssertionError):
        test_required_arrival_unknown_ontime_late_and_unneeded(name, ast, required, unneeded)


@pytest.mark.parametrize("path", ["context", "convenience"])
@pytest.mark.parametrize("latency", [-US, -STEP])
def test_mutation_removing_latency_validation_is_killed(monkeypatch, path, latency):
    monkeypatch.setattr(FT, "_check_time_parameters", lambda *args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        test_i04_review_negative_latency_rejected(path, latency)


@pytest.mark.parametrize("path", ["context", "convenience"])
def test_mutation_removing_staleness_validation_is_killed(monkeypatch, path):
    monkeypatch.setattr(FT, "_check_time_parameters", lambda *args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        test_max_staleness_invalid_domain_rejected(path, dt.timedelta(0))


def test_cache_availability_semantics_version_prevents_legacy_hit(lake_root, monkeypatch):
    ast = _window("Mean")
    bars = _arrival(_bars(), 4, None)
    ctx = FT.SnapshotContext("mm-or05", "gv-or05", derivation_hash="dh-or05", consumable=lambda *args: True)
    original = FT._dependency_availability
    with monkeypatch.context() as mutation:
        mutation.setattr(FT, "AVAILABILITY_VERSION", 1)
        mutation.setattr(FT, "_dependency_availability", lambda *args: original(*args).with_columns(pl.lit(False).alias("__avail_unknown")))
        assert _snapshot(ast, bars, ctx=ctx, cache=True) == (True, 6.)
    # 同输入同血缘，修复后必须 miss 旧语义，再次热读也维持 invalid。
    assert _snapshot(ast, bars, ctx=ctx, cache=True) == (False, None)
    assert _snapshot(ast, bars, ctx=ctx, cache=True) == (False, None)
    assert len(list(FT.cache_dir().glob("*.parquet"))) == 2
