"""十审修复方行为回归；逐测试突变证据见 mutation_evidence.json。"""
import datetime as dt
import json

import polars as pl
import pytest

from quant_lab.market import contract as c, execution as x, vision as v
from tests.market.test_partition_check import bars, rules, run, JAN, INST
from tests.market.test_review_p1 import FIX


@pytest.mark.parametrize("offsets,missing,quarantined", [
    ([0, 1, 60_000_000, 120_000_000], 0, 1),
    ([1, 60_000_001, 120_000_001], 3, 3),
], ids=["mixed-off-grid", "all-off-grid"])
def test_s29_off_grid_never_covers_calendar(offsets, missing, quarantined):
    opens = [JAN + dt.timedelta(microseconds=i) for i in offsets]
    df = bars(len(opens)).with_columns(pl.Series("open_time", opens),
                                       pl.Series("close_time", [t + dt.timedelta(seconds=60) for t in opens]))
    out, qs, report = run(df, rules(eff_from=JAN, eff_to=JAN + dt.timedelta(seconds=180)))
    assert sum(q["reason_code"] == "BAR_TIME_OFF_GRID" for q in qs) == quarantined
    assert out.height == len(opens) - quarantined
    assert report.expected_rows == 3
    assert report.missing == missing >= 0
    assert sum(g["n"] for g in report.gaps) == report.missing
    assert report.status == "quarantined"
    assert not out["gap_flag"].any()


def test_s29_subsecond_calendar_start_has_no_false_first_gap():
    out, _, report = run(bars(2, start=JAN + dt.timedelta(minutes=1)),
                         rules(eff_from=JAN + dt.timedelta(microseconds=1), eff_to=JAN + dt.timedelta(minutes=3)))
    assert report.expected_rows == 2 and report.missing == 0 and report.gaps == []
    assert out["gap_flag"].to_list() == [False, False]


def test_s29_subsecond_end_includes_last_grid_point():
    _, _, report = run(bars(2), rules(eff_from=JAN, eff_to=JAN + dt.timedelta(minutes=2, microseconds=1)))
    assert report.expected_rows == 3 and report.missing == 1
    assert sum(g["n"] for g in report.gaps) == 1


def test_s38_policy_rejects_negative_latency():
    payload = c.POLICIES["fixture-zero-v1"].model_dump()
    with pytest.raises(c.ContractError, match="启动时刻不能早于决策时刻"):
        c.ExecutionPolicy.model_validate({**payload, "latency_s": -1})


def test_s38_resolved_start_checked_for_both_spellings(monkeypatch):
    # 故障注入单一来源：域约束之外，请求边界仍须独立检查解析结果。
    request = FIX["E03"].request
    start = request.t_dec - dt.timedelta(seconds=1)
    monkeypatch.setattr(c, "derived_t_start", lambda t, p: start)
    for explicit in (None, start):
        with pytest.raises(c.ContractError, match="不能早于 t_dec"):
            c.ExecutionRequest.model_validate({**request.model_dump(), "t_start": explicit,
                "horizon_end": start + dt.timedelta(microseconds=1), "horizon_source": "caller"})


def test_s39_loader_grid_return_controls_coverage(tmp_path, monkeypatch):
    # 最小完整本地湖：bar、manifest、funding、rules 都受控，无远程访问。
    request = c.ExecutionRequest.model_validate({**FIX["E03"].request.model_dump(),
        "t_dec": JAN, "t_start": None, "horizon_end": JAN + dt.timedelta(minutes=3), "horizon_source": "caller"})
    lake = v.LakePaths(tmp_path)
    for kind in ("klines", "markPriceKlines"):
        df = bars(3).with_columns(pl.lit(False).alias("gap_flag"), pl.lit(True).alias("ohlc_valid"))
        path = lake.silver_dir(kind, "1m", "BTCUSDT") / "date=2024-01-01" / "part.parquet"
        v.atomic_write_parquet(path, df)
    for kind, interval in (("klines", "1m"), ("markPriceKlines", "1m"), ("fundingRate", "8h")):
        pid = v.partition_id(kind, interval, "BTCUSDT", "2024-01")
        v.atomic_write_json(lake.manifest(pid), {"partition_id": pid, "source_sha256": "h" * 64, "check_status": "ok"})
    from quant_lab.market.partition_check import write_rules
    write_rules(lake, rules())
    v.atomic_write_parquet(lake.silver_dir("fundingRate", "8h", "BTCUSDT") / "date=2024-01-01" / "part.parquet",
                          pl.DataFrame({"calc_time": [JAN], "funding_rate": [0.0], "funding_interval_hours": [8]}))
    real = x.grid_points_between
    baseline = x.load_market_from_lake(request, lake_root=tmp_path)
    assert baseline.bars_complete and baseline.bars_quality_ok
    assert baseline.funding_schedule_complete and baseline.rules_known
    calls = []

    def shifted(a, b, interval):
        calls.append((a, b, interval))
        return real(a, b, interval) + 7

    monkeypatch.setattr(x, "grid_points_between", shifted)
    changed = x.load_market_from_lake(request, lake_root=tmp_path)
    assert calls == [(JAN, request.horizon_end, 60)] * 2
    assert not changed.bars_complete
    assert sum("期望 10 根，实际 3" in note for note in changed.quality_notes) == 2
    monkeypatch.setattr(x, "grid_points_between", real)
    assert x.load_market_from_lake(request, lake_root=tmp_path).bars_complete


@pytest.mark.parametrize("interval", ["1m", "15m"])
@pytest.mark.parametrize("period,days", [("2024-01-01", 1), ("2024-02-29", 1), ("2024-01", 31), ("2024-02", 29), ("2023-02", 28), ("2024-12", 31)])
def test_s33_aligned_calendar_equivalence(interval, period, days):
    assert v.expected_rows("klines", interval, period) == days * 86400 // v.INTERVAL_SECONDS[interval]
