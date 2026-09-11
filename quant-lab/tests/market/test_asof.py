"""M-05：三时钟 as-of 库性质测试——等号、晚到、同秒歧义、跨周期未收盘不可见、MARK_STALE、不前填。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from quant_lab.market import asof

T0 = dt.datetime(2024, 1, 1, 10, 0, tzinfo=dt.UTC)
UTC_US = pl.Datetime("us", "UTC")


def ts(*mins: float) -> list[dt.datetime]:
    return [T0 + dt.timedelta(minutes=m) for m in mins]


def right_df(mins, vals, inst="X", seq=None):
    d = {"instrument_id": [inst] * len(mins), "available_at": ts(*mins), "val": vals}
    if seq is not None:
        d["seq"] = seq
    return pl.DataFrame(d, schema_overrides={"available_at": UTC_US})


def left_df(mins, inst="X", seq=None):
    d = {"instrument_id": [inst] * len(mins), "t_dec": ts(*mins)}
    if seq is not None:
        d["lseq"] = seq
    return pl.DataFrame(d, schema_overrides={"t_dec": UTC_US})


# ---------------- asof_join ----------------
def test_strict_lt_excludes_equal_timestamp():
    r = right_df([0, 1, 2], [10, 11, 12])
    l = left_df([1, 1.5, 0])
    out = asof.asof_join(l, r, by=["instrument_id"])
    assert out["val"].to_list() == [10, 11, None]           # t=1 只见 t=0；t=0 无更早
    assert out["asof_reason"].to_list() == [None, None, "NO_PRIOR"]
    assert out["asof_matched_at"].to_list() == [T0, ts(1)[0], None]
    assert out.height == l.height and out["t_dec"].to_list() == l["t_dec"].to_list()   # 左行全保留、顺序不变


def test_le_with_sequence_uses_equal_only_with_order_evidence():
    r = right_df([0, 1, 1], [10, 11, 12], seq=[0, 5, 9])
    l = left_df([1, 1, 1], seq=[7, 20, 3])
    out = asof.asof_join(l, r, by=["instrument_id"], strategy="le_with_sequence", sequence_cols=("lseq", "seq"))
    # lseq=7：同时刻 seq 5 可用（5<7），seq 9 不可用；lseq=20：取 seq 9；lseq=3：同时刻都不可用 → 退回 t=0
    assert out["val"].to_list() == [11, 12, 10]
    with pytest.raises(ValueError):
        asof.asof_join(l, r, by=["instrument_id"], strategy="le_with_sequence")


def test_late_arrival_uses_available_at_not_event_time():
    # 事件 09:59 发生，但 10:03 才可知：10:01 的决策不可见，10:04 可见
    r = pl.DataFrame({"instrument_id": ["X"], "event_time": ts(-1), "available_at": ts(3), "val": [99]},
                     schema_overrides={"event_time": UTC_US, "available_at": UTC_US})
    l = left_df([1, 4])
    out = asof.asof_join(l, r, by=["instrument_id"])
    assert out["val"].to_list() == [None, 99]


def test_same_second_ambiguity_rejected_without_sequence():
    r = right_df([1, 1], [1, 2])
    with pytest.raises(asof.AsOfKeyDuplicate):
        asof.asof_join(left_df([2]), r, by=["instrument_id"])
    # 有 seq 列且唯一则通过（strict_lt 下取同 by 的最后一条更早行仍然是确定的：两者都 < t）
    r2 = right_df([1, 1], [1, 2], seq=[0, 1])
    out = asof.asof_join(left_df([2], seq=[0]), r2, by=["instrument_id"], strategy="le_with_sequence", sequence_cols=("lseq", "seq"))
    assert out["val"][0] == 2


def test_tolerance_marks_stale_and_does_not_forward_fill():
    r = right_df([0], [10])
    l = left_df([1, 2, 3])
    out = asof.asof_join(l, r, by=["instrument_id"], tolerance=dt.timedelta(minutes=2))
    assert out["val"].to_list() == [10, 10, None]
    assert out["asof_reason"].to_list() == [None, None, "MARK_STALE"]
    assert out["asof_matched_at"][2] == T0   # 保留匹配时刻用于解释，值不前填


def test_by_groups_do_not_cross_contaminate_and_suffix():
    r = pl.concat([right_df([0], [1], "A"), right_df([0.5], [2], "B")])
    l = pl.concat([left_df([1], "A"), left_df([1], "B")]).with_columns(pl.lit(0).alias("val"))
    out = asof.asof_join(l, r, by=["instrument_id"])
    assert out["val_r"].to_list() == [1, 2] and out["val"].to_list() == [0, 0]


def test_naive_datetime_rejected():
    r = pl.DataFrame({"available_at": [dt.datetime(2024, 1, 1)], "val": [1]})
    with pytest.raises(asof.TimeUnitInvalid):
        asof.asof_join(left_df([1]), r)
    with pytest.raises(asof.TimeUnitInvalid):
        asof.last_closed_bar(bars_15m(), at=dt.datetime(2024, 1, 1, 10, 7), instrument_id="X", interval="15m")


# ---------------- last_closed_bar ----------------
def bars_15m(inst="X"):
    opens = ts(-30, -15, 0, 15)
    return pl.DataFrame({
        "instrument_id": [inst] * 4, "interval": ["15m"] * 4,
        "open_time": opens, "close_time": [o + dt.timedelta(minutes=15) for o in opens],
        "close": [1.0, 2.0, 3.0, 4.0],
    }, schema_overrides={"open_time": UTC_US, "close_time": UTC_US})


def test_unclosed_bar_invisible_and_equality_semantics():
    b = bars_15m()
    at = T0 + dt.timedelta(minutes=7)                       # 10:07：10:00–10:15 未闭合
    row = asof.last_closed_bar(b, at=at, instrument_id="X", interval="15m")
    assert row["close"][0] == 2.0 and row["close_time"][0] == T0
    at = T0 + dt.timedelta(minutes=15)                      # 10:15 整点：等号成立（H0）
    assert asof.last_closed_bar(b, at=at, instrument_id="X", interval="15m")["close"][0] == 3.0
    # latency=1s：10:15 整点还不可知
    assert asof.last_closed_bar(b, at=at, instrument_id="X", interval="15m", latency=dt.timedelta(seconds=1))["close"][0] == 2.0
    assert asof.last_closed_bar(b, at=T0 - dt.timedelta(minutes=40), instrument_id="X", interval="15m") is None
    assert asof.last_closed_bar(b, at=at, instrument_id="Y", interval="15m") is None
    assert asof.last_closed_bar(b, at=at, instrument_id="X", interval="1m") is None   # 周期不混用


# ---------------- mark_price_at ----------------
def marks_1m(mins, inst="X", closes=None):
    opens = ts(*mins)
    return pl.DataFrame({
        "instrument_id": [inst] * len(mins), "interval": ["1m"] * len(mins),
        "open_time": opens, "close_time": [o + dt.timedelta(minutes=1) for o in opens],
        "close": closes or [100.0 + m for m in mins],
    }, schema_overrides={"open_time": UTC_US, "close_time": UTC_US})


def test_mark_price_at_values():
    m = marks_1m([0, 1, 2, 10])
    assert asof.mark_price_at(m, T0 + dt.timedelta(minutes=2, seconds=30), "X") == (Decimal("101"), None)
    assert asof.mark_price_at(m, T0 + dt.timedelta(minutes=3), "X") == (Decimal("102"), None)          # 等号：10:03 整点 bar 已闭合
    assert asof.mark_price_at(m, T0 + dt.timedelta(minutes=4, seconds=59), "X") == (Decimal("102"), None)  # 陈旧 119s
    assert asof.mark_price_at(m, T0 + dt.timedelta(minutes=5, seconds=1), "X") == (None, "MARK_STALE")   # 陈旧 121s
    assert asof.mark_price_at(m, T0, "X") == (None, "MARK_STALE")                                      # 无 bar
    assert asof.mark_price_at(m, T0 + dt.timedelta(minutes=1), "Y") == (None, "MARK_STALE")
    d = asof.mark_bar_at(m, T0 + dt.timedelta(minutes=5, seconds=1), "X")
    assert d.reason == "MARK_STALE" and d.close_time == T0 + dt.timedelta(minutes=3) and d.staleness_s == 121.0
    d = asof.mark_bar_at(m, T0 + dt.timedelta(minutes=3), "X", tick_size=Decimal("0.5"))
    assert d.price == Decimal("102.0") and d.staleness_s == 0.0


def test_quantize_price_tick():
    assert asof.quantize_price(Decimal("42123.37"), Decimal("0.1")) == Decimal("42123.4")
    assert asof.quantize_price(Decimal("42123.37"), None) == Decimal("42123.37")


REAL = Path("data/lake/market/silver/binance/um/markPriceKlines/1m/instrument=BTCUSDT-PERP.BINANCE-UM/date=2024-01-15/part.parquet")


@pytest.mark.skipif(not REAL.exists(), reason="真实 2024-01 分区未入湖（M-03 冒烟后可用）")
def test_real_partition_mark_price_at():
    m = pl.read_parquet(REAL)
    at = dt.datetime(2024, 1, 15, 12, 0, 30, tzinfo=dt.UTC)
    d = asof.mark_bar_at(m, at, "BTCUSDT-PERP.BINANCE-UM")
    assert d.reason is None and d.close_time == dt.datetime(2024, 1, 15, 12, 0, tzinfo=dt.UTC) and d.staleness_s == 30.0
    ref = m.filter(pl.col("open_time") == dt.datetime(2024, 1, 15, 11, 59, tzinfo=dt.UTC))["close"][0]
    assert d.price == Decimal(str(ref))
