"""R-01：环境、骨架与桩的冒烟——按 G1/G2 签名核对合成夹具列与类型。"""
from __future__ import annotations

import importlib
import os

import polars as pl

from quant_lab.research import synthetic
from quant_lab.research.paths import ENV_DATA_ROOT, data_root, ledger_path

MODULES = ["ast", "ops", "backends", "backends.polars", "backends.polars_ta", "features", "evaluator",
           "protocol", "ledger", "maxt", "nullmodel", "tiers", "grammar", "api", "synthetic", "paths"]


def test_skeleton_importable():
    for m in MODULES:
        importlib.import_module(f"quant_lab.research.{m}")
    import arch, deap, polars_ta  # noqa: F401  R-01 验收：三方库可 import


def test_data_root_follows_env(lake_root, monkeypatch):
    """湖根目录只经 QUANT_LAB_DATA_ROOT 解析（§7.5）；改环境变量即改路径，不写死相对 data/。"""
    assert os.environ[ENV_DATA_ROOT] == str(lake_root)
    assert ledger_path() == lake_root / "lockbox" / "ledger.parquet"
    monkeypatch.setenv(ENV_DATA_ROOT, str(lake_root / "other"))
    assert ledger_path() == lake_root / "other" / "lockbox" / "ledger.parquet"
    monkeypatch.delenv(ENV_DATA_ROOT)
    assert data_root().name == "data" and data_root().parent.name == "quant-lab"


def test_fake_bars_schema(fake_bars, fake_bars_gapped):
    for c in ("instrument_id", "interval", "open_time", "close_time", "open", "high", "low", "close", "volume",
              "event_time", "available_at", "ingested_at", "ohlc_valid", "spike_flag", "gap_flag"):
        assert c in fake_bars.columns
    assert fake_bars.schema["close_time"] == synthetic.UTC_US
    # close_time = open_time + interval（右端点）；available_at = close_time（H0 latency=0）
    d = fake_bars.select((pl.col("close_time") - pl.col("open_time")).dt.total_seconds().alias("s"))
    assert d["s"].unique().to_list() == [900]
    assert fake_bars["available_at"].equals(fake_bars["close_time"])
    assert fake_bars["instrument_id"].unique().sort().to_list() == sorted(synthetic.DEFAULT_INSTRUMENTS)
    assert all("." in i and i.endswith("BINANCE-UM") for i in synthetic.DEFAULT_INSTRUMENTS)   # 裁定 A1 格式
    assert fake_bars_gapped.height < fake_bars.height                                          # 缺 bar 不插值


def test_fake_episodes_schema(fake_episodes):
    e = fake_episodes
    for c in ("episode_id", "graph_version", "channel_id", "instrument_id", "side", "cluster_id", "duplicate_group_id",
              "t_dec", "processing_delay_s", "order_plan", "decision_snapshot_hash", "decision_eligible_at",
              "right_censored", "censor_reason", "is_tombstone", "event_time", "available_at", "ingested_at"):
        assert c in e.columns, c
    assert e.schema["t_dec"] == synthetic.UTC_US and e.schema["processing_delay_s"] == pl.Int64
    assert e["episode_id"].n_unique() == e.height
    # t_dec 实列 = available_at + processing_delay_s（裁定 A2），不等于诊断列 decision_eligible_at
    chk = e.select((pl.col("t_dec") - pl.col("available_at")).dt.total_seconds().cast(pl.Int64).alias("d"), "processing_delay_s")
    assert chk["d"].equals(chk["processing_delay_s"])
    op = e["order_plan"][0]
    assert set(op) >= {"instrument_id", "side", "entries", "stop", "tps", "sizing", "expiry", "reduce_only_exit"}
    assert op["stop"]["trigger"] == "mark"
    assert e["decision_snapshot_hash"].str.len_chars().unique().to_list() == [64]
    assert e["right_censored"].sum() > 0 and e.filter(pl.col("right_censored"))["censor_reason"].null_count() == 0
    assert e["cluster_id"].n_unique() < e.height     # 有簇合并
    assert (~e["is_tombstone"]).all()


def test_fake_execution_schema(fake_episodes, fake_execution):
    x = fake_execution
    assert list(x.columns) == list(synthetic.BATCH_SCHEMA) and dict(x.schema) == synthetic.BATCH_SCHEMA
    assert x.schema["net_R"] == synthetic.DEC
    from quant_lab.research.evaluator import freeze_opportunity_set
    ids = freeze_opportunity_set(fake_episodes).episode_ids
    assert 0 < x.height == len(ids) < fake_episodes.height and set(x["episode_id"]) == set(ids)
    # §7.4 判定表：删失 → net_R null；未成交 → 0；不含 candidate 概念（只有一个 policy_version）
    cens = x.filter(pl.col("censor_reason").is_not_null())
    assert cens.height > 0 and cens["net_R"].null_count() == cens.height and cens["net_pnl"].null_count() == cens.height
    none = x.filter((pl.col("fill_status") == "none") & pl.col("censor_reason").is_null())
    assert none.height > 0 and none["net_R"].cast(pl.Float64).abs().max() == 0.0
    ok = x.filter(pl.col("censor_reason").is_null() & (pl.col("fill_status") != "none"))
    assert ok["net_R"].null_count() == 0
    assert x["policy_version"].n_unique() == 1
    assert x["trace_hash"].n_unique() == x.height
    fields = {f.name for f in x.schema["canonical_events"].inner.fields}
    assert fields >= {"seq", "ts", "kind", "order_id", "leg", "trigger_basis", "price", "qty", "fee", "reason", "bar_open_time", "path_step", "cash_delta"}
    assert {"mark_ok", "funding_ok", "rules_ok", "bars_ok", "liquidation_unmodeled"} <= set(x.columns)     # coverage_mask 平铺（R9）
    assert set(x["entry_ttl_source"].unique()) <= {"plan", "policy"}
