"""两后端抽象：polars（自写表达式）与 polars_ta（0.5.17 变体）。ADR-G3 §3–§4。

统一接口 `Backend.compute(ast, bars) -> pl.DataFrame[instrument_id, close_time, value]`：
- bars 须含 instrument_id、interval、close_time(UTC) 与 AST 用到的字段；按 instrument_id 分区独立计算（单品种窗口隔离）；
- **计划槽位**：分区内按 interval 建规则网格（min close_time 起，步长 = interval），bar 必须落在槽位上，缺 bar = 缺失槽位；
  所有窗口 strict_required：所需槽位缺失或任一必需值无效即 invalid；Ref(lag) 不跨缺失槽位找更早有效值（ADR §3）；
- close_time 重复 → BarsInvalid；不在槽位上 → BarsInvalid；interval 不唯一 → BarsInvalid；
- 无效值统一用 null 表示（契约文中的 NaN）；NaN/±Inf 一律转 null；
- lookahead=0 由构造保证：窗口只看 (t − W, t]；wallclock W 分钟 = ceil(W / interval) 个槽位。
切换：get_backend(name)；运行中不静默 fallback。
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Protocol

import polars as pl

BACKENDS = ("polars", "polars_ta")
#: 后端 × 算子 隔离登记（契约测试失败即登记，fail closed；源码常量 = 跨进程持久）。解禁须全套契约重验并改此表。
#: polars_ta Corr：rolling_corr 流式核在 1e12 量级违反 rtol=1e-8（report-G3-backend-spike §2）。
BACKEND_QUARANTINE: dict[tuple[str, str], str] = {
    ("polars_ta", "Corr"): "contract reference FAIL: rolling_corr 流式核 1e12 量级抵消误差（spike 2026-09-11）",
}
_RUNTIME_QUARANTINE: dict[tuple[str, str], str] = {}


class BackendOpQuarantined(RuntimeError):
    """被隔离的 (backend, op)：feature_snapshot / compute 一律拒绝依赖它的 AST。"""


def quarantine_backend_op(backend: str, op: str, reason: str) -> None:
    _RUNTIME_QUARANTINE[(backend, op)] = reason


def is_backend_op_quarantined(backend: str, op: str) -> str | None:
    return BACKEND_QUARANTINE.get((backend, op)) or _RUNTIME_QUARANTINE.get((backend, op))


def assert_ast_supported(backend, ast: dict) -> None:
    from quant_lab.research.ast import lint
    for op in lint(ast).ops:
        why = is_backend_op_quarantined(backend.name, op)
        if why is not None:
            raise BackendOpQuarantined(f"{backend.name}.{op} 已隔离：{why}")
        if not backend.supports(op):
            raise BackendOpQuarantined(f"{backend.name} 不支持 {op}（unsupported）")
_INTERVAL_RE = re.compile(r"^(\d+)([mhd])$")
_UNIT_MIN = {"m": 1, "h": 60, "d": 1440}
PRESENT = "__present"


class BarsInvalid(ValueError):
    """bars 不满足前置：缺列、close_time 非 UTC、分区内 close_time 重复/不在槽位、interval 不唯一。"""


class Backend(Protocol):
    name: str
    version: str

    def supports(self, op: str) -> bool: ...
    def compute(self, ast: dict, bars: pl.DataFrame) -> pl.DataFrame: ...
    def compute_series(self, ast: dict, part: pl.DataFrame) -> pl.Series: ...


def get_backend(name: str = "polars") -> Backend:
    if name == "polars":
        from quant_lab.research.backends.polars import PolarsBackend
        return PolarsBackend()
    if name == "polars_ta":
        from quant_lab.research.backends.polars_ta import PolarsTaBackend
        return PolarsTaBackend()
    raise ValueError(f"未知后端 {name!r}，可选 {BACKENDS}")


def interval_minutes(interval: str) -> int:
    m = _INTERVAL_RE.match(str(interval))
    if not m:
        raise BarsInvalid(f"无法解析 interval {interval!r}")
    return int(m.group(1)) * _UNIT_MIN[m.group(2)]


def window_slots(win: dict, interval_min: int) -> int:
    """窗口对应的槽位数：rows.count；wallclock.minutes → ceil(W / interval)（(t−W, t] 内的槽位数）。"""
    if win["unit"] == "rows":
        return int(win["count"])
    return max(1, math.ceil(int(win["minutes"]) / interval_min))


def prepare_partition(bars: pl.DataFrame, fields: tuple[str, ...]) -> pl.DataFrame:
    """单品种分区前置：校验 + 排序 + 字段转 Float64（NaN/Inf → null）。返回按 close_time 升序的实有行。"""
    missing = [c for c in ("close_time", "interval", *fields) if c not in bars.columns]
    if missing:
        raise BarsInvalid(f"缺列 {missing}")
    d = bars.schema["close_time"]
    if not isinstance(d, pl.Datetime) or d.time_zone != "UTC":
        raise BarsInvalid(f"close_time 必须为 Datetime(_, 'UTC')，得到 {d}")
    ivs = bars["interval"].unique().to_list()
    if len(ivs) != 1:
        raise BarsInvalid(f"分区内 interval 不唯一: {ivs}")
    step = interval_minutes(ivs[0])
    out = bars.sort("close_time")
    if out["close_time"].n_unique() != out.height:
        raise BarsInvalid("分区内 close_time 重复")
    if out.height:
        off = ((out["close_time"] - out["close_time"][0]).dt.total_seconds() % (step * 60)) != 0
        if off.any():
            raise BarsInvalid(f"{int(off.sum())} 行 close_time 不在 {ivs[0]} 计划槽位上")
    return out.with_columns([pl.col(f).cast(pl.Float64).fill_nan(None) for f in fields]).with_columns(
        [pl.when(pl.col(f).is_infinite()).then(None).otherwise(pl.col(f)).alias(f) for f in fields])


def to_grid(part: pl.DataFrame, fields: tuple[str, ...]) -> tuple[pl.DataFrame, int]:
    """实有行 → 计划槽位网格（缺失槽位字段为 null，PRESENT=False）。返回 (grid, interval_min)。"""
    step = interval_minutes(part["interval"][0]) if part.height else 1
    if part.height == 0:
        return part.with_columns(pl.lit(True).alias(PRESENT)), step
    t0, t1 = part["close_time"][0], part["close_time"][-1]
    grid = pl.DataFrame({"close_time": pl.datetime_range(t0, t1, interval=dt.timedelta(minutes=step), eager=True, time_unit="us", time_zone="UTC")})
    g = grid.join(part.select("close_time", *fields).with_columns(pl.lit(True).alias(PRESENT)), on="close_time", how="left")
    return g.with_columns(pl.col(PRESENT).fill_null(False)), step


__all__ = ["BACKENDS", "BACKEND_QUARANTINE", "PRESENT", "Backend", "BackendOpQuarantined", "BarsInvalid", "assert_ast_supported", "get_backend", "interval_minutes", "is_backend_op_quarantined", "prepare_partition", "quarantine_backend_op", "to_grid", "window_slots"]
