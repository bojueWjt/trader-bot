"""feature_snapshot（契约 feature-snapshot §3；ADR-G3 §5；review-G3-P1 S01–S03 闭合）。

对齐规则：特征时间戳 = bar close_time（区间右端点），取 `close_time + latency <= t_dec` 的最后一根（H0 latency=0 时等号成立）；
bars 若带 available_at（真实可知时刻），还须 `available_at <= t_dec`，晚到数据不得提前使用。最近计划槽位 bar 缺失或过旧
（staleness >= max_staleness，默认 = interval）→ invalid，不无限向前找有效值。cutoff 是数据可见上限：close_time > cutoff 的 bar 不参与；
t_dec > cutoff 的 anchor 拒评（validity False），不把 t_dec 偷换成 cutoff。t_dec 缺失 → invalid 并计损耗（不推导）。

**唯一有效上下文**：cutoff / latency / max_staleness 只有一套来源——SnapshotContext；便捷参数与 ctx 同时给且不等 → SnapshotInvalid。
缓存 key 含全部有效上下文与数据身份（canonical_hash, canonicalization_version, op_versions, registry_digest, backend/version,
market_manifest, interval, latency, max_staleness, cutoff, fold_id, preprocess_state, graph_version, derivation_hash, revocation_epoch,
anchor 的有序 (episode_id, instrument_id, t_dec) 序列摘要）。缓存条目按 episode_id 身份一对一 join 回读（不按行位置），
行数/列/身份不符即 miss。cache=True 要求 market_manifest / graph_version / derivation_hash 均非 unknown，且 ctx.check_identity(anchors)
（撤销/tombstone 读前校验回调）在读与写前都通过；临时文件唯一。输出列：episode_id, t_dec, f_<hash>..., validity_<hash>...。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import uuid
import warnings
from dataclasses import dataclass, replace
from typing import Callable

import polars as pl

from quant_lab.research import paths
from quant_lab.research.ast import CANONICALIZATION_VERSION, canonical_hash, lint
from quant_lab.research.backends import assert_ast_supported, get_backend, interval_minutes

UTC_US = pl.Datetime("us", "UTC")


class SnapshotInvalid(ValueError):
    """anchors / bars 前置不满足：缺列、t_dec 非 UTC、episode_id 重复、tombstone 行、interval 不唯一。"""


@dataclass(frozen=True)
class SnapshotContext:
    """显式上下文（ADR C04）：进入缓存 key 的全部血缘与时间语义；consumable 为撤销/tombstone 读前校验回调（None = 无法校验）。"""
    market_manifest: str = "unknown"
    graph_version: str = "unknown"
    fold_id: str = "none"
    preprocess_state: str = "none"
    cutoff: dt.datetime | None = None
    latency: dt.timedelta = dt.timedelta(0)
    max_staleness: dt.timedelta | None = None        # None → interval
    derivation_hash: str = "unknown"
    revocation_epoch: int = 0
    consumable: Callable[[str, str, int], bool] | None = None

    def check_identity(self, anchors: pl.DataFrame) -> bool:
        for name in ("graph_version", "derivation_hash", "revocation_epoch"):
            if name in anchors.columns:
                values = anchors[name]
                if values.null_count() or any(v != getattr(self, name) for v in values):
                    raise SnapshotInvalid(f"anchors {name} 身份错配")
        if self.consumable is None:
            raise SnapshotInvalid("缺少绑定身份的撤销校验器")
        try:
            return self.consumable(self.graph_version, self.derivation_hash, self.revocation_epoch) is True
        except TypeError as exc:
            raise SnapshotInvalid("consumable 必须接收 graph_version/derivation_hash/revocation_epoch") from exc

    def cache_allowed(self) -> tuple[bool, str]:
        if "unknown" in (self.market_manifest, self.graph_version, self.derivation_hash):
            return False, "market_manifest/graph_version/derivation_hash 未知，禁缓存（内容身份不可验证）"
        if self.consumable is None:
            return False, "无撤销/tombstone 校验回调，禁缓存"
        return True, ""


def _check_anchors(anchors: pl.DataFrame) -> None:
    for c in ("episode_id", "instrument_id", "t_dec"):
        if c not in anchors.columns:
            raise SnapshotInvalid(f"anchors 缺列 {c}")
    d = anchors.schema["t_dec"]
    if not isinstance(d, pl.Datetime) or d.time_zone != "UTC":
        raise SnapshotInvalid(f"anchors.t_dec 必须为 Datetime(_, 'UTC')，得到 {d}")
    if anchors["episode_id"].n_unique() != anchors.height:
        raise SnapshotInvalid("anchors.episode_id 重复")
    if "is_tombstone" in anchors.columns and bool(anchors["is_tombstone"].fill_null(False).any()):
        raise SnapshotInvalid("anchors 含 is_tombstone=True 的 episode（旧图版本，拒读）")


def bars_digest(bars: pl.DataFrame) -> str:
    """行情内容摘要（同 manifest 不同数据不得共用缓存）：逐行哈希的顺序无关聚合 + 行数 + 列名。"""
    cols = sorted(c for c in bars.columns if c not in ("ingested_at",))
    h = bars.select(cols).hash_rows(seed=7)
    agg = int(h.cast(pl.UInt64).sum()) if bars.height else 0
    return hashlib.sha256(f"{bars.height}|{cols}|{agg}".encode()).hexdigest()[:24]


def snapshot_cache_key(ast: dict, *, backend: str, backend_version: str, interval: str, ctx: SnapshotContext, anchor_hash: str,
                       max_staleness: dt.timedelta | None = None, bars_hash: str = "") -> str:
    rep = lint(ast)
    payload = {
        "bars_digest": bars_hash,
        "canonical_hash": canonical_hash(ast), "canonicalization_version": CANONICALIZATION_VERSION,
        "op_versions": rep.op_versions, "registry_digest": rep.registry_digest, "backend": backend, "backend_version": backend_version,
        "market_manifest": ctx.market_manifest, "interval": interval, "latency_s": ctx.latency.total_seconds(),
        "max_staleness_s": (max_staleness or ctx.max_staleness).total_seconds() if (max_staleness or ctx.max_staleness) else None,
        "cutoff": ctx.cutoff.isoformat() if ctx.cutoff else None, "fold_id": ctx.fold_id,
        "preprocess_state": ctx.preprocess_state, "graph_version": ctx.graph_version, "derivation_hash": ctx.derivation_hash,
        "revocation_epoch": ctx.revocation_epoch, "anchor_hash": anchor_hash,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def anchor_hash(anchors: pl.DataFrame) -> str:
    """有序 anchor 序列摘要（含 episode_id / instrument_id / t_dec 与顺序）。"""
    rows = anchors.select("episode_id", "instrument_id", "t_dec")
    return hashlib.sha256(rows.write_json().encode()).hexdigest()


def _resolve_ctx(ctx: SnapshotContext | None, cutoff, latency, max_staleness, step: dt.timedelta) -> SnapshotContext:
    """唯一有效上下文：便捷参数只能与 ctx 一致或填补 ctx 的默认；冲突 → SnapshotInvalid。"""
    ctx = ctx or SnapshotContext()
    upd = {}
    if cutoff is not None:
        if ctx.cutoff is not None and ctx.cutoff != cutoff:
            raise SnapshotInvalid(f"cutoff 冲突：ctx={ctx.cutoff} 参数={cutoff}")
        upd["cutoff"] = cutoff
    if latency != dt.timedelta(0):
        if ctx.latency != dt.timedelta(0) and ctx.latency != latency:
            raise SnapshotInvalid(f"latency 冲突：ctx={ctx.latency} 参数={latency}")
        upd["latency"] = latency
    if max_staleness is not None:
        if ctx.max_staleness is not None and ctx.max_staleness != max_staleness:
            raise SnapshotInvalid(f"max_staleness 冲突：ctx={ctx.max_staleness} 参数={max_staleness}")
        upd["max_staleness"] = max_staleness
    ctx = replace(ctx, **upd)
    if ctx.max_staleness is None:
        ctx = replace(ctx, max_staleness=step)
    return ctx


def feature_snapshot(
    asts: list[dict], anchors: pl.DataFrame, *, bars: pl.DataFrame, backend: str = "polars",
    cutoff: dt.datetime | None = None, latency: dt.timedelta = dt.timedelta(0), max_staleness: dt.timedelta | None = None,
    ctx: SnapshotContext | None = None, cache: bool = False,
) -> pl.DataFrame:
    _check_anchors(anchors)
    if "interval" not in bars.columns or bars.height == 0:
        raise SnapshotInvalid("bars 缺 interval 列或为空")
    ivs = bars["interval"].unique().to_list()
    if len(ivs) != 1:
        raise SnapshotInvalid(f"一个 snapshot 上下文只允许一个周期，得到 {ivs}")
    step = dt.timedelta(minutes=interval_minutes(ivs[0]))
    ctx = _resolve_ctx(ctx, cutoff, latency, max_staleness, step)
    cutoff, latency, stale = ctx.cutoff, ctx.latency, ctx.max_staleness
    if cache:
        ok, why = ctx.cache_allowed()
        if not ok:
            raise SnapshotInvalid(f"cache=True 但 {why}")
        if not ctx.check_identity(anchors):
            raise SnapshotInvalid("graph 已撤销/tombstone：拒读旧图缓存与计算")
    be = get_backend(backend)
    for ast in asts:
        assert_ast_supported(be, ast)
    vis_bars = bars if cutoff is None else bars.filter(pl.col("close_time") <= pl.lit(cutoff).cast(UTC_US))
    has_avail = "available_at" in bars.columns
    ok = pl.col("t_dec").is_not_null()
    if cutoff is not None:
        ok = ok & (pl.col("t_dec") <= pl.lit(cutoff).cast(UTC_US))
    a = anchors.select("episode_id", "instrument_id", "t_dec").with_row_index("__row").with_columns(ok.alias("__ok"))
    a = a.with_columns((pl.col("t_dec") - latency).alias("__t_query"))
    out = a.select("__row", "episode_id", "t_dec")
    ah = anchor_hash(anchors)
    bh = bars_digest(vis_bars) if cache else ""
    avail_base = vis_bars.select("instrument_id", "close_time", "available_at").sort("instrument_id", "close_time") if has_avail else None
    for ast in asts:
        h = canonical_hash(ast)
        rep = lint(ast)
        avail = None
        if avail_base is not None:
            # 依赖地平线（计划槽位数）：rows 回看 + wallclock 回看换算；EMA 递推无限记忆 → 累计 max
            H = rep.lookback_rows + math.ceil(rep.lookback_minutes / interval_minutes(ivs[0]))
            expr = (pl.col("available_at").cum_max().over("instrument_id") if "EMA" in rep.ops
                    else pl.col("available_at").rolling_max(window_size=H + 1, min_samples=1).over("instrument_id"))
            avail = avail_base.with_columns(expr.alias("__avail_max")).select("instrument_id", pl.col("close_time").alias("__bar_close"), "__avail_max")
        key = snapshot_cache_key(ast, backend=be.name, backend_version=be.version, interval=ivs[0], ctx=ctx, anchor_hash=ah, bars_hash=bh)
        col = _cache_read(key, a) if cache else None
        if col is not None and not ctx.check_identity(anchors):
            raise SnapshotInvalid("热读期间 graph 已撤销：丢弃缓存结果（fail closed）")
        if col is None:
            vals = be.compute(ast, vis_bars).rename({"close_time": "__bar_close"}).sort("__bar_close")
            q = a.filter(pl.col("__ok")).sort("__t_query")
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Sortedness of columns cannot be checked")
                # 严格 staleness：t_query − close_time < max_staleness（默认 = interval ⇒ 最近计划槽位缺失即 invalid）
                j = q.join_asof(vals, left_on="__t_query", right_on="__bar_close", by="instrument_id", strategy="backward",
                                tolerance=stale - dt.timedelta(microseconds=1))
            valid = pl.col("__bar_close").is_not_null() & pl.col("value").is_not_null()
            if avail is not None:
                j = j.join(avail, on=["instrument_id", "__bar_close"], how="left")
                # 窗口内（回看地平线）任一依赖 bar 晚到（available_at > t_dec）→ 整个特征在该 anchor 不可见
                valid = valid & pl.col("__avail_max").is_not_null() & (pl.col("__avail_max") <= pl.col("t_dec"))
            j = j.with_columns(valid.alias("__valid"))
            col = a.select("__row", "episode_id").join(j.select("__row", "value", "__valid"), on="__row", how="left").with_columns(
                pl.col("__valid").fill_null(False)).sort("__row")
            col = col.with_columns(pl.when(pl.col("__valid")).then(pl.col("value")).otherwise(None).alias("value"))
            if cache:
                if not ctx.check_identity(anchors):
                    raise SnapshotInvalid("发布前复查：graph 已撤销，丢弃待发布缓存")
                _cache_write(key, col)
        out = out.with_columns(col["value"].alias(f"f_{h}"), col["__valid"].alias(f"validity_{h}"))
    if cache and not ctx.check_identity(anchors):
        raise SnapshotInvalid("返回前复查：graph 已撤销，丢弃本次快照结果")
    return out.sort("__row").drop("__row")


# ---------------------------------------------------------------- 缓存（内容寻址，路径经 QUANT_LAB_DATA_ROOT）
def cache_dir():
    return paths.data_root() / "research" / "cache"


def _cache_read(key: str, a: pl.DataFrame) -> pl.DataFrame | None:
    """按 episode_id 身份一对一回读；缺行/多行/重复/schema 不符 → miss（不按位置兜底）。"""
    p = cache_dir() / f"{key}.parquet"
    if not p.exists():
        return None
    try:
        df = pl.read_parquet(p)
    except Exception:
        return None            # 损坏即 miss
    if set(df.columns) != {"episode_id", "value", "__valid"} or df["episode_id"].n_unique() != df.height:
        return None
    if df.height != a.height or set(df["episode_id"].to_list()) != set(a["episode_id"].to_list()):
        return None
    j = a.select("__row", "episode_id").join(df, on="episode_id", how="left").sort("__row")
    if j["__valid"].null_count():
        return None
    return j.select("__row", "value", "__valid")


def _cache_write(key: str, col: pl.DataFrame) -> None:
    d = paths.ensure_dir(cache_dir())
    tmp = d / f".{key}.{uuid.uuid4().hex}.tmp"        # 唯一临时文件：同 key 并发写不互相覆盖
    col.select("episode_id", "value", "__valid").write_parquet(tmp)
    tmp.replace(d / f"{key}.parquet")


__all__ = ["SnapshotContext", "SnapshotInvalid", "anchor_hash", "bars_digest", "cache_dir", "feature_snapshot", "snapshot_cache_key"]
