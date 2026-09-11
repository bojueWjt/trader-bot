"""层 4 规范化 + 层 5 行情校验（合并稿 C.2 Telegram 4/5；ADR-G1 §2.3）。

层 4（canonicalize）：品种当时身份（InstrumentRegistry，按 t_a）、方向/价格/档位（TP 按方向排序；SL/TP 方向一致性只标不改）、
百分比 TP 转价（有入场参考才转，记 checks）、H1 版本原始入场隔离（EDIT_ORIGINAL_UNAVAILABLE，eligibility original_entry=false）。
层 5（market_check）：t_a = 字段闭包 available_at；m = 最后一根已闭合 1m mark（陈旧 >120s → MARK_STALE，拒价格校验）；
δ = |ln(p/m)| 区间两端分别记、取最近端为告警指标；数量级门 δ ≥ ln3 → UNIT_SCALE_CONFLICT（致命，不自动乘除）；
合理性带 δ > T_plaus（每频道×订单类型冻结 p99，无校准 → insufficient，不猜阈值）→ ENTRY_MARK_DEVIATION（一般，待复核）；
SL/TP 相对入场参考的数量级门同样适用。只标异常不改价。
输出 silver/canonical_plan.parquet：每条 extracted_event 一行（plan_id 确定性）。
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
import math
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from .lake import D12, q12, LayerLedger, Layout, append_quarantine, cum_prev, loss_row_from_ledger, mapping_rows, now_utc, preserve_ingested_at, quarantine_row, schema_hash, stable_id, write_loss, write_mapping, write_parquet_atomic
from .market_stub import InstrumentRegistry, MarkProvider, PlausibilityCalibration, fixture_marks, fixture_registry, log_deviation
from .reasons import Reason

RULE_VERSION = "tg45-validate-v0.5"
LN3 = math.log(3.0)
MAX_STALENESS_S = 120
SIGNAL_KINDS = {"entry_proposal", "amend", "add", "stop_move", "tp_ladder", "reduce", "close_claimed", "entry_claimed", "cancel", "expire", "correction", "delete_notice"}

CANONICAL_PLAN_SCHEMA: dict[str, Any] = {
    "plan_id": pl.String,
    "branch_index": pl.Int32,
    "extract_id": pl.String,
    "source_version_id": pl.String,
    "channel_id": pl.Int64,
    "message_id": pl.Int64,
    "extractor_name": pl.String,
    "kind": pl.String,
    "symbol_raw": pl.String,
    "instrument_id": pl.String,
    "instrument_status": pl.String,
    "side": pl.String,
    "entry": pl.Struct({"lo": D12, "hi": D12, "kind": pl.String}),
    "entries": pl.List(D12),
    "entry_ref": D12,
    "stop": D12,
    "tps": pl.List(pl.Struct({"level": D12, "fraction": D12, "kind": pl.String})),
    "size_hint": pl.Struct({"qty": D12, "fraction": D12, "leverage": pl.Int64, "notional": D12}),
    "expires_after_s": pl.Int64,
    "time_grade": pl.String,
    "t_a": pl.Datetime("us", "UTC"),
    "mark_price": D12,
    "mark_close_time": pl.Datetime("us", "UTC"),
    "mark_staleness_s": pl.Float64,
    "market_manifest": pl.String,
    "delta_lo": pl.Float64,
    "delta_hi": pl.Float64,
    "delta_near": pl.Float64,
    "delta_stop": pl.Float64,
    "scale_gate": pl.String,  # ok | conflict | unavailable | n/a
    "plausibility_status": pl.String,  # ok | deviation | insufficient | unavailable | n/a
    "t_plaus": pl.Float64,
    "calibration_version": pl.String,
    "entry_mode": pl.String,
    "direction_ok": pl.Boolean,
    "checks": pl.String,  # json
    "reason_codes": pl.List(pl.String),
    "eligibility_by_estimand": pl.String,  # json
    "registry_version": pl.String,
    "rule_version": pl.String,
    "batch_id": pl.String,
    "event_time": pl.Datetime("us", "UTC"),
    "available_at": pl.Datetime("us", "UTC"),
    "ingested_at": pl.Datetime("us", "UTC"),
}
SCHEMA_HASH = schema_hash(CANONICAL_PLAN_SCHEMA)


def order_kind(entry: dict | None) -> str:
    if not entry:
        return "market"
    return entry.get("kind") or "limit"


def canonicalize_row(r: dict[str, Any], *, registry: InstrumentRegistry) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """层 4：返回 (规范字段, reason_codes, checks)。不改价、不补缺。"""
    reasons: list[str] = []
    checks: dict[str, Any] = {}
    t_a = r["available_at"]
    inst, status = registry.resolve(r["symbol_raw"], t_a)
    if status == "invalid_time":
        reasons.append(Reason.SYMBOL_TIME_INVALID)
    checks["instrument"] = status
    side = r["side"]
    entry = dict(r["entry"]) if r["entry"] else None
    if entry:
        entry["lo"], entry["hi"] = Decimal(str(entry["lo"])), Decimal(str(entry["hi"]))
    entries = [Decimal(str(v)) for v in (r["entries"] or [])]
    tps = [{**dict(t), "level": Decimal(str(t["level"])) if t.get("level") is not None else None, "fraction": Decimal(str(t["fraction"])) if t.get("fraction") is not None else None} for t in (r["tps"] or [])]
    stop = Decimal(str(r["stop"])) if r["stop"] is not None else None
    # 入场参考：区间中点 / 单价 / 梯首档；市价无参考
    entry_ref = None
    if entry and entry.get("lo") is not None and entry.get("hi") is not None:
        entry_ref = (entry["lo"] + entry["hi"]) / 2 if entry["kind"] == "zone" else (entries[0] if entries else entry["lo"])
    # 百分比 TP → 价（只有入场参考时；记 checks）
    conv = 0
    for t in tps:
        if t.get("kind") == "pct" and entry_ref and side in ("long", "short"):
            pct = t["level"] / 100
            t["level"] = entry_ref * (1 + pct) if side == "long" else entry_ref * (1 - pct)
            t["kind"] = "price"
            conv += 1
    if conv:
        checks["tp_pct_converted"] = conv
    # 档位按方向排序（long 升序 / short 降序）；方向一致性只标不改
    direction_ok = True
    if side == "long":
        tps.sort(key=lambda t: t["level"])
    elif side == "short":
        tps.sort(key=lambda t: -t["level"])
    if entry_ref and side in ("long", "short"):
        if stop is not None:
            ok = stop < entry_ref if side == "long" else stop > entry_ref
            checks["sl_direction"] = ok
            direction_ok &= ok
        if tps:
            ok = all(t["level"] > entry_ref for t in tps) if side == "long" else all(t["level"] < entry_ref for t in tps)
            checks["tp_direction"] = ok
            direction_ok &= ok
    if not direction_ok:
        reasons.append(Reason.INTENT_AMBIGUOUS)
        checks["direction_conflict"] = True  # 方向错=致命类语义（契约 §4），用途准入见下
    # H1：仅最终编辑版可见 → 原始入场隔离
    elig = {"original_entry": True, "price_check": True, "execution": True, "description": True}
    if r["time_grade"] == "H1" and r["kind"] in ("entry_proposal", "amend"):
        reasons.append(Reason.EDIT_ORIGINAL_UNAVAILABLE)
        elig["original_entry"] = False
    if r["time_grade"] in ("U", "H2"):
        elig["execution"] = False
        elig["price_check"] = False
    if status != "mapped" or not direction_ok:
        elig["execution"] = False
    if r.get("entry_mode") == "unknown" and r["kind"] == "entry_proposal":
        elig["execution"] = False  # 入场方式无原文依据：不猜市价（S06）
        checks["entry_mode_unknown"] = True
    out = {
        "instrument_id": inst, "instrument_status": status, "side": side, "entry": entry, "entries": entries, "entry_ref": entry_ref,
        "stop": stop, "tps": tps, "direction_ok": direction_ok,
    }
    return out, reasons, checks | {"eligibility": elig}


def market_check_row(c: dict[str, Any], *, t_a: datetime | None, marks: MarkProvider, t_plaus: PlausibilityCalibration | dict | None, channel_id: int) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """层 5：数量级门 / 合理性带 / MARK_STALE。只标不改。数量级门对区间两端逐一检查（A04：任一端 ≥ ln3 即冲突）。"""
    reasons: list[str] = []
    out: dict[str, Any] = {"mark_price": None, "mark_close_time": None, "mark_staleness_s": None, "delta_lo": None, "delta_hi": None, "delta_near": None,
                           "delta_stop": None, "scale_gate": "n/a", "plausibility_status": "n/a", "t_plaus": None, "calibration_version": None, "market_manifest": marks.manifest}
    if isinstance(t_plaus, dict):  # 裸字典视为未冻结的临时阈值：无版本/冻结时刻 → 不可用
        t_plaus = None if not t_plaus else PlausibilityCalibration(dict(t_plaus), version="adhoc-unfrozen", frozen_at=datetime.max.replace(tzinfo=t_a.tzinfo) if t_a else datetime.max)
    checks: dict[str, Any] = {"dependencies": []}
    inst = c["instrument_id"]
    has_price = c["entry"] is not None or c["stop"] is not None
    if inst is None or t_a is None or not has_price:
        return out, reasons, checks
    m = marks.mark_at(inst, t_a, max_staleness_s=MAX_STALENESS_S)
    out["mark_close_time"], out["mark_staleness_s"] = m.close_time, m.staleness_s
    if m.price is None:
        reasons.append(Reason.MARK_STALE)
        out["scale_gate"] = out["plausibility_status"] = "unavailable"
        checks["mark"] = m.reason
        return out, reasons, checks
    mp = Decimal(str(m.price))
    out["mark_price"] = mp
    latency = getattr(marks, "latency", timedelta(0))
    mark_available = m.close_time + latency
    if hasattr(marks, "marks"):
        evidence = marks.marks.filter((pl.col("instrument_id") == inst) & (pl.col("close_time") == m.close_time) & (pl.col("available_at") <= t_a))
        if evidence.height:
            mark_available = max(mark_available, evidence["available_at"].max())
    checks["dependencies"].append({"ref": f"mark:{marks.manifest}:{inst}:{m.close_time.isoformat()}", "purpose": "price_check", "available_at": mark_available.isoformat(), "close_time": m.close_time.isoformat(), "latency_s": latency.total_seconds()})
    e = c["entry"]
    if e and e.get("lo") is not None:
        dlo, dhi = log_deviation(e["lo"], mp), log_deviation(e["hi"], mp)
        out["delta_lo"], out["delta_hi"] = dlo, dhi
        out["delta_near"] = min(x for x in (dlo, dhi) if x is not None)
    ref = c["entry_ref"] if c["entry_ref"] else mp  # 市价单：SL/TP 相对 mark
    if c["stop"] is not None:
        out["delta_stop"] = log_deviation(c["stop"], ref)
    # 数量级门（致命）
    conflict = False
    lo_bad, hi_bad = (out["delta_lo"] is not None and out["delta_lo"] >= LN3), (out["delta_hi"] is not None and out["delta_hi"] >= LN3)
    if lo_bad or hi_bad:
        conflict = True
        checks["scale_conflict_field"] = "entry"
        near_is_lo = out["delta_lo"] is not None and out["delta_hi"] is not None and out["delta_lo"] <= out["delta_hi"]
        near_bad, far_bad = (lo_bad if near_is_lo else hi_bad), (hi_bad if near_is_lo else lo_bad)
        checks["scale_basis"] = "both_ends" if (near_bad and far_bad) else ("near_end_only" if near_bad else "far_end_only")  # A10
        checks["scale_conflict_end"] = "entries[0].price_lo" if lo_bad else "entries[0].price_hi"
    if out["delta_stop"] is not None and out["delta_stop"] >= LN3:
        conflict = True
        checks.setdefault("scale_conflict_field", "stop")
    for t in c["tps"]:
        d = log_deviation(t["level"], ref)
        if d is not None and d >= LN3:
            conflict = True
            checks.setdefault("scale_conflict_field", "tps")
            break
    out["scale_gate"] = "conflict" if conflict else "ok"
    if conflict:
        reasons.append(Reason.UNIT_SCALE_CONFLICT)
    # 合理性带（一般）：仅对有入场价的计划；阈值按 (channel, order_kind) 冻结
    if out["delta_near"] is not None and not conflict:
        th, why = (None, "no_frozen_T_plaus") if t_plaus is None else t_plaus.lookup(channel_id, order_kind(e), t_a)
        if th is None:
            out["plausibility_status"] = "insufficient"
            checks["plausibility"] = why
        else:
            out["t_plaus"] = th
            out["calibration_version"] = t_plaus.version
            checks["dependencies"].append({"ref": f"calibration:{t_plaus.version}", "purpose": "price_check", "available_at": t_plaus.frozen_at.isoformat()})
            checks["plausibility"] = why
            if out["delta_near"] > th:
                out["plausibility_status"] = "deviation"
                reasons.append(Reason.ENTRY_MARK_DEVIATION)
            else:
                out["plausibility_status"] = "ok"
    return out, reasons, checks


def validate_frame(ex: pl.DataFrame, mv: pl.DataFrame, *, registry: InstrumentRegistry, marks: MarkProvider, t_plaus: PlausibilityCalibration | None = None,
                   ingested_at: datetime | None = None) -> tuple[pl.DataFrame, list[dict], dict[tuple[int, str], LayerLedger], dict[str, Any]]:
    ingested_at = ingested_at or now_utc()
    batch_id = ex["batch_id"][0] if ex.height else "tg-empty"
    frame = ex.join(mv.select("source_version_id", "time_grade", pl.col("message_date").dt.year().cast(pl.String).fill_null("unknown").alias("_year")), on="source_version_id", how="left")
    rows: list[dict[str, Any]] = []
    qrows: list[dict[str, Any]] = []
    for r in frame.iter_rows(named=True):
        if r["kind"] not in SIGNAL_KINDS:
            continue
        can, reasons4, checks = canonicalize_row(r, registry=registry)
        checks.update(json.loads(r.get("checks") or "{}"))
        mk, reasons5, checks5 = market_check_row(can, t_a=r["available_at"], marks=marks, t_plaus=t_plaus, channel_id=r["channel_id"])
        inherited = [c for c in (r["reason_codes"] or []) if c != Reason.NOT_SIGNAL]  # 层 1/3 拒因跨层携带（S07）
        reasons = sorted(set(reasons4 + reasons5 + inherited))
        elig = checks.pop("eligibility")
        if any(c in inherited for c in ("TEXT_IMAGE_CONFLICT", "INTENT_AMBIGUOUS", "OCR_UNREADABLE", "TIME_UNIT_INVALID", "VERSION_TIME_UNKNOWN", "RAW_HASH_MISMATCH", "SCHEMA_DRIFT", "KEY_DUPLICATE_OR_ORDER", "MEDIA_MISSING")):
            elig["execution"] = False  # 层 1/3 拒因按用途限制消费（S07）
            if any(c in inherited for c in ("RAW_HASH_MISMATCH", "SCHEMA_DRIFT", "KEY_DUPLICATE_OR_ORDER", "TIME_UNIT_INVALID")):
                elig["price_check"] = False
        if r["available_at"] is None:
            elig["execution"] = False
            elig["price_check"] = False
        if Reason.MARK_STALE in reasons or Reason.UNIT_SCALE_CONFLICT in reasons:
            elig["price_check"] = False
        if Reason.UNIT_SCALE_CONFLICT in reasons or Reason.SYMBOL_TIME_INVALID in reasons:
            elig["execution"] = False
        deps = list(checks.get("dependencies", [])) + list(checks5.get("dependencies", []))
        matching_rules = [rule for rule in registry.rules if rule.instrument_id == can["instrument_id"] and r["available_at"] is not None and rule.effective_from <= r["available_at"] and (rule.effective_to is None or r["available_at"] < rule.effective_to)]
        if matching_rules:
            deps.append({"ref": f"registry:{registry.version}:{can['instrument_id']}", "purpose": "all", "available_at": matching_rules[0].effective_from.isoformat()})
        checks5["dependencies"] = deps
        row = {
            "plan_id": stable_id("plan", r["extract_id"], RULE_VERSION, registry.version, marks.manifest),
            "branch_index": r.get("branch_index", 0),
            "extract_id": r["extract_id"], "source_version_id": r["source_version_id"], "channel_id": r["channel_id"], "message_id": r["message_id"],
            "extractor_name": r["extractor"]["name"], "kind": r["kind"], "symbol_raw": r["symbol_raw"], **can,
            "size_hint": {k: (Decimal(str(v)) if k != "leverage" and v is not None else v) for k, v in r["size_hint"].items()} if r["size_hint"] else None, "expires_after_s": r["expires_after_s"], "time_grade": r["time_grade"], "t_a": r["available_at"], **mk, "entry_mode": r.get("entry_mode"),
            "checks": json.dumps(checks | checks5, ensure_ascii=False, sort_keys=True), "reason_codes": reasons,
            "eligibility_by_estimand": json.dumps(elig, sort_keys=True), "registry_version": registry.version, "rule_version": RULE_VERSION, "batch_id": batch_id,
            "event_time": r["event_time"], "available_at": r["available_at"], "ingested_at": ingested_at, "_year": r["_year"],
        }
        rows.append(row)
        if reasons:
            fp = ("order_plan." + checks5.get("scale_conflict_end", "entries[0].price_lo")) if Reason.UNIT_SCALE_CONFLICT in reasons else ("entry" if Reason.ENTRY_MARK_DEVIATION in reasons else ("instrument_id" if Reason.SYMBOL_TIME_INVALID in reasons else "mark"))
            qrows.append(
                quarantine_row(
                    batch_id=batch_id, object_kind="canonical_plan", object_id=row["plan_id"], object_version=r["source_version_id"], partition_id=f"channel={r['channel_id']}",
                    reason_codes=reasons, rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH, source_refs={"extract_id": r["extract_id"], "message_id": r["message_id"], "channel_id": r["channel_id"]},
                    event_time=r["event_time"], available_at=r["available_at"], ingested_at=ingested_at, field_path=fp,
                    observed_value_ref=json.dumps({"entry": can["entry"], "stop": can["stop"], "mark": mk["mark_price"], "delta_near": mk["delta_near"], "basis": checks5.get("scale_basis")}, default=str),
                    expected_contract="合并稿 C.2 Telegram 4/5", dependency_refs=[f"market:{marks.manifest}"] if mk["mark_price"] is not None else [],
                    severity_override="fatal" if not can["direction_ok"] else None,  # 方向错=致命（契约 §4），无专属码用覆盖
                )
            )
    year_of = {r["plan_id"]: r.pop("_year") for r in rows}

    def decimal_value(value):
        if value is None:
            return None
        return Decimal(q12(value))

    for row in rows:
        if row["entry"]:
            row["entry"] = {**row["entry"], "lo": decimal_value(row["entry"]["lo"]), "hi": decimal_value(row["entry"]["hi"])}
        row["entries"] = [decimal_value(v) for v in row["entries"]]
        for field in ("entry_ref", "stop", "mark_price"):
            row[field] = decimal_value(row[field])
        row["tps"] = [{**tp, "level": decimal_value(tp["level"]), "fraction": decimal_value(tp["fraction"])} for tp in row["tps"]]
        if row["size_hint"]:
            row["size_hint"] = {k: decimal_value(v) if k != "leverage" else v for k, v in row["size_hint"].items()}
    df = pl.DataFrame(rows, schema=CANONICAL_PLAN_SCHEMA) if rows else pl.DataFrame(schema=CANONICAL_PLAN_SCHEMA)
    ledgers: dict[tuple[int, str], LayerLedger] = {}
    for r in rows:
        y = year_of[r["plan_id"]]
        l4 = ledgers.setdefault((r["channel_id"], y, 4), LayerLedger(4, {"channel_id": r["channel_id"], "year": y}, "extracted_event", "canonical_plan"))
        rc = r["reason_codes"]
        st4 = "quarantine" if "SYMBOL_TIME_INVALID" in rc else ("review" if (not r["direction_ok"] or "EDIT_ORIGINAL_UNAVAILABLE" in rc) else "ok")
        l4.mark(r["extract_id"], st4, [c for c in rc if c in ("SYMBOL_TIME_INVALID", "EDIT_ORIGINAL_UNAVAILABLE", "INTENT_AMBIGUOUS")])
        l4.map(r["extract_id"], r["plan_id"], "one_to_one" if st4 != "quarantine" else "excluded", [c for c in rc if c == "SYMBOL_TIME_INVALID"])
        if r["scale_gate"] != "n/a":
            l5 = ledgers.setdefault((r["channel_id"], y, 5), LayerLedger(5, {"channel_id": r["channel_id"], "year": y}, "canonical_plan", "price_checked_plan"))
            st5 = "quarantine" if r["scale_gate"] == "conflict" else ("review" if r["scale_gate"] == "unavailable" or r["plausibility_status"] == "deviation" else "ok")
            basis = json.loads(r["checks"]).get("scale_basis")
            l5.mark(r["plan_id"], st5, [(c + f":{basis}" if c == "UNIT_SCALE_CONFLICT" and basis else c) for c in rc if c in ("UNIT_SCALE_CONFLICT", "ENTRY_MARK_DEVIATION", "MARK_STALE")])
            l5.map(r["plan_id"], r["plan_id"] if st5 != "quarantine" else None, "one_to_one" if st5 != "quarantine" else "excluded", [c for c in rc if c == "UNIT_SCALE_CONFLICT"])
    # Account for every physical upstream object, including excluded non-signals.
    emitted = {r["extract_id"] for r in rows}
    for r in frame.iter_rows(named=True):
        if r["extract_id"] in emitted:
            continue
        y = r["_year"]
        led = ledgers.setdefault((r["channel_id"], y, 4), LayerLedger(4, {"channel_id": r["channel_id"], "year": y}, "extracted_event", "canonical_plan"))
        led.mark(r["extract_id"], "ok", [Reason.NOT_SIGNAL])
        led.map(r["extract_id"], None, "excluded", [Reason.NOT_SIGNAL])
    for r in rows:
        if r["scale_gate"] != "n/a":
            continue
        y = year_of[r["plan_id"]]
        led = ledgers.setdefault((r["channel_id"], y, 5), LayerLedger(5, {"channel_id": r["channel_id"], "year": y}, "canonical_plan", "price_checked_plan"))
        reason = Reason.NOT_SIGNAL
        if r["kind"] == "entry_proposal":
            reason = Reason.DEPENDENCY_NOT_AVAILABLE
        led.mark(r["plan_id"], "review", [reason])
        led.map(r["plan_id"], None, "excluded", [reason])
    summary = {
        "batch_id": batch_id, "n_plans": df.height,
        "instrument_status": {k: v for k, v in sorted(df.group_by("instrument_status").len().iter_rows())} if df.height else {},
        "scale_gate": {k: v for k, v in sorted(df.group_by("scale_gate").len().iter_rows())} if df.height else {},
        "plausibility": {k: v for k, v in sorted(df.group_by("plausibility_status").len().iter_rows())} if df.height else {},
        "n_quarantine": len(qrows), "rule_version": RULE_VERSION, "registry_version": registry.version, "market_manifest": marks.manifest,
    }
    return df, qrows, ledgers, summary


def run(layout: Layout, *, registry: InstrumentRegistry | None = None, marks: MarkProvider | None = None, t_plaus: PlausibilityCalibration | None = None,
        ingested_at: datetime | None = None, synthetic: bool = False) -> dict[str, Any]:
    """真实导出必须显式传入行情 provider 与品种登记；合成桩只在 synthetic=True（夹具模式）下允许（S04）。"""
    if marks is None or registry is None:
        if not synthetic:
            raise ValueError("validate.run 需要显式 marks/registry；合成行情桩与合成品种登记只允许 synthetic=True 的夹具模式")
        marks = marks or fixture_marks()
        registry = registry or fixture_registry()
    ingested_at = ingested_at or now_utc()
    ex = pl.read_parquet(layout.extracted_event)
    mv = pl.read_parquet(layout.message_version)
    df, qrows, ledgers, summary = validate_frame(ex, mv, registry=registry, marks=marks, t_plaus=t_plaus, ingested_at=ingested_at)
    layout.ensure()
    old = pl.read_parquet(layout.canonical_plan) if layout.canonical_plan.exists() else None
    write_parquet_atomic(preserve_ingested_at(df, old, "plan_id"), layout.canonical_plan)
    append_quarantine(layout.quarantine_path, qrows)
    batch_id = summary["batch_id"]
    lrows, maps4, maps5 = [], [], []
    cum4: dict[str, set[str]] = {}
    for key in sorted(k for k in ledgers if k[2] == 4):
        led = ledgers[key]
        row, cum = loss_row_from_ledger(led, batch_id=batch_id, cum_excluded_prev=cum_prev(layout, batch_id, (1, 2, 3), led.stratum), rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH, clocks=(df["event_time"].max() if df.height else None, df["available_at"].max() if df.height else None, ingested_at))
        lrows.append(row)
        cum4[json.dumps(led.stratum, sort_keys=True, ensure_ascii=False)] = cum
        maps4 += mapping_rows(led, batch_id=batch_id, rule_version=RULE_VERSION, ingested_at=ingested_at)
    for key in sorted(k for k in ledgers if k[2] == 5):
        led = ledgers[key]
        row, _ = loss_row_from_ledger(led, batch_id=batch_id, cum_excluded_prev=cum4.get(json.dumps(led.stratum, sort_keys=True, ensure_ascii=False), set()), rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH, clocks=(df["event_time"].max() if df.height else None, df["available_at"].max() if df.height else None, ingested_at))
        lrows.append(row)
        maps5 += mapping_rows(ledgers[key], batch_id=batch_id, rule_version=RULE_VERSION, ingested_at=ingested_at)
    write_loss(layout.loss(batch_id), lrows, replace_layers={4, 5})
    write_mapping(layout.mapping(batch_id, 4), maps4)
    write_mapping(layout.mapping(batch_id, 5), maps5)
    summary["paths"] = {"canonical_plan": str(layout.canonical_plan)}
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="层 4/5 规范化 + 行情校验（CLI 只跑夹具模式：合成行情桩与合成品种登记）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out")
    g.add_argument("--lake-root")
    a = ap.parse_args(argv)
    layout = Layout.flat(a.out) if a.out else Layout.from_root(a.lake_root)
    print(json.dumps(run(layout, synthetic=True), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
