"""真实行情适配与构建准入：每个关键边界配可执行突变。"""
from datetime import timedelta
from decimal import Decimal
import json

import polars as pl
import pytest

from quant_lab.data import api, harvest, sources
from quant_lab.data.lake import Layout
from quant_lab.data.market_lake import LakeMarket
from quant_lab.data.market_stub import FrameMarks
from quant_lab.market import asof
from quant_lab.market.vision import instrument_id
from tests.data.l0_fixtures import CHANNEL, OTHER, SECRET, T0, account, market_lake, mutant


def test_lake_asof_no_lookahead_and_mutants(tmp_path, monkeypatch):
    lake = market_lake(tmp_path, days=1)
    path = lake.silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-07-01" / "part.parquet"
    df = pl.read_parquet(path).with_columns(
        pl.when(pl.col("close_time") >= T0).then(999.0).otherwise(pl.col("close")).alias("close"))
    df.write_parquet(path)

    def invariant():
        marks = LakeMarket(lake.root)
        hit = marks.mark_at(instrument_id("BTCUSDT"), T0)
        assert hit.price == Decimal(100) and hit.close_time == T0 - timedelta(minutes=1)

    invariant()
    # 两层严格可知时刻门都变为 <=，边界未来价必须暴露。
    monkeypatch.setattr(FrameMarks, "mark_at", mutant(FrameMarks.mark_at, '< pl.lit(at)', '<= pl.lit(at)'))
    monkeypatch.setattr(asof, "last_closed_bar", mutant(asof.last_closed_bar, '< pl.lit(at)', '<= pl.lit(at)'))
    with pytest.raises(AssertionError):
        invariant()


def test_lake_registry_bounds_gaps_and_mutants(tmp_path, monkeypatch):
    lake = market_lake(tmp_path, days=1)
    provider = LakeMarket(lake.root)
    inst = instrument_id("BTCUSDT")
    for alias in ("BTC", "btcusdt", inst):
        assert provider.registry.resolve(alias, T0) == (inst, "mapped")
    assert provider.registry.resolve("ETH", T0) == (None, "unknown_symbol")
    assert provider.registry.resolve("BTC", T0 - timedelta(days=1)) == (None, "invalid_time")
    assert provider.registry.resolve("BTC", T0 + timedelta(days=1)) == (None, "invalid_time")
    # 去掉登记时间约束的突变不能通过边界断言。
    cls = type(provider.registry)
    monkeypatch.setattr(cls, "resolve", mutant(cls.resolve,
        'r.effective_from <= at and (r.effective_to is None or at < r.effective_to)', 'True'))
    with pytest.raises(AssertionError):
        assert provider.registry.resolve("BTC", T0 + timedelta(days=1))[1] == "invalid_time"
    path = lake.silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-07-01" / "part.parquet"
    df = pl.read_parquet(path)
    df.filter(pl.col("open_time") < T0 - timedelta(minutes=3)).write_parquet(path)

    def gap_invariant():
        hit = LakeMarket(lake.root).mark_at(inst, T0 + timedelta(seconds=1))
        assert hit.price is None and hit.reason == "MARK_STALE"

    gap_invariant()
    # 容差放大使旧价跨洞续用；应被突变断言杀死。
    monkeypatch.setattr(LakeMarket, "mark_at", mutant(LakeMarket.mark_at,
        'at = at.astimezone(UTC)', 'at = at.astimezone(UTC)\n    max_staleness_s = 86400'))
    with pytest.raises(AssertionError):
        gap_invariant()


