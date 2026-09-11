"""手算 reference（纯 Python 循环，独立于 polars 表达式），契约测试对拍用（契约 §2、ADR §3.1）。

只用 list / math / datetime；自行按 interval 建计划槽位网格，逐槽位按定义计算，最后映射回实有行。
None 表示无效值。语义定义见 backends/polars.py docstring 与 ops.REGISTRY。
"""
from __future__ import annotations

import datetime as dt
import math

from quant_lab.research.ops import REGISTRY


def _num(v):
    if v is None:
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _fin(v):
    return v if v is None or isinstance(v, bool) or math.isfinite(v) else None


def evaluate(node: dict, cols: dict[str, list], times: list[dt.datetime], interval_min: int) -> list:
    """cols: 字段 → 值列表（按 close_time 升序，与 times 对齐）。返回与 times 对齐的列表。"""
    if not times:
        return []
    step = dt.timedelta(minutes=interval_min)
    n_slots = int((times[-1] - times[0]) / step) + 1
    pos = [int((t - times[0]) / step) for t in times]
    grid_cols = {}
    for f, vals in cols.items():
        g = [None] * n_slots
        for p, v in zip(pos, vals):
            g[p] = _num(v)
        grid_cols[f] = g
    out = _eval(node, grid_cols, n_slots, interval_min)
    return [out[p] for p in pos]


def _eval(node, cols, n, step_min):
    if "field" in node:
        return list(cols[node["field"]])
    if "const" in node:
        return [float(node["const"])] * n
    name = node["op"]
    args = [_eval(a, cols, n, step_min) for a in node["args"]]
    out = _OPS[name](args, node.get("window"), node.get("params") or {}, step_min)
    return [_fin(v) for v in out]


def _bin(a, b, f):
    return [None if x is None or y is None else f(x, y) for x, y in zip(a, b)]


def _slots(win, step_min):
    if win["unit"] == "rows":
        return int(win["count"])
    return max(1, math.ceil(int(win["minutes"]) / step_min))


def _window_op(name, fn):
    spec = REGISTRY[name]

    def op(args, win, params, step_min):
        k = _slots(win, step_min)
        out = []
        for i in range(len(args[0])):
            if i - k + 1 < 0 or k < spec.min_samples:
                out.append(None)
                continue
            vals = [c[i - k + 1:i + 1] for c in args]
            if any(v is None for col in vals for v in col):
                out.append(None)
                continue
            out.append(fn(*vals))
        return out
    return op


def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs):
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _tsrank(xs):
    last = xs[-1]
    less = sum(1 for x in xs if x < last)
    eq = sum(1 for x in xs if x == last)
    return (less + (eq + 1) / 2) / len(xs)


def _corr(xs, ys):
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def _shift(a, k):
    return [None] * min(k, len(a)) + a[: max(0, len(a) - k)]


def _ema(args, win, params, step_min):
    n = int(win["count"])
    alpha = 2.0 / (n + 1.0)
    out, state, buf = [], None, []
    for v in args[0]:
        if v is None:
            state, buf = None, []
            out.append(None)
            continue
        if state is None:
            buf.append(v)
            if len(buf) == n:
                state = sum(buf) / n
                out.append(state)
            else:
                out.append(None)
            continue
        state = alpha * v + (1 - alpha) * state
        out.append(state)
    return out


_OPS = {
    "Add": lambda a, w, k, s: _bin(a[0], a[1], lambda x, y: x + y),
    "Sub": lambda a, w, k, s: _bin(a[0], a[1], lambda x, y: x - y),
    "Mul": lambda a, w, k, s: _bin(a[0], a[1], lambda x, y: x * y),
    "SafeDiv": lambda a, w, k, s: _bin(a[0], a[1], lambda x, y: None if y == 0 else x / y),
    "Abs": lambda a, w, k, s: [None if x is None else abs(x) for x in a[0]],
    "LogPositive": lambda a, w, k, s: [None if x is None or x <= 0 else math.log(x) for x in a[0]],
    "Gt": lambda a, w, k, s: _bin(a[0], a[1], lambda x, y: x > y),
    "Lt": lambda a, w, k, s: _bin(a[0], a[1], lambda x, y: x < y),
    "And": lambda a, w, k, s: _bin(a[0], a[1], lambda x, y: bool(x) and bool(y)),
    "Ref": lambda a, w, k, s: _shift(a[0], int(k["lag"])),
    "Delta": lambda a, w, k, s: _bin(a[0], _shift(a[0], int(k["lag"])), lambda x, y: x - y),
    "Mean": _window_op("Mean", _mean),
    "Std": _window_op("Std", _std),
    "Min": _window_op("Min", min),
    "Max": _window_op("Max", max),
    "TSRank": _window_op("TSRank", _tsrank),
    "Corr": _window_op("Corr", _corr),
    "EMA": _ema,
}

__all__ = ["evaluate"]
