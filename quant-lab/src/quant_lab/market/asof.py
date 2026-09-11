"""quant_lab.market.asof —— 三时钟 as-of 库（契约 §2 + G2 修订 report-G2-contract-revision-M01 §2，M-05）。

三时钟：event_time（bar 区间右端点）/ available_at（可证明可知时刻；历史 bar 用 H0 = close_time + latency）/ ingested_at。
决策只能用 available_at < t_dec 的记录；**等号只在有顺序证据（sequence）时可用**（合并稿 C.1）。
不前填：超过 tolerance 的匹配置 null 并给 reason=MARK_STALE；无更早记录 reason=NO_PRIOR。
"""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal, NamedTuple

import polars as pl

REASON_MARK_STALE = "MARK_STALE"
REASON_NO_PRIOR = "NO_PRIOR"
MATCHED_AT = "asof_matched_at"
REASON_COL = "asof_reason"
_ROW = "__asof_row"
_KEY = "__asof_key"


class AsOfKeyDuplicate(ValueError):
    """右表 (by, right_on[, seq]) 非唯一：as-of 结果依赖未文档化顺序，拒绝（选型报告 Q3）。"""


class TimeUnitInvalid(ValueError):
    """时间列不是 tz=UTC 的 Datetime：原因码 TIME_UNIT_INVALID。"""


def _utc_us(df: pl.DataFrame, col: str) -> pl.DataFrame:
    t = df.schema[col]
    if not isinstance(t, pl.Datetime) or t.time_zone != "UTC":
        raise TimeUnitInvalid(f"列 {col} 必须为 Datetime(_, 'UTC')，实际 {t}（规范：非 UTC 抛 TIME_UNIT_INVALID，不自动转换）")
    return df.with_columns(pl.col(col).cast(pl.Datetime("us", "UTC")))


def _check_at(at: dt.datetime) -> dt.datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise TimeUnitInvalid("at 必须是 tz-aware（UTC）")
    return at.astimezone(dt.UTC)


def asof_join(
    left: pl.DataFrame,
    right: pl.DataFrame,
    *,
    left_on: str = "t_dec",
    right_on: str = "available_at",
    by: list[str] | None = None,
    strategy: Literal["strict_lt", "le_with_sequence"] = "strict_lt",
    tolerance: dt.timedelta | None = None,
    sequence_cols: tuple[str, str] | None = None,
    suffix: str = "_r",
) -> pl.DataFrame:
    """左连接 as-of：每个左行取右表中最后一条"可知"记录。

    strict_lt        : right_on <  left_on
    le_with_sequence : right_on <  left_on 或 (right_on == left_on 且 right_seq < left_seq)；需 sequence_cols
    输出 = 左表全部行（原顺序）+ 右表非键列（与左表同名者加 suffix）+ `asof_matched_at` + `asof_reason`
    （null=匹配；NO_PRIOR=无更早记录；MARK_STALE=超过 tolerance，右侧列置 null，不前填）。
    """
    by = list(by or [])
    if strategy not in ("strict_lt", "le_with_sequence"):
        raise ValueError(f"未知 strategy {strategy}")
    if strategy == "le_with_sequence" and not sequence_cols:
        raise ValueError("le_with_sequence 需要 sequence_cols=(left_seq, right_seq)")
    left = _utc_us(left, left_on)
    right = _utc_us(right, right_on)
    lseq, rseq = sequence_cols if sequence_cols else (None, None)
    if rseq is not None:
        # S09：右序号列与左表撞名（含同名 sequence）时先改名，等号候选过滤必须比较 右seq < 左seq
        r_alias = "__asof_rseq"
        right = right.rename({rseq: r_alias})
        rseq = r_alias
    for reserved in (MATCHED_AT, REASON_COL, _ROW):
        if reserved in left.columns or reserved in right.columns:
            raise ValueError(f"保留列名 {reserved} 不得出现在输入")

    # 右表唯一键先验收
    ukey = by + [right_on] + ([rseq] if rseq else [])
    if right.select(ukey).is_duplicated().any():
        dups = right.filter(right.select(ukey).is_duplicated()).select(ukey).head(3).to_dicts()
        raise AsOfKeyDuplicate(f"右表 {ukey} 非唯一，示例 {dups}")

    # 右表列重命名：非键列与左表撞名加后缀；right_on 统一暴露为 asof_matched_at
    value_cols = [c for c in right.columns if c not in by and c != right_on and c != rseq]
    rename = {c: (c + suffix if c in left.columns else c) for c in value_cols}
    r = right.rename(rename).with_columns(pl.col(right_on).alias(MATCHED_AT))
    r_sort = by + [right_on] + ([rseq] if rseq else [])
    r = r.sort(r_sort)
    out_value_cols = list(rename.values())

    l = left.with_row_index(_ROW).sort(by + [left_on])
    r_strict = r.drop(right_on).drop(rseq) if rseq else r.drop(right_on)
    r_strict_by = by if by else None
    joined = l.join_asof(
        r_strict, left_on=left_on, right_on=MATCHED_AT, by=r_strict_by,
        strategy="backward", allow_exact_matches=False, coalesce=False, suffix="__dup", check_sortedness=False,
    )
    # join_asof(coalesce=False) 保留右侧 MATCHED_AT 为原名（无 by 撞名时）；统一列名
    if MATCHED_AT + "__dup" in joined.columns:
        joined = joined.drop(MATCHED_AT).rename({MATCHED_AT + "__dup": MATCHED_AT})

    if strategy == "le_with_sequence":
        # 等号候选：right_on == left_on 且 right_seq < left_seq，取 right_seq 最大者
        eq = (
            l.select([_ROW, left_on, lseq] + by)
            .join(r.select(by + [right_on, rseq, MATCHED_AT] + out_value_cols),
                  left_on=by + [left_on], right_on=by + [right_on], how="inner")
            .filter(pl.col(rseq) < pl.col(lseq))
            .sort([_ROW, rseq])
            .group_by(_ROW, maintain_order=True).last()
            .select([_ROW, MATCHED_AT] + out_value_cols)
        )
        if eq.height:
            # S09：等号候选行存在则整行采用（保留其 null），不按单元格回退到 strict 旧行
            eq = eq.rename({c: c + "__eq" for c in [MATCHED_AT] + out_value_cols}).with_columns(pl.lit(True).alias("__eq_hit"))
            joined = joined.join(eq, on=_ROW, how="left")
            hit = pl.col("__eq_hit").fill_null(False)
            for c in [MATCHED_AT] + out_value_cols:
                joined = joined.with_columns(pl.when(hit).then(pl.col(c + "__eq")).otherwise(pl.col(c)).alias(c)).drop(c + "__eq")
            joined = joined.drop("__eq_hit")

    age = pl.col(left_on) - pl.col(MATCHED_AT)
    reason = (
        pl.when(pl.col(MATCHED_AT).is_null()).then(pl.lit(REASON_NO_PRIOR))
        .when(pl.lit(tolerance is not None) & (age > pl.lit(tolerance if tolerance is not None else dt.timedelta(0))))
        .then(pl.lit(REASON_MARK_STALE))
        .otherwise(pl.lit(None, dtype=pl.Utf8))
    )
    joined = joined.with_columns(reason.alias(REASON_COL))
    stale = pl.col(REASON_COL) == REASON_MARK_STALE
    joined = joined.with_columns(
        [pl.when(stale).then(pl.lit(None)).otherwise(pl.col(c)).alias(c) for c in out_value_cols]
    )
    return joined.sort(_ROW).drop(_ROW)


