"""层 B5 标注与分级放行工具（合并稿 B.3 / B.4 / B.5；ADR-G1 §12）。

- OC 曲线：二项接受概率 P_accept(n,c;p)=Σ_{k≤c} C(n,k)p^k(1-p)^(n-k)；有限批用超几何；Wilson 双侧区间（z=1.96）。
- 分级放行：致命零容忍（抽到一条即停整批）；一般类 AQL=0.5%/RQL=3%、n=200、c=3 一次判定；n<200 全审；每版本一次正式验收，修复后最多一次重验。
- 全窗口抽样框：全部频道 × UTC 6h 半开窗口，每频道简单随机 30 窗（π=min(1,30/W_c)），正负窗口都可抽；π 加权召回
  recall=Σ TP/π ÷ Σ true/π，区间按频道内窗口簇重采样；无真实机会 insufficient。
- 金标分歧率报告：关键字段 / 整 episode / 未决 三门（默认 2% / 5% / 5%），先报原始分歧再仲裁，不用仲裁后零分歧遮盖。
- Label Studio：episode 模板（Repeater 逐事件）与任务导出（不展示模型预测与未来走势）。
- 数据集元数据：audit_plan_version, batch_id, frame_hash, n, c, seed, error_count_by_severity, p_hat, interval_method, interval,
  acceptance_result, attempt_history, review_hours, excluded_layers, low_confidence_rule。
用户尚未批准 200/3 的误收风险：本模块只提供计算与合成验证，不启动真实批次。
"""
from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

import polars as pl

from .lake import stable_id

AUDIT_PLAN_VERSION = "audit-plan-v0.1"
DEFAULT_GATES = {"key_field": 0.02, "whole_episode": 0.05, "unresolved": 0.05}
Z95 = 1.96  # 合并稿 B.4 冻结 z=1.96（非 1.959964），数值逐位复现其表
WINDOW_H = 6
WINDOWS_PER_CHANNEL = 30


# ---------------------------------------------------------------- 概率
def binom_accept(n: int, c: int, p: float) -> float:
    return sum(math.comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(0, c + 1))


def hypergeom_accept(N: int, D: int, n: int, c: int) -> float:
    """有限批：批量 N、缺陷 D、抽 n、允许 c。"""
    tot = math.comb(N, n)
    return sum(math.comb(D, k) * math.comb(N - D, n - k) for k in range(0, min(c, D) + 1)) / tot


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n <= 0:
        raise ValueError("n 必须 > 0")
    ph = k / n
    den = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / den
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


OC_P = (0.001, 0.005, 0.01, 0.03, 0.05)


def oc_table(plans: Iterable[tuple[int, int]], ps: Iterable[float] = OC_P) -> list[dict[str, Any]]:
    rows = []
    for p in ps:
        row: dict[str, Any] = {"p": p}
        for n, c in plans:
            row[f"{n}/{c}"] = binom_accept(n, c, p)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- 分级放行
@dataclass
class AcceptancePlan:
    n: int = 200
    c: int = 3
    aql: float = 0.005
    rql: float = 0.03
    max_formal_attempts: int = 2  # 一次正式 + 修复后一次重验


@dataclass
class AcceptanceResult:
    result: str  # pass | fail | insufficient | full_audit_required
    n: int
    errors_general: int
    errors_fatal: int
    p_hat: float | None
    interval: tuple[float, float] | None
    interval_method: str
    reason: str
    attempt: int


