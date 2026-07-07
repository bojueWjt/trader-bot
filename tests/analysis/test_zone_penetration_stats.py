from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from scripts.analysis.zone_penetration_stats import (
    UnsupportedSymbol,
    aggregate,
    atr_1h,
    chase_window_hit,
    classify_retrace,
    fill_at_depth,
    first_touch_near,
    load_klines,
    load_zone_signals,
    normalize_symbol,
    penetration_depth,
    reached_before,
    zone_height_pct,
)


def kline(open_time, open_=100.0, high=100.0, low=100.0, close=100.0):
    return (open_time, open_, high, low, close)


def signal(
    *,
    symbol="BTCUSDT-PERP.BINANCE",
    side="long",
    price_min=99.0,
    price_max=100.0,
    take_profits=(105.0,),
    received_at=None,
):
    ts = received_at or datetime(2026, 7, 2, 12, tzinfo=timezone.utc)
    return {
        "decision_id": f"{symbol}-{side}",
        "symbol": symbol,
        "side": side,
        "entry_price_min": price_min,
        "entry_price_max": price_max,
        "created_at": ts.isoformat(),
        "source_received_at": ts.isoformat(),
        "take_profits": list(take_profits),
    }


def test_penetration_depth_long_allows_not_touched_far_and_breach_values():
    assert penetration_depth("long", 99.0, 100.0, 100.5) == pytest.approx(-0.5)
    assert penetration_depth("long", 99.0, 100.0, 100.0) == pytest.approx(0.0)
    assert penetration_depth("long", 99.0, 100.0, 99.0) == pytest.approx(1.0)
    assert penetration_depth("long", 99.0, 100.0, 98.7) == pytest.approx(1.3)


def test_penetration_depth_short_mirrors_long_math():
    assert penetration_depth("short", 100.0, 101.0, 99.5) == pytest.approx(-0.5)
    assert penetration_depth("short", 100.0, 101.0, 100.0) == pytest.approx(0.0)
    assert penetration_depth("short", 100.0, 101.0, 101.0) == pytest.approx(1.0)
    assert penetration_depth("short", 100.0, 101.0, 101.3) == pytest.approx(1.3)


def test_first_touch_near_and_reached_before_identify_long_no_retrace():
    bars = [
        kline(1, open_=101.0, high=105.1, low=100.2, close=104.8),
        kline(2, open_=104.8, high=105.3, low=99.9, close=100.1),
    ]

    assert first_touch_near("long", bars, 100.0) == 1
    assert reached_before(105.0, bars, before_index=first_touch_near("long", bars, 100.0))
    assert classify_retrace("long", 99.0, 100.0, 105.0, bars)["classification"] == "no_retrace"


def test_first_touch_near_and_reached_before_identify_short_no_retrace():
    bars = [
        kline(1, open_=99.0, high=99.8, low=94.9, close=95.1),
        kline(2, open_=95.1, high=100.1, low=94.5, close=99.9),
    ]

    assert first_touch_near("short", bars, 100.0) == 1
    assert reached_before(95.0, bars, before_index=first_touch_near("short", bars, 100.0))
    assert classify_retrace("short", 100.0, 101.0, 95.0, bars)["classification"] == "no_retrace"


def test_classify_retrace_long_reports_full_breach_depth():
    bars = [
        kline(1, open_=101.0, high=101.2, low=99.8, close=100.2),
        kline(2, open_=100.2, high=100.4, low=98.7, close=99.1),
    ]

    result = classify_retrace("long", 99.0, 100.0, 105.0, bars)

    assert result["classification"] == "retrace"
    assert result["penetration_depth"] == pytest.approx(1.3)
    assert result["breached_far_edge"] is True


def test_classify_retrace_short_reports_partial_penetration():
    bars = [
        kline(1, open_=99.0, high=100.2, low=98.9, close=100.1),
        kline(2, open_=100.1, high=100.5, low=99.7, close=99.9),
    ]

    result = classify_retrace("short", 100.0, 101.0, 95.0, bars)

    assert result["classification"] == "retrace"
    assert result["penetration_depth"] == pytest.approx(0.5)
    assert result["breached_far_edge"] is False


def test_fill_at_depth_long_can_make_zero_and_twentyfive_percent_identical():
    bars = [kline(1, open_=101.0, high=101.2, low=99.74, close=100.1)]

    assert fill_at_depth("long", 99.0, 100.0, 0.0, bars) is True
    assert fill_at_depth("long", 99.0, 100.0, 0.25, bars) is True
    assert fill_at_depth("long", 99.0, 100.0, 0.5, bars) is False


def test_fill_at_depth_short_can_make_zero_and_twentyfive_percent_identical():
    bars = [kline(1, open_=99.0, high=100.26, low=98.8, close=100.1)]

    assert fill_at_depth("short", 100.0, 101.0, 0.0, bars) is True
    assert fill_at_depth("short", 100.0, 101.0, 0.25, bars) is True
    assert fill_at_depth("short", 100.0, 101.0, 0.5, bars) is False