def test_build_whitelist_channel_pull_and_mutants(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path))
    base = account(tmp_path)
    lake = market_lake(tmp_path, days=1)
    layout = Layout.from_root(tmp_path)
    harvest.run()
    harvested = layout.message_version.read_bytes()
    result = api.build(None, layout, graph_version="teacher", market_lake=lake.root, channel=CHANNEL)
    assert result["normalize"]["n_messages"] == 2
    bronze = pl.read_parquet(result["normalize"]["paths"]["message_version"])
    assert set(bronze["channel_id"]) == {CHANNEL}
    assert SECRET not in json.dumps(bronze.to_dicts(), default=str)
    assert "UNLISTED_TEXT" not in json.dumps(bronze.to_dicts(), default=str)
    assert set(api.load_episodes("teacher")["channel_id"]) == {CHANNEL}
    assert layout.message_version.read_bytes() == harvested
    # 去掉单频道收窄后，会把第二位白名单老师摄入并构建。
    monkeypatch.setattr(harvest, "scan_inputs", mutant(harvest.scan_inputs,
        'allowed = frozenset({channel})', 'allowed = allowed'))
    changed = api.build(None, layout, graph_version="channel-mutant", market_lake=lake.root, channel=CHANNEL)
    with pytest.raises(AssertionError):
        assert changed["normalize"]["n_messages"] == 2
    assert OTHER in set(api.load_episodes("channel-mutant")["channel_id"])


def test_build_whitelist_refusal_before_writes_and_mutant(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path))
    base = account(tmp_path)
    (base / "channels.txt").unlink()
    with pytest.raises(harvest.HarvestRefusal, match="CHANNEL_WHITELIST_REQUIRED"):
        api.build(None, Layout.from_root(tmp_path), graph_version="no-list", market="real")
    assert not (tmp_path / "lake").exists()
    monkeypatch.setattr(harvest, "scan_inputs", mutant(harvest.scan_inputs,
        'raise HarvestRefusal("CHANNEL_WHITELIST_REQUIRED")', 'pass'))
    with pytest.raises(AssertionError):
        inputs = harvest.scan_inputs(tmp_path, require_whitelist=True)
        assert inputs["allowed"] is not None


def test_build_real_switch_and_mark_stale_mutant(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path))
    account(tmp_path)
    lake = market_lake(tmp_path, days=1)
    layout = Layout.from_root(tmp_path)
    assert api.main(["--build", "--market", "real", "--channel", str(CHANNEL), "--graph-version", "cli"]) == 0
    assert api.load_episodes("cli").height == 2
    mark_path = lake.silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-07-01" / "part.parquet"
    mark_path.unlink()
    api.build(None, layout, graph_version="gap", market="real", channel=CHANNEL)
    gap = api.load_episodes("gap")
    assert gap.height == 2  # 现有 G1 语义：MARK_STALE 禁价格校验，执行侧再判覆盖。
    assert all("MARK_STALE" in codes for codes in gap["reason_codes"])
    assert not gap["eligibility_by_estimand"].struct.field("price_check").any()
    from quant_lab.data.market_stub import SyntheticMarks
    monkeypatch.setattr(LakeMarket, "mark_at", lambda self, inst, at, **kw: SyntheticMarks({inst: [(T0 - timedelta(days=1), 100)]}).mark_at(inst, at))
    api.build(None, layout, graph_version="gap-mutant", market="real", channel=CHANNEL)
    with pytest.raises(AssertionError):
        assert all("MARK_STALE" in codes for codes in api.load_episodes("gap-mutant")["reason_codes"])