def _calculate_acceptance(*, n_sampled: int, errors_general: int, errors_fatal: int, plan: AcceptancePlan = AcceptancePlan(), attempt: int = 1, batch_size: int | None = None,
                        sample_ids_hash: str | None = None, reviewer_ids: tuple[str, ...] = (), producer_ids: tuple[str, ...] = ()) -> AcceptanceResult:
    """一次随机抽满后只判一次；致命零容忍；批量 < n 全审；重验最多一次；缺固定样本身份或缺独立复核人 → not_run（S14）。"""
    if not sample_ids_hash:
        return AcceptanceResult("not_run", n_sampled, errors_general, errors_fatal, None, None, "none", "缺固定抽样身份（sample_ids_hash）：不能出 pass", attempt)
    if not [r for r in reviewer_ids if r] or set(reviewer_ids) & set(producer_ids):
        return AcceptanceResult("not_run", n_sampled, errors_general, errors_fatal, None, None, "none", "缺独立复核人（或复核人=产线作者）：真值门 not_run", attempt)
    if attempt < 1:
        return AcceptanceResult("insufficient", n_sampled, errors_general, errors_fatal, None, None, "none", "attempt 必须 ≥1（正式验收记录）", attempt)
    if attempt > plan.max_formal_attempts:
        return AcceptanceResult("insufficient", n_sampled, errors_general, errors_fatal, None, None, "none", "超过正式验收次数上限（一次正式+一次重验）", attempt)
    if errors_fatal > 0:
        return AcceptanceResult("fail", n_sampled, errors_general, errors_fatal, errors_general / n_sampled if n_sampled else None, None, "none", "致命类零容忍：停整批、隔离关联版本、查根因", attempt)
    if batch_size is not None and batch_size < plan.n:
        return AcceptanceResult("full_audit_required", n_sampled, errors_general, errors_fatal, None, None, "none", f"批量 {batch_size} < {plan.n}：全审，修正/隔离所有已见错，只对全审记录放行", attempt)
    if n_sampled != plan.n:
        return AcceptanceResult("insufficient", n_sampled, errors_general, errors_fatal, None, None, "none", f"抽样 {n_sampled} ≠ 冻结 n={plan.n}（一次随机抽满后只判一次）", attempt)
    lo, hi = wilson(errors_general, n_sampled)
    ok = errors_general <= plan.c
    return AcceptanceResult("pass" if ok else "fail", n_sampled, errors_general, errors_fatal, errors_general / n_sampled, (lo, hi), "wilson_95_two_sided",
                            f"一般错 {errors_general} {'≤' if ok else '>'} c={plan.c}；接受后仍记录非零错误估计，不宣称总体≤1%", attempt)


# Formal acceptance is an append-only audit stream; calculator calls without a batch never sign records.
ACCEPTANCE_SCHEMA = {
    "graph_version": pl.String, "input_hash": pl.String,
    "repair_evidence_refs": pl.List(pl.String), "repair_evidence_hash": pl.String,
    "labels": pl.String, "previous_record_hash": pl.String,
    "audit_plan_version": pl.String, "batch_id": pl.String, "sample_ids_hash": pl.String,
    "n": pl.Int64, "c": pl.Int64, "seed": pl.Int64, "attempt": pl.Int64, "result": pl.String,
    "errors_by_severity": pl.Struct({"general": pl.Int64, "fatal": pl.Int64}),
    "reviewer_ids": pl.List(pl.String), "producer_ids": pl.List(pl.String),
    "full_audit": pl.Boolean, "sample_ids": pl.List(pl.String),
    "decision_at": pl.Datetime("us", "UTC"), "record_hash": pl.String,
    "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}
SIGNOFF_SCHEMA = {"reviewer_id": pl.String, "scope": pl.String, "decision_at": pl.Datetime("us", "UTC"), "record_hash": pl.String, "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC")}


def read_acceptance_records(layout) -> pl.DataFrame:
    files = sorted((layout.gold_dir / "_audit" / "acceptance_records").glob("*.parquet"))
    if not files:
        return pl.DataFrame(schema=ACCEPTANCE_SCHEMA)
    return pl.concat([pl.read_parquet(p) for p in files]).sort("decision_at", "attempt")


def read_signoffs(layout) -> pl.DataFrame:
    files = sorted((layout.gold_dir / "_audit" / "signoffs").glob("*.parquet"))
    if not files:
        return pl.DataFrame(schema=SIGNOFF_SCHEMA)
    return pl.concat([pl.read_parquet(p) for p in files])