def test_chase_window_includes_exact_premium_boundary_for_long_and_short():
    assert chase_window_hit("long", 100.0, 100.35, premium=0.0035) is True
    assert chase_window_hit("long", 100.0, 100.3501, premium=0.0035) is False
    assert chase_window_hit("short", 100.0, 99.65, premium=0.0035) is True
    assert chase_window_hit("short", 100.0, 99.6499, premium=0.0035) is False


def test_zone_height_pct_uses_midpoint_denominator():
    assert zone_height_pct(99.0, 101.0) == pytest.approx(2.0 / 100.0)


def test_atr_1h_aggregates_1m_bars_and_averages_true_ranges():
    start = datetime(2026, 7, 2, 0, tzinfo=timezone.utc)
    bars = []
    for minute in range(60):
        bars.append(kline(start + timedelta(minutes=minute), 100.0, 110.0, 90.0, 105.0))
    for minute in range(60, 120):
        bars.append(kline(start + timedelta(minutes=minute), 105.0, 120.0, 100.0, 118.0))

    assert atr_1h(bars) == pytest.approx(20.0)


def test_aggregate_returns_section_7_style_counts_and_medians():
    ts = datetime(2026, 7, 2, 12, tzinfo=timezone.utc)
    signals = [
        signal(side="long", received_at=ts, take_profits=(105.0,)),
        signal(side="long", received_at=ts + timedelta(minutes=1), take_profits=(105.0,)),
        signal(symbol="SOLUSDT-PERP.BINANCE", side="short", price_min=100.0, price_max=101.0, received_at=ts, take_profits=(95.0,)),
        signal(symbol="XAUUSDT-PERP.BINANCE", side="long", received_at=ts),
    ]
    by_symbol = {
        "BTCUSDT": [
            kline(ts - timedelta(hours=1), 101.0, 101.0, 99.0, 100.5),
            kline(ts, 101.0, 105.1, 100.2, 104.9),
            kline(ts + timedelta(minutes=1), 101.0, 101.5, 99.8, 100.0),
            kline(ts + timedelta(minutes=2), 100.0, 100.2, 98.7, 99.0),
        ],
        "SOLUSDT": [
            kline(ts - timedelta(hours=1), 99.0, 101.0, 99.0, 100.0),
            kline(ts, 99.65, 99.9, 94.9, 95.0),
            kline(ts + timedelta(minutes=1), 99.0, 100.26, 98.7, 99.7),
        ],
    }

    def provider(symbol, start, end):
        return by_symbol[symbol]

    result = aggregate(signals, provider, window_hours=2)

    assert result["sample_count"] == 3
    assert result["skipped"][0]["symbol"] == "XAUUSDT"
    assert result["no_retrace"]["count"] == 2
    assert result["retrace"]["count"] == 1
    assert result["retrace"]["median_penetration_depth"] == pytest.approx(1.3)
    assert result["fill_rates"]["0%"]["filled"] == 3
    assert result["fill_rates"]["25%"]["filled"] == 3
    assert result["fill_rates"]["50%"]["filled"] == 2
    assert result["chase_window"]["hits"] == 1
    assert result["zone_height"]["median_price_pct"] == pytest.approx(1.0 / 99.5)


def test_normalize_symbol_removes_perp_binance_suffix_and_skips_xau():
    assert normalize_symbol("BTCUSDT-PERP.BINANCE") == "BTCUSDT"
    assert normalize_symbol("solusdt") == "SOLUSDT"
    with pytest.raises(UnsupportedSymbol):
        normalize_symbol("XAUUSDT-PERP.BINANCE")


def test_load_zone_signals_reads_offline_json_export(tmp_path):
    path = tmp_path / "signals.json"
    path.write_text(json.dumps([signal()]), encoding="utf-8")

    loaded = load_zone_signals(export_json_path=path)

    assert len(loaded) == 1
    assert loaded[0]["symbol"] == "BTCUSDT-PERP.BINANCE"
    assert loaded[0]["side"] == "long"


def test_load_klines_reads_cached_binance_zip_without_network(tmp_path):
    day = datetime(2026, 7, 2, tzinfo=timezone.utc)
    symbol_dir = tmp_path / "um" / "BTCUSDT" / "1m"
    symbol_dir.mkdir(parents=True)
    zip_path = symbol_dir / "BTCUSDT-1m-2026-07-02.zip"
    row = "1782950400000,100,101,99,100.5,1,1782950459999,1,1,1,1,0\n"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("BTCUSDT-1m-2026-07-02.csv", row)

    bars = load_klines(
        "BTCUSDT",
        day,
        day + timedelta(days=1),
        cache_dir=tmp_path,
        base_url="https://example.invalid",
    )

    assert bars == [(1782950400000, 100.0, 101.0, 99.0, 100.5)]
