"""R-06：feature_snapshot as-of 等号语义、latency、cutoff、缺槽位 invalid、t_dec 缺失、tombstone、缓存 key。"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quant_lab.research import features as FT
from quant_lab.research.ast import canonical_hash
from quant_lab.research.contract_tests import T0, make_bars
from quant_lab.research.features import SnapshotContext, SnapshotInvalid, feature_snapshot, snapshot_cache_key

UTC = pl.Datetime("us", "UTC")
STEP = dt.timedelta(minutes=15)
MEAN3 = {"op": "Mean", "args": [{"field": "close"}], "window": {"unit": "rows", "count": 3}}
CLOSE = {"op": "Ref", "args": [{"field": "close"}], "params": {"lag": 0}}
H_MEAN, H_CLOSE = canonical_hash(MEAN3), canonical_hash(CLOSE)


def anchors(ts, inst="X", ids=None):
    return pl.DataFrame({"episode_id": ids or [f"e{i}" for i in range(len(ts))], "instrument_id": [inst] * len(ts), "t_dec": ts}, schema_overrides={"t_dec": UTC})


@pytest.fixture
def bars():
    return make_bars([float(i) for i in range(10)])          # close_time = T0 + i*15m，close = i


def test_equality_semantics_and_column_names(bars):
    us = dt.timedelta(microseconds=1)
    t = T0 + 4 * STEP                                          # 第 5 根 bar 的 close_time
    out = feature_snapshot([CLOSE, MEAN3], anchors([t, t - us, t + us, t + 14 * dt.timedelta(minutes=1)]), bars=bars)
    assert out.columns == ["episode_id", "t_dec", f"f_{H_CLOSE}", f"validity_{H_CLOSE}", f"f_{H_MEAN}", f"validity_{H_MEAN}"]
    assert out[f"f_{H_CLOSE}"].to_list() == [4.0, 3.0, 4.0, 4.0]          # 等号可见；早 1us 取前一根
    assert out[f"f_{H_MEAN}"].to_list() == [3.0, 2.0, 3.0, 3.0]
    assert out[f"validity_{H_MEAN}"].all()


def test_latency_shifts_visibility(bars):
    t = T0 + 4 * STEP
    out = feature_snapshot([CLOSE], anchors([t, t + dt.timedelta(seconds=1)]), bars=bars, latency=dt.timedelta(seconds=1))
    assert out[f"f_{H_CLOSE}"].to_list() == [3.0, 4.0]                      # latency=1s：整点不可见，+1s 可见


def test_warmup_and_missing_slot_invalid(bars):
    # 前 2 根 Mean3 warm-up → invalid；丢第 5 根 bar 后 t_dec 落在其上 → 最近计划槽位缺失，不向前找 → invalid
    gapped = bars.filter(pl.col("close") != 4.0)
    ts = [T0 + 0 * STEP, T0 + 1 * STEP, T0 + 4 * STEP, T0 + 4 * STEP + dt.timedelta(minutes=10), T0 + 5 * STEP]
    out = feature_snapshot([CLOSE, MEAN3], anchors(ts), bars=gapped)
    assert out[f"validity_{H_MEAN}"].to_list() == [False, False, False, False, False]   # 5 根：窗含缺失槽位
    assert out[f"validity_{H_CLOSE}"].to_list() == [True, True, False, False, True]
    assert out[f"f_{H_CLOSE}"].to_list() == [0.0, 1.0, None, None, 5.0]
    # 放宽 staleness 才允许取更早的 bar（显式冻结容限）
    out2 = feature_snapshot([CLOSE], anchors(ts), bars=gapped, max_staleness=2 * STEP)
    assert out2[f"f_{H_CLOSE}"].to_list() == [0.0, 1.0, 3.0, 3.0, 5.0]


def test_cutoff_bounds_data_and_rejects_late_anchors(bars):
    cutoff = T0 + 5 * STEP
    ts = [T0 + 4 * STEP, T0 + 5 * STEP, T0 + 6 * STEP, T0 + 9 * STEP]
    out = feature_snapshot([CLOSE], anchors(ts), bars=bars, cutoff=cutoff)
    assert out[f"validity_{H_CLOSE}"].to_list() == [True, True, False, False]        # t_dec > cutoff 拒评，不偷换
    assert out[f"f_{H_CLOSE}"].to_list() == [4.0, 5.0, None, None]


def test_t_dec_missing_is_invalid_not_derived(bars):
    a = anchors([T0 + 4 * STEP, None])
    out = feature_snapshot([CLOSE], a, bars=bars)
    assert out[f"validity_{H_CLOSE}"].to_list() == [True, False] and out["t_dec"][1] is None


def test_preconditions(bars):
    with pytest.raises(SnapshotInvalid, match="UTC"):
        feature_snapshot([CLOSE], anchors([T0]).with_columns(pl.col("t_dec").dt.replace_time_zone(None)), bars=bars)
    with pytest.raises(SnapshotInvalid, match="重复"):
        feature_snapshot([CLOSE], anchors([T0, T0], ids=["a", "a"]), bars=bars)
    with pytest.raises(SnapshotInvalid, match="tombstone"):
        feature_snapshot([CLOSE], anchors([T0]).with_columns(pl.lit(True).alias("is_tombstone")), bars=bars)
    with pytest.raises(SnapshotInvalid, match="周期"):
        feature_snapshot([CLOSE], anchors([T0]), bars=pl.concat([bars, bars.with_columns(pl.lit("1m").alias("interval"), pl.col("close_time") + dt.timedelta(minutes=1))]))


def test_multi_instrument_isolation(fake_bars, fake_episodes):
    a = fake_episodes.select("episode_id", "instrument_id", "t_dec")
    out = feature_snapshot([MEAN3], a, bars=fake_bars)
    assert out.height == a.height and out["episode_id"].to_list() == a["episode_id"].to_list()
    # 与逐品种单算一致
    for inst in a["instrument_id"].unique():
        sub = feature_snapshot([MEAN3], a.filter(pl.col("instrument_id") == inst), bars=fake_bars.filter(pl.col("instrument_id") == inst))
        assert sub[f"f_{H_MEAN}"].to_list() == out.filter(a["instrument_id"] == inst)[f"f_{H_MEAN}"].to_list()


CTX = SnapshotContext(market_manifest="mm1", graph_version="gv1", fold_id="f0", derivation_hash="dh1", consumable=lambda graph_version, derivation_hash, revocation_epoch: True)


def test_cache_key_and_roundtrip(bars, lake_root):
    a = anchors([T0 + 4 * STEP, T0 + 6 * STEP])
    k1 = snapshot_cache_key(MEAN3, backend="polars", backend_version="0.2", interval="15m", ctx=CTX, anchor_hash=FT.anchor_hash(a))
    k2 = snapshot_cache_key(MEAN3, backend="polars", backend_version="0.2", interval="15m", ctx=SnapshotContext("mm1", "gv2", "f0", derivation_hash="dh1", consumable=lambda graph_version, derivation_hash, revocation_epoch: True), anchor_hash=FT.anchor_hash(a))
    k3 = snapshot_cache_key(MEAN3, backend="polars_ta", backend_version="0.2", interval="15m", ctx=CTX, anchor_hash=FT.anchor_hash(a))
    k4 = snapshot_cache_key(MEAN3, backend="polars", backend_version="0.2", interval="15m", ctx=SnapshotContext("mm1", "gv1", "f0", derivation_hash="dh2", consumable=lambda graph_version, derivation_hash, revocation_epoch: True), anchor_hash=FT.anchor_hash(a))
    k5 = snapshot_cache_key(MEAN3, backend="polars", backend_version="0.2", interval="15m", ctx=CTX, anchor_hash=FT.anchor_hash(a[::-1]))
    assert len({k1, k2, k3, k4, k5}) == 5 and len(k1) == 64                       # graph_version / backend / derivation_hash / anchor 顺序进 key
    out1 = feature_snapshot([MEAN3], a, bars=bars, ctx=CTX, cache=True)
    assert FT.cache_dir().is_relative_to(lake_root) and any(FT.cache_dir().iterdir())   # 缓存落在 QUANT_LAB_DATA_ROOT 下
    out2 = feature_snapshot([MEAN3], a, bars=bars, ctx=CTX, cache=True)               # 热读
    assert out1.equals(out2)
    out3 = feature_snapshot([MEAN3], a, bars=bars, ctx=SnapshotContext("mm1", "gv2", "f0", derivation_hash="dh1", consumable=lambda graph_version, derivation_hash, revocation_epoch: True), cache=True)   # 新图版本 → miss，重算
    assert out3.equals(out1)


def test_cache_hot_read_follows_episode_identity_not_position(bars, lake_root):
    """S01：同集合换序热读必须逐 episode 一致；缓存条目按 episode_id join。"""
    a = anchors([T0 + 4 * STEP, T0 + 6 * STEP], ids=["a", "b"])
    cold = feature_snapshot([CLOSE], a, bars=bars, ctx=CTX, cache=True)
    assert cold[f"f_{H_CLOSE}"].to_list() == [4.0, 6.0]
    rev = a[::-1]
    hot_rev = feature_snapshot([CLOSE], rev, bars=bars, ctx=CTX, cache=True)
    assert hot_rev["episode_id"].to_list() == ["b", "a"] and hot_rev[f"f_{H_CLOSE}"].to_list() == [6.0, 4.0]
    nocache = feature_snapshot([CLOSE], rev, bars=bars, ctx=CTX, cache=False)
    assert hot_rev.equals(nocache)
    # 缓存条目篡改成缺行/错 schema → miss 重算，不按位置兜底
    import polars as pl_
    for f in FT.cache_dir().glob("*.parquet"):
        pl_.DataFrame({"__row": [0, 1], "value": [99.0, 98.0], "__valid": [True, True]}).write_parquet(f)
    again = feature_snapshot([CLOSE], a, bars=bars, ctx=CTX, cache=True)
    assert again[f"f_{H_CLOSE}"].to_list() == [4.0, 6.0]


def test_context_is_single_source_and_in_key(bars, lake_root):
    """S02：ctx 与便捷参数冲突拒绝；latency / cutoff / staleness 变化不会命中旧缓存。"""
    a = anchors([T0 + 4 * STEP, T0 + 6 * STEP])
    with pytest.raises(SnapshotInvalid, match="latency 冲突"):
        feature_snapshot([CLOSE], a, bars=bars, ctx=SnapshotContext(latency=dt.timedelta(seconds=1)), latency=dt.timedelta(seconds=2))
    with pytest.raises(SnapshotInvalid, match="cutoff 冲突"):
        feature_snapshot([CLOSE], a, bars=bars, ctx=SnapshotContext(cutoff=T0 + 5 * STEP), cutoff=T0 + 6 * STEP)
    base = feature_snapshot([CLOSE], a, bars=bars, ctx=CTX, cache=True)
    assert base[f"f_{H_CLOSE}"].to_list() == [4.0, 6.0]
    lat = feature_snapshot([CLOSE], a, bars=bars, ctx=SnapshotContext("mm1", "gv1", "f0", latency=dt.timedelta(seconds=1), derivation_hash="dh1", consumable=lambda graph_version, derivation_hash, revocation_epoch: True), cache=True)
    assert lat[f"f_{H_CLOSE}"].to_list() == [3.0, 5.0]                                # ctx-only latency 生效且不复用旧缓存
    cut = feature_snapshot([CLOSE], a, bars=bars, ctx=SnapshotContext("mm1", "gv1", "f0", cutoff=T0 + 5 * STEP, derivation_hash="dh1", consumable=lambda graph_version, derivation_hash, revocation_epoch: True), cache=True)
    assert cut[f"validity_{H_CLOSE}"].to_list() == [True, False]                     # ctx-only cutoff 限制数据与 anchor
    gapped = bars.filter(pl.col("close") != 4.0)
    wide = feature_snapshot([CLOSE], a, bars=gapped, ctx=SnapshotContext("mm1", "gv1", "f0", max_staleness=2 * STEP, derivation_hash="dh1", consumable=lambda graph_version, derivation_hash, revocation_epoch: True), cache=True)
    strict = feature_snapshot([CLOSE], a, bars=gapped, ctx=CTX, cache=True)
    assert wide[f"f_{H_CLOSE}"].to_list() == [3.0, 6.0] and strict[f"f_{H_CLOSE}"].to_list() == [None, 6.0]
    with pytest.raises(SnapshotInvalid, match="禁缓存"):
        feature_snapshot([CLOSE], a, bars=bars, ctx=SnapshotContext("mm1", "gv1", "f0"), cache=True)   # unknown derivation / 无回调 → 禁缓存


def test_late_available_at_is_not_visible(bars):
    """bars 带 available_at 时：晚到（available_at > t_dec）的 bar 不可用。"""
    ts = [T0 + 4 * STEP, T0 + 4 * STEP + dt.timedelta(minutes=10)]
    on_time = bars.with_columns(pl.col("close_time").alias("available_at"))
    assert feature_snapshot([CLOSE], anchors(ts), bars=on_time)[f"validity_{H_CLOSE}"].to_list() == [True, True]
    late = bars.with_columns(pl.when(pl.col("close") == 4.0).then(pl.col("close_time") + dt.timedelta(minutes=30)).otherwise(pl.col("close_time")).alias("available_at"))
    out = feature_snapshot([CLOSE], anchors(ts), bars=late)
    assert out[f"validity_{H_CLOSE}"].to_list() == [False, False]                    # bar4 晚到 30 分钟：两个 anchor 都不可见，且不回退到 bar3


def test_revocation_gate_blocks_cached_and_fresh_reads(bars, lake_root):
    """S03：撤销/tombstone 回调在读前与发布前均生效；旧图缓存不可读。"""
    a = anchors([T0 + 4 * STEP])
    state = {"ok": True}
    ctx = SnapshotContext("mm1", "gv1", "f0", derivation_hash="dh1", consumable=lambda graph_version, derivation_hash, revocation_epoch: state["ok"])
    feature_snapshot([CLOSE], a, bars=bars, ctx=ctx, cache=True)
    state["ok"] = False
    with pytest.raises(SnapshotInvalid, match="撤销"):
        feature_snapshot([CLOSE], a, bars=bars, ctx=ctx, cache=True)
    # 计算期间被撤销：发布前复查丢弃
    calls = {"n": 0}
    def flaky(graph_version, derivation_hash, revocation_epoch):
        calls["n"] += 1
        return calls["n"] == 1
    ctx2 = SnapshotContext("mm1", "gv1", "f0", derivation_hash="dh9", consumable=flaky)
    with pytest.raises(SnapshotInvalid, match="发布前复查"):
        feature_snapshot([CLOSE], a, bars=bars, ctx=ctx2, cache=True)
    assert not list(FT.cache_dir().glob("*.tmp"))


def test_quarantined_backend_op_rejected_in_snapshot(bars):
    from quant_lab.research.backends import BackendOpQuarantined
    corr = {"op": "Corr", "args": [{"field": "close"}, {"field": "volume"}], "window": {"unit": "rows", "count": 3}}
    with pytest.raises(BackendOpQuarantined):
        feature_snapshot([corr], anchors([T0 + 4 * STEP]), bars=bars, backend="polars_ta")
    feature_snapshot([corr], anchors([T0 + 4 * STEP]), bars=bars, backend="polars")


def test_s02_historical_dependency_late_available_at(bars):
    """二审 S02：窗口内历史 bar 晚到 → 依赖它的特征在该 anchor 不可见（不仅核末根）。"""
    late3 = bars.with_columns(pl.when(pl.col("close") == 3.0).then(pl.col("close_time") + dt.timedelta(hours=2)).otherwise(pl.col("close_time")).alias("available_at"))
    t4 = T0 + 4 * STEP
    out = feature_snapshot([CLOSE, MEAN3], anchors([t4, t4 + dt.timedelta(hours=2)]), bars=late3)
    assert out[f"validity_{H_CLOSE}"].to_list() == [True, False]                      # 末根 bar4 准时可见；2h 后最近槽位是 bar12 → 不在 bars 里？→ invalid
    assert out[f"validity_{H_MEAN}"].to_list() == [False, False]                      # Mean3@bar4 依赖 bar3（晚到 2h）→ 不可见
    vis = feature_snapshot([MEAN3], anchors([t4]), bars=late3.filter(pl.col("available_at") <= t4))
    assert vis[f"validity_{H_MEAN}"].to_list() == [False]                             # 与"只喂真实可见 bar"一致
    ema = {"op": "EMA", "args": [{"field": "close"}], "window": {"unit": "rows", "count": 2}}
    he = canonical_hash(ema)
    out2 = feature_snapshot([ema], anchors([T0 + 8 * STEP]), bars=late3)
    assert out2[f"validity_{he}"].to_list() == [False]                                # EMA 无限记忆：任一历史 bar 晚到即不可见
    on_time = bars.with_columns(pl.col("close_time").alias("available_at"))
    assert feature_snapshot([MEAN3, ema], anchors([t4, T0 + 8 * STEP]), bars=on_time).select(pl.col(f"^validity_.*$")).to_numpy().all()


def test_s03_revocation_during_hot_read_and_before_return(bars, lake_root, monkeypatch):
    a = anchors([T0 + 4 * STEP])
    state = {"ok": True}
    ctx = SnapshotContext("mm1", "gv1", "f0", derivation_hash="dh1", consumable=lambda graph_version, derivation_hash, revocation_epoch: state["ok"])
    feature_snapshot([CLOSE], a, bars=bars, ctx=ctx, cache=True)
    orig = FT._cache_read
    def read_then_revoke(key, aa):
        state["ok"] = False
        return orig(key, aa)
    monkeypatch.setattr(FT, "_cache_read", read_then_revoke)
    with pytest.raises(SnapshotInvalid, match="热读期间"):
        feature_snapshot([CLOSE], a, bars=bars, ctx=ctx, cache=True)