def acceptance_decision(*, n_sampled: int, errors_general: int, errors_fatal: int, plan: AcceptancePlan = AcceptancePlan(), attempt: int = 1, batch_size: int | None = None,
                        sample_ids_hash: str | None = None, reviewer_ids: tuple[str, ...] = (), producer_ids: tuple[str, ...] = (),
                        layout=None, batch_id: str | None = None, audit_plan_version: str = "audit-200-3-v1", seed: int = 20260911,
                        full_audit: bool = False, sample_ids: tuple[str, ...] = (),
                        graph_version: str = "", input_hash: str = "", labels: tuple[dict, ...] = (),
                        repair_evidence_refs: tuple[str, ...] = ()) -> AcceptanceResult:
    """Persist formal batch decisions under a lock; attempt is derived from history, never supplied by a producer."""
    import fcntl
    import re
    from .lake import Layout, now_utc, stable_id, write_parquet_atomic

    args = dict(n_sampled=n_sampled, errors_general=errors_general, errors_fatal=errors_fatal, plan=plan, attempt=attempt,
                batch_size=batch_size, sample_ids_hash=sample_ids_hash, reviewer_ids=reviewer_ids, producer_ids=producer_ids)
    invalid_counts = any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (n_sampled, errors_general, errors_fatal))
    if invalid_counts or errors_general + errors_fatal > n_sampled:
        return AcceptanceResult("insufficient", n_sampled, errors_general, errors_fatal, None, None, "none", "invalid error counts", 0)
    if not batch_id:
        return _calculate_acceptance(**args)
    if not audit_plan_version or not re.fullmatch(r"[0-9a-f]{64}", sample_ids_hash or "") or not reviewer_ids or not producer_ids or any(not r for r in reviewer_ids + producer_ids) or set(reviewer_ids) & set(producer_ids):
        return AcceptanceResult("not_run", n_sampled, errors_general, errors_fatal, None, None, "none", "formal record requires sample identity and independent named producers/reviewers", 0)
    layout = layout or Layout.from_root()
    def reject(reason, current_attempt=0):
        return AcceptanceResult("insufficient", n_sampled, errors_general, errors_fatal, None, None, "none", reason, current_attempt)

    if not sample_ids or len(sample_ids) != n_sampled or len(set(sample_ids)) != n_sampled or any(not isinstance(i, str) or not i for i in sample_ids) or sample_identity(list(sample_ids)) != sample_ids_hash:
        return reject("formal record requires nonempty, recomputable physical sample ids")
    from .graph import verify_manifest, resolve_alias
    if not graph_version or not input_hash or resolve_alias(layout, graph_version) != graph_version:
        return reject("immutable artifact identity required")
    try:
        manifest = verify_manifest(layout, graph_version)
        artifact = pl.read_parquet(layout.episode(graph_version))
    except LookupError:
        return reject("reviewed artifact unavailable")
    if manifest["input_hash"] != input_hash or batch_id not in set(artifact["batch_id"]):
        return reject("reviewed artifact identity mismatch")
    if "episode_id" not in artifact.columns:
        return reject("reviewed artifact has no physical episode identities")
    physical_ids = set(artifact.filter(pl.col("batch_id") == batch_id)["episode_id"])
    if not set(sample_ids) <= physical_ids:
        return reject("sample IDs must belong to the reviewed artifact")
    if len(labels) != n_sampled or any(not isinstance(r, dict) for r in labels) or {r.get("sample_id") for r in labels} != set(sample_ids):
        return reject("complete frozen labels required")
    required_annotation = {"instrument_id", "side", "entry", "stop", "kind"}
    for r in labels:  # W03：键齐全不够，值也要有——每个关键字段要么有非空真值，要么在 not_applicable 里附来源依据（"原文无此字段"），空值不能靠 severity='ok' 通行
        ann = r.get("annotation")
        if not isinstance(ann, dict) or not required_annotation <= ann.keys() or not ann.get("kind"):
            return reject("complete per-sample field annotations required")
        na = ann.get("not_applicable")
        na = na if isinstance(na, dict) else {}
        for f in required_annotation - {"kind"}:
            if ann.get(f) in (None, "") and not (isinstance(na.get(f), str) and na[f].strip()):
                return reject(f"annotation value missing for {f} without not_applicable reason")
        if ann.get("unresolved"):
            return reject("unresolved labels cannot enter a formal acceptance")
    if any(r.get("severity") not in ("ok", "general", "fatal") or r.get("reviewer_id") not in reviewer_ids for r in labels):
        return reject("independent labels required")
    if sum(r["severity"] == "general" for r in labels) != errors_general or sum(r["severity"] == "fatal" for r in labels) != errors_fatal:
        return reject("error counts disagree with frozen labels")
    audit_dir = layout.gold_dir / "_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    with (audit_dir / ".acceptance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = read_acceptance_records(layout).filter((pl.col("graph_version") == graph_version) & (pl.col("input_hash") == input_hash) & (pl.col("batch_id") == batch_id))
        attempt = history.height + 1
        args["attempt"] = attempt
        if attempt > min(2, plan.max_formal_attempts):
            return reject("maximum two formal attempts per artifact", attempt)
        if history.height and any(r["n"] != n_sampled or r["c"] != plan.c or r["seed"] != seed or r["audit_plan_version"] != audit_plan_version for r in history.iter_rows(named=True)):
            return AcceptanceResult("insufficient", n_sampled, errors_general, errors_fatal, None, None, "none", "frozen audit plan changed", attempt)
        if history.height and sample_ids_hash in history["sample_ids_hash"].to_list():
            return AcceptanceResult("insufficient", n_sampled, errors_general, errors_fatal, None, None, "none", "retest requires a new fixed sample", attempt)
        previous_record_hash = ""
        repair_hash = ""
        if history.height:
            previous = history.row(-1, named=True)
            previous_record_hash = previous["record_hash"]
            if previous["result"] != "fail" or not repair_evidence_refs:
                return reject("retest requires a failed acceptance and repair evidence", attempt)
            from .lake import sha256_file
            import pathlib
            evidence = []
            for reference in repair_evidence_refs:
                path = pathlib.Path(reference).resolve()
                if not path.is_relative_to(audit_dir.resolve()) or not path.is_file():
                    return reject("repair evidence must be a saved local audit artifact", attempt)
                evidence.append((str(path), sha256_file(path)))
            repair_hash = stable_id(previous_record_hash, evidence)
        result = _calculate_acceptance(**args)
        if full_audit:
            complete = set(sample_ids) == physical_ids and batch_size == len(physical_ids) and batch_size is not None and 0 < batch_size < plan.n and n_sampled == batch_size and len(sample_ids) == batch_size and len(set(sample_ids)) == batch_size and sample_identity(list(sample_ids)) == sample_ids_hash
            if not complete:
                return AcceptanceResult("insufficient", n_sampled, errors_general, errors_fatal, None, None, "none", "full audit requires every physical sample id", attempt)
            result = AcceptanceResult("pass" if errors_general == 0 and errors_fatal == 0 else "fail", n_sampled, errors_general, errors_fatal, errors_general / n_sampled, None, "census", "applies only to the fully audited record ids", attempt)
        if result.result not in ("pass", "fail"):
            return result
        when = now_utc()
        record = dict(graph_version=graph_version, input_hash=input_hash, labels=json.dumps(labels, sort_keys=True),
                      repair_evidence_refs=list(repair_evidence_refs), repair_evidence_hash=repair_hash, previous_record_hash=previous_record_hash,
                      audit_plan_version=audit_plan_version, batch_id=batch_id, sample_ids_hash=sample_ids_hash, n=n_sampled, c=plan.c, seed=seed, attempt=attempt,
                      result=result.result, errors_by_severity={"general": errors_general, "fatal": errors_fatal}, reviewer_ids=list(reviewer_ids), producer_ids=list(producer_ids),
                      full_audit=full_audit, sample_ids=list(sample_ids), decision_at=when, event_time=when, available_at=when, ingested_at=when)
        record_hash = stable_id("acceptance", record)
        record["record_hash"] = record_hash
        dest = audit_dir / "acceptance_records" / f"{record_hash}.parquet"
        signs = [{"reviewer_id": r, "scope": f"{batch_id}/{audit_plan_version}/{sample_ids_hash}", "decision_at": when, "record_hash": record_hash, "event_time": when, "available_at": when, "ingested_at": when} for r in reviewer_ids]
        # Signatures are published before the acceptance commit; orphan signatures do not authorize a batch.
        write_parquet_atomic(pl.DataFrame(signs, schema=SIGNOFF_SCHEMA), audit_dir / "signoffs" / f"{record_hash}.parquet")
        write_parquet_atomic(pl.DataFrame([record], schema=ACCEPTANCE_SCHEMA), dest)
        return result