def last_closed_bar(
    bars: pl.DataFrame, *, at: dt.datetime, instrument_id: str, interval: str,
    latency: dt.timedelta = dt.timedelta(0),
) -> pl.DataFrame | None:
    """只取 close_time + latency <= at 的最后一根（等号成立：H0 收盘整点即闭合；latency>0 为模型假设）。"""
    at = _check_at(at)
    bars = _utc_us(bars, "close_time")
    cond = (pl.col("instrument_id") == instrument_id) & (pl.col("close_time") + latency <= pl.lit(at))
    if "interval" in bars.columns:
        cond = cond & (pl.col("interval") == interval)
    vis = bars.filter(cond)
    if vis.height == 0:
        return None
    return vis.sort("close_time").tail(1)


class MarkAt(NamedTuple):
    price: Decimal | None
    reason: str | None
    close_time: dt.datetime | None
    staleness_s: float | None


def quantize_price(price: Decimal, tick_size: Decimal | None) -> Decimal:
    if tick_size is None or tick_size <= 0:
        return price
    return (price / tick_size).quantize(Decimal(1), rounding=ROUND_HALF_EVEN) * tick_size


def mark_bar_at(
    marks: pl.DataFrame, at: dt.datetime, instrument_id: str, *,
    max_staleness_s: int = 120, tick_size: Decimal | None = None, latency: dt.timedelta = dt.timedelta(0),
) -> MarkAt:
    """as-of 标记价：最后一根已闭合 1m markPrice bar 的 close；陈旧 > max_staleness_s 或不存在 → MARK_STALE。"""
    at = _check_at(at)
    row = last_closed_bar(marks, at=at, instrument_id=instrument_id, interval="1m", latency=latency)
    if row is None:
        return MarkAt(None, REASON_MARK_STALE, None, None)
    ct: dt.datetime = row["close_time"][0]
    stale = (at - ct).total_seconds()
    if stale > max_staleness_s:
        return MarkAt(None, REASON_MARK_STALE, ct, stale)
    price = quantize_price(Decimal(str(row["close"][0])), tick_size)
    return MarkAt(price, None, ct, stale)


def mark_price_at(
    marks: pl.DataFrame, at: dt.datetime, instrument_id: str, *, max_staleness_s: int = 120,
    tick_size: Decimal | None = None,
) -> tuple[Decimal | None, str | None]:
    """契约 §2 二元组形式：(价格, reason_code)。"""
    m = mark_bar_at(marks, at, instrument_id, max_staleness_s=max_staleness_s, tick_size=tick_size)
    return m.price, m.reason


__all__ = [
    "asof_join", "last_closed_bar", "mark_bar_at", "mark_price_at", "MarkAt", "quantize_price",
    "AsOfKeyDuplicate", "TimeUnitInvalid", "REASON_MARK_STALE", "REASON_NO_PRIOR", "MATCHED_AT", "REASON_COL",
]
