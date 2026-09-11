"""polars 自写算子后端（契约 §2 首批 18 算子；ADR-G3 §3 v1 语义）。

在计划槽位网格上求值（见 backends/__init__），无效值 = null：
- 逐点算术 null 传播；非有限输出 → null。SafeDiv 分母精确 0 → null；LogPositive x <= 0 → null。
- Gt/Lt 严格比较，相等 false；And 两输入都须有效（严格三值）。
- Ref(lag)：第 lag 个更早槽位的值，缺失槽位 → null；Delta = x − Ref(x, lag)。
- 窗口 n 槽位（rows.count 或 ceil(minutes/interval)）：需 n 个槽位全部存在且有效（strict_required），否则 null；
  wallclock 另要求 n >= min_samples。
- Std ddof=1 显式两遍去均值；Corr 显式去均值和，任一零方差 → null；
  TSRank = (小于数 + (相等数+1)/2) / n ∈ (0, 1]。
- EMA(rows n)：alpha=2/(n+1)，前 n 个连续有效槽位的均值（SMA）为种子，之后递推；遇无效/缺失槽位重置 warm-up。
"""
from __future__ import annotations

import numpy as np
import polars as pl

from quant_lab.research.ast import lint
from quant_lab.research.backends import PRESENT, assert_ast_supported, is_backend_op_quarantined, prepare_partition, to_grid, window_slots
from quant_lab.research.ops import REGISTRY

_IDX = "__idx"


def _ema_sma_seed(x: np.ndarray, n: int) -> np.ndarray:
    alpha = 2.0 / (n + 1.0)
    out = np.full(len(x), np.nan)
    buf: list[float] = []
    state = np.nan
    for i, v in enumerate(x):
        if np.isnan(v):
            state, buf = np.nan, []
            continue
        if np.isnan(state):
            buf.append(v)
            if len(buf) == n:
                state = float(sum(buf) / n)
                out[i] = state
            continue
        state = alpha * v + (1 - alpha) * state
        out[i] = state
    return out


