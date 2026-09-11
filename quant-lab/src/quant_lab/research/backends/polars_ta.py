"""polars_ta 0.5.17 变体后端（契约 §2 backends；ADR-G3 §4；合并稿 D.1/D.3）。

同一合同、同一计划槽位网格与 strict_required 门（复用 PolarsBackend 的求值骨架），只把算子核换成 polars_ta 的
表达式函数（wq.ts_mean / ts_std_dev / ts_min / ts_max / ts_delay / ts_delta / ts_rank / ts_corr / abs_ / log / divide 等）。
适配要点（ADR §4：行窗、ddof、EMA seed、秩规则必须适配；无法同合同则标 unsupported，不以近似替代）：
- 行窗 d = 槽位数；min_samples=d 令窗内任一 null → null（polars rolling 只数非 null），与 strict_required 等价；
- Std 显式 ddof=1（polars_ta 默认 0）；
- TSRank：polars_ta.ts_rank = rolling_rank / 非 null 计数，并列规则以契约测试实测为准（见 spike 报告）；
- EMA：polars_ta.ta.overlap.EMA = ewm_mean(span, adjust=False, min_samples=span)，种子是首值递推而非 SMA(n)，
  且 null 不重置 warm-up → 与合同不等价，标 **unsupported**（不静默近似）；
- Gt/Lt/And/SafeDiv 的 null 规则由外层统一门控。
- **Add/Mul 不用 wq.add/wq.multiply**：二者是"水平多列相加/相乘、null 按 0/1 填充、全 null 才 null"的语义，
  与合同"任一无效则无效"不等价（spike 实测 reference 对拍失败），显式路由到 polars 原生表达式（NATIVE_OPS 登记，非静默 fallback）。
"""
from __future__ import annotations

import polars as pl

from quant_lab.research.backends import is_backend_op_quarantined, window_slots
from quant_lab.research.backends.polars import PolarsBackend
from quant_lab.research.ops import REGISTRY

try:
    import polars_ta
    from polars_ta import wq
    from polars_ta.ta.overlap import EMA as _TA_EMA  # noqa: F401  仅作存在性证据，不用于合同（见 docstring）
    POLARS_TA_VERSION = polars_ta.__version__
except Exception as e:  # pragma: no cover
    wq = None
    POLARS_TA_VERSION = f"unavailable: {e}"

UNSUPPORTED = frozenset({"EMA"})
#: 无 polars_ta 同合同核、显式用 polars 原生表达式的算子（与 PolarsBackend 同实现）
NATIVE_OPS = frozenset({"Add", "Mul", "Gt", "Lt", "And"})
#: 使用 polars_ta 核的算子
TA_OPS = frozenset({"Sub", "SafeDiv", "Abs", "LogPositive", "Ref", "Delta", "Mean", "Std", "Min", "Max", "TSRank", "Corr"})


class BackendUnsupported(NotImplementedError):
    pass


class PolarsTaBackend(PolarsBackend):
    name = "polars_ta"
    version = f"0.1+polars_ta-{POLARS_TA_VERSION}"

    def supports(self, op: str) -> bool:
        return wq is not None and op in REGISTRY and op not in UNSUPPORTED and is_backend_op_quarantined(self.name, op) is None

    def _ta1(self, a: pl.Series, fn, **kw) -> pl.Series:
        return pl.DataFrame({"x": a}).select(fn(pl.col("x"), **kw).alias("value")).to_series()

    def _ta2(self, a: pl.Series, b: pl.Series, fn, **kw) -> pl.Series:
        return pl.DataFrame({"x": a, "y": b}).select(fn(pl.col("x"), pl.col("y"), **kw).alias("value")).to_series()

    def _op_Sub(self, g, a, w, k):
        return self._ta2(a[0], a[1], wq.subtract)

    def _op_SafeDiv(self, g, a, w, k):
        out = self._ta2(a[0], a[1], wq.divide)
        return pl.select(pl.when(pl.lit(a[1]) == 0).then(None).otherwise(pl.lit(out))).to_series()

    def _op_Abs(self, g, a, w, k):
        return self._ta1(a[0], wq.abs_)

    def _op_LogPositive(self, g, a, w, k):
        out = self._ta1(a[0], wq.log)
        return pl.select(pl.when(pl.lit(a[0]) > 0).then(pl.lit(out)).otherwise(None)).to_series()

    def _op_Ref(self, g, a, w, k):
        return self._ta1(a[0], wq.ts_delay, d=int(k["lag"])) if int(k["lag"]) > 0 else a[0]

    def _op_Delta(self, g, a, w, k):
        return self._ta1(a[0], wq.ts_delta, d=int(k["lag"]))

    def _roll1(self, a, w, fn, **kw):
        d = window_slots(w, self._step)
        return self._ta1(a[0], fn, d=d, min_samples=d, **kw)

    def _op_Mean(self, g, a, w, k):
        return self._roll1(a, w, wq.ts_mean)

    def _op_Std(self, g, a, w, k):
        d = window_slots(w, self._step)
        if d < REGISTRY["Std"].min_samples:
            return pl.Series([None] * g.height, dtype=pl.Float64)
        return self._ta1(a[0], wq.ts_std_dev, d=d, ddof=1, min_samples=d)

    def _op_Min(self, g, a, w, k):
        return self._roll1(a, w, wq.ts_min)

    def _op_Max(self, g, a, w, k):
        return self._roll1(a, w, wq.ts_max)

    def _op_TSRank(self, g, a, w, k):
        return self._roll1(a, w, wq.ts_rank)

    def _op_Corr(self, g, a, w, k):
        d = window_slots(w, self._step)
        if d < REGISTRY["Corr"].min_samples:
            return pl.Series([None] * g.height, dtype=pl.Float64)
        return self._ta2(a[0], a[1], wq.ts_corr, d=d, ddof=1, min_samples=d).fill_nan(None)

    def _op_EMA(self, g, a, w, k):
        raise BackendUnsupported("polars_ta EMA（ewm_mean 首值种子、null 不重置）与合同 SMA 种子/重置不等价，标 unsupported")


__all__ = ["NATIVE_OPS", "POLARS_TA_VERSION", "TA_OPS", "UNSUPPORTED", "BackendUnsupported", "PolarsTaBackend"]