def test_build_asof_uses_known_close_not_current_bar(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path))
    account(tmp_path)
    lake = market_lake(tmp_path, days=1)
    path = lake.silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-07-01" / "part.parquet"
    pl.read_parquet(path).with_columns(
        pl.when(pl.col("close_time") >= T0 + timedelta(minutes=1)).then(999.0).otherwise(pl.col("close")).alias("close")
    ).write_parquet(path)
    layout = Layout.from_root(tmp_path)
    result = api.build(None, layout, graph_version="asof", market="real", channel=CHANNEL)
    plans = pl.read_parquet(result["validate"]["paths"]["canonical_plan"]).sort("available_at")
    assert plans["mark_price"][0] == Decimal(100)
    assert api.load_episodes("asof").height == 2  # Unique omitted-unit recovery keeps the later plan.
    repaired = [json.loads(c) for c in plans["checks"] if "unit_rescaled" in json.loads(c)]
    assert repaired and all(c["unit_rescaled"] == {"factor": "10", "basis": "mark"} for c in repaired)
    monkeypatch.setattr(FrameMarks, "mark_at", mutant(FrameMarks.mark_at, '< pl.lit(at)', '<= pl.lit(at)'))
    monkeypatch.setattr(asof, "last_closed_bar", mutant(asof.last_closed_bar, '< pl.lit(at)', '<= pl.lit(at)'))
    changed = api.build(None, layout, graph_version="asof-mutant", market="real", channel=CHANNEL)
    with pytest.raises(AssertionError):
        assert pl.read_parquet(changed["validate"]["paths"]["canonical_plan"]).sort("available_at")["mark_price"][0] == Decimal(100)


@pytest.mark.parametrize("kind", ["whitelist", "private", "pull"])
def test_build_admission_mutants(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path))
    account(tmp_path)
    market_lake(tmp_path, days=1)
    layout = Layout.from_root(tmp_path)

    def assert_private(result):
        # 检查所有湖内 Parquet，包含 bronze、派生图、隔离表与工作集。
        for path in [*(tmp_path / "lake").rglob("*.parquet"), *(tmp_path / "quarantine").rglob("*.parquet")]:
            content = str(pl.read_parquet(path).to_dicts())
            assert SECRET not in content and "UNLISTED_TEXT" not in content

    assert_private(api.build(None, layout, graph_version="safe", market="real"))
    if kind == "whitelist":
        monkeypatch.setattr(sources, "_scan_whole_account", mutant(sources._scan_whole_account,
            'ingest = allowed_peer_ids is None or peer in allowed_peer_ids', 'ingest = True'))
    elif kind == "private":
        # 双门禁同时绕过，模拟把 personal_chat 误当作已准入频道。
        monkeypatch.setattr(sources, "_skip_non_channel", lambda kind: False)
        original = sources.canonical_channel_id
        monkeypatch.setattr(sources, "canonical_channel_id", lambda peer, kind: original(peer, "channel"))
    else:
        monkeypatch.setattr(sources, "read_telethon_jsonl", mutant(sources.read_telethon_jsonl,
            'if allowed_peer_ids is not None and peer not in allowed_peer_ids:', 'if False:'))
        monkeypatch.setattr(sources, "ingest_pull_dir", mutant(sources.ingest_pull_dir,
            'ingest = allowed_peer_ids is None or peer in allowed_peer_ids', 'ingest = True'))
    changed = api.build(None, layout, graph_version="privacy-mutant", market="real")
    with pytest.raises(AssertionError):
        assert_private(changed)


def test_lake_late_revision_and_source_mismatch(tmp_path):
    lake = market_lake(tmp_path, days=1)
    path = lake.silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-07-01" / "part.parquet"
    original = pl.read_parquet(path)
    at = T0 + timedelta(seconds=1)
    late = original.with_columns(pl.lit(T0 + timedelta(days=1)).alias("available_at"))
    late.write_parquet(path)
    assert LakeMarket(lake.root).mark_at(instrument_id("BTCUSDT"), at).reason == "MARK_STALE"
    # 把晚到修订伪装成历史可知时刻的突变，错误地恢复可用价。
    late.with_columns(pl.col("close_time").alias("available_at")).write_parquet(path)
    with pytest.raises(AssertionError):
        assert LakeMarket(lake.root).mark_at(instrument_id("BTCUSDT"), at).price is None
    original.with_columns(pl.lit("wrong-source").alias("source_sha256")).write_parquet(path)
    assert LakeMarket(lake.root).mark_at(instrument_id("BTCUSDT"), at).reason == "MARK_STALE"
    original.write_parquet(path)
    with pytest.raises(AssertionError):
        assert LakeMarket(lake.root).mark_at(instrument_id("BTCUSDT"), at).price is None
