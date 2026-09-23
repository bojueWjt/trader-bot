"""OR-05 I01/I02/I04（G2）：隔离数据根、标记价可见性、公开入口负 latency。

每条正向断言配内存 mutant：去掉修复后同一断言必须失败。不改旧输入、不跳过旧测试。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import os
import textwrap
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest

from quant_lab.market import asof, contract as c, execution as x, vision as v
from quant_lab.market.partition_check import write_rules
from tests.market.test_partition_check import bars, rules
from tests.market.test_review_p1 import FIX
from tests.market.test_vision import JAN, REL, bar_rows, make_zip, mock_client

INST = "BTCUSDT-PERP.BINANCE-UM"
T_DEC = dt.datetime(2024, 1, 1, 0, 2, tzinfo=dt.UTC)
UTC_US = pl.Datetime("us", "UTC")
SENTINEL_IN = "ISOLATED-ROOT-SENTINEL-100\n"
SENTINEL_OUT = "OUTSIDE-CWD-SENTINEL-999\n"
SENTINEL_OVERRIDE = "OVERRIDE-MARKET-LAKE-SENTINEL\n"

I01_DATA_ROOT_LINE = 'root = Path(data_root) / "lake" / "market"'
I01_LOADER_LAKE_LINE = "lake = LakePaths(Path(lake_root)) if lake_root is not None else LakePaths.default()"
I02_AVAIL_LINE = 'cond = cond & pl.col("available_at").is_not_null() & (pl.col("available_at") < pl.lit(at))'
I02_STALE_LINE = "if stale > max_staleness_s:"
I02_AGE_LINE = "age > pl.lit"
I02_SEQ_LT = ".filter(pl.col(rseq) < pl.col(lseq))"
I02_NULL_FILTER = "right = right.filter(pl.col(right_on).is_not_null())"
I04_LATENCY_LINE = "if latency < dt.timedelta(0):"
I04_ENTRIES = ("last_closed_bar", "mark_bar_at")
I04_NEG = (dt.timedelta(microseconds=-1), dt.timedelta(seconds=-60))


def _mutant_fn(fn, old: str, new: str):
    src = textwrap.dedent(inspect.getsource(fn))
    assert src.count(old) == 1, (getattr(fn, "__name__", fn), src.count(old))
    ns = dict(fn.__globals__)
    exec(compile(src.replace(old, new), f"<mutant-{fn.__name__}>", "exec"), ns)
    return ns[fn.__name__]


def _fp(path: Path) -> str:
    h = hashlib.sha256()
    if not path.exists():
        return "missing"
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    for p in files:
        rel = p.name if path.is_file() else p.relative_to(path).as_posix()
        h.update(rel.encode()); h.update(b"\0"); h.update(p.read_bytes()); h.update(b"\0")
    return h.hexdigest()


def _as_float(v):
    return None if v is None else float(v)


# ---------------------------------------------------------------------------
# I01 路径隔离
# ---------------------------------------------------------------------------
def _isolate_data_root_only(tmp_path, monkeypatch):
    """只设 QUANT_LAB_DATA_ROOT，清除 MARKET_LAKE；cwd 下另放一份冲突湖。"""
    isolated_data = tmp_path / "isolated" / "data"
    fake_cwd = tmp_path / "fake_cwd"
    isolated_lake = isolated_data / "lake" / "market"
    outside_lake = fake_cwd / "data" / "lake" / "market"
    isolated_lake.mkdir(parents=True)
    outside_lake.mkdir(parents=True)
    monkeypatch.chdir(fake_cwd)
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(isolated_data))
    monkeypatch.delenv("QUANT_LAB_MARKET_LAKE", raising=False)
    return isolated_data, isolated_lake, outside_lake


def _seed_sentinel(lake: Path, text: str) -> None:
    lake.mkdir(parents=True, exist_ok=True)
    (lake / "_i01_sentinel.txt").write_text(text, encoding="utf-8")


def _seed_loader_lake(lake_root: Path, close: float, sentinel: str) -> None:
    lake = v.LakePaths(lake_root)
    _seed_sentinel(Path(lake_root), sentinel)
    df = bars(3).with_columns(
        pl.lit(close).alias("open"), pl.lit(close).alias("high"),
        pl.lit(close).alias("low"), pl.lit(close).alias("close"),
        pl.lit(False).alias("gap_flag"), pl.lit(True).alias("ohlc_valid"),
    )
    for kind in ("klines", "markPriceKlines"):
        v.atomic_write_parquet(lake.silver_dir(kind, "1m", "BTCUSDT") / "date=2024-01-01" / "part.parquet", df)
    for kind, interval in (("klines", "1m"), ("markPriceKlines", "1m"), ("fundingRate", "8h")):
        pid = v.partition_id(kind, interval, "BTCUSDT", "2024-01")
        v.atomic_write_json(lake.manifest(pid), {"partition_id": pid, "source_sha256": "h" * 64, "check_status": "ok"})
    write_rules(lake, rules())
    v.atomic_write_parquet(
        lake.silver_dir("fundingRate", "8h", "BTCUSDT") / "date=2024-01-01" / "part.parquet",
        pl.DataFrame({"calc_time": [JAN], "funding_rate": [0.0], "funding_interval_hours": [8]}),
    )


def _i01_request():
    return c.ExecutionRequest.model_validate({
        **FIX["E03"].request.model_dump(),
        "t_dec": JAN, "t_start": None,
        "horizon_end": JAN + dt.timedelta(minutes=3), "horizon_source": "caller",
    })


def _assert_i01_loader_prices() -> None:
    mk = x.load_market_from_lake(_i01_request())
    prices_mark = [float(b.c) for b in mk.bars_mark]
    prices_last = [float(b.c) for b in mk.bars_last]
    assert prices_mark and prices_last
    assert all(p == 100.0 for p in prices_mark) and all(p != 999.0 for p in prices_mark)
    assert all(p == 100.0 for p in prices_last) and all(p != 999.0 for p in prices_last)


def _assert_i01_fetch_isolated(isolated_lake: Path, outside_lake: Path) -> None:
    outside_before = _fp(outside_lake)
    z = make_zip(bar_rows(JAN, 5), "x.csv")
    client, _ = mock_client({REL: z})
    m = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", client=client)
    assert m.actual_rows == 5
    bronze = isolated_lake / "bronze" / "binance" / "um" / "markPriceKlines" / "1m" / "BTCUSDT" / "2024-01.zip"
    assert bronze.exists() and hashlib.sha256(bronze.read_bytes()).hexdigest() == hashlib.sha256(z).hexdigest()
    assert not (outside_lake / "bronze").exists()
    assert _fp(outside_lake) == outside_before


def _cwd_default_mutant():
    return classmethod(_mutant_fn(
        v.LakePaths.default.__func__, I01_DATA_ROOT_LINE, 'root = Path("data/lake/market")',
    ))


def _cwd_loader_mutant():
    return _mutant_fn(
        x.load_market_from_lake, I01_LOADER_LAKE_LINE, 'lake = LakePaths(Path("data/lake/market"))',
    )


def test_i01_data_root_only_loader_uses_copy_not_cwd(tmp_path, monkeypatch):
    _, isolated_lake, outside_lake = _isolate_data_root_only(tmp_path, monkeypatch)
    _seed_loader_lake(isolated_lake, 100.0, SENTINEL_IN)
    _seed_loader_lake(outside_lake, 999.0, SENTINEL_OUT)
    _assert_i01_loader_prices()

    with patch.object(v.LakePaths, "default", _cwd_default_mutant()):
        with pytest.raises(AssertionError):
            _assert_i01_loader_prices()

    with patch.object(x, "load_market_from_lake", _cwd_loader_mutant()):
        with pytest.raises(AssertionError):
            _assert_i01_loader_prices()


def test_i01_data_root_only_fetch_writes_copy_fingerprint_unchanged(tmp_path, monkeypatch):
    _, isolated_lake, outside_lake = _isolate_data_root_only(tmp_path, monkeypatch)
    _seed_loader_lake(isolated_lake, 100.0, SENTINEL_IN)
    _seed_loader_lake(outside_lake, 999.0, SENTINEL_OUT)
    _assert_i01_fetch_isolated(isolated_lake, outside_lake)

    with patch.object(v.LakePaths, "default", _cwd_default_mutant()):
        with pytest.raises(AssertionError):
            _assert_i01_fetch_isolated(isolated_lake, outside_lake)


def test_i01_market_lake_override_wins_over_data_root_and_cwd(tmp_path, monkeypatch):
    data_root = tmp_path / "data_root"
    override = tmp_path / "override_lake"
    fake_cwd = tmp_path / "fake_cwd"
    cwd_lake = fake_cwd / "data" / "lake" / "market"
    (data_root / "lake" / "market").mkdir(parents=True)
    override.mkdir(parents=True)
    cwd_lake.mkdir(parents=True)
    _seed_loader_lake(override, 100.0, SENTINEL_OVERRIDE)
    _seed_loader_lake(data_root / "lake" / "market", 999.0, SENTINEL_IN)
    _seed_loader_lake(cwd_lake, 999.0, SENTINEL_OUT)
    monkeypatch.chdir(fake_cwd)
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("QUANT_LAB_MARKET_LAKE", str(override))
    _assert_i01_loader_prices()
    outside_before = _fp(cwd_lake)
    data_before = _fp(data_root / "lake" / "market")
    z = make_zip(bar_rows(JAN, 3), "x.csv")
    client, _ = mock_client({REL: z})
    v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", client=client)
    assert (override / "bronze" / "binance" / "um" / "markPriceKlines" / "1m" / "BTCUSDT" / "2024-01.zip").exists()
    assert _fp(cwd_lake) == outside_before
    assert _fp(data_root / "lake" / "market") == data_before


def test_i01_no_env_uses_project_absolute_not_cwd(tmp_path, monkeypatch):
    fake = tmp_path / "fake_cwd"
    cwd_lake = fake / "data" / "lake" / "market"
    cwd_lake.mkdir(parents=True)
    _seed_sentinel(cwd_lake, SENTINEL_OUT)
    monkeypatch.chdir(fake)
    monkeypatch.delenv("QUANT_LAB_MARKET_LAKE", raising=False)
    monkeypatch.delenv("QUANT_LAB_DATA_ROOT", raising=False)
    root = v.LakePaths.default().root.resolve()
    assert root == v._PROJECT_MARKET_LAKE.resolve()
    assert root != cwd_lake.resolve()
    assert (cwd_lake / "_i01_sentinel.txt").read_text(encoding="utf-8") == SENTINEL_OUT


def test_i01_envroot_matches_g1_g3_paths(tmp_path, monkeypatch):
    isolated_data, isolated_lake, _ = _isolate_data_root_only(tmp_path, monkeypatch)
    from quant_lab.data.lake import Layout
    from quant_lab.research.paths import data_root as g3_data_root
    assert g3_data_root().resolve() == isolated_data.resolve()
    assert Layout.from_root().bronze_dir.resolve().parent.parent.parent == isolated_data.resolve()
    assert v.LakePaths.default().root.resolve() == (g3_data_root() / "lake" / "market").resolve()
    assert v.LakePaths.default().root.resolve() == isolated_lake.resolve()
    assert os.environ["QUANT_LAB_DATA_ROOT"] == str(isolated_data)


# ---------------------------------------------------------------------------
# I02 可见性：晚到 / null / H0 等号 / 显式等号 / 未闭合 / 120s
# ---------------------------------------------------------------------------
def _bars(close_times, available_ats, closes, seqs=None) -> pl.DataFrame:
    n = len(closes)
    d = {"instrument_id": [INST] * n, "interval": ["1m"] * n, "close_time": close_times, "close": closes}
    overrides = {"close_time": UTC_US}
    if available_ats is not None:
        d["available_at"] = available_ats
        overrides["available_at"] = UTC_US
    if seqs is not None:
        d["seq"] = seqs
    return pl.DataFrame(d, schema_overrides=overrides)


def _four(frame: pl.DataFrame, at: dt.datetime, *, join_kw=None, left_seq=None, with_left_seq=False):
    left_d = {"episode_id": ["e"], "instrument_id": [INST], "t_dec": [at]}
    overrides = {"t_dec": UTC_US}
    if with_left_seq or left_seq is not None:
        left_d["lseq"] = [left_seq]
        overrides["lseq"] = pl.Int64
    left = pl.DataFrame(left_d, schema_overrides=overrides)
    asof_close = None
    if "available_at" in frame.columns:
        j = asof.asof_join(left, frame, by=["instrument_id"], **(join_kw or {}))
        asof_close = j["close"][0]
    row = asof.last_closed_bar(frame, at=at, instrument_id=INST, interval="1m")
    lcb = None if row is None else row["close"][0]
    mb = asof.mark_bar_at(frame, at, INST)
    mp, _ = asof.mark_price_at(frame, at, INST)
    return asof_close, lcb, mb.price, mp


def _assert_all_100_not_999(frame, at, *, join_kw=None, left_seq=None, include_asof=True):
    asof_c, lcb, mb, mp = _four(frame, at, join_kw=join_kw, left_seq=left_seq)
    vals = (lcb, mb, mp) if not include_asof else (asof_c, lcb, mb, mp)
    for val in vals:
        assert _as_float(val) == 100.0
        assert _as_float(val) != 999.0


def _i02_late_frame():
    t = T_DEC
    return _bars(
        [t - dt.timedelta(minutes=1), t],
        [t - dt.timedelta(minutes=1), t + dt.timedelta(minutes=10)],
        [100.0, 999.0],
    ), t


def test_i02_late_available_at_not_exposed_on_four_entries():
    frame, at = _i02_late_frame()
    _assert_all_100_not_999(frame, at)
    mutant = _mutant_fn(asof.last_closed_bar, I02_AVAIL_LINE, "cond = cond & (pl.col('close_time') + latency <= pl.lit(at))")
    with patch.object(asof, "last_closed_bar", mutant):
        with pytest.raises(AssertionError):
            _assert_all_100_not_999(frame, at)


def test_i02_null_available_at_not_exposed_on_four_entries():
    t = T_DEC
    frame = _bars(
        [t - dt.timedelta(minutes=1), t],
        [t - dt.timedelta(minutes=1), None],
        [100.0, 999.0],
    )
    _assert_all_100_not_999(frame, t)
    mutant = _mutant_fn(asof.last_closed_bar, I02_AVAIL_LINE, "cond = cond & (pl.col('close_time') + latency <= pl.lit(at))")
    with patch.object(asof, "last_closed_bar", mutant):
        with pytest.raises(AssertionError):
            _assert_all_100_not_999(frame, t)


def test_i02_h0_equality_without_available_at_column():
    t = T_DEC
    frame = _bars([t, t + dt.timedelta(minutes=1)], None, [100.0, 999.0])
    _assert_all_100_not_999(frame, t, include_asof=False)
    # latency=1s：收盘整点尚不可知，退回更早 bar；本夹具无更早 bar
    assert asof.last_closed_bar(frame, at=t, instrument_id=INST, interval="1m", latency=dt.timedelta(seconds=1)) is None
    mutant = _mutant_fn(
        asof.last_closed_bar,
        "cond = cond & (pl.col(\"close_time\") + latency <= pl.lit(at))",
        "cond = cond & (pl.col(\"close_time\") < pl.lit(at))",
    )
    with patch.object(asof, "last_closed_bar", mutant):
        with pytest.raises(AssertionError):
            _assert_all_100_not_999(frame, t, include_asof=False)


def test_i02_explicit_equality_without_sequence_rejected():
    t = T_DEC
    frame = _bars(
        [t - dt.timedelta(minutes=1), t],
        [t - dt.timedelta(minutes=1), t],
        [100.0, 999.0],
    )
    _assert_all_100_not_999(frame, t)
    mutant = _mutant_fn(
        asof.last_closed_bar, I02_AVAIL_LINE,
        "cond = cond & pl.col('available_at').is_not_null() & (pl.col('available_at') <= pl.lit(at))",
    )
    with patch.object(asof, "last_closed_bar", mutant):
        with pytest.raises(AssertionError):
            _assert_all_100_not_999(frame, t)


def test_i02_explicit_equality_with_asof_sequence_only():
    t = T_DEC
    frame = _bars(
        [t - dt.timedelta(minutes=1), t],
        [t - dt.timedelta(minutes=1), t],
        [100.0, 999.0],
        seqs=[0, 1],
    )
    asof_c, lcb, mb, mp = _four(
        frame, t, join_kw={"strategy": "le_with_sequence", "sequence_cols": ("lseq", "seq")}, left_seq=5,
    )
    assert _as_float(asof_c) == 999.0          # 有顺序证据：asof_join 等号可用
    for val in (lcb, mb, mp):
        assert _as_float(val) == 100.0           # bar/mark 入口无 sequence API，不接纳等号
        assert _as_float(val) != 999.0
    asof_strict, _, _, _ = _four(frame, t)
    assert _as_float(asof_strict) == 100.0     # 无顺序：strict_lt 拒等号


def test_i02_sequence_null_equal_reverse_reject_equality():
    t = T_DEC
    early, eq = t - dt.timedelta(minutes=1), t
    kw = {"strategy": "le_with_sequence", "sequence_cols": ("lseq", "seq")}

    def reject(left_seq, right_seqs):
        frame = _bars([early, eq], [early, eq], [100.0, 999.0], seqs=right_seqs)
        asof_c, lcb, mb, mp = _four(frame, t, join_kw=kw, left_seq=left_seq, with_left_seq=True)
        for val in (asof_c, lcb, mb, mp):
            assert _as_float(val) == 100.0
            assert _as_float(val) != 999.0

    reject(None, [0, 1])     # left_seq null
    reject(5, [0, None])     # right_seq null on 等号行
    reject(1, [0, 1])        # 相等
    reject(0, [0, 5])        # 反向

    with patch.object(asof, "asof_join", _mutant_fn(asof.asof_join, I02_SEQ_LT, ".filter(pl.col(rseq) <= pl.col(lseq))")):
        with pytest.raises(AssertionError):
            reject(1, [0, 1])
    with patch.object(asof, "asof_join", _mutant_fn(asof.asof_join, I02_SEQ_LT, ".filter(pl.col(rseq) > pl.col(lseq))")):
        with pytest.raises(AssertionError):
            reject(0, [0, 5])


def test_i02_unclosed_bar_not_visible():
    t = T_DEC
    # 999 未闭合但 available_at 已过：asof_join（只键可知时刻）会看到；闭合入口必须拒绝。
    frame = _bars(
        [t - dt.timedelta(minutes=1), t + dt.timedelta(minutes=1)],
        [t - dt.timedelta(minutes=1), t - dt.timedelta(seconds=1)],
        [100.0, 999.0],
    )
    asof_c, lcb, mb, mp = _four(frame, t)
    assert _as_float(asof_c) == 999.0
    for val in (lcb, mb, mp):
        assert _as_float(val) == 100.0
        assert _as_float(val) != 999.0
    mutant = _mutant_fn(
        asof.last_closed_bar,
        'cond = cond & (pl.col("close_time") <= pl.lit(at))',
        "cond = cond & True",
    )
    with patch.object(asof, "last_closed_bar", mutant):
        with pytest.raises(AssertionError):
            _, lcb_m, mb_m, mp_m = _four(frame, t)
            for val in (lcb_m, mb_m, mp_m):
                assert _as_float(val) == 100.0
                assert _as_float(val) != 999.0


def test_i02_staleness_120s_boundary_does_not_fall_to_late_bar():
    t = T_DEC
    closed = t - dt.timedelta(seconds=119)
    frame = _bars(
        [closed, t],
        [closed, t + dt.timedelta(minutes=10)],
        [100.0, 999.0],
    )
    px, reason = asof.mark_price_at(frame, t, INST)
    assert px == Decimal("100.0") and reason is None
    d = asof.mark_bar_at(frame, t, INST)
    assert d.staleness_s == 119.0 and _as_float(d.price) == 100.0
    later = t + dt.timedelta(seconds=2)          # 陈旧 121s；999 仍未 available
    px2, reason2 = asof.mark_price_at(frame, later, INST)
    assert px2 is None and reason2 == asof.REASON_MARK_STALE
    d2 = asof.mark_bar_at(frame, later, INST)
    assert d2.reason == asof.REASON_MARK_STALE and d2.close_time == closed
    assert _as_float(d2.price) != 999.0
    mutant = _mutant_fn(asof.last_closed_bar, I02_AVAIL_LINE, "cond = cond & (pl.col('close_time') + latency <= pl.lit(at))")
    with patch.object(asof, "last_closed_bar", mutant):
        with pytest.raises(AssertionError):
            px_m, _ = asof.mark_price_at(frame, t, INST)
            assert px_m == Decimal("100.0")
            assert _as_float(px_m) != 999.0


def test_i02_staleness_exact_120_and_plus_1us():
    t = T_DEC
    closed = t - dt.timedelta(seconds=120)
    late_at = t + dt.timedelta(minutes=10)
    frame = _bars([closed, t], [closed, late_at], [100.0, 999.0])
    px, reason = asof.mark_price_at(frame, t, INST)
    assert px == Decimal("100.0") and reason is None
    d = asof.mark_bar_at(frame, t, INST)
    assert d.staleness_s == 120.0 and d.reason is None and _as_float(d.price) == 100.0
    left = pl.DataFrame({"episode_id": ["e"], "instrument_id": [INST], "t_dec": [t]}, schema_overrides={"t_dec": UTC_US})
    j = asof.asof_join(left, frame, by=["instrument_id"], tolerance=dt.timedelta(seconds=120))
    assert _as_float(j["close"][0]) == 100.0 and j["asof_reason"][0] is None
    assert _as_float(j["close"][0]) != 999.0

    later = t + dt.timedelta(microseconds=1)
    px2, reason2 = asof.mark_price_at(frame, later, INST)
    assert px2 is None and reason2 == asof.REASON_MARK_STALE
    d2 = asof.mark_bar_at(frame, later, INST)
    assert d2.reason == asof.REASON_MARK_STALE and d2.close_time == closed
    assert _as_float(d2.price) != 999.0
    left2 = pl.DataFrame({"episode_id": ["e"], "instrument_id": [INST], "t_dec": [later]}, schema_overrides={"t_dec": UTC_US})
    j2 = asof.asof_join(left2, frame, by=["instrument_id"], tolerance=dt.timedelta(seconds=120))
    assert j2["close"][0] is None and j2["asof_reason"][0] == asof.REASON_MARK_STALE
    assert j2["asof_matched_at"][0] == closed
    assert _as_float(j2["close"][0]) != 999.0

    with patch.object(asof, "mark_bar_at", _mutant_fn(asof.mark_bar_at, I02_STALE_LINE, "if stale >= max_staleness_s:")):
        with pytest.raises(AssertionError):
            d_m = asof.mark_bar_at(frame, t, INST)
            assert d_m.staleness_s == 120.0 and d_m.reason is None and _as_float(d_m.price) == 100.0
        with pytest.raises(AssertionError):
            px_m, reason_m = asof.mark_price_at(frame, t, INST)
            assert px_m == Decimal("100.0") and reason_m is None
    with patch.object(asof, "asof_join", _mutant_fn(asof.asof_join, I02_AGE_LINE, "age >= pl.lit")):
        with pytest.raises(AssertionError):
            j_m = asof.asof_join(left, frame, by=["instrument_id"], tolerance=dt.timedelta(seconds=120))
            assert _as_float(j_m["close"][0]) == 100.0 and j_m["asof_reason"][0] is None


def test_i02_asof_join_typed_null_and_multiple_unknown_rows():
    t = T_DEC
    left = pl.DataFrame({"episode_id": ["e"], "instrument_id": [INST], "t_dec": [t]}, schema_overrides={"t_dec": UTC_US})
    typed_one = pl.DataFrame({
        "instrument_id": [INST],
        "available_at": pl.Series("available_at", [None], dtype=UTC_US),
        "close": [999.0],
    })
    out = asof.asof_join(left, typed_one, by=["instrument_id"])
    assert out["close"][0] is None and out["asof_reason"][0] == asof.REASON_NO_PRIOR

    multi = pl.DataFrame({
        "instrument_id": [INST] * 4,
        "available_at": pl.Series(
            "available_at",
            [t - dt.timedelta(minutes=1), None, None, t + dt.timedelta(minutes=10)],
            dtype=UTC_US,
        ),
        "close": [100.0, 999.0, 888.0, 999.0],
        "interval": ["1m"] * 4,
        "close_time": [t - dt.timedelta(minutes=1), t, t, t],
    }, schema_overrides={"close_time": UTC_US})
    out = asof.asof_join(left, multi, by=["instrument_id"])
    assert _as_float(out["close"][0]) == 100.0
    assert _as_float(out["close"][0]) != 999.0
    _assert_all_100_not_999(multi, t)

    all_null = pl.DataFrame({
        "instrument_id": [INST, INST],
        "available_at": pl.Series("available_at", [None, None], dtype=UTC_US),
        "close": [999.0, 888.0],
    })
    out = asof.asof_join(left, all_null, by=["instrument_id"])
    assert out["close"][0] is None and out["asof_reason"][0] == asof.REASON_NO_PRIOR

    with patch.object(asof, "asof_join", _mutant_fn(asof.asof_join, I02_NULL_FILTER, "right = right")):
        with pytest.raises(asof.AsOfKeyDuplicate):
            asof.asof_join(left, multi, by=["instrument_id"])
        with pytest.raises(asof.AsOfKeyDuplicate):
            asof.asof_join(left, all_null, by=["instrument_id"])


def test_i02_explicit_available_at_does_not_stack_h0_latency():
    t = T_DEC
    ct = t - dt.timedelta(seconds=1)
    with_avail = _bars([ct], [ct], [100.0])
    without = _bars([ct], None, [100.0])
    lat = dt.timedelta(seconds=5)
    row = asof.last_closed_bar(with_avail, at=t, instrument_id=INST, interval="1m", latency=lat)
    assert row is not None and row["close"][0] == 100.0
    assert asof.last_closed_bar(without, at=t, instrument_id=INST, interval="1m", latency=lat) is None
    mutant = _mutant_fn(
        asof.last_closed_bar, I02_AVAIL_LINE,
        "cond = cond & pl.col('available_at').is_not_null() & (pl.col('available_at') + latency < pl.lit(at))",
    )
    with patch.object(asof, "last_closed_bar", mutant):
        with pytest.raises(AssertionError):
            row_m = asof.last_closed_bar(with_avail, at=t, instrument_id=INST, interval="1m", latency=lat)
            assert row_m is not None and row_m["close"][0] == 100.0


# ---------------------------------------------------------------------------
# I04 负 latency 公开入口
# ---------------------------------------------------------------------------
def _i04_h0_frame():
    t = T_DEC
    return _bars([t, t + dt.timedelta(minutes=1)], None, [100.0, 999.0]), t


def _call_i04(entry: str, frame, at, latency):
    if entry == "last_closed_bar":
        return asof.last_closed_bar(frame, at=at, instrument_id=INST, interval="1m", latency=latency)
    if entry == "mark_bar_at":
        return asof.mark_bar_at(frame, at, INST, latency=latency)
    raise AssertionError(entry)


def _assert_i04_domain(frame, at):
    for entry in I04_ENTRIES:
        for bad in I04_NEG:
            with pytest.raises(asof.LatencyDomainInvalid, match="latency") as blocked:
                _call_i04(entry, frame, at, bad)
            assert "latency" in str(blocked.value)
    row0 = asof.last_closed_bar(frame, at=at, instrument_id=INST, interval="1m", latency=dt.timedelta(0))
    assert row0["close"][0] == 100.0
    row_pos = asof.last_closed_bar(
        frame, at=at + dt.timedelta(seconds=60), instrument_id=INST, interval="1m",
        latency=dt.timedelta(seconds=60),
    )
    assert row_pos["close"][0] == 100.0
    assert row_pos["close"][0] != 999.0
    d0 = asof.mark_bar_at(frame, at, INST, latency=dt.timedelta(0))
    assert _as_float(d0.price) == 100.0
    d_pos = asof.mark_bar_at(frame, at + dt.timedelta(seconds=60), INST, latency=dt.timedelta(seconds=60))
    assert _as_float(d_pos.price) == 100.0
    assert _as_float(d_pos.price) != 999.0


def test_i04_public_entries_reject_negative_latency_keep_h0_equality():
    frame, at = _i04_h0_frame()
    _assert_i04_domain(frame, at)


@pytest.mark.parametrize("entry", I04_ENTRIES)
@pytest.mark.parametrize("bad", I04_NEG)
def test_i04_missing_guard_did_not_raise_per_entry(entry, bad):
    frame, at = _i04_h0_frame()
    _assert_i04_domain(frame, at)
    mutant_guard = _mutant_fn(asof._require_non_negative_latency, I04_LATENCY_LINE, "if False and latency < dt.timedelta(0):")
    with patch.object(asof, "_require_non_negative_latency", mutant_guard):
        with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
            with pytest.raises(asof.LatencyDomainInvalid, match="latency"):
                _call_i04(entry, frame, at, bad)
