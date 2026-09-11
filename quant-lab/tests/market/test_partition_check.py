"""M-04：分区体检 + quarantine。注入缺口/重复/OHLC 违规/尖刺/上市前/精度/funding 周期/schema 漂移 → 对应原因码。"""
from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest

from quant_lab.market import partition_check as pc
from quant_lab.market import vision as v
from tests.market.test_vision import bar_rows, make_zip, mock_client, JAN, REL

INST = "BTCUSDT-PERP.BINANCE-UM"


def bars(n=200, start=JAN, price=100.0) -> pl.DataFrame:
    df, _ = v.parse_zip(make_zip(bar_rows(start, n, price=price), "x.csv"), "markPriceKlines", "1m", "BTCUSDT")
    return df.with_columns(pl.lit(start).cast(pl.Datetime("us", "UTC")).alias("ingested_at"),
                           pl.lit("h" * 64).alias("source_sha256"), pl.lit("v1").alias("rule_version"))


def rules(eff_from=JAN - dt.timedelta(days=365), eff_to=None, tick="0.1"):
    return pl.DataFrame([{"instrument_id": INST, "effective_from": eff_from, "effective_to": eff_to, "tick_size": tick,
                          "step_size": "0.001", "min_notional": "5", "multiplier": "1", "funding_interval_hours": 8,
                          "status": "TRADING", "source": "test"}], schema=pc.RULES_SCHEMA)


def run(df, rules_df=rules(), period="2024-01", data_type="klines", **kw):
    return pc.check_bars(df, pid="p", data_type=data_type, interval="1m", inst=INST, period=period, rules=rules_df, **kw)


def codes(qs):
    return sorted({q["reason_code"] for q in qs})


def test_clean_partition_no_quarantine():
    df = bars(31 * 1440)
    out, qs, rep = run(df)
    assert qs == [] and rep.status == "ok" and rep.missing == 0 and rep.expected_rows == 31 * 1440
    assert out["ohlc_valid"].all() and not out["spike_flag"].any() and not out["gap_flag"].any()


def test_gap_flagged_on_next_bar_not_filled():
    df = bars(100)
    df = df.filter(~pl.col("open_time").is_in([JAN + dt.timedelta(minutes=m) for m in (10, 11, 12)]))
    out, qs, rep = run(df)
    assert pc.R_BAR_GAP in codes(qs) and out.height == 97           # 不插值
    assert out.filter(pl.col("gap_flag"))["open_time"].to_list() == [JAN + dt.timedelta(minutes=13)]
    gap = next(g for g in rep.gaps if g["n"] == 3)
    assert gap["from"] == (JAN + dt.timedelta(minutes=10)).isoformat()
    assert rep.missing == 31 * 1440 - 97 and rep.status == "gap"    # 月末缺口也算 missing


def test_duplicate_and_unsorted_keys():
    df = bars(50)
    df = pl.concat([df, df.slice(5, 1)]).sort("open_time", descending=True)
    out, qs, rep = run(df)
    assert pc.R_KEY_DUP in codes(qs) and rep.duplicates == 1 and out.height == 50 and out["open_time"].is_sorted()


def test_ohlc_invalid_flagged_row_kept():
    df = bars(60)
    df = df.with_columns(pl.when(pl.col("open_time") == JAN + dt.timedelta(minutes=7)).then(0.0).otherwise(pl.col("low")).alias("low"))
    out, qs, rep = run(df)
    assert pc.R_OHLC in codes(qs) and out.height == 60 and out.filter(~pl.col("ohlc_valid")).height == 1
    q = next(q for q in qs if q["reason_code"] == pc.R_OHLC)
    assert q["severity"] == "error" and q["object_id"].endswith("10:07:00+00:00".replace("10:07", "00:07")) and rep.status == "quarantined"


def test_spike_flagged_not_dropped_and_not_across_gap():
    df = bars(300)
    # 注入尖刺：第 150 根 close 跳 +30%，其余围绕 100 微幅噪声
    import random
    rnd = random.Random(1)
    closes = [100 + rnd.uniform(-0.05, 0.05) for _ in range(300)]
    closes[150] = 130.0
    df = df.with_columns(pl.Series("close", closes)).with_columns(pl.max_horizontal("high", "close").alias("high"))
    out, qs, rep = run(df)
    spikes = out.filter(pl.col("spike_flag"))
    assert out.height == 300 and pc.R_SPIKE in codes(qs)
    assert (JAN + dt.timedelta(minutes=150)) in spikes["open_time"].to_list()
    assert out["spike_score"][150] > pc.SPIKE_K and rep.status != "quarantined"   # 尖刺 severity=info，不隔离分区（300 根<整月 → gap）
    # 缺口后的首根 bar 不计收益：删掉 149，150 的跳变不再被当作尖刺
    df2 = df.filter(pl.col("open_time") != JAN + dt.timedelta(minutes=149))
    out2, _, _ = run(df2)
    assert out2.filter(pl.col("open_time") == JAN + dt.timedelta(minutes=150))["spike_score"][0] is None


def test_lifecycle_symbol_time_invalid_and_calendar_from_rules():
    df = bars(1440 * 3)                                   # 1/1–1/3
    listed = JAN + dt.timedelta(days=1)                   # 1/2 才上市
    out, qs, rep = run(df, rules(eff_from=listed))
    assert pc.R_SYMBOL_TIME in codes(qs)
    assert sum(q["reason_code"] == pc.R_SYMBOL_TIME for q in qs) == 1440
    assert rep.expected_rows == 30 * 1440                  # 日历从上市日起算
    assert not out.filter(pl.col("open_time") < listed)["gap_flag"].any()


