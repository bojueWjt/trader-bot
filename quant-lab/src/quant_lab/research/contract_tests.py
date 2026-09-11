"""算子契约测试框架（契约 §2；合并稿 D.2 "时间与数值" / "reference 与资源"）。

四类契约（每算子每后端必过）：
  1. reference   ≥20 边界例 + 100 随机序列 与 reference.py 手算逐行对拍：数值 atol=1e-10 / rtol=1e-8，布尔完全一致；
  2. truncation  截断重算：任意 cutoff，用 close_time <= cutoff 的 bars 重算，历史行结果与全量计算逐行一致；
  3. future      未来 NaN / 极值 / 乱序不影响过去：cutoff 之后注入 null、±1e12、打乱行序，cutoff 前结果不变；
  4. past_null   过去缺值按 nan_policy 传播：注入 null 后 (a) 与 reference 一致 (b) 超出回看地平线的行不变；
  5. gap         墙钟缺 bar 不跨洞：随机丢 bar 后 (a) 与 reference 一致 (b) 每行结果只由 (t − W, t] 内实际存在的 bar 决定。
任一失败 → 该算子隔离（ops.quarantine），依赖它的 AST 全部禁用（features 层检查）。
R-05 用同一框架对 polars_ta 后端出通过率。
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from quant_lab.research import reference
from quant_lab.research.ast import lint
from quant_lab.research.backends import interval_minutes, prepare_partition, quarantine_backend_op
from quant_lab.research.ops import REGISTRY

ATOL, RTOL = 1e-10, 1e-8
T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
UTC_US = pl.Datetime("us", "UTC")
STEP = dt.timedelta(minutes=15)
INTERVAL = "15m"
INTERVAL_MIN = 15
FIELDS = ("close", "open", "volume")

F = lambda n: {"field": n}                                  # noqa: E731
ROWS = lambda n: {"unit": "rows", "count": n}               # noqa: E731
WALL = lambda m: {"unit": "wallclock", "minutes": m}        # noqa: E731


def op_asts(name: str) -> list[tuple[str, dict]]:
    """每个算子的测试 AST 集合（含窗口/参数变体）。"""
    spec = REGISTRY[name]
    if name in ("Ref", "Delta"):
        return [(f"{name}[lag={n}]", {"op": name, "args": [F("close")], "params": {"lag": n}}) for n in (1, 5)] + \
               ([("Ref[lag=0]", {"op": "Ref", "args": [F("close")], "params": {"lag": 0}})] if name == "Ref" else [])
    if name == "EMA":
        return [(f"EMA[rows={n}]", {"op": "EMA", "args": [F("close")], "window": ROWS(n)}) for n in (3, 10)]
    if spec.windowed:
        args = [F("close"), F("volume")] if spec.arity == 2 else [F("close")]
        wins = [("rows=3", ROWS(3)), ("rows=10", ROWS(10)), ("wall=60", WALL(60)), ("wall=100", WALL(100)), ("wall=300", WALL(300))]
        return [(f"{name}[{lbl}]", {"op": name, "args": args, "window": w}) for lbl, w in wins]
    if spec.arity == 2:
        if name == "And":
            return [("And", {"op": "And", "args": [{"op": "Gt", "args": [F("close"), F("open")]}, {"op": "Lt", "args": [F("volume"), {"const": 50.0}]}]})]
        b = {"const": 2.0} if name in ("Mul",) else F("open")
        return [(f"{name}[close,open]", {"op": name, "args": [F("close"), F("open")]}), (f"{name}[close,const]", {"op": name, "args": [F("close"), {"const": 2.0}]})]
    return [(name, {"op": name, "args": [F("close")]})]


# ---------------------------------------------------------------- 夹具
def make_bars(close, *, open_=None, volume=None, times=None) -> pl.DataFrame:
    n = len(close)
    times = times if times is not None else [T0 + i * STEP for i in range(n)]
    open_ = open_ if open_ is not None else [None if c is None else c * 0.999 for c in close]
    volume = volume if volume is not None else [None if c is None else 10.0 + (i % 7) for i, c in enumerate(close)]
    return pl.DataFrame({"instrument_id": ["X"] * n, "interval": [INTERVAL] * n, "close_time": times, "close": close, "open": open_, "volume": volume},
                        schema={"instrument_id": pl.Utf8, "interval": pl.Utf8, "close_time": UTC_US, "close": pl.Float64, "open": pl.Float64, "volume": pl.Float64})


def boundary_cases() -> list[tuple[str, pl.DataFrame]]:
    """≥20 边界例：常数、单调、并列、零方差、null 位置、单行、恰好/不足窗长、负值、零、极大极小、尖刺、缺 bar、乱序。"""
    r = list(range(1, 31))
    rng = np.random.default_rng(7)
    rnd = list(100 + rng.normal(0, 1, 30))
    cases = [
        ("constant", [5.0] * 30),
        ("increasing", [float(i) for i in r]),
        ("decreasing", [float(31 - i) for i in r]),
        ("alternating", [1.0 if i % 2 else 2.0 for i in r]),
        ("ties_blocks", [float(i // 5) for i in range(30)]),
        ("single_row", [3.0]),
        ("two_rows", [1.0, 2.0]),
        ("exactly_3", [1.0, 2.0, 4.0]),
        ("exactly_10", [float(i) for i in range(10)]),
        ("nine_rows", [float(i) for i in range(9)]),
        ("null_head", [None, None, None] + rnd[3:]),
        ("null_mid", rnd[:10] + [None] + rnd[11:]),
        ("null_tail", rnd[:-3] + [None, None, None]),
        ("null_scattered", [None if i % 4 == 0 else v for i, v in enumerate(rnd)]),
        ("all_null", [None] * 12),
        ("negatives", [float(-i) for i in r]),
        ("zeros_mixed", [0.0 if i % 3 == 0 else float(i) for i in r]),
        ("huge", [1e12 + i for i in r]),
        ("tiny", [1e-12 * i for i in r]),
        ("spike", rnd[:15] + [1e9] + rnd[16:]),
        ("neg_spike", rnd[:15] + [-1e9] + rnd[16:]),
        ("random", rnd),
    ]
    out = [(nm, make_bars(c)) for nm, c in cases]
    # 缺 bar：墙钟洞（丢第 5–12 根与第 20 根）
    keep = [i for i in range(30) if not (5 <= i <= 12 or i == 20)]
    out.append(("gap_wallclock", make_bars([rnd[i] for i in keep], times=[T0 + i * STEP for i in keep])))
    # 多洞：每隔 3 根丢 1 根（rows 窗几乎处处跨洞 → strict_required 全 invalid）
    keep2 = [i for i in range(30) if i % 3 != 2]
    out.append(("gap_periodic", make_bars([rnd[i] for i in keep2], times=[T0 + i * STEP for i in keep2])))
    # 乱序输入（后端须自行排序）
    perm = rng.permutation(30)
    out.append(("unsorted_input", make_bars([rnd[i] for i in perm], times=[T0 + int(i) * STEP for i in perm])))
    # open == close 处处相等（Gt/Lt 全 False，Corr 零方差）
    out.append(("open_eq_close", make_bars(rnd, open_=rnd)))
    out.append(("volume_zero", make_bars(rnd, volume=[0.0] * 30)))
    assert len(out) >= 20
    return out


def random_cases(n: int = 100, seed: int = 0, length: int = 120) -> list[tuple[str, pl.DataFrame]]:
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n):
        L = int(rng.integers(20, length))
        close = list(100 * np.exp(np.cumsum(rng.normal(0, 0.01, L))))
        open_ = list(np.array(close) * (1 + rng.normal(0, 0.002, L)))
        vol = list(rng.gamma(2, 20, L))
        null_p = rng.choice([0.0, 0.05, 0.2])
        for i in range(L):
            if rng.random() < null_p:
                close[i] = None
            if rng.random() < null_p / 2:
                vol[i] = None
            if rng.random() < 0.01:
                close[i] = float("inf")     # 非有限输入 → invalid
        gap_p = rng.choice([0.0, 0.1, 0.3])
        keep = [i for i in range(L) if rng.random() >= gap_p] or [0]
        out.append((f"rand{k}", make_bars([close[i] for i in keep], open_=[open_[i] for i in keep],
                                          volume=[vol[i] for i in keep], times=[T0 + i * STEP for i in keep])))
    return out


# ---------------------------------------------------------------- 对拍
def _ref(ast: dict, part: pl.DataFrame) -> list:
    cols = {f: part[f].to_list() for f in FIELDS if f in part.columns}
    return reference.evaluate(ast, cols, part["close_time"].to_list(), INTERVAL_MIN)


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, float) and math.isnan(a):
        return b is not None and isinstance(b, float) and math.isnan(b)
    return math.isclose(a, b, rel_tol=RTOL, abs_tol=ATOL)


def compare(got: pl.Series, exp: list, label: str = "") -> list[str]:
    g = got.to_list()
    if len(g) != len(exp):
        return [f"{label}: 长度 {len(g)} != {len(exp)}"]
    return [f"{label}: 行 {i} got={x!r} exp={y!r}" for i, (x, y) in enumerate(zip(g, exp)) if not _close(x, y)]


@dataclass
class ContractResult:
    op: str
    backend: str
    checks: dict[str, int] = field(default_factory=dict)      # 检查名 → 通过次数
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _prep(backend, ast: dict, bars: pl.DataFrame) -> tuple[pl.DataFrame, pl.Series]:
    part = prepare_partition(bars, lint(ast).fields)
    return part, backend.compute_series(ast, part, _contract_probe=True)


def check_reference(backend, ast: dict, bars: pl.DataFrame, label: str) -> list[str]:
    part, got = _prep(backend, ast, bars)
    return compare(got, _ref(ast, part), label)


def check_truncation(backend, ast: dict, bars: pl.DataFrame, label: str, cutoffs=(0.3, 0.6, 0.9)) -> list[str]:
    part, full = _prep(backend, ast, bars)
    errs = []
    for c in cutoffs:
        k = max(1, int(part.height * c))
        t = part["close_time"][k - 1]
        sub = part.filter(pl.col("close_time") <= t)
        got = backend.compute_series(ast, sub, _contract_probe=True)
        errs += compare(got, full[:sub.height].to_list(), f"{label} truncation@{c}")
    return errs


def check_future_immunity(backend, ast: dict, bars: pl.DataFrame, label: str, seed: int = 0) -> list[str]:
    part, full = _prep(backend, ast, bars)
    n = part.height
    if n < 4:
        return []
    k = n // 2
    rng = np.random.default_rng(seed)
    errs = []
    fields = [f for f in FIELDS if f in part.columns]
    variants = {
        "future_null": part.with_columns([pl.when(pl.int_range(pl.len()) >= k).then(None).otherwise(pl.col(f)).alias(f) for f in fields]),
        "future_extreme": part.with_columns([pl.when(pl.int_range(pl.len()) >= k).then(pl.lit(1e12) * (pl.int_range(pl.len()) % 2 * 2 - 1)).otherwise(pl.col(f)).alias(f) for f in fields]),
        "future_shuffled": pl.concat([part[:k], part[k:][list(rng.permutation(n - k))]]),
    }
    for nm, v in variants.items():
        got = backend.compute_series(ast, prepare_partition(v, tuple(fields)), _contract_probe=True)
        errs += compare(got[:k], full[:k].to_list(), f"{label} {nm}")
    return errs


def horizon_minutes(ast: dict) -> int:
    rep = lint(ast)
    return rep.lookback_rows * INTERVAL_MIN + rep.lookback_minutes


def horizon_rows(ast: dict, part: pl.DataFrame, i: int) -> int:
    """注入 null 于第 i 行后可能受影响的最后一行索引（含）：close_time < t_i + 回看地平线 的行。"""
    t = part["close_time"][i] + dt.timedelta(minutes=horizon_minutes(ast))
    j = int((part["close_time"] <= t).sum())      # close_time <= t_i + H 的行都可能受影响
    return min(part.height - 1, j - 1)


def check_past_null(backend, ast: dict, bars: pl.DataFrame, label: str) -> list[str]:
    part, full = _prep(backend, ast, bars)
    n = part.height
    if n < 6:
        return []
    errs = []
    for i in (n // 3, n // 2):
        v = part.with_columns(pl.when(pl.int_range(pl.len()) == i).then(None).otherwise(pl.col("close")).alias("close"))
        got = backend.compute_series(ast, v, _contract_probe=True)
        errs += compare(got, _ref(ast, v), f"{label} past_null@{i}")
        h = horizon_rows(ast, part, i)
        if "EMA" in lint(ast).ops:
            continue    # EMA 无限记忆：null 重置状态后后续值全部改变，属定义行为
        if h + 1 < n:
            errs += compare(got[h + 1:], full[h + 1:].to_list(), f"{label} past_null@{i} beyond_horizon")
    return errs


def check_gap(backend, ast: dict, bars: pl.DataFrame, label: str, seed: int = 0) -> list[str]:
    rep = lint(ast)
    part, _ = _prep(backend, ast, bars)
    if part.height < 8:
        return []
    rng = np.random.default_rng(seed)
    keep = sorted(set(rng.choice(part.height, size=max(4, int(part.height * 0.7)), replace=False).tolist()))
    gapped = part[keep]
    got = backend.compute_series(ast, gapped, _contract_probe=True)
    errs = compare(got, _ref(ast, gapped), f"{label} gap")
    if (rep.windows or "Ref" in rep.ops or "Delta" in rep.ops) and "EMA" not in rep.ops:
        # 只由 [t − H, t] 内实际存在的 bar 决定：单独喂该子集，最后一行结果应一致（缺 bar 不跨洞、不向左扩张）
        # EMA 无限记忆，不适用子集等价，只做 reference 对拍
        W = dt.timedelta(minutes=horizon_minutes(ast))
        for i in (gapped.height // 2, gapped.height - 1):
            t = gapped["close_time"][i]
            sub = gapped.filter((pl.col("close_time") >= t - W) & (pl.col("close_time") <= t))
            g2 = backend.compute_series(ast, sub, _contract_probe=True)
            errs += compare(g2[-1:], [got[i]], f"{label} gap window_only@{i}")
    return errs


def run_contract(backend, op: str, *, n_random: int = 100, seed: int = 0) -> ContractResult:
    res = ContractResult(op=op, backend=backend.name)
    cases = boundary_cases() + random_cases(n_random, seed)
    for lbl, ast in op_asts(op):
        for cname, bars in cases:
            for chk in (check_reference, check_truncation, check_future_immunity, check_past_null, check_gap):
                try:
                    errs = chk(backend, ast, bars, f"{lbl}/{cname}")
                except Exception as e:  # noqa: BLE001 — 记录为契约失败，不中断
                    errs = [f"{lbl}/{cname} {chk.__name__}: {type(e).__name__}: {e}"]
                res.checks[chk.__name__] = res.checks.get(chk.__name__, 0) + (0 if errs else 1)
                res.failures += errs[:3]
    if res.failures:
        quarantine_backend_op(backend.name, op, f"contract FAIL {len(res.failures)}+ 条，例: {res.failures[0][:80]}")   # 任一失败 → 隔离
    return res


__all__ = ["ATOL", "RTOL", "ContractResult", "boundary_cases", "check_future_immunity", "check_gap", "check_past_null",
           "check_reference", "check_truncation", "compare", "make_bars", "op_asts", "random_cases", "run_contract"]
