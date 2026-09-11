"""quant_lab.market.partition_check —— 分区体检 + quarantine（契约 §1、合并稿 C.2/C.3，M-04）。

对一个 silver 分区（一个 instrument × data_type × interval × period）做：
  1. 完整性对上市日历（instrument_rules 生命周期）：缺 bar → gap_flag（标在缺口后的首根 bar）+ BAR_GAP 记录；不插值不填零
  2. 重复主键 / 非单调 → KEY_DUPLICATE_OR_ORDER（源端重复在入湖时按"完全副本折叠 / 同键异值全部隔离"处理并写 manifest；
     本模块只读 silver，不重放 bronze——bronze 深度重放留 P2）
  3. OHLC 不变量与数值合法 → ohlc_valid=False + OHLC_INVALID（行保留）
  4. 尖刺 → spike_flag + spike_score + PRICE_SPIKE_FLAG（只标不删；缺 bar 不跨洞算收益）
  5. funding 周期 → 相邻 calc_time 间隔 ≠ funding_interval_hours → FUNDING_SCHEDULE_GAP
  6. 生命周期与精度：上市前/下线后的行 → SYMBOL_TIME_INVALID；价格不在 tick 网格 → PRECISION_INVALID；无规则区间 → RULE_HISTORY_MISSING
  8. 非合法日历网格行 → BAR_TIME_OFF_GRID（error，移出 silver 并写 quarantine，不抵扣覆盖）
  7. schema 哈希 → 与 manifest / 期望不符 → SCHEMA_DRIFT（整分区隔离）
产物：带 mask 的 silver（原子改写）、`data/quarantine/market.parquet` 追加（同输入同规则不重复）、manifest 回填。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

import polars as pl

from quant_lab.market.contract import first_grid_point, grid_points_between
from quant_lab.market.vision import (
    BAR_TYPES, INTERVAL_SECONDS, KEY_COL, LakePaths, atomic_write_json, atomic_write_parquet, instrument_id,
    period_bounds, schema_hash,
)

CHECK_RULE_VERSION = "partition-check-v1"
SPIKE_K = 10.0            # |r| / (1.4826·MAD) 超过即标尖刺（低可信提案，M-11 T_plaus 校准前不改）
SPIKE_MIN_SAMPLES = 30

R_BAR_GAP = "BAR_GAP"
R_TIME_GRID = "BAR_TIME_OFF_GRID"
R_KEY_DUP = "KEY_DUPLICATE_OR_ORDER"
R_OHLC = "OHLC_INVALID"
R_SPIKE = "PRICE_SPIKE_FLAG"
R_FUNDING = "FUNDING_SCHEDULE_GAP"
R_SYMBOL_TIME = "SYMBOL_TIME_INVALID"
R_PRECISION = "PRECISION_INVALID"
R_RULE_MISSING = "RULE_HISTORY_MISSING"
R_SCHEMA = "SCHEMA_DRIFT"
SEVERITY = {R_TIME_GRID: "error", R_BAR_GAP: "warn", R_KEY_DUP: "error", R_OHLC: "error", R_SPIKE: "info", R_FUNDING: "error",
            R_SYMBOL_TIME: "error", R_PRECISION: "warn", R_RULE_MISSING: "warn", R_SCHEMA: "fatal"}

from quant_lab.market.quarantine import QUARANTINE_SCHEMA, append_quarantine, quarantine_id, quarantine_path  # noqa: F401,E402
RULES_SCHEMA = {
    "instrument_id": pl.Utf8, "effective_from": pl.Datetime("us", "UTC"), "effective_to": pl.Datetime("us", "UTC"),
    "tick_size": pl.Utf8, "step_size": pl.Utf8, "min_notional": pl.Utf8, "multiplier": pl.Utf8,
    "funding_interval_hours": pl.Int64, "status": pl.Utf8, "source": pl.Utf8,
}


# ---------------------------------------------------------------------------
# 生命周期与精度表
# ---------------------------------------------------------------------------
def rules_path(lake: LakePaths, inst: str) -> Path:
    return lake.root / "silver" / "binance" / "um" / "instrument_rules" / f"instrument={inst}" / "rules.parquet"


def rules_from_exchange_info(info: dict, *, effective_from: dt.datetime, source: str = "exchangeInfo-snapshot") -> pl.DataFrame:
    """从一份 exchangeInfo JSON 快照（用户提供的公开文件，不在线拉）构造规则表。快照只能证明该时刻起的规则。"""
    rows = []
    for s in info.get("symbols", []):
        if s.get("contractType") not in (None, "PERPETUAL"):
            continue
        f = {x["filterType"]: x for x in s.get("filters", [])}
        onboard = s.get("onboardDate")
        eff = dt.datetime.fromtimestamp(onboard / 1000, dt.UTC) if onboard else effective_from
        rows.append({
            "instrument_id": instrument_id(s["symbol"]), "effective_from": max(eff, effective_from) if onboard else effective_from,
            "effective_to": None, "tick_size": f.get("PRICE_FILTER", {}).get("tickSize"),
            "step_size": f.get("LOT_SIZE", {}).get("stepSize"), "min_notional": f.get("MIN_NOTIONAL", {}).get("notional"),
            "multiplier": "1", "funding_interval_hours": None, "status": s.get("status", "UNKNOWN"), "source": source,
        })
    return pl.DataFrame(rows, schema=RULES_SCHEMA)


def rules_from_manifests(lake: LakePaths, inst: str, *, tick_size: str | None = None, step_size: str | None = None,
                         min_notional: str | None = None) -> pl.DataFrame:
    """从归档 manifest 推断生命周期（首个有数据分区的 key_min → 最后分区的 key_max）。只证明"至少这段在交易"。"""
    recs = [json.loads(p.read_text()) for p in sorted((lake.root / "_manifest").glob("*.json"))]
    recs = [r for r in recs if r.get("instrument_id") == inst and r.get("actual_rows")]
    if not recs:
        return pl.DataFrame(schema=RULES_SCHEMA)
    kmin = min(dt.datetime.fromisoformat(r["key_min"]) for r in recs)
    kmax = max(dt.datetime.fromisoformat(r["key_max"]) for r in recs)
    # 只证明"自 kmin 起在交易"；归档最后月份不能推出下线（可能只是没下载后续分区），effective_to 一律 None，下线证据走 exchangeInfo 快照
    del kmax
    return pl.DataFrame([{
        "instrument_id": inst, "effective_from": kmin, "effective_to": None,
        "tick_size": tick_size, "step_size": step_size, "min_notional": min_notional, "multiplier": "1",
        "funding_interval_hours": None, "status": "TRADING", "source": "manifests-inferred",
    }], schema=RULES_SCHEMA)


def write_rules(lake: LakePaths, rules: pl.DataFrame) -> None:
    for (inst,), part in rules.group_by("instrument_id"):
        atomic_write_parquet(rules_path(lake, inst), part.sort("effective_from"))


def load_rules(lake: LakePaths, inst: str) -> pl.DataFrame | None:
    p = rules_path(lake, inst)
    return pl.read_parquet(p) if p.exists() else None


def rule_at(rules: pl.DataFrame | None, inst: str, at: dt.datetime) -> dict | None:
    if rules is None or rules.height == 0:
        return None
    r = rules.filter((pl.col("instrument_id") == inst) & (pl.col("effective_from") <= at)
                     & (pl.col("effective_to").is_null() | (pl.col("effective_to") > at)))
    return r.sort("effective_from").tail(1).to_dicts()[0] if r.height else None


# ---------------------------------------------------------------------------
# 体检
# ---------------------------------------------------------------------------
@dataclass
class CheckReport:
    partition_id: str
    data_type: str
    interval: str
    instrument_id: str
    period: str
    rows_in: int
    rows_out: int
    expected_rows: int | None
    missing: int
    gaps: list[dict]
    duplicates: int
    reason_counts: dict = field(default_factory=dict)
    quarantine_n: int = 0
    schema_hash: str = ""
    rule_version: str = CHECK_RULE_VERSION
    status: str = "ok"          # ok | gap | quarantined
    notes: list[str] = field(default_factory=list)


def _q(pid, obj_id, obj_ver, reason, *, event_time=None, available_at=None, ingested_at=None, field_path=None,
       observed=None, expected=None, raw_hash=None, shash="", all_codes=None, batch_id="") -> dict:
    return {
        "quarantine_id": quarantine_id(pid, obj_id, obj_ver or (raw_hash or ""), CHECK_RULE_VERSION, reason, raw_hash),
        "batch_id": batch_id, "object_kind": "bar" if obj_id != pid else "partition", "object_id": obj_id,
        "object_version": obj_ver, "partition_id": pid, "raw_uri": None, "raw_hash": raw_hash,
        "event_time": event_time, "available_at": available_at, "ingested_at": ingested_at,
        "reason_code": reason, "all_reason_codes": all_codes or [reason], "severity": SEVERITY[reason],
        "field_path": field_path, "observed_value_ref": None if observed is None else str(observed),
        "expected_contract": expected, "rule_version": CHECK_RULE_VERSION, "schema_hash": shash, "status": "open",
    }


def spike_scores(close: pl.Series, gap_before: pl.Series, *, k: float = SPIKE_K, min_samples: int = SPIKE_MIN_SAMPLES) -> tuple[pl.Series, pl.Series]:
    """稳健 z：r=log(close/prev)，缺口后的首根 bar 不计收益（不跨洞）；s=1.4826·MAD(r)。样本不足 → 全 null/false。"""
    unknown = (gap_before.is_null() | close.is_null() | ~close.is_finite() | (close <= 0)).fill_null(True)
    r = (close / close.shift(1)).log()
    r = pl.select(pl.when(gap_before.fill_null(True) | unknown | r.is_null() | r.is_infinite()).then(None).otherwise(r)).to_series()
    valid = r.drop_nulls()
    if valid.len() < min_samples:
        return pl.Series([None] * close.len(), dtype=pl.Float64), unknown
    med = valid.median()
    mad = (valid - med).abs().median()
    s = 1.4826 * mad
    if s is None or s <= 0 or not math.isfinite(s):
        return pl.Series([None] * close.len(), dtype=pl.Float64), unknown
    score = ((r - med).abs() / s).cast(pl.Float64)
    flag = (score.fill_null(0.0) > k) | unknown
    return score, flag


def _on_grid(px: float, tick: Decimal) -> bool:
    if px is None or not math.isfinite(px):
        return False
    d = Decimal(str(px))
    return (d / tick) % 1 == 0


def check_bars(df: pl.DataFrame, *, pid: str, data_type: str, interval: str, inst: str, period: str,
               rules: pl.DataFrame | None, expected_schema_hash: str | None = None, batch_id: str = "") -> tuple[pl.DataFrame, list[dict], CheckReport]:
    key = "open_time"
    if key not in df.columns or df[key].is_null().any():
        raise ValueError("unknown bar time: cannot verify gap_flag; refusing partition")
    sec = INTERVAL_SECONDS[interval]
    step = dt.timedelta(seconds=sec)
    a, b = period_bounds(period)
    qs: list[dict] = []
    shash = schema_hash(df)
    rep = CheckReport(pid, data_type, interval, inst, period, df.height, df.height, None, 0, [], 0, schema_hash=shash)
    raw_hash = df["source_sha256"][0] if "source_sha256" in df.columns and df.height else None
    ver = df["rule_version"][0] if "rule_version" in df.columns and df.height else ""

    # 7. schema 漂移：整分区隔离
    if expected_schema_hash and expected_schema_hash != shash:
        qs.append(_q(pid, pid, ver, R_SCHEMA, observed=shash, expected=expected_schema_hash, raw_hash=raw_hash, shash=shash, batch_id=batch_id))
        rep.status, rep.reason_counts[R_SCHEMA] = "quarantined", 1
        rep.quarantine_n = 1
        return df, qs, rep

    # 2. 重复 / 非单调
    n_dup = df.height - df[key].n_unique()
    if n_dup:
        dup_rows = df.filter(df.select(key).is_duplicated())
        for r in dup_rows.unique(subset=[key]).to_dicts():
            qs.append(_q(pid, f"{inst}|{interval}|{r[key].isoformat()}", ver, R_KEY_DUP, event_time=r.get("close_time"),
                         available_at=r.get("available_at"), ingested_at=r.get("ingested_at"), field_path=key,
                         observed="duplicate", expected="unique (instrument_id, interval, open_time)", raw_hash=raw_hash, shash=shash, batch_id=batch_id))
        df = df.unique(subset=[key], keep="first", maintain_order=True)
    if not df[key].is_sorted():
        qs.append(_q(pid, pid, ver, R_KEY_DUP, field_path=key, observed="non-monotonic", expected="sorted", raw_hash=raw_hash, shash=shash, batch_id=batch_id))
        df = df.sort(key)
    rep.duplicates = n_dup

    # 6. 生命周期：规则表决定期望日历
    rule_start = rule_at(rules, inst, a)
    rule_end = rule_at(rules, inst, b - step)
    if rules is None or rules.height == 0:
        qs.append(_q(pid, pid, ver, R_RULE_MISSING, expected="instrument_rules 有效时段", raw_hash=raw_hash, shash=shash, batch_id=batch_id))
        cal_from, cal_to = a, b
        rep.notes.append("无规则表：日历按整月，精度不校验")
    else:
        rr = rules.filter(pl.col("instrument_id") == inst).sort("effective_from")
        if rr.height == 0:
            qs.append(_q(pid, pid, ver, R_RULE_MISSING, expected="instrument_rules 有效时段", raw_hash=raw_hash, shash=shash, batch_id=batch_id))
            cal_from, cal_to = a, b
        else:
            eff_from = rr["effective_from"].min()
            eff_to = rr["effective_to"].max() if rr["effective_to"].null_count() == 0 else None
            cal_from = max(a, eff_from)
            cal_to = b if eff_to is None else min(b, eff_to)
            # 上市前 / 下线后的行
            bad = df.filter((pl.col(key) < eff_from) | (pl.lit(eff_to is not None) & (pl.col(key) >= (eff_to or b))))
            for r in bad.to_dicts():
                qs.append(_q(pid, f"{inst}|{interval}|{r[key].isoformat()}", ver, R_SYMBOL_TIME, event_time=r["close_time"],
                             available_at=r.get("available_at"), ingested_at=r.get("ingested_at"), field_path=key,
                             observed=r[key].isoformat(), expected=f"[{eff_from}, {eff_to})", raw_hash=raw_hash, shash=shash, batch_id=batch_id))

    # 1. 完整性：期望网格 [cal_from, cal_to)
    # S29：改用单一来源 contract.grid_points_between（精确微秒、半开区间），此前自带一份整秒取整的网格数学
    exp_n = grid_points_between(cal_from, cal_to, sec)
    rep.expected_rows = exp_n
    legal = pl.Series("legal_grid", [cal_from <= t < cal_to and first_grid_point(t, sec) == t for t in df[key]], dtype=pl.Boolean)
    for r in df.filter(~legal).to_dicts():
        qs.append(_q(pid, f"{inst}|{interval}|{r[key].isoformat()}", ver, R_TIME_GRID,
                     event_time=r["close_time"], field_path=key, observed=r[key].isoformat(),
                     expected=f"UTC {sec}s 网格 ∩ [{cal_from}, {cal_to})", raw_hash=raw_hash, shash=shash, batch_id=batch_id))
    present = df.filter(legal)[key]
    rep.missing = exp_n - present.n_unique()
    assert rep.missing >= 0, "present 必须是期望网格的子集"
    df = df.filter(legal)
    gap_before = pl.Series([False] * df.height, dtype=pl.Boolean)
    if df.height:
        prev = df[key].shift(1)
        gap_before = pl.Series(
            [False] + [grid_points_between(prev[i] + step, df[key][i], sec) > 0
                       for i in range(1, df.height)], dtype=pl.Boolean,
        )
        # S29：首行 gap 必须以"确有缺失网格点"为准，不能只看首 bar 晚于日历起点（会出现 gap=True 而 n=0）
        first_gap = bool(df.height and grid_points_between(cal_from, df[key][0], sec) > 0)
        if first_gap:
            gap_before[0] = True
        # 缺口清单
        gaps = []
        if first_gap:
            n0 = grid_points_between(cal_from, df[key][0], sec)
            if n0 > 0:
                gaps.append({"from": cal_from.isoformat(), "to": df[key][0].isoformat(), "n": n0})
        for i in range(1, df.height):
            if gap_before[i]:
                ni = grid_points_between(prev[i] + step, df[key][i], sec)
                if ni > 0:
                    gaps.append({"from": (prev[i] + step).isoformat(), "to": df[key][i].isoformat(), "n": ni})
        if grid_points_between(df[key][-1] + step, cal_to, sec) > 0:
            nt = grid_points_between(df[key][-1] + step, cal_to, sec)
            if nt > 0:
                gaps.append({"from": (df[key][-1] + step).isoformat(), "to": cal_to.isoformat(), "n": nt})
        rep.gaps = gaps
    elif exp_n:
        rep.gaps = [{"from": cal_from.isoformat(), "to": cal_to.isoformat(), "n": exp_n}]
    assert sum(g["n"] for g in rep.gaps) == rep.missing, "缺口清单与 missing 必须一致"
    for g in rep.gaps:
        qs.append(_q(pid, f"{inst}|{interval}|gap|{g['from']}", ver, R_BAR_GAP, field_path=key, observed=json.dumps(g),
                     expected="连续 bar", raw_hash=raw_hash, shash=shash, batch_id=batch_id))
    df = df.with_columns(gap_before.alias("gap_flag"))

    # 3. OHLC 不变量
    ohlc_valid = ((pl.col("low") <= pl.min_horizontal("open", "close")) & (pl.max_horizontal("open", "close") <= pl.col("high"))
                  & (pl.col("low") > 0) & pl.col("open").is_finite() & pl.col("high").is_finite() & pl.col("low").is_finite()
                  & pl.col("close").is_finite() & pl.col("volume").is_finite() & (pl.col("volume") >= 0))
    df = df.with_columns(ohlc_valid.fill_null(False).alias("ohlc_valid"))
    for r in df.filter(~pl.col("ohlc_valid")).to_dicts():
        qs.append(_q(pid, f"{inst}|{interval}|{r[key].isoformat()}", ver, R_OHLC, event_time=r["close_time"], available_at=r.get("available_at"),
                     ingested_at=r.get("ingested_at"), field_path="open,high,low,close,volume",
                     observed=f"o={r['open']} h={r['high']} l={r['low']} c={r['close']} v={r['volume']}",
                     expected="low<=min(o,c)<=max(o,c)<=high, price>0 finite, volume>=0", raw_hash=raw_hash, shash=shash, batch_id=batch_id))

    # 4. 尖刺（只标不删）
    score, flag = spike_scores(df["close"], df["gap_flag"])
    df = df.with_columns(score.alias("spike_score"), flag.alias("spike_flag"))
    for r in df.filter(pl.col("spike_flag")).select([key, "close_time", "available_at", "ingested_at", "close", "spike_score"]).to_dicts():
        qs.append(_q(pid, f"{inst}|{interval}|{r[key].isoformat()}", ver, R_SPIKE, event_time=r["close_time"], available_at=r["available_at"],
                     ingested_at=r["ingested_at"], field_path="close", observed=f"close={r['close']} score={r['spike_score']}",
                     expected=f"|r|/(1.4826·MAD) <= {SPIKE_K}", raw_hash=raw_hash, shash=shash, batch_id=batch_id))

    # 6b. 精度：成交价（klines）必须落在 tick 网格；mark/index/premium 是计算价，精度不受 tick 约束
    rule = rule_start or rule_end
    if data_type == "klines" and rule and rule.get("tick_size"):
        tick = Decimal(rule["tick_size"])
        off = [i for i, (o, h, l, c) in enumerate(zip(df["open"], df["high"], df["low"], df["close"]))
               if not all(_on_grid(x, tick) for x in (o, h, l, c))]
        for i in off:
            r = df.row(i, named=True)
            qs.append(_q(pid, f"{inst}|{interval}|{r[key].isoformat()}", ver, R_PRECISION, event_time=r["close_time"], available_at=r.get("available_at"),
                         ingested_at=r.get("ingested_at"), field_path="open,high,low,close", observed=f"{r['open']},{r['high']},{r['low']},{r['close']}",
                         expected=f"tick_size={tick}", raw_hash=raw_hash, shash=shash, batch_id=batch_id))

    rep.rows_out = df.height
    rep.reason_counts = _count(qs)
    rep.quarantine_n = len(qs)
    fatal = any(q["severity"] in ("error", "fatal") for q in qs)
    rep.status = "quarantined" if fatal else ("gap" if rep.missing else "ok")
    return df, qs, rep


def check_funding(df: pl.DataFrame, *, pid: str, inst: str, period: str, batch_id: str = "") -> tuple[pl.DataFrame, list[dict], CheckReport]:
    """相邻结算间隔必须等于行内 funding_interval_hours（周期可变，不固定 8h）。"""
    qs: list[dict] = []
    shash = schema_hash(df)
    rep = CheckReport(pid, "fundingRate", "8h", inst, period, df.height, df.height, None, 0, [], 0, schema_hash=shash)
    raw_hash = df["source_sha256"][0] if "source_sha256" in df.columns and df.height else None
    n_dup = df.height - df["calc_time"].n_unique()
    if n_dup:
        qs.append(_q(pid, pid, "", R_KEY_DUP, field_path="calc_time", observed=f"{n_dup} duplicates", expected="unique calc_time", raw_hash=raw_hash, shash=shash, batch_id=batch_id))
        df = df.unique(subset=["calc_time"], keep="first", maintain_order=True)
    df = df.sort("calc_time")
    ct, ih = df["calc_time"], df["funding_interval_hours"]
    for i in range(1, df.height):
        gap = ct[i] - ct[i - 1]
        # 允许结算时刻有秒级抖动（归档 calc_time 偶有 +数秒），容差 60s
        if abs(gap - dt.timedelta(hours=ih[i])) > dt.timedelta(seconds=60):
            qs.append(_q(pid, f"{inst}|funding|{ct[i].isoformat()}", "", R_FUNDING, event_time=ct[i], available_at=ct[i], field_path="calc_time",
                         observed=f"gap={gap}", expected=f"funding_interval_hours={ih[i]}", raw_hash=raw_hash, shash=shash, batch_id=batch_id))
    rep.duplicates = n_dup
    rep.rows_out = df.height
    rep.reason_counts = _count(qs)
    rep.quarantine_n = len(qs)
    rep.status = "quarantined" if qs else "ok"
    return df, qs, rep


def _count(qs: list[dict]) -> dict:
    out: dict = {}
    for q in qs:
        out[q["reason_code"]] = out.get(q["reason_code"], 0) + 1
    return out


# ---------------------------------------------------------------------------
# quarantine 表（追加、幂等）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 分区级入口：读 silver → 体检 → 回写 mask、quarantine、manifest
# ---------------------------------------------------------------------------
def load_partition(lake: LakePaths, data_type: str, interval: str, symbol: str, period: str) -> pl.DataFrame:
    a, b = period_bounds(period)
    sdir = lake.silver_dir(data_type, interval, symbol)
    parts = []
    d = a.date()
    while d < b.date():
        p = sdir / f"date={d.isoformat()}" / "part.parquet"
        if p.exists():
            parts.append(pl.read_parquet(p))
        d += dt.timedelta(days=1)
    return pl.concat(parts, how="vertical") if parts else pl.DataFrame()


def check_partition(lake: LakePaths, *, data_type: str, interval: str, symbol: str, period: str,
                    rules: pl.DataFrame | None = None, write: bool = True, batch_id: str | None = None) -> CheckReport:
    from quant_lab.market.vision import normalize_interval, partition_id
    interval = normalize_interval(data_type, interval)
    pid = partition_id(data_type, interval, symbol, period)
    inst = instrument_id(symbol)
    batch_id = batch_id or f"check-{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}"
    df = load_partition(lake, data_type, interval, symbol, period)
    mpath = lake.manifest(pid)
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {}
    if df.height == 0:
        # 同族（S22 falsy 兜底）：expected_rows 未知时不得用 `or 0` 把 missing 填成 0——那是在宣称"一根不缺"。
        exp_rows = manifest.get("expected_rows")
        rep = CheckReport(pid, data_type, interval, inst, period, 0, 0, exp_rows, exp_rows if exp_rows is not None else None,
                          [], 0, status="gap")
        rep.notes.append("分区无数据")
        return rep
    rules = rules if rules is not None else load_rules(lake, inst)
    if data_type in BAR_TYPES:
        out, qs, rep = check_bars(df, pid=pid, data_type=data_type, interval=interval, inst=inst, period=period, rules=rules,
                                  expected_schema_hash=manifest.get("schema_hash"), batch_id=batch_id)
    elif data_type == "fundingRate":
        out, qs, rep = check_funding(df, pid=pid, inst=inst, period=period, batch_id=batch_id)
    else:
        return CheckReport(pid, data_type, interval, inst, period, df.height, df.height, None, 0, [], 0, notes=["metrics 体检未实现（M-11）"])
    if write:
        if data_type in BAR_TYPES and rep.status != "quarantined" or data_type in BAR_TYPES and R_SCHEMA not in rep.reason_counts:
            sdir = lake.silver_dir(data_type, interval, symbol)
            for (day,), part in out.with_columns(pl.col(KEY_COL[data_type]).dt.date().alias("_d")).group_by("_d", maintain_order=True):
                atomic_write_parquet(sdir / f"date={day.isoformat()}" / "part.parquet", part.drop("_d"))
        append_quarantine(lake, qs)
        if manifest:
            manifest.update({"quarantine_n": rep.quarantine_n, "missing": rep.missing, "check_status": rep.status,
                             "check_rule_version": CHECK_RULE_VERSION, "check_reason_counts": rep.reason_counts,
                             "checked_at": dt.datetime.now(dt.UTC).isoformat(), "gaps": rep.gaps[:200]})
            atomic_write_json(mpath, manifest)
    return rep


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="quant_lab.market.partition_check")
    p.add_argument("--symbol", required=True); p.add_argument("--type", required=True); p.add_argument("--interval")
    p.add_argument("--month", required=True); p.add_argument("--lake"); p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    lake = LakePaths(Path(a.lake)) if a.lake else LakePaths.default()
    rep = check_partition(lake, data_type=a.type, interval=a.interval, symbol=a.symbol, period=a.month, write=not a.dry_run)
    d = asdict(rep); d["gaps"] = d["gaps"][:20]
    print(json.dumps(d, ensure_ascii=False, default=str))
    return 0 if rep.status != "quarantined" else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
