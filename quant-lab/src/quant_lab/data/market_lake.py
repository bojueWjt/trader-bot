"""G1 的 G2 行情湖适配：分区覆盖登记 + 严格 as-of 标记价，不补洞。"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import polars as pl

from quant_lab.market.asof import MarkAt
from quant_lab.market.contract import sha256_canonical
from quant_lab.market.vision import LakePaths, symbol_of

from .market_stub import FrameMarks, InstrumentRegistry, InstrumentRule


class LakeMarket:
    """只缓存查询日附近的 mark 分区；多年行情不整湖装入内存。

    登记有效区间是 klines 分区的 [key_min, key_max + 1m)，不是上市/退市推断。
    分区之间的空档不桥接；分区内缺 bar 由 as-of 陈旧度判定。
    """

    def __init__(self, root: str | Path) -> None:
        self.lake = LakePaths(Path(root).expanduser().resolve())
        self.records = []
        for path in sorted((self.lake.root / "_manifest").glob("*.json")):
            rec = json.loads(path.read_text())
            if rec.get("data_type") in ("klines", "markPriceKlines") and rec.get("interval") == "1m":
                self.records.append(rec)
        self.manifest = "g2-lake-" + sha256_canonical(self.records)
        rules = []
        for rec in self.records:
            if rec.get("data_type") != "klines" or not self._usable(rec):
                continue
            inst = rec["instrument_id"]
            symbol = symbol_of(inst)
            if not symbol.endswith("USDT"):
                continue
            start = datetime.fromisoformat(rec["key_min"]).astimezone(UTC)
            end = datetime.fromisoformat(rec["key_max"]).astimezone(UTC) + timedelta(minutes=1)
            for alias in (symbol[:-4], symbol, inst):
                rules.append(InstrumentRule(alias, inst, start, end))
        self.registry = InstrumentRegistry(rules, version="registry-" + self.manifest)

    @staticmethod
    def _usable(rec: dict) -> bool:
        return (rec.get("status") in ("ok", "gap") and rec.get("check_status", "ok") in ("ok", "gap")
                and bool(rec.get("actual_rows")) and bool(rec.get("key_min")) and bool(rec.get("key_max")))

    @lru_cache(maxsize=32)
    def _day(self, instrument: str, day: date) -> pl.DataFrame:
        parts = []
        for rec in self.records:
            if rec.get("instrument_id") != instrument or rec.get("data_type") != "markPriceKlines" or not self._usable(rec):
                continue
            if not rec["key_min"][:10] <= day.isoformat() <= rec["key_max"][:10]:
                continue
            path = self.lake.silver_dir("markPriceKlines", "1m", symbol_of(instrument)) / f"date={day}" / "part.parquet"
            if not path.exists():
                continue
            df = pl.read_parquet(path)
            required = {"instrument_id", "interval", "open_time", "close_time", "close", "available_at", "source_sha256"}
            if not required.issubset(df.columns) or not rec.get("source_sha256"):
                continue
            valid = ((pl.col("source_sha256") == rec["source_sha256"])
                     & pl.col("close").is_finite() & (pl.col("close") > 0))
            if "ohlc_valid" in df.columns:
                valid = valid & pl.col("ohlc_valid").fill_null(False)
            # 行身份及源版本必须可验证，未知或冲突记录不参与 as-of。
            df = df.filter(valid).filter(~pl.struct(["instrument_id", "open_time"]).is_duplicated())
            parts.append(df.select(sorted(required)))
        return pl.concat(parts).unique() if parts else pl.DataFrame()

    def mark_at(self, instrument_id: str, at: datetime, *, max_staleness_s: int = 120) -> MarkAt:
        if at.tzinfo is None:
            raise ValueError("at 必须含 UTC 时区")
        at = at.astimezone(UTC)
        # 包含容差左端点所在 bar 的开盘日（午夜也必须能读到前一天）。
        day = (at - timedelta(seconds=max_staleness_s + 60)).date()
        frames = []
        while day <= at.date():
            frame = self._day(instrument_id, day)
            if frame.height:
                frames.append(frame)
            day += timedelta(days=1)
        if not frames:
            return MarkAt(None, "MARK_STALE", None, None)
        return FrameMarks(pl.concat(frames), manifest=self.manifest).mark_at(
            instrument_id, at, max_staleness_s=max_staleness_s,
        )