def repost_audit_report(layout, batch_id: str) -> dict[str, Any]:
    """A12: distinguish unreviewed candidates from measured confirmation rates."""
    if not layout.duplicate_group.exists():
        return {"status": "not_run", "unconfirmed_repost_candidates": 0, "confirmation_rate": None, "n_reviewed": 0}
    groups = pl.read_parquet(layout.duplicate_group)
    candidates = set(groups.filter((pl.col("batch_id") == batch_id) & (pl.col("dup_kind") == "repost_same_channel"))["source_version_id"])
    decisions = {}
    records = read_acceptance_records(layout)
    for record in records.filter(pl.col("batch_id") == batch_id).iter_rows(named=True):
        artifact_path = layout.episode(record["graph_version"])
        if not artifact_path.exists():
            continue
        artifact = pl.read_parquet(artifact_path)
        if "root_source_version_id" not in artifact.columns:
            continue
        source_of = dict(artifact.select("episode_id", "root_source_version_id").iter_rows())
        for label in json.loads(record["labels"]):
            sample_id = source_of.get(label["sample_id"])
            if sample_id in candidates and isinstance(label.get("repost_confirmed"), bool):
                decisions[sample_id] = label["repost_confirmed"]
    rate = None
    status = "not_run"
    if decisions:
        rate = sum(decisions.values()) / len(decisions)
        status = "reviewed"
    return {"status": status, "unconfirmed_repost_candidates": len(candidates - decisions.keys()),
            "n_reviewed": len(decisions), "confirmation_rate": rate,
            "mapping_hash": stable_id("repost-audit", sorted(decisions.items()))}