class PolarsBackend:
    name = "polars"
    version = "0.2"

    def supports(self, op: str) -> bool:
        return op in REGISTRY and is_backend_op_quarantined(self.name, op) is None

    # -------------------------------------------------------------- public
    def compute(self, ast: dict, bars: pl.DataFrame) -> pl.DataFrame:
        assert_ast_supported(self, ast)
        rep = lint(ast)
        if "instrument_id" not in bars.columns:
            raise ValueError("bars 缺 instrument_id")
        parts = []
        for (inst,), part in bars.partition_by("instrument_id", as_dict=True).items():
            p = prepare_partition(part, rep.fields)
            s = self.compute_series(ast, p)
            parts.append(pl.DataFrame({"instrument_id": [inst] * p.height, "close_time": p["close_time"], "value": s}))
        if not parts:
            return pl.DataFrame(schema={"instrument_id": pl.Utf8, "close_time": pl.Datetime("us", "UTC"), "value": pl.Float64})
        return pl.concat(parts, how="vertical_relaxed")

    def compute_series(self, ast: dict, part: pl.DataFrame, *, _contract_probe: bool = False) -> pl.Series:
        """单分区（已 prepare、按 close_time 升序）求值，返回与 part 行对齐的 Series。_contract_probe 仅供契约测试框架对隔离算子取证。"""
        if not _contract_probe:
            assert_ast_supported(self, ast)
        rep = lint(ast)
        grid, step = to_grid(part, rep.fields)
        self._step = step
        s = self._eval(ast, grid)
        return s.filter(grid[PRESENT]).alias("value")

    # -------------------------------------------------------------- eval
    def _eval(self, node: dict, g: pl.DataFrame) -> pl.Series:
        if "field" in node:
            return g[node["field"]].alias("value")
        if "const" in node:
            return pl.Series("value", [float(node["const"])] * g.height, dtype=pl.Float64)
        name = node["op"]
        args = [self._eval(a, g) for a in node["args"]]
        out = getattr(self, f"_op_{name}")(g, args, node.get("window"), node.get("params") or {}).alias("value")
        if out.dtype == pl.Float64:
            out = pl.select(pl.when(pl.lit(out).is_infinite()).then(None).otherwise(pl.lit(out))).to_series().fill_nan(None)
        return out

    def _op_Add(self, g, a, w, k):
        return a[0] + a[1]

    def _op_Sub(self, g, a, w, k):
        return a[0] - a[1]

    def _op_Mul(self, g, a, w, k):
        return a[0] * a[1]

    def _op_SafeDiv(self, g, a, w, k):
        return pl.select(pl.when(pl.lit(a[1]) == 0).then(None).otherwise(pl.lit(a[0]) / pl.lit(a[1]))).to_series()

    def _op_Abs(self, g, a, w, k):
        return a[0].abs()

    def _op_LogPositive(self, g, a, w, k):
        return pl.select(pl.when(pl.lit(a[0]) > 0).then(pl.lit(a[0]).log()).otherwise(None)).to_series()

    def _op_Gt(self, g, a, w, k):
        return a[0] > a[1]

    def _op_Lt(self, g, a, w, k):
        return a[0] < a[1]

    def _op_And(self, g, a, w, k):
        x, y = pl.lit(a[0]), pl.lit(a[1])
        return pl.select(pl.when(x.is_null() | y.is_null()).then(None).otherwise(x & y)).to_series()

    def _op_Ref(self, g, a, w, k):
        return a[0].shift(int(k["lag"]))

    def _op_Delta(self, g, a, w, k):
        return a[0] - a[0].shift(int(k["lag"]))

    # ---- 窗口（网格上一律整数槽位 rolling）
    def _rolling(self, g: pl.DataFrame, cols: list[pl.Series], win: dict, aggs: list[pl.Expr], min_samples: int) -> pl.DataFrame:
        n = window_slots(win, self._step)
        names = [f"__a{i}" for i in range(len(cols))]
        df = pl.DataFrame({_IDX: pl.int_range(0, g.height, eager=True)}).with_columns([c.alias(nm) for c, nm in zip(cols, names)])
        df = df.with_columns(pl.any_horizontal([pl.col(nm).is_null() for nm in names]).alias("__bad"))
        out = df.rolling(index_column=_IDX, period=f"{n}i", closed="right").agg(*aggs, pl.len().alias("__n"), pl.col("__bad").any().alias("__anybad"))
        ok = (pl.col("__n") >= n) & ~pl.col("__anybad") & (pl.lit(n) >= min_samples)
        return out.with_columns(ok.alias("__ok"))

    def _win1(self, g, a, win, agg: pl.Expr, min_samples: int) -> pl.Series:
        out = self._rolling(g, [a[0]], win, [agg.alias("__v")], min_samples)
        return out.select(pl.when(pl.col("__ok")).then(pl.col("__v")).otherwise(None)).to_series()

    def _op_Mean(self, g, a, w, k):
        return self._win1(g, a, w, pl.col("__a0").mean(), REGISTRY["Mean"].min_samples)

    def _op_Std(self, g, a, w, k):
        x = pl.col("__a0")
        ss = ((x - x.mean()) ** 2).sum()
        return self._win1(g, a, w, (ss / (pl.len().cast(pl.Float64) - 1.0)).sqrt(), REGISTRY["Std"].min_samples)

    def _op_Min(self, g, a, w, k):
        return self._win1(g, a, w, pl.col("__a0").min(), REGISTRY["Min"].min_samples)

    def _op_Max(self, g, a, w, k):
        return self._win1(g, a, w, pl.col("__a0").max(), REGISTRY["Max"].min_samples)

    def _op_TSRank(self, g, a, w, k):
        x, last = pl.col("__a0"), pl.col("__a0").last()
        rank = ((x < last).sum().cast(pl.Float64) + ((x == last).sum().cast(pl.Float64) + 1.0) / 2.0) / pl.len().cast(pl.Float64)
        return self._win1(g, a, w, rank, REGISTRY["TSRank"].min_samples)

    def _op_Corr(self, g, a, w, k):
        x, y = pl.col("__a0"), pl.col("__a1")
        dx, dy = x - x.mean(), y - y.mean()
        sxx, syy, sxy = (dx ** 2).sum(), (dy ** 2).sum(), (dx * dy).sum()
        corr = pl.when((sxx == 0) | (syy == 0)).then(None).otherwise(sxy / (sxx * syy).sqrt())
        out = self._rolling(g, [a[0], a[1]], w, [corr.alias("__v")], REGISTRY["Corr"].min_samples)
        return out.select(pl.when(pl.col("__ok")).then(pl.col("__v")).otherwise(None)).to_series().fill_nan(None)

    def _op_EMA(self, g, a, w, k):
        x = a[0].cast(pl.Float64).fill_null(float("nan")).to_numpy()
        return pl.Series(_ema_sma_seed(np.asarray(x, dtype=float), int(w["count"]))).fill_nan(None)


__all__ = ["PolarsBackend"]