def test_no_rules_marks_rule_history_missing():
    _, qs, rep = run(bars(60), None)
    assert pc.R_RULE_MISSING in codes(qs) and rep.expected_rows == 31 * 1440


def test_precision_off_grid():
    df = bars(60).with_columns(pl.when(pl.col("open_time") == JAN).then(100.05).otherwise(pl.col("close")).alias("close"))
    _, qs, _ = run(df, rules(tick="0.1"))
    assert sum(q["reason_code"] == pc.R_PRECISION for q in qs) == 1
    _, qs2, _ = run(df, rules(tick="0.01"))
    assert pc.R_PRECISION not in codes(qs2)
    _, qs3, _ = run(df, rules(tick="0.1"), data_type="markPriceKlines")   # 计算价不受 tick 约束
    assert pc.R_PRECISION not in codes(qs3)


def test_schema_drift_quarantines_whole_partition():
    df = bars(60)
    out, qs, rep = run(df, expected_schema_hash="deadbeef")
    assert codes(qs) == [pc.R_SCHEMA] and rep.status == "quarantined" and qs[0]["severity"] == "fatal"


def funding(times, hours):
    return pl.DataFrame({"instrument_id": [INST] * len(times), "calc_time": times, "funding_interval_hours": hours,
                         "funding_rate": [0.0001] * len(times)}, schema_overrides={"calc_time": pl.Datetime("us", "UTC")})


def test_funding_schedule_gap_and_variable_interval():
    ok = funding([JAN + dt.timedelta(hours=h) for h in (0, 8, 16, 20, 24)], [8, 8, 8, 4, 4])
    _, qs, rep = pc.check_funding(ok, pid="f", inst=INST, period="2024-01")
    assert qs == [] and rep.status == "ok"
    bad = funding([JAN + dt.timedelta(hours=h) for h in (0, 8, 24)], [8, 8, 8])   # 缺 16:00
    _, qs, rep = pc.check_funding(bad, pid="f", inst=INST, period="2024-01")
    assert codes(qs) == [pc.R_FUNDING] and rep.status == "quarantined"
    jitter = funding([JAN, JAN + dt.timedelta(hours=8, seconds=7)], [8, 8])       # 秒级抖动容忍
    _, qs, _ = pc.check_funding(jitter, pid="f", inst=INST, period="2024-01")
    assert qs == []


def test_end_to_end_partition_idempotent_quarantine(lake_dir):
    lake = v.LakePaths(lake_dir)
    rows = bar_rows(JAN, 31 * 1440)
    del rows[100]                       # 缺口
    rows[200][3] = 0.0                  # OHLC 违规 (low=0)
    client, _ = mock_client({REL: make_zip(rows, "x.csv")})
    v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    pc.write_rules(lake, rules())
    rep = pc.check_partition(lake, data_type="markPriceKlines", interval="1m", symbol="BTCUSDT", period="2024-01")
    assert rep.reason_counts == {pc.R_BAR_GAP: 1, pc.R_OHLC: 1} and rep.status == "quarantined"
    q = pl.read_parquet(pc.quarantine_path(lake))
    assert q.height == 2 and set(q["reason_code"]) == {pc.R_BAR_GAP, pc.R_OHLC}
    m = json.loads(lake.manifest(v.partition_id("markPriceKlines", "1m", "BTCUSDT", "2024-01")).read_text())
    assert m["quarantine_n"] == 2 and m["check_status"] == "quarantined" and m["missing"] == 1
    # silver 回写了 mask
    part = pl.read_parquet(lake.silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-01-01" / "part.parquet")
    assert part.filter(pl.col("gap_flag")).height == 1 and part.filter(~pl.col("ohlc_valid")).height == 1 and part.height == 1439
    # 同输入同规则重跑：不产生重复隔离记录
    rep2 = pc.check_partition(lake, data_type="markPriceKlines", interval="1m", symbol="BTCUSDT", period="2024-01")
    assert rep2.quarantine_n == 2 and pl.read_parquet(pc.quarantine_path(lake)).height == 2
    assert pc.append_quarantine(lake, []) == 0


def test_rules_from_exchange_info_snapshot():
    info = {"symbols": [{"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING", "onboardDate": 1569398400000,
                         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"}, {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                                     {"filterType": "MIN_NOTIONAL", "notional": "100"}]},
                        {"symbol": "BTCUSDT_240329", "contractType": "CURRENT_QUARTER", "filters": []}]}
    r = pc.rules_from_exchange_info(info, effective_from=JAN)
    assert r.height == 1 and r["tick_size"][0] == "0.10" and r["effective_from"][0] == JAN
    assert pc.rule_at(r, INST, JAN + dt.timedelta(days=3))["step_size"] == "0.001"
    assert pc.rule_at(r, INST, JAN - dt.timedelta(days=3)) is None


def test_rules_from_manifests_never_infers_delisting(lake_dir):
    lake = v.LakePaths(lake_dir)
    client, _ = mock_client({REL: make_zip(bar_rows(JAN, 50), "x.csv")})
    v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    r = pc.rules_from_manifests(lake, INST, tick_size="0.1")
    assert r.height == 1 and r["effective_from"][0] == JAN and r["effective_to"][0] is None
    assert pc.rules_from_manifests(lake, "NOPE").height == 0
