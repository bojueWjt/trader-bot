"""层 5 行情校验的数据提供者与品种登记（G2 as-of 未就绪/无真实行情时的桩；接口与 G2 契约对齐）。

- `MarkProvider` 协议：`mark_at(instrument_id, at, *, max_staleness_s) -> MarkAt`（同 quant_lab.market.asof.MarkAt）。
- `FrameMarks`：包一张 silver 1m markPriceKlines 表，直接调用 G2 的 `quant_lab.market.asof.mark_bar_at`（真实接缝）。
- `SyntheticMarks`：按品种锚点分段线性生成 1m 标记价（合成；带覆盖窗与缺口，用于 MARK_STALE / 未成交 / 数量级门测试）。
- `InstrumentRegistry`：symbol_raw → instrument_id 的**当时市场身份**（effective_from/to），格式 `BTCUSDT-PERP.BINANCE-UM`（契约 §9.2）。
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import polars as pl

from quant_lab.market.asof import MarkAt, mark_bar_at

VENUE_SUFFIX = "-PERP.BINANCE-UM"


def instrument_id_for(symbol: str) -> str:
    return f"{symbol}USDT{VENUE_SUFFIX}"


class MarkProvider(Protocol):
    manifest: str

    def mark_at(self, instrument_id: str, at: datetime, *, max_staleness_s: int = 120) -> MarkAt: ...


class FrameMarks:
    """真实接缝：G2 silver 1m markPriceKlines 表（列按 report-G2 §1.1）→ asof.mark_bar_at。"""

    def __init__(self, marks: pl.DataFrame, *, manifest: str = "frame", latency: timedelta = timedelta(0)) -> None:
        self.marks = marks
        self.manifest = manifest
        self.latency = latency

    def mark_at(self, instrument_id: str, at: datetime, *, max_staleness_s: int = 120) -> MarkAt:
        at = at.astimezone(UTC)
        visible = self.marks
        if "available_at" not in visible.columns:
            return MarkAt(None, "MARK_STALE", None, None)  # 无可知时刻证据的行情表整体不可用（S04）
        # 已闭合 ≠ 当时已知：记录的 available_at（含最终修订晚到）必须 ≤ at
        visible = visible.filter(pl.col("available_at").is_not_null() & (pl.col("available_at") < pl.lit(at)))  # 可知时刻未知 = 不可知
        return mark_bar_at(visible, at, instrument_id, max_staleness_s=max_staleness_s, latency=self.latency)


@dataclass(frozen=True)
class PlausibilityCalibration:
    """合理性带 T_plaus 的冻结产物：阈值按 (channel_id, order_kind)，带版本、冻结时刻、样本量。frozen_at > t_a 的阈值不可用（未来校准）。"""

    thresholds: dict[tuple[int, str], float]
    version: str
    frozen_at: datetime
    n_samples: dict[tuple[int, str], int] = field(default_factory=dict)
    min_samples: int = 50

    def lookup(self, channel_id: int, order_kind: str, at: datetime | None) -> tuple[float | None, str]:
        if at is None or not self.version:
            return None, "calibration_evidence_unknown"
        if self.frozen_at >= at:
            return None, "calibration_not_yet_frozen"
        for key in ((channel_id, order_kind), (0, order_kind)):
            if key in self.thresholds:
                n = self.n_samples.get(key)
                if n is None:
                    continue  # 无样本量记录的阈值不可用（不能当有效校准）
                if n < self.min_samples:
                    continue  # 样本不足退到合并分布
                if not math.isfinite(self.thresholds[key]) or self.thresholds[key] <= 0:
                    continue
                return self.thresholds[key], "ok" if key[0] != 0 else "pooled"
        return None, "no_frozen_T_plaus_with_sample_count"


@dataclass
class SyntheticMarks:
    """锚点 {instrument_id: [(datetime, price), ...]} 分段线性；coverage {instrument_id: (start, end)}；gaps 列表 (instrument_id, start, end)。"""

    anchors: dict[str, list[tuple[datetime, float]]]
    coverage: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)
    gaps: list[tuple[str, datetime, datetime]] = field(default_factory=list)
    manifest: str = "synthetic-marks-v1"

    def _price(self, instrument_id: str, t: datetime) -> float | None:
        """阶梯函数：只用 t 之前（含）的最后一个锚点，绝不用未来锚点插值（review S04）。上市前无价。"""
        pts = self.anchors.get(instrument_id)
        if not pts:
            return None
        ts = [p[0] for p in pts]
        i = bisect.bisect_right(ts, t)
        if i == 0:
            return None
        return pts[i - 1][1]

    def _covered(self, instrument_id: str, t: datetime) -> bool:
        cov = self.coverage.get(instrument_id)
        if cov and not (cov[0] <= t <= cov[1]):
            return False
        return not any(i == instrument_id and a <= t <= b for i, a, b in self.gaps)

    def mark_at(self, instrument_id: str, at: datetime, *, max_staleness_s: int = 120) -> MarkAt:
        at = at.astimezone(UTC)
        close_time = at.replace(second=0, microsecond=0)  # 最后一根已闭合 1m bar 的右端点
        # 缺口/未覆盖：往前找最近可用闭合 bar，超过陈旧容差即 MARK_STALE
        probe = close_time
        for _ in range(0, 24 * 60):
            if self._covered(instrument_id, probe) and self._price(instrument_id, probe) is not None:
                stale = (at - probe).total_seconds()
                if stale > max_staleness_s:
                    return MarkAt(None, "MARK_STALE", probe, stale)
                return MarkAt(Decimal(str(round(self._price(instrument_id, probe), 8))), None, probe, stale)
            probe -= timedelta(minutes=1)
        return MarkAt(None, "MARK_STALE", None, None)

    def bars(self, instrument_id: str, start: datetime, end: datetime) -> pl.DataFrame:
        """物化 1m bars（report-G2 §1.1 列），供 G2 asof / 未成交测试。"""
        rows = []
        t = start.replace(second=0, microsecond=0)
        while t < end:
            ct = t + timedelta(minutes=1)
            if self._covered(instrument_id, ct):
                p = self._price(instrument_id, ct)
                rows.append({"instrument_id": instrument_id, "interval": "1m", "open_time": t, "close_time": ct, "open": p, "high": p * 1.0005, "low": p * 0.9995, "close": p,
                             "volume": 0.0, "event_time": ct, "available_at": ct, "ingested_at": ct})
            t = ct
        return pl.DataFrame(rows, schema={"instrument_id": pl.String, "interval": pl.String, "open_time": pl.Datetime("us", "UTC"), "close_time": pl.Datetime("us", "UTC"), "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC")})


def _d(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


#: 合成夹具用锚点（虚构价格；与 tdesktop_sample 的信号价位配套：反例 2 的 ETH 限价 2900 远低于市价）
FIXTURE_ANCHORS: dict[str, list[tuple[datetime, float]]] = {
    instrument_id_for("BTC"): [(_d("2024-01-01"), 42000), (_d("2024-03-04"), 65500), (_d("2024-03-08"), 65200), (_d("2024-04-02"), 62300), (_d("2024-05-06"), 63500),
                               (_d("2024-06-10"), 60100), (_d("2024-06-11T15:00"), 58600), (_d("2024-07-01"), 62500), (_d("2025-01-06"), 94200), (_d("2025-02-01"), 97000)],
    instrument_id_for("ETH"): [(_d("2024-01-01"), 2300), (_d("2024-03-04"), 3300), (_d("2024-04-02"), 3400), (_d("2024-05-06"), 3100), (_d("2024-05-07"), 3120), (_d("2025-01-06"), 3060), (_d("2025-02-01"), 3200)],
    instrument_id_for("SOL"): [(_d("2024-01-01"), 100), (_d("2024-05-20"), 151), (_d("2024-08-15"), 141), (_d("2025-01-06"), 186), (_d("2025-02-01"), 200)],
    instrument_id_for("ZRO"): [(_d("2024-06-20"), 3.0), (_d("2025-02-01"), 4.0)],
    instrument_id_for("FIL"): [(_d("2024-01-01"), 6.0), (_d("2024-03-09"), 6.3), (_d("2025-02-01"), 5.0)],
    instrument_id_for("ETHFI"): [(_d("2024-03-18"), 3.6), (_d("2024-05-02"), 3.6), (_d("2025-02-01"), 2.0)],
    instrument_id_for("TIA"): [(_d("2024-01-01"), 15.0), (_d("2025-02-01"), 5.0)],
    instrument_id_for("GOOGL"): [(_d("2024-01-01"), 300.0), (_d("2025-01-21"), 341.5), (_d("2025-02-01"), 345.0)],
}
#: 覆盖窗（上市前无数据）+ 一个人工缺口（BTC 2024-06-12 08:00–10:00，用于 MARK_STALE 测试）
FIXTURE_COVERAGE = {instrument_id_for("ZRO"): (_d("2024-06-20"), _d("2026-12-31")), instrument_id_for("ETHFI"): (_d("2024-03-18"), _d("2026-12-31"))}
FIXTURE_GAPS = [(instrument_id_for("BTC"), _d("2024-06-12T08:00"), _d("2024-06-12T10:00"))]


def fixture_marks() -> SyntheticMarks:
    return SyntheticMarks(FIXTURE_ANCHORS, coverage=dict(FIXTURE_COVERAGE), gaps=list(FIXTURE_GAPS))


@dataclass(frozen=True)
class InstrumentRule:
    symbol: str
    instrument_id: str
    effective_from: datetime
    effective_to: datetime | None
    tick_size: float | None = None
    step_size: float | None = None


class InstrumentRegistry:
    """symbol_raw → 当时身份。同一 symbol 可有多段（改名/下线再上线）；查询按 `at` 落入的有效段。"""

    def __init__(self, rules: list[InstrumentRule], *, version: str = "registry-synthetic-v1") -> None:
        self.rules = rules
        self.version = version
        self._by_symbol: dict[str, list[InstrumentRule]] = {}
        for r in rules:
            self._by_symbol.setdefault(r.symbol.upper(), []).append(r)

    def resolve(self, symbol_raw: str | None, at: datetime | None) -> tuple[str | None, str]:
        """返回 (instrument_id, status)，status ∈ {mapped, unknown_symbol, invalid_time, time_unknown}。"""
        if not symbol_raw:
            return None, "unknown_symbol"
        segs = self._by_symbol.get(symbol_raw.upper())
        if not segs:
            return None, "unknown_symbol"
        if at is None:
            return None, "time_unknown"
        for r in segs:
            if r.effective_from <= at and (r.effective_to is None or at < r.effective_to):
                return r.instrument_id, "mapped"
        return None, "invalid_time"

    def tick_size(self, instrument_id: str) -> float | None:
        for r in self.rules:
            if r.instrument_id == instrument_id:
                return r.tick_size
        return None


def fixture_registry() -> InstrumentRegistry:
    """合成登记：含上市日（ZRO 2024-06-20、ETHFI 2024-03-18）与改名（MATIC→POL 2024-09-13）。真实登记来自 G2 instrument_rules。"""
    far = None
    rules = [
        InstrumentRule("BTC", instrument_id_for("BTC"), _d("2019-09-08"), far, 0.1, 0.001),
        InstrumentRule("ETH", instrument_id_for("ETH"), _d("2019-11-27"), far, 0.01, 0.001),
        InstrumentRule("SOL", instrument_id_for("SOL"), _d("2020-09-14"), far, 0.001, 1),
        InstrumentRule("ZRO", instrument_id_for("ZRO"), _d("2024-06-20"), far, 0.0001, 0.1),
        InstrumentRule("FIL", instrument_id_for("FIL"), _d("2020-10-15"), far, 0.001, 0.1),
        InstrumentRule("ETHFI", instrument_id_for("ETHFI"), _d("2024-03-18"), far, 0.0001, 1),
        InstrumentRule("TIA", instrument_id_for("TIA"), _d("2023-10-31"), far, 0.0001, 0.1),
        InstrumentRule("GOOGL", instrument_id_for("GOOGL"), _d("2025-01-01"), far, 0.01, 0.01),
        InstrumentRule("MU", instrument_id_for("MU"), _d("2025-01-01"), far, 0.01, 0.01),
        InstrumentRule("CL", instrument_id_for("CL"), _d("2025-01-01"), far, 0.01, 0.01),
        InstrumentRule("MATIC", instrument_id_for("MATIC"), _d("2020-10-22"), _d("2024-09-13"), 0.0001, 1),
        InstrumentRule("MATIC", instrument_id_for("POL"), _d("2024-09-13"), far, 0.0001, 1),
        InstrumentRule("POL", instrument_id_for("POL"), _d("2024-09-13"), far, 0.0001, 1),
    ]
    return InstrumentRegistry(rules)


def log_deviation(p: float | None, m: float | None) -> float | None:
    if p is None or m is None or p <= 0 or m <= 0:
        return None
    return abs(math.log(p / m))
