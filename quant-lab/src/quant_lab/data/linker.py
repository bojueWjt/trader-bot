"""规则链接器（ADR-G1 §4；合并稿 B.1 级 2/3；review-G1-P1 S03/S05/S13）：候选边生成 + 强/弱守卫 + 规则裁决 + LLM 裁决出口。

候选来源与守卫：
- reply：同 peer 的 reply_to_message_id 沿父链递归到入场根（或链顶孤儿管理）；链上每个中间父帖都是依赖，
  edge_available_at = max(事件、根、所有中间父帖的 available_at)；任一未知 → 边不可用（DEPENDENCY_NOT_AVAILABLE）。
  强边守卫：品种（两端都知则必须同）与方向（两端都知则必须同）一致；不一致 → 冲突边（weak+ENTRY_LINK_AMBIGUOUS）。缺父 → PARENT_MISSING。环 → LIFECYCLE_INVALID。
- same_source（版本链）：同 source_id 的后续版本 → amend，公共 link_method=plan_ref，payload.basis=same_source_version_chain。
- plan_ref：显式计划号，只在事件 available_at 之前已可知的根中查（当时可见集合）；唯一命中 → 强，多命中 → 弱。
- window：同频道、同品种、同方向（无方向则只同品种）、根 available_at **严格早于** 事件 available_at 且 ≤72h → 永远弱；
  多候选 → unresolved（ENTRY_LINK_AMBIGUOUS），可交 LLM 裁决（录制/gated），裁决仍歧义 → unresolved。
- 复制指纹只进 duplicate_group（不建归属边）。
输出 silver/candidate_edges.parquet（含未选边）与 silver/adjudications.parquet。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import polars as pl

from .lake import Layout, now_utc, preserve_ingested_at, schema_hash, stable_id, write_parquet_atomic
from .llm import AdjudicationRequest, LLMClient, adjudicate
from .reasons import Reason

RULE_VERSION = "tg-link-v0.5"
WINDOW_S = 72 * 3600
ROOT_KINDS = {"entry_proposal"}
MGMT_KINDS = {"amend", "cancel", "expire", "entry_claimed", "add", "reduce", "stop_move", "tp_ladder", "close_claimed", "correction", "delete_notice"}

CANDIDATE_EDGE_SCHEMA: dict[str, Any] = {
    "candidate_id": pl.String, "from_plan_id": pl.String, "to_plan_id": pl.String, "method": pl.String, "strength": pl.String, "selected": pl.Boolean,
    "evidence": pl.String, "dependency_refs": pl.List(pl.String), "reason_codes": pl.List(pl.String), "edge_available_at": pl.Datetime("us", "UTC"),
    "rule_version": pl.String, "batch_id": pl.String, "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}
ADJUDICATION_SCHEMA: dict[str, Any] = {
    "request_id": pl.String, "from_plan_id": pl.String, "action": pl.String, "selected_candidate_id": pl.String, "rejected_candidate_ids": pl.List(pl.String),
    "reason_codes": pl.List(pl.String), "provider": pl.String, "attempts": pl.Int32, "extended": pl.Boolean, "context_hash": pl.String, "response_hash": pl.String,
    "rule_version": pl.String, "batch_id": pl.String, "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}
SCHEMA_HASH = schema_hash(CANDIDATE_EDGE_SCHEMA)


@dataclass
class Plan:
    plan_id: str
    source_version_id: str
    channel_id: int
    message_id: int
    kind: str
    instrument_id: str | None
    symbol_raw: str | None
    side: str | None
    available_at: datetime | None
    event_time: datetime | None
    sequence: int | None
    version_no: int
    reply_to_message_id: int | None
    plan_ref: str | None
    time_grade: str
    text: str = ""
    row: dict[str, Any] = field(default_factory=dict)


def _plans(cp: pl.DataFrame, mv: pl.DataFrame, ex: pl.DataFrame | None) -> list[Plan]:
    if "author_id" not in mv.columns:
        mv = mv.with_columns(pl.lit(None, dtype=pl.String).alias("author_id"))
    j = cp.filter(pl.col("extractor_name") == "parser").join(
        mv.select("source_version_id", "reply_to_message_id", "sequence", "version_no", "text", "author_id"), on="source_version_id", how="left")
    pref = dict(zip(ex["extract_id"].to_list(), ex["plan_ref"].to_list())) if ex is not None and "plan_ref" in ex.columns else {}
    return [Plan(r["plan_id"], r["source_version_id"], r["channel_id"], r["message_id"], r["kind"], r["instrument_id"], r["symbol_raw"], r["side"], r["available_at"], r["event_time"],
                 r["sequence"], r["version_no"] or 1, r["reply_to_message_id"], pref.get(r["extract_id"]), r["time_grade"], r["text"] or "", r) for r in j.iter_rows(named=True)]


def _max_or_none(times: list[datetime | None]) -> datetime | None:
    if not times or any(t is None for t in times):
        return None
    return max(times)


def build_candidates(cp: pl.DataFrame, mv: pl.DataFrame, ex: pl.DataFrame | None = None, *, adjudicator: LLMClient | None = None, ingested_at: datetime | None = None) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, Any]]:
    ingested_at = ingested_at or now_utc()
    plans = _plans(cp, mv, ex)
    versions = {r["source_version_id"]: r for r in mv.iter_rows(named=True)}
    # A projected MV may omit its clock; CP retains that same source clock.
    # An explicitly unknown MV clock must remain unknown.
    if "available_at" not in mv.columns:
        for plan in plans:
            if plan.source_version_id in versions:
                versions[plan.source_version_id]["available_at"] = plan.available_at
    batch_id = cp["batch_id"][0] if cp.height else "tg-empty"
    by_msg: dict[tuple[int, int], list[Plan]] = {}
    for p in plans:
        by_msg.setdefault((p.channel_id, p.message_id), []).append(p)
    for lst in by_msg.values():
        lst.sort(key=lambda p: (p.available_at or ingested_at, p.version_no))
    first_of_source = {k: lst[0] for k, lst in by_msg.items()}
    def branch_key(p):
        branch = p.row.get("branch_index")
        if branch is None:
            branch = (p.instrument_id, p.side)
        return (p.channel_id, p.message_id, branch)

    first_branch = {}
    for p in plans:
        identity = branch_key(p)
        prior = first_branch.get(identity)
        if prior is None or (p.available_at or ingested_at, p.version_no) < (prior.available_at or ingested_at, prior.version_no):
            first_branch[identity] = p
    root_ids = {p.plan_id for p in first_branch.values() if p.kind in ROOT_KINDS}
    roots = [p for p in plans if p.plan_id in root_ids]
    mv_ids = set(zip(mv["channel_id"].to_list(), mv["source_id"].struct.field("message_id").to_list()))

    def chain(p: Plan) -> tuple[Plan | None, str, list[Plan]]:
        """沿 reply 父链；返回 (根或链顶, 状态, 中间父帖列表)。状态：found / chain_top / parent_missing / no_root / cycle。"""
        seen: set[str] = {p.plan_id}
        mids: list[Plan] = []
        cur = p
        while True:
            if cur.plan_id in root_ids and cur is not p:
                return cur, "found", mids
            if cur.reply_to_message_id is None:
                if cur is p:
                    return None, "no_root", mids
                return (cur, "chain_top", mids[:-1]) if cur.kind in MGMT_KINDS else (None, "no_root", mids)
            parent = first_of_source.get((cur.channel_id, cur.reply_to_message_id))
            if parent is None:
                return (None, "no_root", mids) if (cur.channel_id, cur.reply_to_message_id) in mv_ids else (None, "parent_missing", mids)
            if parent.plan_id in seen:
                return None, "cycle", mids
            seen.add(parent.plan_id)
            mids.append(parent)
            cur = parent

    edges: list[dict[str, Any]] = []
    adj: list[dict[str, Any]] = []

    def guard_ok(ev: Plan, root: Plan) -> tuple[bool, str | None]:
        if ev.available_at is not None and root.available_at is not None and ev.available_at < root.available_at:
            return False, "child_known_before_parent"  # 回复不可能早于被回复者：顺序证据矛盾
        if ev.available_at == root.available_at and (ev.sequence is None or root.sequence is None or root.sequence >= ev.sequence):
            return False, "same_time_without_order"
        if ev.kind == "correction" and (not ev.row.get("author_id") or ev.row.get("author_id") != root.row.get("author_id")):
            return False, "author_mismatch"
        if ev.instrument_id and root.instrument_id and ev.instrument_id != root.instrument_id:
            return False, "instrument_mismatch"
        if ev.side and root.side and ev.side != root.side and ev.kind not in ("close_claimed", "reduce"):
            return False, "side_mismatch"
        return True, None

    def edge(ev: Plan, root: Plan, method: str, strength: str, evidence: dict, deps: list[Plan], reasons: list[str] | None = None) -> dict[str, Any]:
        from .graph import plan_dependencies
        closures = [plan_dependencies(p.row, versions, {}) for p in [ev, root] + deps]
        ea = _max_or_none([clock for clock, _ in closures])
        closure_refs = sorted({ref for _, refs in closures for ref in refs})
        rs = list(reasons or [])
        if ea is None:
            rs.append(Reason.DEPENDENCY_NOT_AVAILABLE)
        return {"candidate_id": stable_id("cb", ev.plan_id, root.plan_id, method, evidence), "from_plan_id": ev.plan_id, "to_plan_id": root.plan_id, "method": method, "strength": strength if ea is not None else "weak",
                "selected": False, "evidence": json.dumps(evidence, ensure_ascii=False, default=str), "dependency_refs": closure_refs,
                "reason_codes": sorted(set(rs)), "edge_available_at": ea, "rule_version": RULE_VERSION, "batch_id": batch_id, "event_time": ev.event_time, "available_at": ea, "ingested_at": ingested_at}

    for ev in plans:
        if ev.plan_id in root_ids:
            continue
        cands: list[dict[str, Any]] = []
        reasons: list[str] = []
        first = first_branch[branch_key(ev)]
        if first.plan_id != ev.plan_id and first.plan_id in root_ids:
            cands.append(edge(ev, first, "same_source", "strong", {"basis": "same_source_version_chain", "version_no": ev.version_no}, []))
        if ev.kind == "correction" and ev.reply_to_message_id is None and re.search(r"撤回上一条(?:断言)?|上一条作废", ev.text):
            previous = [p for p in plans if p.channel_id == ev.channel_id and p.row.get("author_id") == ev.row.get("author_id") and ev.row.get("author_id") and p.available_at is not None and ev.available_at is not None and p.available_at < ev.available_at]
            if previous:
                latest_time = max(p.available_at for p in previous)
                latest = [p for p in previous if p.available_at == latest_time]
                if len(latest) == 1:
                    ev.reply_to_message_id = latest[0].message_id
            if ev.reply_to_message_id is None:
                reasons.append(Reason.LIFECYCLE_INVALID)
        if ev.reply_to_message_id is not None:
            root, st, mids = chain(ev)
            if root is not None:
                # Every branch in the referenced source is a candidate; a known symbol/side can disambiguate it.
                branches = [r for r in roots if (r.channel_id, r.message_id) == (root.channel_id, root.message_id)]
                if not branches:
                    branches = [root]
                compatible = [r for r in branches if guard_ok(ev, r)[0]]
                for candidate in branches:
                    ok, why = guard_ok(ev, candidate)
                    unique = ok and len(compatible) == 1
                    cands.append(edge(ev, candidate, "reply", "strong" if unique else "weak", {"reply_to_message_id": ev.reply_to_message_id, "chain_top_orphan": st == "chain_top", "chain_len": len(mids) + 1, "guard": why}, mids, [] if unique else [Reason.ENTRY_LINK_AMBIGUOUS]))
            elif st == "parent_missing":
                reasons.append(Reason.PARENT_MISSING)
            elif st == "cycle":
                reasons.append(Reason.LIFECYCLE_INVALID)
        if ev.plan_ref and ev.available_at is not None:
            hits = [r for r in roots if r.channel_id == ev.channel_id and r.row.get("author_id") == ev.row.get("author_id") and r.plan_ref == ev.plan_ref and r.available_at is not None and r.available_at < ev.available_at]
            for r in hits:
                unique = len(hits) == 1 and bool(ev.row.get("author_id"))
                cands.append(edge(ev, r, "plan_ref", "strong" if unique else "weak", {"basis": "explicit_plan_reference", "plan_ref": ev.plan_ref, "n_hits": len(hits)}, [], [] if unique else [Reason.ENTRY_LINK_AMBIGUOUS]))
        quotes = re.findall(r'[“"「]([^”"」]+)[”"」]', ev.text)
        quotes.extend(re.findall(r"(?m)^>\s*(.+)$", ev.text))
        if quotes:
            hits = [p for p in plans if p.kind in ROOT_KINDS and p.channel_id == ev.channel_id and p.plan_id != ev.plan_id and p.available_at is not None and ev.available_at is not None and p.available_at < ev.available_at and any(p.text.strip().startswith(q.strip()) for q in quotes if q.strip())]
            version_ids = {p.source_version_id for p in hits}
            for matched in hits:
                candidate = first_branch[branch_key(matched)]
                ok, why = guard_ok(ev, candidate)
                unique = len(version_ids) == 1 and len(hits) == 1 and ok
                cands.append(edge(ev, candidate, "quote", "strong" if unique else "weak", {"quotes": quotes, "quoted_version": matched.source_version_id, "n_hits": len(hits), "guard": why}, [matched], [] if unique else [Reason.ENTRY_LINK_AMBIGUOUS]))
        if ev.available_at is not None and ev.instrument_id:
            for r in roots:
                if r.channel_id != ev.channel_id or r.instrument_id != ev.instrument_id or r.available_at is None:
                    continue
                if ev.side and r.side and ev.side != r.side:
                    continue
                dt = (ev.available_at - r.available_at).total_seconds()
                same_time_ordered = dt == 0 and ev.sequence is not None and r.sequence is not None and r.sequence < ev.sequence
                if (0 < dt <= WINDOW_S or same_time_ordered) and all(c["to_plan_id"] != r.plan_id for c in cands):  # 严格早于（同刻顺序未知不算）
                    cands.append(edge(ev, r, "window", "weak", {"dt_s": dt, "side_matched": bool(ev.side and r.side)}, []))
        if any(Reason.DEPENDENCY_NOT_AVAILABLE in c["reason_codes"] for c in cands):
            reasons.append(Reason.DEPENDENCY_NOT_AVAILABLE)
        usable = [c for c in cands if c["edge_available_at"] is not None]
        strong = [c for c in usable if c["strength"] == "strong"]
        weak = [c for c in usable if c["strength"] == "weak"]
        strong_targets = {c["to_plan_id"] for c in strong}
        action, sel, provider, attempts, extended, ctx_hash, resp_hash = "new_episode", None, "rule", 0, False, None, None
        if len(strong_targets) == 1:
            sel, action = next(c for c in strong if c["to_plan_id"] in strong_targets), "link"
        elif len(strong_targets) > 1:
            action = "unresolved"
            reasons.append(Reason.ENTRY_LINK_AMBIGUOUS)
        elif len(weak) == 1 and not any(Reason.ENTRY_LINK_AMBIGUOUS in c["reason_codes"] for c in weak):
            sel, action = weak[0], "link"
        elif len(weak) > 1 or weak:
            action = "unresolved"
            reasons.append(Reason.ENTRY_LINK_AMBIGUOUS)
            if adjudicator is not None:
                # 上下文只含事件 available_at 之前可知的消息（决策用途 as-of），有界
                ctx_plans = [p for p in plans if p.channel_id == ev.channel_id and p.available_at is not None and p.available_at <= ev.available_at and p.plan_id != ev.plan_id]
                ctx = [{"peer_id": ev.channel_id, "message_id": ev.message_id, "source_version_id": ev.source_version_id, "text": ev.text, "available_at": ev.available_at.isoformat()}] + [
                    {"peer_id": p.channel_id, "message_id": p.message_id, "source_version_id": p.source_version_id, "text": p.text, "available_at": p.available_at.isoformat()} for p in sorted(ctx_plans, key=lambda p: p.available_at)[-31:]]
                req = AdjudicationRequest(request_id=stable_id("adjreq", ev.plan_id, RULE_VERSION)[:32], event_plan_id=ev.plan_id,
                                          candidates=[{k: c[k] for k in ("candidate_id", "to_plan_id", "method", "strength", "evidence")} for c in weak],
                                          context=ctx, decision_cutoff=ev.available_at.isoformat() if ev.available_at else None, purpose="description")
                res = adjudicate(req, client=adjudicator)
                provider, attempts, extended, resp_hash = f"llm:{res.provider}", res.attempts, res.extended, res.response_hash or None
                ctx_hash = stable_id("ctx", [c["source_version_id"] for c in ctx], [c["candidate_id"] for c in weak])
                if res.action == "link":
                    sel = next(c for c in weak if c["candidate_id"] == res.selected_candidate_id)
                    action = "link"
                    reasons = [r for r in reasons if r != Reason.ENTRY_LINK_AMBIGUOUS]
                    sel["evidence"] = json.dumps({**json.loads(sel["evidence"]), "llm_evidence": res.evidence_message_ids}, ensure_ascii=False, default=str)
                elif res.action == "new_episode":
                    action = "new_episode"
                    reasons = [r for r in reasons if r != Reason.ENTRY_LINK_AMBIGUOUS]
        if not usable and any(r in reasons for r in (Reason.DEPENDENCY_NOT_AVAILABLE, Reason.LIFECYCLE_INVALID)):
            action = "unresolved"
        if sel is not None:
            sel["selected"] = True
        edges.extend(cands)
        adj.append({"request_id": stable_id("adj", ev.plan_id, RULE_VERSION), "from_plan_id": ev.plan_id, "action": action, "selected_candidate_id": sel["candidate_id"] if sel else None,
                    "rejected_candidate_ids": [c["candidate_id"] for c in cands if not c["selected"]], "reason_codes": sorted(set(reasons)), "provider": provider, "attempts": attempts,
                    "extended": extended, "context_hash": ctx_hash, "response_hash": resp_hash, "rule_version": RULE_VERSION, "batch_id": batch_id, "event_time": ev.event_time,
                    "available_at": ev.available_at, "ingested_at": ingested_at})
    cb = pl.DataFrame(edges, schema=CANDIDATE_EDGE_SCHEMA) if edges else pl.DataFrame(schema=CANDIDATE_EDGE_SCHEMA)
    jd = pl.DataFrame(adj, schema=ADJUDICATION_SCHEMA) if adj else pl.DataFrame(schema=ADJUDICATION_SCHEMA)
    summary = {"n_plans": len(plans), "n_roots": len(root_ids), "n_candidates": cb.height,
               "actions": {k: v for k, v in sorted(jd.group_by("action").len().iter_rows())} if jd.height else {},
               "methods_selected": {k: v for k, v in sorted(cb.filter(pl.col("selected")).group_by("method").len().iter_rows())} if cb.height else {},
               "providers": {k: v for k, v in sorted(jd.group_by("provider").len().iter_rows())} if jd.height else {}, "rule_version": RULE_VERSION}
    return cb, jd, summary


def run(layout: Layout, *, adjudicator: LLMClient | None = None, ingested_at: datetime | None = None) -> dict[str, Any]:
    ingested_at = ingested_at or now_utc()
    cp = pl.read_parquet(layout.canonical_plan)
    mv = pl.read_parquet(layout.message_version)
    ex = pl.read_parquet(layout.extracted_event) if layout.extracted_event.exists() else None
    cb, jd, summary = build_candidates(cp, mv, ex, adjudicator=adjudicator, ingested_at=ingested_at)
    layout.ensure()
    p_cb, p_jd = layout.silver_dir / "candidate_edges.parquet", layout.silver_dir / "adjudications.parquet"
    write_parquet_atomic(preserve_ingested_at(cb, pl.read_parquet(p_cb) if p_cb.exists() else None, "candidate_id"), p_cb)
    write_parquet_atomic(preserve_ingested_at(jd, pl.read_parquet(p_jd) if p_jd.exists() else None, "request_id"), p_jd)
    return summary