# ---------------------------------------------------------------- 全窗口抽样框
@dataclass(frozen=True)
class Window:
    channel_id: int
    start: datetime
    end: datetime

    @property
    def key(self) -> tuple[int, str]:
        return self.channel_id, self.start.isoformat()


def build_frame(channels: Iterable[int], span_start: datetime, span_end: datetime, *, window_h: int = WINDOW_H) -> list[Window]:
    """全部频道 × UTC 日历 6h 半开窗口 [start, start+6h)。"""
    s0 = span_start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    s0 = s0.replace(hour=(s0.hour // window_h) * window_h)
    out = []
    for ch in sorted(set(channels)):
        t = s0
        while t < span_end:
            out.append(Window(ch, t, t + timedelta(hours=window_h)))
            t += timedelta(hours=window_h)
    return out


def frame_hash(frame: list[Window]) -> str:
    return stable_id("frame", [(w.channel_id, w.start.isoformat()) for w in frame])


def sample_windows(frame: list[Window], *, per_channel: int = WINDOWS_PER_CHANNEL, seed: int = 20260911) -> dict[tuple[int, str], float]:
    """每频道简单随机 per_channel 窗；返回 {window.key: π}（π=min(1, per_channel/W_c)）。确定性（seed）。"""
    rng = random.Random(seed)
    by_ch: dict[int, list[Window]] = {}
    for w in frame:
        by_ch.setdefault(w.channel_id, []).append(w)
    out: dict[tuple[int, str], float] = {}
    for ch, ws in sorted(by_ch.items()):
        k = min(per_channel, len(ws))
        pi = min(1.0, per_channel / len(ws)) if ws else 0.0
        for w in rng.sample(sorted(ws, key=lambda w: w.start), k):
            out[w.key] = pi
    return out


def window_of(frame: list[Window], channel_id: int, t: datetime) -> Window | None:
    for w in frame:
        if w.channel_id == channel_id and w.start <= t < w.end:
            return w
    return None


def weighted_recall(sampled: dict[tuple[int, str], float], true_opps: list[tuple[tuple[int, str], str]], predicted: set[str], *, n_boot: int = 500, seed: int = 7) -> dict[str, Any]:
    """true_opps: [(window.key, opportunity_id)]（人工在抽中窗口内枚举的真实机会，每个机会唯一归属一个窗）；predicted: 产线识别到的机会 id 集合。
    recall = Σ TP/π ÷ Σ true/π；区间：频道内按窗口簇重采样（percentile bootstrap）。无真实机会 → insufficient。"""
    per_win: dict[tuple[int, str], list[str]] = {}
    for key, oid in true_opps:
        if key in sampled:
            per_win.setdefault(key, []).append(oid)
    if not per_win:
        return {"status": "insufficient", "recall": None, "n_true": 0}
    def est(keys: list[tuple[int, str]]) -> float | None:
        num = den = 0.0
        for k in keys:
            pi = sampled[k]
            for oid in per_win.get(k, []):
                den += 1 / pi
                if oid in predicted:
                    num += 1 / pi
        return num / den if den else None
    keys = sorted(per_win)
    point = est(keys)
    rng = random.Random(seed)
    by_ch: dict[int, list[tuple[int, str]]] = {}
    for k in sorted(sampled):  # 重采样单位 = 全部抽中窗口（含零机会的负窗口，S14）
        by_ch.setdefault(k[0], []).append(k)
    boots = []
    for _ in range(n_boot):
        ks: list[tuple[int, str]] = []
        for ch, kk in by_ch.items():
            ks += [rng.choice(kk) for _ in kk]
        v = est(ks)
        if v is not None:
            boots.append(v)
    boots.sort()
    lo, hi = (boots[int(0.025 * len(boots))], boots[min(len(boots) - 1, int(0.975 * len(boots)))]) if boots else (None, None)
    n_true = sum(len(v) for v in per_win.values())
    return {"status": "ok", "recall": point, "interval": (lo, hi), "interval_method": "cluster_bootstrap_all_sampled_windows_within_channel", "n_true": n_true,
            "n_windows": len(sampled), "n_windows_with_opportunity": len(keys)}


# ---------------------------------------------------------------- 金标分歧
def disagreement_report(labels_a: list[dict[str, Any]], labels_b: list[dict[str, Any]], *, key_fields: tuple[str, ...] = ("instrument_id", "side", "entry", "stop", "kind"), gates: dict[str, float] = DEFAULT_GATES) -> dict[str, Any]:
    """两名标注者盲标同一批 episode（按 episode_id 对齐）。先报原始分歧率与未决率，再给门判定；不做仲裁。"""
    b = {x["episode_id"]: x for x in labels_b}
    n = 0
    missing_pairs = 0
    field_dis = {f: 0 for f in key_fields}
    field_n = {f: 0 for f in key_fields}
    whole_dis = 0
    unresolved = 0
    for a in labels_a:
        bb = b.get(a["episode_id"])
        if bb is None:
            missing_pairs += 1
            continue
        n += 1
        if a.get("unresolved") or bb.get("unresolved"):
            unresolved += 1
            continue
        diff = False
        if any(f not in a or f not in bb for f in key_fields):
            unresolved += 1  # 标签不完整（任一关键字段缺失）= 未决，不能只评部分字段就给 pass
            continue
        for f in key_fields:
            if f in a and f in bb:
                if a.get(f) is None and bb.get(f) is None:
                    unresolved += 1  # 双方空值不是一致，是未标
                    diff = None
                    break
                field_n[f] += 1
                if a.get(f) != bb.get(f):
                    field_dis[f] += 1
                    diff = True

        if diff is None:
            continue
        whole_dis += diff
    scored_fields = [f for f in key_fields if field_n[f] > 0]
    if n == 0 or not scored_fields or missing_pairs:
        return {"status": "insufficient", "n": n, "missing_pairs": missing_pairs, "scored_fields": scored_fields, "note": "缺标注/缺配对/无可评字段：不出 pass"}
    rates = {f: (field_dis[f] / field_n[f] if field_n[f] else None) for f in key_fields}
    key_rate = max(r for r in rates.values() if r is not None)
    whole_rate, unres_rate = whole_dis / n, unresolved / n
    verdict = "pass" if key_rate <= gates["key_field"] and whole_rate <= gates["whole_episode"] and unres_rate <= gates["unresolved"] else "pause"
    return {"status": "ok", "n": n, "missing_pairs": 0, "scored_fields": scored_fields, "field_disagreement": rates, "key_field_rate_max": key_rate, "whole_episode_rate": whole_rate,
            "unresolved_rate": unres_rate, "gates": gates, "verdict": verdict, "note": "原始意见分歧率；仲裁后的零分歧不得回填此表"}


# ---------------------------------------------------------------- Label Studio
LABEL_STUDIO_CONFIG = """<View>
  <Header value="Episode $episode_id · channel $channel_id"/>
  <Text name="root_text" value="$root_text"/>
  <Choices name="kind" toName="root_text" choice="single" required="true">
    <Choice value="entry_proposal"/><Choice value="analysis"/><Choice value="result_post"/><Choice value="chatter"/><Choice value="undecidable"/>
  </Choices>
  <Choices name="side" toName="root_text" choice="single"><Choice value="long"/><Choice value="short"/><Choice value="unclear"/></Choices>
  <TextArea name="entry" toName="root_text" placeholder="入场（原文数字，区间写 lo-hi；无则留空）" maxSubmissions="1"/>
  <TextArea name="stop" toName="root_text" placeholder="止损（原文数字；无则留空，不补默认）" maxSubmissions="1"/>
  <TextArea name="tps" toName="root_text" placeholder="止盈档位，逗号分隔" maxSubmissions="1"/>
  <Repeater on="$events" indexFlag="{{idx}}">
    <Text name="event_text_{{idx}}" value="$events[{{idx}}].text"/>
    <Choices name="event_kind_{{idx}}" toName="event_text_{{idx}}" choice="single">
      <Choice value="amend"/><Choice value="cancel"/><Choice value="entry_claimed"/><Choice value="stop_move"/><Choice value="reduce"/><Choice value="close_claimed"/><Choice value="result_post"/><Choice value="chatter"/><Choice value="belongs_to_other_episode"/>
    </Choices>
  </Repeater>
  <Choices name="quality" toName="root_text" choice="multiple">
    <Choice value="unresolved"/><Choice value="fatal_direction_or_symbol"/><Choice value="fatal_attribution"/><Choice value="general_error"/>
  </Choices>
</View>
"""


def label_studio_tasks(episodes: pl.DataFrame, events: pl.DataFrame, mv: pl.DataFrame) -> list[dict[str, Any]]:
    """任务导出：只给原文与事件文本；不含模型预测字段、reconstructed_outcome、任何行情走势。"""
    text_of = dict(zip(mv["source_version_id"].to_list(), mv["text"].to_list()))
    tasks = []
    for r in episodes.iter_rows(named=True):
        evs = events.filter((pl.col("episode_id") == r["episode_id"]) & (pl.col("event_seq") > 0)).sort("event_seq")
        tasks.append({"data": {  # 盲标：不带模型品种/归属/预测（S14）
            "episode_id": r["episode_id"], "channel_id": r["channel_id"], "root_text": text_of.get(r["root_source_version_id"], ""),
            "events": [{"seq": e["event_seq"], "text": text_of.get(e["source_version_id"], "")} for e in evs.iter_rows(named=True)],
        }})
    return tasks


# ---------------------------------------------------------------- 元数据
def sample_identity(sample_ids: list[str]) -> str:
    return stable_id("sample", sorted(sample_ids))


def dataset_metadata(*, batch_id: str, frame: list[Window], plan: AcceptancePlan, seed: int, result: AcceptanceResult, review_hours: float, excluded_layers: list[str], low_confidence_rule: str, attempt_history: list[str],
                     sample_ids_hash: str | None = None, reviewer_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "sample_ids_hash": sample_ids_hash, "reviewer_ids": list(reviewer_ids),
        "audit_plan_version": AUDIT_PLAN_VERSION, "batch_id": batch_id, "frame_hash": frame_hash(frame), "n": plan.n, "c": plan.c, "seed": seed,
        "error_count_by_severity": {"fatal": result.errors_fatal, "general": result.errors_general}, "p_hat": result.p_hat, "interval_method": result.interval_method,
        "interval": result.interval, "acceptance_result": result.result, "attempt_history": attempt_history, "review_hours": review_hours, "excluded_layers": excluded_layers,
        "low_confidence_rule": low_confidence_rule, "risk_acknowledged_by_user": False,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="放行计算器：OC 表 / Wilson")
    ap.add_argument("--oc", nargs=2, type=int, action="append", metavar=("N", "C"), help="放行方案 n c（可重复）")
    ap.add_argument("--wilson", nargs=2, type=int, metavar=("K", "N"))
    a = ap.parse_args(argv)
    if a.oc:
        plans = [tuple(x) for x in a.oc]
        for row in oc_table(plans):
            cells = "  ".join(f"{n}/{c} accept={row[f'{n}/{c}'] * 100:.4f}%" for n, c in plans)
            print(f"p={row['p'] * 100:g}%  {cells}")
    if a.wilson:
        lo, hi = wilson(a.wilson[0], a.wilson[1])
        print(f"wilson {a.wilson[0]}/{a.wilson[1]}: [{lo * 100:.5f}%, {hi * 100:.5f}%]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
