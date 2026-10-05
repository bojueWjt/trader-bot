#!/usr/bin/env python3
"""v8 长期盈利判定（方案 §10.4，CA-1）。在任何 v8 L0 结果出来之前冻结；记下本文件 sha 再跑 L0。

口径（冻结，修改必须写进 CHANGELOG，并且新旧两版都重报）：

- 样本：可评估且已成交的单 —— fill_status ∈ {filled, partial}、censor_reason 为空、
  mark_ok/funding_ok/rules_ok/bars_ok 严格为 true、net_R 非空（同 v7 final_report2）。
- 区间（按 t_dec，UTC）：判定期 = 各频道数据起点至 2026-06-30，只用这一段下结论；
  校准期 = 2026-07-01 至 09-30，单独出表，不下结论；09-30 之后的行不进任何表。
- 三张表（按频道，再加六频道合计 ALL）：
  1. 有止损块（sizing_basis=risk，主口径 w60）：每单平均 net_R；
  2. 无止损块（sizing_basis=nostop）：每单平均 net_U（= net_R × risk_budget），另列 net_U / 成交名义；
  3. 金额合计：每周 net_U 之和（有止损 net_R×B + 无止损 net_U）的周均值，跨度内的空周按 0 计入。
     有止损块与无止损块在同一组周上联合重抽（先按周合并成一个数，再抽周）。
  合计只有金额：不出合计均值 R、不出合计胜率（无止损单的 R 是金额等价，不是风险等价）。
- 不确定性：按 t_dec 所在 ISO 周（周一 00:00 UTC）分块，有放回抽块，B=10,000，seed=20261006，取 2.5%/97.5% 分位。
  统计量 = 抽到的块的总和 / 抽到的块的计数（每单表：单数；合计表：周数）。每单表只抽有样本的周；
  合计表抽跨度内全部周（空周为 0）。稳健性：28 天块（自跨度首周起每 4 个 ISO 周一块）再算一遍，
  判定不同就标「对块长敏感」，判定本身仍取 7 天块。
- 判定：可评估单 < 30 或非空周 < 20 → 样本不足；下界 > 0 → 盈利；上界 < 0 → 亏损；其余 → 无显著优势。
- 持有期：有止损块只用主口径 w60；无止损块分别用 w1 / 5 天 / w14 / w60 四档的结果，四档判定一致才给出该判定，
  否则写「依持有期而定」，缺档写「持有期档缺失」；合计表对四档无止损分别配 w60 有止损块，规则相同。
- 逐月表（主口径）：每月 n（分块）、有止损 U、无止损 U、合计 U、累计合计 U；按出场日期的累计已实现 U 曲线
  与最大回撤；亏损月占比（分母为有可评估单的月份）；无止损在场名义峰值 / 9,000U。
- 可实现性敏感性（CA-12，只作说明）：按成交时间顺序，无止损在场总名义 + 新单 > 9,000U 时跳过新单，报告此时的合计；
  单频道各自计算，ALL 在六频道合并后计算（同一账户）。直接过滤 trades，不另跑。
- 只有主口径参与判定。S、E、W、H、C 各批次（其余 l0-v8* 目录）以及 edit_may_contain_outcome 剔除，只报区间。

用法（研究机）：
  python scripts/v8_report.py --reports $R --out $R/final-report-v8 \
      [--channels fengge,titan,gauls,cash,shuqin,jianguo] [--main l0-v8-w60lf-ns300] \
      [--hold w1=l0-v8-w1-ns300 --hold 5d=l0-v8-5d-ns300 --hold w14=l0-v8-w14-ns300] \
      [--edit-variant l0-v8e] [--sensitivity NAME=DIR ...]
产出 <out>.json 与 <out>.txt；输入不一致（策略、B、图版本、笔数、parquet 新于 summary）时拒绝出报告。
本脚本只依赖 polars/numpy 与标准库，不 import quant_lab，以免判定随研究代码改动而漂移。
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import polars as pl

JUDGMENT_VERSION = "v8-report-1"
CHANGELOG = [
    {"version": "v8-report-1", "date": "2026-10-05", "change": "初版：§10.4 口径冻结（在任何 v8 L0 结果之前）"},
]

UTC = dt.timezone.utc
JUDGMENT_END = dt.datetime(2026, 7, 1, tzinfo=UTC)        # 判定期：t_dec < 2026-07-01（含 06-30 全天）
CALIBRATION_END = dt.datetime(2026, 10, 1, tzinfo=UTC)    # 校准期：2026-07-01 ≤ t_dec < 2026-10-01
N_BOOT = 10_000
SEED = 20261006
BLOCK_DAYS = (7, 28)
MIN_TRADES = 30
MIN_WEEKS = 20
B_MAIN = Decimal("180")
ACCOUNT_U = Decimal("9000")
FILLED = ("filled", "partial")
COVERAGE = ("mark_ok", "funding_ok", "rules_ok", "bars_ok")
SIZING_RISK, SIZING_NOSTOP = "risk", "nostop"

MAIN_DIR = "l0-v8-w60lf-ns300"
MAIN_POLICY = "base-v1-timeexit-w60-live-follow-ns300"
HOLDS = ("w1", "5d", "w14", "w60")
HOLD_DIRS = {"w1": "l0-v8-w1-ns300", "5d": "l0-v8-5d-ns300", "w14": "l0-v8-w14-ns300"}     # w60 = 主口径目录
HOLD_POLICIES = {"w1": "base-v1-timeexit-w1-live-follow-ns300", "5d": "base-v1-timeexit-live-follow-ns300",
                 "w14": "base-v1-timeexit-w14-live-follow-ns300", "w60": MAIN_POLICY}
EDIT_DIR = "l0-v8e"
SENSITIVITY_PREFIX = "l0-v8"
ALL = "ALL"

PROFIT, LOSS, NO_EDGE, INSUFFICIENT = "盈利", "亏损", "无显著优势", "样本不足"
DEPENDS_ON_HOLD, HOLD_MISSING = "依持有期而定", "持有期档缺失"
BLOCK_SENSITIVE = "对块长敏感"


class ReportError(Exception):
    """Inputs that cannot be judged as they are (inconsistent or wrong 口径): refuse instead of reporting."""


# ---------------------------------------------------------------------------
# 样本与单位
# ---------------------------------------------------------------------------

def evaluable(row: dict) -> bool:
    return (row.get("fill_status") in FILLED and row.get("censor_reason") in (None, "")
            and row.get("net_R") is not None and all(row.get(key) is True for key in COVERAGE))


def sizing_of(row: dict) -> str:
    return row.get("sizing_basis") or SIZING_RISK      # 旧行（v8 之前）全部按风险定量


def net_U(row: dict) -> Decimal:
    budget = row.get("risk_budget")
    if budget is None:
        raise ReportError(f"{row.get('episode_id')}: risk_budget missing, cannot convert to U")
    return Decimal(str(row["net_R"])) * Decimal(str(budget))


def period_of(t_dec: dt.datetime) -> str | None:
    if t_dec < JUDGMENT_END:
        return "judgment"
    if t_dec < CALIBRATION_END:
        return "calibration"
    return None


def week_start(t: dt.datetime) -> dt.datetime:
    t = t.astimezone(UTC)
    day = t.date() - dt.timedelta(days=t.weekday())
    return dt.datetime(day.year, day.month, day.day, tzinfo=UTC)


def block_of(t: dt.datetime, origin: dt.datetime, days: int) -> dt.datetime:
    """Start of the calendar block containing t: ISO week for 7 days; for 28, groups of 4 weeks from the origin week."""
    week = week_start(t)
    if days == 7:
        return week
    span = dt.timedelta(days=days)
    return origin + span * ((week - origin) // span)


def weeks_between(first: dt.datetime, last: dt.datetime) -> list[dt.datetime]:
    out, at = [], week_start(first)
    while at <= week_start(last):
        out.append(at)
        at += dt.timedelta(days=7)
    return out


# ---------------------------------------------------------------------------
# 区间与判定
# ---------------------------------------------------------------------------

def bootstrap_interval(sums, counts, *, n_boot: int = N_BOOT, seed: int = SEED) -> list[float] | None:
    """Block bootstrap of a ratio: resample blocks with replacement, statistic Σsums / Σcounts over the draw."""
    k = len(sums)
    if k < 2:
        return None
    s, c = np.asarray(sums, dtype=float), np.asarray(counts, dtype=float)
    if (c <= 0).any():
        raise ValueError("every block needs a positive count")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(n_boot, k))
    stats = s[idx].sum(axis=1) / c[idx].sum(axis=1)
    lo, hi = np.quantile(stats, [0.025, 0.975])
    return [float(lo), float(hi)]


def verdict(n: int, n_weeks: int, interval: list[float] | None) -> str:
    if n < MIN_TRADES or n_weeks < MIN_WEEKS or interval is None:
        return INSUFFICIENT
    if interval[0] > 0:
        return PROFIT
    if interval[1] < 0:
        return LOSS
    return NO_EDGE


def _judge(table: dict, judged: bool) -> dict:
    v7 = verdict(table["n"], table["n_weeks"], table["ci95"]["7d"])
    v28 = verdict(table["n"], table["n_weeks"], table["ci95"]["28d"])
    if judged:
        table.update({"verdict": v7, "verdict_28d": v28, "flags": [BLOCK_SENSITIVE] if v7 != v28 else []})
    else:
        table.update({"verdict": None, "verdict_28d": None, "flags": []})
    return table


def per_trade_table(samples: list[tuple[dt.datetime, float]], origin: dt.datetime | None, *, judged: bool,
                    unit: str) -> dict:
    """Mean per trade with calendar-block intervals; only weeks (blocks) with trades are resampled."""
    n = len(samples)
    weeks = {week_start(t) for t, _ in samples}
    table = {"unit": unit, "n": n, "n_weeks": len(weeks), "mean": (sum(v for _, v in samples) / n) if n else None,
             "sum": float(sum(v for _, v in samples)), "ci95": {}}
    for days in BLOCK_DAYS:
        sums, counts = defaultdict(float), defaultdict(int)
        for t, v in samples:
            key = block_of(t, origin, days)
            sums[key] += v
            counts[key] += 1
        keys = sorted(sums)
        table["ci95"][f"{days}d"] = bootstrap_interval([sums[k] for k in keys], [counts[k] for k in keys]) if n else None
    return _judge(table, judged)


def weekly_total_table(samples: list[tuple[dt.datetime, float]], span: list[dt.datetime], *, judged: bool) -> dict:
    """Mean weekly money total over every week of the span (empty weeks are 0); both blocks resampled together."""
    weekly = {w: 0.0 for w in span}
    used = set()
    for t, v in samples:
        w = week_start(t)
        if w not in weekly:
            raise ReportError(f"sample at {t.isoformat()} outside the span")
        weekly[w] += v
        used.add(w)
    n_span = len(span)
    table = {"unit": "U/周", "n": len(samples), "n_weeks": len(used), "n_span_weeks": n_span,
             "mean": (sum(weekly.values()) / n_span) if n_span else None, "sum": float(sum(weekly.values())), "ci95": {}}
    for days in BLOCK_DAYS:
        sums, counts = defaultdict(float), defaultdict(int)
        for w, v in weekly.items():
            key = block_of(w, span[0], days)
            sums[key] += v
            counts[key] += 1
        keys = sorted(sums)
        table["ci95"][f"{days}d"] = bootstrap_interval([sums[k] for k in keys], [counts[k] for k in keys]) if n_span else None
    return _judge(table, judged)


def consensus(tables: dict[str, dict | None]) -> dict:
    """Four holding periods must agree for a stopless/total verdict; missing → no verdict."""
    if any(tables.get(h) is None for h in HOLDS):
        missing = [h for h in HOLDS if tables.get(h) is None]
        return {"verdict": HOLD_MISSING, "missing": missing, "flags": []}
    v7 = {tables[h]["verdict"] for h in HOLDS}
    v28 = {tables[h]["verdict_28d"] for h in HOLDS}
    c7 = v7.pop() if len(v7) == 1 else DEPENDS_ON_HOLD
    c28 = v28.pop() if len(v28) == 1 else DEPENDS_ON_HOLD
    return {"verdict": c7, "verdict_28d": c28, "flags": [BLOCK_SENSITIVE] if c7 != c28 else []}


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------

@dataclass
class Run:
    directory: Path
    channel: str
    summary: dict
    rows: list[dict] = field(default_factory=list)
    identity: dict = field(default_factory=dict)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_run(directory: Path, channel: str, *, policy: str | None = None, budget: Decimal | None = None) -> Run | None:
    """One published L0 run; None when absent. Mixed generations or a different 口径 are refused."""
    folder = directory / channel
    summary_path, trades_path = folder / "summary.json", folder / "trades.parquet"
    if not summary_path.is_file() and not trades_path.is_file():
        return None
    if not summary_path.is_file() or not trades_path.is_file():
        raise ReportError(f"{folder}: summary.json and trades.parquet must both exist")
    before = [(p.stat().st_mtime_ns, p.stat().st_size) for p in (summary_path, trades_path)]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = pl.read_parquet(trades_path).to_dicts()
    after = [(p.stat().st_mtime_ns, p.stat().st_size) for p in (summary_path, trades_path)]
    if before != after:
        raise ReportError(f"{folder}: changed while reading")
    if before[0][0] < before[1][0]:
        raise ReportError(f"{folder}: trades.parquet is newer than summary.json (run in progress or mixed generations)")
    expected = summary["overall"]["n_trades"] + (summary["blocks"]["nostop"]["n_trades"] if "blocks" in summary else 0)
    if expected != len(rows):
        raise ReportError(f"{folder}: summary counts {expected} trades, parquet has {len(rows)}")
    for row in rows:
        if row.get("graph_version") != summary["graph_version"] or row.get("policy_hash") != summary["policy_hash"]:
            raise ReportError(f"{folder}: row {row.get('episode_id')} graph/policy differs from summary")
    if policy is not None and summary.get("policy_version") != policy:
        raise ReportError(f"{folder}: policy {summary.get('policy_version')} is not the frozen {policy}")
    if budget is not None and Decimal(str(summary.get("risk_budget"))) != budget:
        raise ReportError(f"{folder}: risk_budget {summary.get('risk_budget')} is not the frozen {budget}")
    identity = {"directory": str(folder), "policy_version": summary.get("policy_version"),
                "policy_hash": summary.get("policy_hash"), "graph_version": summary.get("graph_version"),
                "risk_budget": str(summary.get("risk_budget")), "n_rows": len(rows),
                "summary_sha256": _sha(summary_path), "trades_sha256": _sha(trades_path)}
    return Run(folder, channel, summary, rows, identity)


# ---------------------------------------------------------------------------
# 表
# ---------------------------------------------------------------------------

def in_period(rows: list[dict], period: str) -> list[dict]:
    return [r for r in rows if r.get("t_dec") is not None and period_of(r["t_dec"]) == period]


def span_for(period: str, data_start: dt.datetime | None) -> list[dt.datetime]:
    if period == "judgment":
        if data_start is None or data_start >= JUDGMENT_END:
            return []
        return weeks_between(data_start, JUDGMENT_END - dt.timedelta(microseconds=1))
    return weeks_between(JUDGMENT_END, CALIBRATION_END - dt.timedelta(microseconds=1))


def block_samples(rows: list[dict], basis: str, period: str) -> list[dict]:
    return [r for r in in_period(rows, period) if sizing_of(r) == basis and evaluable(r)]


def with_stop_table(rows, period, origin, *, judged) -> dict:
    samples = [(r["t_dec"], float(r["net_R"])) for r in block_samples(rows, SIZING_RISK, period)]
    return per_trade_table(samples, origin, judged=judged, unit="R/单")


def nostop_table(rows, period, origin, *, judged) -> dict:
    chosen = block_samples(rows, SIZING_NOSTOP, period)
    table = per_trade_table([(r["t_dec"], float(net_U(r))) for r in chosen], origin, judged=judged, unit="U/单")
    with_notional = [r for r in chosen if r.get("entry_notional_U") not in (None, 0)]
    total_notional = sum((Decimal(str(r["entry_notional_U"])) for r in with_notional), Decimal(0))
    table["net_U_over_notional"] = {
        "sum_ratio": float(sum((net_U(r) for r in with_notional), Decimal(0)) / total_notional) if total_notional else None,
        "mean_ratio": (float(sum(net_U(r) / Decimal(str(r["entry_notional_U"])) for r in with_notional) / len(with_notional))
                       if with_notional else None),
        "n_notional_missing": len(chosen) - len(with_notional)}
    return table


def total_table(stop_rows, nostop_rows, period, span, *, judged) -> dict:
    samples = [(r["t_dec"], float(net_U(r))) for r in block_samples(stop_rows, SIZING_RISK, period)]
    samples += [(r["t_dec"], float(net_U(r))) for r in block_samples(nostop_rows, SIZING_NOSTOP, period)]
    return weekly_total_table(samples, span, judged=judged)


def three_tables(stop_rows, nostop_rows, period, data_start, *, judged) -> dict:
    span = span_for(period, data_start)
    origin = span[0] if span else None
    return {"with_stop": with_stop_table(stop_rows, period, origin, judged=judged),
            "nostop": nostop_table(nostop_rows, period, origin, judged=judged),
            "total": total_table(stop_rows, nostop_rows, period, span, judged=judged)}


def _open_close(row: dict) -> tuple[dt.datetime | None, dt.datetime | None]:
    return row.get("position_open_at"), row.get("position_close_at") or row.get("censor_at")


def _filled_nostop(rows: list[dict]) -> list[dict]:
    return [r for r in rows if sizing_of(r) == SIZING_NOSTOP and r.get("position_open_at") is not None
            and r.get("filled_qty") is not None and Decimal(str(r["filled_qty"])) > 0]


def concurrency_by_month(rows: list[dict], months: list[str]) -> dict[str, float]:
    """Peak open stopless notional per month: filled notional from open to close (censor time when never closed,
    open-ended when neither is known); closes precede opens at the same instant. A month starts at the carried level."""
    edges = []
    for r in _filled_nostop(rows):
        if r.get("entry_notional_U") is None:
            continue
        start, end = _open_close(r)
        notional = float(r["entry_notional_U"])
        edges.append((start, 1, notional))
        if end is not None:
            edges.append((end, 0, -notional))
    edges.sort(key=lambda e: (e[0], e[1]))
    peaks, level, i = {}, 0.0, 0
    for month in months:
        nxt = _next_month(month)
        peak = level
        while i < len(edges) and edges[i][0].strftime("%Y-%m") < nxt:
            level += edges[i][2]
            if edges[i][0].strftime("%Y-%m") == month:
                peak = max(peak, level)
            else:
                peak = level          # edges before the first listed month only set the carried level
            i += 1
        peaks[month] = max(peak, level)
    return peaks


def _next_month(month: str) -> str:
    year, number = int(month[:4]), int(month[5:7])
    return f"{year + (number == 12)}-{1 if number == 12 else number + 1:02d}"


def drawdown_curve(rows: list[dict]) -> dict:
    """Cumulative realized U by exit time (close; t_dec when absent) and its maximum drawdown from the running peak (≥0 start)."""
    points = sorted(((r.get("position_close_at") or r["t_dec"], _key(r), float(net_U(r))) for r in rows),
                    key=lambda p: (p[0], p[1]))
    value, peak, worst = 0.0, 0.0, 0.0
    for _, _, v in points:
        value += v
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return {"n": len(points), "final_U": value, "max_drawdown_U": worst}


def monthly(rows: list[dict]) -> dict:
    """Main-口径 month table by t_dec month, plus per-period loss-month share and exit-date drawdown."""
    samples = [r for r in rows if r.get("t_dec") is not None and period_of(r["t_dec"]) is not None and evaluable(r)]
    dated = [r["t_dec"] for r in rows if r.get("t_dec") is not None and period_of(r["t_dec"]) is not None]
    months = []
    if dated:
        cursor, last = min(dated).strftime("%Y-%m"), max(dated).strftime("%Y-%m")
        while cursor <= last:
            months.append(cursor)
            cursor = _next_month(cursor)
    peaks = concurrency_by_month(rows, months)
    by_month = defaultdict(list)
    for r in samples:
        by_month[r["t_dec"].strftime("%Y-%m")].append(r)
    table, cumulative = [], 0.0
    for month in months:
        part = by_month.get(month, [])
        stop = [r for r in part if sizing_of(r) == SIZING_RISK]
        nostop = [r for r in part if sizing_of(r) == SIZING_NOSTOP]
        stop_U, nostop_U = float(sum(map(net_U, stop), Decimal(0))), float(sum(map(net_U, nostop), Decimal(0)))
        cumulative += stop_U + nostop_U
        peak = peaks.get(month, 0.0)
        table.append({"month": month, "period": "judgment" if month < "2026-07" else "calibration",
                      "n_with_stop": len(stop), "n_nostop": len(nostop), "with_stop_U": stop_U, "nostop_U": nostop_U,
                      "total_U": stop_U + nostop_U, "cumulative_total_U": cumulative,
                      "peak_nostop_notional_U": peak, "peak_over_account": peak / float(ACCOUNT_U)})
    periods = {}
    for period in ("judgment", "calibration", "all"):
        chosen = [m for m in table if period == "all" or m["period"] == period]
        active = [m for m in chosen if m["n_with_stop"] + m["n_nostop"] > 0]
        rows_in = [r for r in samples if period == "all" or period_of(r["t_dec"]) == period]
        periods[period] = {"n_months": len(chosen), "n_active_months": len(active),
                           "loss_month_share": (sum(m["total_U"] < 0 for m in active) / len(active)) if active else None,
                           "realized_by_exit": drawdown_curve(rows_in),
                           "peak_nostop_notional_U": max((m["peak_nostop_notional_U"] for m in chosen), default=0.0)}
    return {"months": table, "periods": periods}


def realizable(rows: list[dict], cap: Decimal = ACCOUNT_U) -> tuple[list[dict], dict]:
    """CA-12: walk filled stopless trades in fill order; skip a new one if open notional + its notional would exceed cap."""
    order = sorted(_filled_nostop(rows), key=lambda r: (r["position_open_at"], _key(r)))
    open_legs: list[tuple[dt.datetime | None, Decimal]] = []
    skipped, unknown = set(), []
    for r in order:
        start, end = _open_close(r)
        open_legs = [(until, n) for until, n in open_legs if until is None or until > start]
        if r.get("entry_notional_U") is None:
            unknown.append(_key(r))      # 名义未知：不能判断，保留并单列
            continue
        notional = Decimal(str(r["entry_notional_U"]))
        if sum((n for _, n in open_legs), Decimal(0)) + notional > cap:
            skipped.add(_key(r))
            continue
        open_legs.append((end, notional))
    kept = [r for r in rows if _key(r) not in skipped]
    return kept, {"cap_U": float(cap), "n_skipped": len(skipped), "skipped": sorted(skipped),
                  "n_notional_unknown": len(unknown)}


def _key(row: dict) -> str:
    return f"{row.get('_channel', '')}:{row['episode_id']}"


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _tag(rows: list[dict], channel: str) -> list[dict]:
    return [dict(r, _channel=channel) for r in rows]


def _data_start(rows: list[dict]) -> dt.datetime | None:
    dated = [r["t_dec"] for r in rows if r.get("t_dec") is not None]
    return min(dated) if dated else None


def build_report(reports: Path, channels: list[str], *, main_dir: str = MAIN_DIR, hold_dirs: dict | None = None,
                 edit_dir: str = EDIT_DIR, sensitivities: dict | None = None) -> dict:
    hold_dirs = {**HOLD_DIRS, **(hold_dirs or {}), "w60": main_dir}
    if not channels:
        raise ReportError("no channels")
    inputs: dict[str, dict] = defaultdict(dict)
    main_rows, hold_rows = {}, {h: {} for h in HOLDS}
    for ch in channels:
        run = load_run(reports / main_dir, ch, policy=MAIN_POLICY, budget=B_MAIN)
        if run is None:
            raise ReportError(f"main 口径 missing for channel {ch}: {reports / main_dir / ch}")
        main_rows[ch] = _tag(run.rows, ch)
        inputs["main"][ch] = run.identity
        for hold in HOLDS:
            if hold == "w60":
                hold_rows[hold][ch] = main_rows[ch]
                continue
            hrun = load_run(reports / hold_dirs[hold], ch, policy=HOLD_POLICIES[hold], budget=B_MAIN)
            if hrun is None:
                continue
            if hrun.summary["graph_version"] != run.summary["graph_version"]:
                raise ReportError(f"{hrun.directory}: graph {hrun.summary['graph_version']} differs from main "
                                  f"{run.summary['graph_version']}")
            hold_rows[hold][ch] = _tag(hrun.rows, ch)
            inputs[f"hold:{hold}"][ch] = hrun.identity
    scopes = [*channels, ALL]

    def rows_for(source: dict, scope: str) -> list[dict] | None:
        if scope == ALL:
            # 合计只在全部频道都有时出，缺一个频道就不能叫合计。
            if any(ch not in source for ch in channels):
                return None
            return [r for ch in channels for r in source[ch]]
        return source.get(scope)

    # 数据起点：主口径与各持有期档里最早的 t_dec（同一张图，正常相同；取最早以免样本落到跨度之外）。
    starts = {scope: _data_start([r for source in (main_rows, *hold_rows.values()) for r in (rows_for(source, scope) or [])])
              for scope in scopes}
    out = {"judgment": {}, "calibration": {}}
    for period, judged in (("judgment", True), ("calibration", False)):
        tables = {"with_stop": {}, "nostop": {}, "total": {}}
        for scope in scopes:
            main = rows_for(main_rows, scope)
            span = span_for(period, starts[scope])
            origin = span[0] if span else None
            tables["with_stop"][scope] = with_stop_table(main, period, origin, judged=judged)
            per_hold_nostop, per_hold_total = {}, {}
            for hold in HOLDS:
                rows = rows_for(hold_rows[hold], scope)
                if rows is None:
                    per_hold_nostop[hold] = per_hold_total[hold] = None
                    continue
                per_hold_nostop[hold] = nostop_table(rows, period, origin, judged=judged)
                per_hold_total[hold] = total_table(main, rows, period, span, judged=judged)
            nostop_entry = {"holds": per_hold_nostop}
            total_entry = {"holds": per_hold_total}
            if judged:
                nostop_entry.update(consensus(per_hold_nostop))
                total_entry.update(consensus(per_hold_total))
            tables["nostop"][scope] = nostop_entry
            tables["total"][scope] = total_entry
        out[period] = tables

    monthly_tables = {scope: monthly(rows_for(main_rows, scope)) for scope in scopes}

    realizability, edit_excluded = {}, {}
    for scope in scopes:
        main = rows_for(main_rows, scope)
        kept, info = realizable(main)
        realizability[scope] = {**info, **{p: three_tables(kept, kept, p, starts[scope], judged=False)["total"]
                                           for p in ("judgment", "calibration")}}
        if not any("edit_may_contain_outcome" in r for r in main):
            edit_excluded[scope] = {"status": "列缺失（G1 未写 edit_may_contain_outcome）"}
        else:
            clean = [r for r in main if r.get("edit_may_contain_outcome") is not True]
            edit_excluded[scope] = {"n_excluded": len(main) - len(clean),
                                    **{p: three_tables(clean, clean, p, starts[scope], judged=False)
                                       for p in ("judgment", "calibration")}}

    # 其余 l0-v8* 目录（S/E/W/H/C 批次）：只报区间，不给判定。
    named = dict(sensitivities or {})
    if reports.is_dir():
        taken = {main_dir, *hold_dirs.values(), *named.values()}
        for directory in sorted(p.name for p in reports.iterdir() if p.is_dir()):
            if directory.startswith(SENSITIVITY_PREFIX) and directory not in taken:
                named[directory] = directory
    if edit_dir and (reports / edit_dir).is_dir() and edit_dir not in named.values():
        named[edit_dir] = edit_dir
    sensitivity_out = {}
    sensitivity_rows = {}
    for name, directory in sorted(named.items()):
        per, loaded, refused = {}, {}, {}
        for ch in channels:
            try:
                srun = load_run(reports / directory, ch)
            except ReportError as error:
                # 敏感性只报区间：一个批次没跑完不能挡住主口径的判定，但也不能拿半截结果出区间。
                refused[ch] = str(error)
                continue
            if srun is None:
                continue
            loaded[ch] = _tag(srun.rows, ch)
            inputs[f"sensitivity:{name}"][ch] = srun.identity
        sensitivity_rows[directory] = loaded
        for scope in scopes:
            rows = rows_for(loaded, scope)
            if rows is None:
                per[scope] = {"status": "未出", **({"refused": refused[scope]} if scope in refused else {})}
                continue
            start = _data_start(rows)
            per[scope] = {p: three_tables(rows, rows, p, start, judged=False) for p in ("judgment", "calibration")}
        sensitivity_out[name] = {"directory": directory, "tables": per}

    headline = {}
    edit_rows = sensitivity_rows.get(edit_dir, {})
    for scope in scopes:
        entry = {"main": {"verdict": out["judgment"]["total"][scope].get("verdict"),
                          "w60": out["judgment"]["total"][scope]["holds"]["w60"]}}
        rows = rows_for(edit_rows, scope) if edit_rows else None
        entry["v8e"] = (three_tables(rows, rows, "judgment", _data_start(rows), judged=False)["total"]
                        if rows is not None else {"status": "未出"})
        headline[scope] = entry

    return {"judgment_version": JUDGMENT_VERSION, "changelog": CHANGELOG, "frozen_rules": frozen_rules(),
            "channels": channels, "inputs": dict(inputs), "judgment": out["judgment"], "calibration": out["calibration"],
            "monthly": monthly_tables, "headline": headline, "realizability": realizability,
            "edit_excluded": edit_excluded, "sensitivities": sensitivity_out}


def frozen_rules() -> dict:
    return {"sample": "fill_status∈{filled,partial} ∧ censor_reason 空 ∧ 四项覆盖为 true ∧ net_R 非空",
            "judgment_period": f"数据起点 ≤ t_dec < {JUDGMENT_END.isoformat()}",
            "calibration_period": f"{JUDGMENT_END.isoformat()} ≤ t_dec < {CALIBRATION_END.isoformat()}（不下结论）",
            "bootstrap": {"blocks": "ISO 周（周一 00:00 UTC）；稳健性 28 天块", "n_boot": N_BOOT, "seed": SEED,
                          "quantiles": [0.025, 0.975]},
            "verdict": {"insufficient": f"n<{MIN_TRADES} 或非空周<{MIN_WEEKS}", "profit": "下界>0", "loss": "上界<0",
                        "else": NO_EDGE},
            "holds": {"with_stop": "w60（主口径）", "nostop": list(HOLDS), "rule": "四档一致才给判定"},
            "risk_budget_B": str(B_MAIN), "account_U": str(ACCOUNT_U), "main_policy": MAIN_POLICY,
            "hold_policies": HOLD_POLICIES}


def script_identity() -> dict:
    path = Path(__file__).resolve()
    head = None
    try:
        head = subprocess.run(["git", "-C", str(path.parent), "log", "-1", "--format=%H", "--", path.name],
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return {"script_sha256": _sha(path), "script_last_commit": head}


def _fmt(value, digits=3) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def _ci(table: dict, key: str = "7d") -> str:
    ci = table["ci95"].get(key)
    return "—" if ci is None else f"[{_fmt(ci[0])}, {_fmt(ci[1])}]"


def render_text(report: dict) -> str:
    lines = [f"v8 盈利判定 {report['judgment_version']} · script sha256 {report.get('script_sha256', '—')}",
             "合计只有金额；不出合计均值 R 与合计胜率。无止损块仅内核 A。", ""]

    def row(scope, table, verdict_text=None):
        flags = ",".join(table.get("flags") or [])
        return (f"  {scope:<10} n={table['n']:<5} 非空周={table['n_weeks']:<4} 点估计={_fmt(table['mean'])} "
                f"7天块{_ci(table)} 28天块{_ci(table, '28d')} {verdict_text if verdict_text is not None else (table.get('verdict') or '')}"
                f"{' ' + flags if flags else ''}")

    for period, title in (("judgment", "判定期（数据起点 – 2026-06-30）"), ("calibration", "校准期（2026-07-01 – 09-30，不下结论）")):
        tables = report[period]
        lines += [f"== {title} ==", "[有止损块] 每单平均 net_R（w60）"]
        for scope, table in tables["with_stop"].items():
            lines.append(row(scope, table))
        for key, label in (("nostop", "[无止损块] 每单平均 net_U"), ("total", "[金额合计] 每周 net_U 之和的周均值（空周=0）")):
            lines.append(label)
            for scope, entry in tables[key].items():
                lines.append(f"  {scope}: {entry.get('verdict') or ''}{' ' + ','.join(entry.get('flags') or []) if entry.get('flags') else ''}")
                for hold, table in entry["holds"].items():
                    lines.append("  " + (row(f"·{hold}", table) if table is not None else f"  ·{hold} 未出"))
        lines.append("")
    lines.append("== 逐月（主口径） ==")
    for scope, data in report["monthly"].items():
        lines.append(f"[{scope}]")
        for m in data["months"]:
            lines.append(f"  {m['month']} {m['period'][:5]} n={m['n_with_stop']}+{m['n_nostop']} 有止损U={_fmt(m['with_stop_U'], 1)} "
                         f"无止损U={_fmt(m['nostop_U'], 1)} 合计U={_fmt(m['total_U'], 1)} 累计U={_fmt(m['cumulative_total_U'], 1)} "
                         f"在场名义峰值/9000={_fmt(m['peak_over_account'], 2)}")
        for period, p in data["periods"].items():
            lines.append(f"  {period}: 亏损月占比={_fmt(p['loss_month_share'], 2)} 出场日累计U={_fmt(p['realized_by_exit']['final_U'], 1)} "
                         f"最大回撤U={_fmt(p['realized_by_exit']['max_drawdown_U'], 1)}")
    lines += ["", "== 头条：主口径与 v8e 并列（v8e 只报区间） =="]
    for scope, entry in report["headline"].items():
        v8e = entry["v8e"]
        lines.append(f"  {scope}: 主口径 {entry['main']['verdict']} · w60 {_ci(entry['main']['w60'])} · "
                     f"v8e {_ci(v8e) if 'ci95' in v8e else v8e.get('status')}")
    lines += ["", "== 敏感性（只报区间，不判定） =="]
    for scope, info in report["realizability"].items():
        lines.append(f"  可实现性 {scope}: 跳过 {info['n_skipped']} 单 · 判定期合计 {_ci(info['judgment'])}")
    for scope, info in report["edit_excluded"].items():
        if "judgment" in info:
            lines.append(f"  剔除 edit 标记 {scope}: 剔除 {info['n_excluded']} 单 · 判定期合计 {_ci(info['judgment']['total'])}")
        else:
            lines.append(f"  剔除 edit 标记 {scope}: {info['status']}")
    for name, data in report["sensitivities"].items():
        for scope, tables in data["tables"].items():
            text = tables.get("status") or _ci(tables["judgment"]["total"])
            lines.append(f"  {name} {scope}: 判定期合计 {text}")
    return "\n".join(lines) + "\n"


def _pairs(values: list[str], what: str) -> dict:
    out = {}
    for value in values or []:
        name, sep, directory = value.partition("=")
        if not sep or not name or not directory:
            raise SystemExit(f"--{what} expects NAME=DIR, got {value!r}")
        out[name] = directory
    return out


def _json_default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v8 长期盈利判定（§10.4，冻结口径）")
    parser.add_argument("--reports", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="输出前缀：<out>.json 与 <out>.txt")
    parser.add_argument("--channels", help="逗号分隔；缺省为主口径目录下的全部频道")
    parser.add_argument("--main", default=MAIN_DIR)
    parser.add_argument("--hold", action="append", default=[], help="w1|5d|w14=DIR")
    parser.add_argument("--edit-variant", default=EDIT_DIR)
    parser.add_argument("--sensitivity", action="append", default=[], help="NAME=DIR（只报区间）")
    args = parser.parse_args(argv)
    holds = _pairs(args.hold, "hold")
    if set(holds) - set(HOLD_DIRS):
        raise SystemExit(f"--hold names must be among {sorted(HOLD_DIRS)}")
    reports = args.reports.expanduser().resolve()
    if args.channels:
        channels = [c for c in args.channels.split(",") if c]
    else:
        main_dir = reports / args.main
        channels = sorted(p.name for p in main_dir.iterdir() if p.is_dir()) if main_dir.is_dir() else []
    try:
        report = build_report(reports, channels, main_dir=args.main, hold_dirs=holds, edit_dir=args.edit_variant,
                              sensitivities=_pairs(args.sensitivity, "sensitivity"))
    except ReportError as error:
        print(f"v8_report: refused: {error}", file=sys.stderr)
        return 2
    report.update(script_identity())
    out = args.out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    Path(f"{out}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default, allow_nan=False),
                                   encoding="utf-8")
    Path(f"{out}.txt").write_text(render_text(report), encoding="utf-8")
    print(f"v8_report: wrote {out}.json and {out}.txt (sha256 {report['script_sha256'][:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
