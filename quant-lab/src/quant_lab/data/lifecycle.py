"""双轨状态机 + 描述图/决策快照 + gold 发布（ADR-G1 §3 / §6 / §8；契约 §2 gold/episode、episode_event、§9；review-G1-P1 S01/S03/S08/S09/S10/S11）。

- 作者轨 P×C 转移表逐字照 ADR §3。`I` 保留事件不改状态；`L` 缺提议改单：只记录、**不应用字段补丁**；`J` 重开（提议作为事件到达终态根）：
  记录并在 payload 标 reopen，该提议自身仍是独立根；`R` 纠错：不原位改，标 replay_required（reason LIFECYCLE_INVALID 不用，改用 payload+eligibility）。
- 到期只由提议自带的冻结期限派生，且只在 expire_at ≤ observation_end 时生成；观察终点之后的事件不进图。
- 观察终点/研究 H=7 日：P 仍 active 且未全平 → right_censored（censor_at=min(horizon, observation_end)），不伪造作者状态。
- delete_notice：事件保留、状态不变、payload 记删帖证据。
- 决策快照（S01/S03）：依赖闭包 = 根版本 + 根消息必要媒体 + 品种登记版本 + 规则版本；A* 未知 → 无决策；t_dec = A* + delay。
  决策列全部从 t_dec 前可见的边重放（P/C、stop/tps、n_events、reason_codes、time_grade_min、复制组成员、派生哈希），并全部纳入 decision_snapshot_hash。
- EE：available_at = 所引源版本的 available_at；edge_available_at = 端点与依赖的 max（契约 §9.1）；版本链 payload.basis=same_source_version_chain（§9.6）。
- 发布（S10/S11）：input_hash 覆盖 plan/选边/裁决/规则/假设/观测终点；同名版本输入不同 → 拒写（无 force）；写 EP/EE 后发布 manifest（文件 hash）；tombstone 版本拒写。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from . import dedup as _dedup
from . import extract as _extract
from . import linker as _linker
from . import normalize as _normalize
from . import validate as _validate
from .graph import DEFAULT_PROCESSING_DELAY_S, closure_available_at, plan_dependencies, decision_visible, is_tombstoned, publish_manifest, read_manifest, snapshot_hash, t_dec_of
from .lake import D12, LayerLedger, Layout, append_quarantine, cum_prev, loss_row_from_ledger, mapping_rows, now_utc, preserve_ingested_at, quarantine_row, schema_hash, stable_id, write_loss, write_mapping, write_parquet_atomic
from .reasons import Reason

RULE_VERSION = "tg-lifecycle-v0.5"  # r4: causal copy-family audit stratum and immutable formal audit evidence.
HORIZON_S = 7 * 24 * 3600

P_STATES = ("none", "active", "cancelled", "expired")
C_STATES = ("unknown", "claimed_open", "claimed_closed")
_P = {"N": "none", "A": "active", "X": "cancelled", "E": "expired"}
_C = {"U": "unknown", "O": "claimed_open", "C": "claimed_closed"}
MGMT = ("add", "stop_move", "tp_ladder")
COLS = ("entry_proposal", "amend", "cancel", "expire", "entry_claimed", "mgmt", "reduce", "close_claimed", "correction")
_TABLE = {
    "N,U": ("A,U", "L", "I", "I", "N,O", "=", "=", "N,C", "R"),
    "N,O": ("J", "L", "I", "I", "I", "=", "=", "N,C", "R"),
    "N,C": ("J", "I", "I", "I", "J", "I", "I", "I", "R"),
    "A,U": ("J", "=", "X,U", "E,U", "A,O", "=", "=", "A,C", "R"),
    "A,O": ("J", "=", "X,O", "E,O", "I", "=", "=", "A,C", "R"),
    "A,C": ("J", "I", "I", "I", "J", "I", "I", "I", "R"),
    "X,U": ("J", "I", "I", "I", "X,O", "I", "I", "X,C", "R"),
    "X,O": ("J", "I", "I", "I", "I", "I", "I", "X,C", "R"),
    "X,C": ("J", "I", "I", "I", "J", "I", "I", "I", "R"),
    "E,U": ("J", "I", "I", "I", "E,O", "I", "I", "E,C", "R"),
    "E,O": ("J", "I", "I", "I", "I", "I", "I", "E,C", "R"),
    "E,C": ("J", "I", "I", "I", "J", "I", "I", "I", "R"),
}
TRANSITIONS: dict[tuple[str, str, str], str] = {}
for _row, _cells in _TABLE.items():
    _p, _c = _row.split(",")
    for _col, _cell in zip(COLS, _cells):
        TRANSITIONS[(_P[_p], _C[_c], _col)] = _cell
DESCRIPTIVE_KINDS = ("analysis", "result_post", "chatter", "undecidable", "delete_notice")


def transition(p: str, c: str, kind: str) -> tuple[str, str, str]:
    """返回 (new_p, new_c, action)；action ∈ {=, I, L, J, R, move, desc}。描述类 kind 不改状态（desc）；未列组合一律 I。"""
    if kind in DESCRIPTIVE_KINDS:
        return p, c, "desc"
    col = "mgmt" if kind in MGMT else kind
    cell = TRANSITIONS.get((p, c, col))
    if cell is None:
        return p, c, "I"
    if cell in ("=", "I", "L", "J", "R"):
        return p, c, cell
    np_, nc = cell.split(",")
    return _P[np_], _C[nc], "move"


TIME_GRADE_ORDER = ("V", "H0", "H1", "H2", "U")


def weakest(grades: list[str]) -> str:
    seen = set(grades)
    for g in reversed(TIME_GRADE_ORDER):
        if g in seen:
            return g
    return "U"


ORDER_PLAN_SCHEMA = pl.Struct({  # 契约 §9.10.1 A8：进 hash/build_request 的数值一律 Decimal(38,12)
    "instrument_id": pl.String, "side": pl.String,
    "entries": pl.List(pl.Struct({"kind": pl.String, "price_lo": D12, "price_hi": D12, "fraction": D12, "tif": pl.String, "post_only": pl.Boolean})),
    "stop": pl.Struct({"price": D12, "trigger": pl.String}),
    "tps": pl.List(pl.Struct({"level": D12, "fraction": D12})),
    "sizing": pl.Struct({"mode": pl.String, "qty": D12}),
    "expiry": pl.Struct({"entry_ttl_s": pl.Int64, "max_holding_s": pl.Int64}),
    "reduce_only_exit": pl.Boolean,
})
#: claimed_outcome.kind 取值域（G1 申报，待 G0 并入 §9.10.1）：作者声称的终态来源
#:   close_claimed（作者明确全平 → author_claim_state=claimed_closed）、cancel（作者取消未成交计划 → author_plan_state=cancelled）、
#:   expire（提议自带期限到期 → author_plan_state=expired）。reconstructed_outcome.kind 由 G2 回填：{filled_closed, unfilled_expired, stopped, tp_hit, right_censored}（G2 属主，此处只登记）。
CLAIMED_OUTCOME_KINDS = ("close_claimed", "cancel", "expire")
#: 契约 v1.5 §9.10.11 A15：七值；unevaluable（行情/规则证据缺失的删失）与 rejected（计划被 filter/保证金拒绝，从未挂出）必须与 right_censored / unfilled_expired 分开计数
RECONSTRUCTED_OUTCOME_KINDS = ("filled_closed", "unfilled_expired", "stopped", "tp_hit", "right_censored", "unevaluable", "rejected")
OUTCOME_SCHEMA = pl.Struct({"kind": pl.String, "at": pl.Datetime("us", "UTC"), "source_version_id": pl.String, "execution_contract_version": pl.String})
RECONSTRUCTED_OUTCOME_SCHEMA = pl.Struct({"kind": pl.String, "at": pl.Datetime("us", "UTC"), "source_version_id": pl.String, "execution_contract_version": pl.String, "censor_reason": pl.String})


def validate_reconstructed_outcome(o: dict | None) -> list[str]:
    """G2 回填的 reconstructed_outcome 校验（A15）：kind 在七值内；kind=unevaluable 时 censor_reason 非空（原样保留 G2 取值）。"""
    if o is None:
        return []
    errs = []
    if o.get("kind") not in RECONSTRUCTED_OUTCOME_KINDS:
        errs.append(f"bad_kind:{o.get('kind')!r}")
    if o.get("kind") == "unevaluable" and not o.get("censor_reason"):
        errs.append("unevaluable_without_censor_reason")
    return errs
ELIG_KEYS = ("description", "entry_decision", "execution", "original_entry", "price_check", "outcome")
ELIG_SCHEMA = pl.Struct({k: pl.Boolean for k in ELIG_KEYS})

EPISODE_SCHEMA: dict[str, Any] = {
    "episode_id": pl.String, "graph_version": pl.String, "predecessor_ids": pl.List(pl.String), "successor_ids": pl.List(pl.String),
    "channel_id": pl.Int64, "trader_id": pl.String, "instrument_id": pl.String, "side": pl.String,
    "entry_branch_id": pl.String, "duplicate_group_id": pl.String, "cluster_id": pl.String,
    "author_plan_state": pl.String, "author_claim_state": pl.String,
    "entry_observed": pl.Boolean, "exit_observed": pl.Boolean, "left_truncated": pl.Boolean, "right_censored": pl.Boolean,
    "censor_at": pl.Datetime("us", "UTC"), "censor_reason": pl.String,
    "time_grade_min": pl.String, "decision_eligible_at": pl.Datetime("us", "UTC"), "t_dec": pl.Datetime("us", "UTC"), "processing_delay_s": pl.Int64,
    "order_plan": ORDER_PLAN_SCHEMA, "decision_snapshot_hash": pl.String, "temporal_assumptions": pl.String,
    "claimed_outcome": OUTCOME_SCHEMA, "reconstructed_outcome": RECONSTRUCTED_OUTCOME_SCHEMA, "eligibility_by_estimand": ELIG_SCHEMA,
    "audit_stratum": pl.String, "sampling_probability": pl.Float64, "label_status": pl.String, "signoffs": pl.List(pl.String),
    "derivation_hash": pl.String, "is_tombstone": pl.Boolean, "tombstoned_at": pl.Datetime("us", "UTC"), "migration_reason": pl.String,
    # 决策快照列（t_dec 前可见边重放）
    "dec_author_plan_state": pl.String, "dec_author_claim_state": pl.String, "dec_stop": D12, "dec_tps": pl.List(D12), "dec_n_events": pl.Int32,
    "dec_n_invalid_transitions": pl.Int32, "dec_reason_codes": pl.List(pl.String), "dec_time_grade_min": pl.String, "dec_duplicate_group_id": pl.String,
    "dec_derivation_hash": pl.String, "dec_eligibility": ELIG_SCHEMA, "dec_cluster_id": pl.String, "dec_temporal_assumptions": pl.String, "dec_audit_stratum": pl.String, "dependency_refs": pl.List(pl.String),
    "deleted_after_observation_any": pl.Boolean,
    "root_plan_id": pl.String, "root_source_version_id": pl.String, "root_message_id": pl.Int64, "replay_required": pl.Boolean,
    "n_events": pl.Int32, "n_invalid_transitions": pl.Int32, "reason_codes": pl.List(pl.String), "rule_version": pl.String, "batch_id": pl.String,
    "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}
EVENT_SCHEMA: dict[str, Any] = {
    "event_id": pl.String, "episode_id": pl.String, "graph_version": pl.String, "channel_id": pl.Int64, "kind": pl.String, "event_seq": pl.Int64,
    "extract_id": pl.String, "plan_id": pl.String, "source_version_id": pl.String, "supersedes_event_id": pl.String,
    "superseded": pl.Boolean, "superseded_at": pl.Datetime("us", "UTC"),
    "link_method": pl.String, "link_confidence": pl.Float64, "edge_available_at": pl.Datetime("us", "UTC"), "dependency_refs": pl.List(pl.String), "payload": pl.String,
    "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}
SCHEMA_HASH = schema_hash(EPISODE_SCHEMA)
LINK_METHOD_PUBLIC = {"reply": "reply", "quote": "quote", "same_source": "plan_ref", "plan_ref": "plan_ref", "window": "window", "llm": "llm"}


def _elig(d: dict[str, Any]) -> dict[str, bool]:
    """契约 §9.10.9 A13：六键一律非空 Boolean（未算=不合格=False，不留 null）。"""
    return {k: bool(d.get(k, False)) for k in ELIG_KEYS}


def dec_plan_possible(root: dict[str, Any]) -> bool:
    """决策快照能否成计划：品种/方向已知，且入场有价格或有市价证据（S06）。"""
    if root["instrument_id"] is None or root["side"] not in ("long", "short"):
        return False
    return root["entry"] is not None or (root.get("entry_mode") == "market_ref")


def _order_plan(root: dict[str, Any], stop: float | None, tps: list[dict], expires_after_s: int | None) -> dict[str, Any] | None:
    """冻结计划快照：只写有原文来源的字段；缺入场方式 → None（不猜市价）；缺分配比例留 null（由 G2 policy 展开，S06）。"""
    if root["instrument_id"] is None or root["side"] not in ("long", "short"):
        return None
    e = root["entry"]
    mode = root.get("entry_mode") or ("price" if e else "unknown")
    if e is None and mode != "market_ref":
        return None
    if e is None:
        entries = [{"kind": "market_ref", "price_lo": None, "price_hi": None, "fraction": None, "tif": "IOC", "post_only": False}]
    elif e["kind"] == "ladder" and root["entries"]:
        entries = [{"kind": "limit", "price_lo": x, "price_hi": x, "fraction": None, "tif": "GTC", "post_only": False} for x in root["entries"]]
    elif e["kind"] == "zone":
        entries = [{"kind": "ladder", "price_lo": e["lo"], "price_hi": e["hi"], "fraction": None, "tif": "GTC", "post_only": False}]
    else:
        entries = [{"kind": "limit", "price_lo": e["lo"], "price_hi": e["hi"], "fraction": None, "tif": "GTC", "post_only": False}]
    return {"instrument_id": root["instrument_id"], "side": root["side"], "entries": entries,
            "stop": {"price": stop, "trigger": "mark"} if stop is not None else None,
            "tps": [{"level": t["level"], "fraction": t.get("fraction")} for t in tps],
            "sizing": {"mode": "risk_budget", "qty": None}, "expiry": {"entry_ttl_s": int(expires_after_s) if expires_after_s else None, "max_holding_s": None}, "reduce_only_exit": True}


def build_graph(cp: pl.DataFrame, mv: pl.DataFrame, cb: pl.DataFrame, jd: pl.DataFrame, dg: pl.DataFrame | None, *, graph_version: str, registry_version: str = "",
                processing_delay_s: int = DEFAULT_PROCESSING_DELAY_S, horizon_s: int = HORIZON_S, ingested_at: datetime | None = None,
                observation_end: datetime | None = None) -> tuple[pl.DataFrame, pl.DataFrame, list[dict], dict[tuple[int, str], LayerLedger], dict[str, Any]]:
    ingested_at = ingested_at or now_utc()
    batch_id = cp["batch_id"][0] if cp.height else "tg-empty"
    plans = {r["plan_id"]: r for r in cp.filter(pl.col("extractor_name") == "parser").iter_rows(named=True)}
    mvd = {r["source_version_id"]: r for r in mv.iter_rows(named=True)}
    dg_rows = {r["source_version_id"]: r for r in dg.iter_rows(named=True)} if dg is not None and dg.height else {}
    dg_members: dict[str, list[str]] = {}
    for r in dg_rows.values():
        dg_members.setdefault(r["duplicate_group_id"], []).append(r["source_version_id"])
    sel = {r["from_plan_id"]: r for r in cb.filter(pl.col("selected")).iter_rows(named=True) if r["from_plan_id"] in plans and r["to_plan_id"] in plans}
    adj = {r["from_plan_id"]: r for r in jd.iter_rows(named=True) if r["from_plan_id"] in plans}
    roots = [pid for pid, p in plans.items() if p["kind"] == "entry_proposal" and pid not in adj]
    targets = {e["to_plan_id"] for e in sel.values()}
    orphan_roots = sorted(({pid for pid, a in adj.items() if a["action"] == "new_episode" and plans[pid]["kind"] != "entry_proposal"} | (targets - set(roots))) & set(plans))
    members: dict[str, list[str]] = {r: [] for r in roots + orphan_roots}
    unresolved: list[str] = []
    for pid, a in adj.items():
        if a["action"] == "link" and pid in sel and sel[pid]["to_plan_id"] in members:
            members[sel[pid]["to_plan_id"]].append(pid)
        elif a["action"] in ("link", "unresolved"):
            unresolved.append(pid)
    obs_end = observation_end or max((p["available_at"] for p in plans.values() if p["available_at"]), default=ingested_at)
    if not registry_version and "registry_version" in cp.columns and cp.height:
        registry_version = cp["registry_version"][0] or ""
    rule_versions = {"normalize": _normalize.RULE_VERSION, "dedup": _dedup.RULE_VERSION, "extract": _extract.RULE_VERSION, "validate": _validate.RULE_VERSION, "linker": _linker.RULE_VERSION, "lifecycle": RULE_VERSION, "registry": registry_version}

    def key(pid: str) -> tuple:
        p = plans[pid]
        return (p["available_at"] or obs_end, mvd.get(p["source_version_id"], {}).get("sequence") or 0, pid)

    ep_rows: list[dict[str, Any]] = []
    ev_rows: list[dict[str, Any]] = []
    qrows: list[dict[str, Any]] = []
    for root_id in roots + orphan_roots:
        root = plans[root_id]
        if root["available_at"] is not None and root["available_at"] > obs_end:
            continue
        is_orphan = root_id in orphan_roots
        episode_id = stable_id("ep", graph_version, root_id)[:32]
        root_mv = mvd.get(root["source_version_id"], {})
        evs: list[dict[str, Any]] = [{"pid": root_id, "kind": root["kind"], "root": True, "method": None, "strength": None, "edge_available_at": root["available_at"], "synth": False, "deps": [root["source_version_id"]], "basis": None}]
        for pid in sorted(members[root_id], key=key):
            e = sel[pid]
            kind = plans[pid]["kind"]
            if e["method"] == "same_source" and kind == "entry_proposal":
                kind = "amend"
            if e["edge_available_at"] is not None and e["edge_available_at"] > obs_end:
                continue  # 观察终点之后的事件不进图
            evs.append({"pid": pid, "kind": kind, "root": False, "method": e["method"], "strength": e["strength"], "edge_available_at": e["edge_available_at"], "synth": False,
                        "deps": list(e["dependency_refs"] or []) + [plans[pid]["source_version_id"]], "basis": json.loads(e["evidence"]).get("basis") if e["evidence"] else None})
        expire_at = None
        if not is_orphan and root.get("expires_after_s") and root["event_time"]:
            cand_exp = root["event_time"] + timedelta(seconds=int(root["expires_after_s"]))
            if cand_exp <= obs_end:  # 期限尚未到观察终点 → 不生成（S09）
                expire_at = cand_exp
                evs.append({"pid": root_id, "kind": "expire", "root": False, "method": "plan_ref", "strength": "strong", "edge_available_at": expire_at, "synth": True, "deps": [root["source_version_id"]], "basis": "frozen_deadline_in_proposal"})
        evs.sort(key=lambda e: (e["edge_available_at"] or obs_end, 0 if e["root"] else 1, mvd.get(plans[e["pid"]]["source_version_id"], {}).get("sequence") or 0))
        # 依赖闭包（S03）：根版本 + 根必要媒体（同版本时钟）+ 品种登记 + 规则版本；任一未知 → 无决策
        decision_eligible_at = t_dec = None
        a_star, dep_refs = plan_dependencies(root, mvd, rule_versions)
        price_a_star, price_refs = plan_dependencies(root, mvd, rule_versions, purpose="price_check")
        h1 = root.get("time_grade") == "H1"
        if not is_orphan and not h1 and root["instrument_id"] is not None:
            decision_eligible_at = a_star
            t_dec = t_dec_of(a_star, processing_delay_s)
        p_state, c_state = "none", "unknown"
        stop, tps = (root["stop"], list(root["tps"] or [])) if not is_orphan else (None, [])
        stop_event_id = None
        field_events = {}
        deleted_any = False
        decision_replay_required = False
        entry_observed, exit_observed = (not is_orphan), False
        n_invalid = 0
        replay_required = False
        reasons: list[str] = list(root["reason_codes"] or [])
        if a_star is None:
            reasons.append(Reason.DEPENDENCY_NOT_AVAILABLE)
        grades = [root_mv.get("time_grade", "U")]
        claimed_outcome = None
        dec: dict[str, Any] = {"state": None, "stop": stop, "tps": [dict(t) for t in tps], "events": [], "invalid": 0, "grades": [grades[0]], "reasons": sorted(set(reasons))}
        for i, e in enumerate(evs):
            p = plans[e["pid"]]
            kind = e["kind"]
            np_, nc, action = transition(p_state, c_state, "entry_proposal" if (e["root"] and not is_orphan) else kind)
            if e["root"] and is_orphan:
                reasons.append(Reason.PARENT_MISSING)
            payload: dict[str, Any] = {"from": [p_state, c_state], "to": [np_, nc], "action": action, "invalid_transition": action == "I", "synthesized": e["synth"]}
            if e["basis"]:
                payload["basis"] = e["basis"]
            if action == "I":
                n_invalid += 1
                if Reason.LIFECYCLE_INVALID not in reasons:
                    reasons.append(Reason.LIFECYCLE_INVALID)
            correction_targets = {}
            if action == "R":
                fields = [f for f in ("entry", "stop", "tps") if p.get(f)]
                correction_targets = {f: field_events[f] for f in fields if f in field_events}
                source_text = mvd.get(p["source_version_id"], {}).get("text", "")
                if not fields and _extract.re.search(r"撤回上一条(?:断言)?|上一条作废", source_text):
                    source_row = mvd.get(p["source_version_id"], {})
                    target_mid = source_row.get("reply_to_message_id")
                    selected_edge = sel.get(e["pid"], {})
                    if target_mid is None:
                        target_mid = json.loads(selected_edge.get("evidence") or "{}").get("reply_to_message_id")
                    prior_events = []
                    for previous in ev_rows:
                        identity = mvd.get(previous["source_version_id"], {}).get("source_id", {})
                        if previous["episode_id"] == episode_id and not previous.get("superseded") and identity.get("message_id") == target_mid:
                            prior_events.append(previous)
                    if prior_events:
                        correction_targets = {"assertion": prior_events[-1]["event_id"]}
                        fields = ["assertion"]
                if not fields or len(correction_targets) != len(fields):
                    action = "I"
                    n_invalid += 1
                    reasons.append(Reason.LIFECYCLE_INVALID)
                    payload.update(action="I", invalid_transition=True, unresolved=True)
                else:
                    replay_required = True
                    payload["replay_required"] = True
                    payload["corrected_fields"] = correction_targets
                    for previous in ev_rows:
                        if previous["event_id"] in correction_targets.values():
                            previous["superseded"] = True
                            previous["superseded_at"] = e["edge_available_at"]
                    if decision_visible(e["edge_available_at"], t_dec):
                        decision_replay_required = True
            if action == "J":
                payload["reopen"] = True
            if action == "L":
                payload["left_truncated_amend"] = True  # 只记录，不应用补丁（S09）
            supersedes = next(iter(correction_targets.values()), None)
            if action in ("=", "move") and kind in ("amend", "stop_move") and p["stop"] is not None:
                payload["stop"] = {"old": stop, "new": p["stop"]}
                stop = p["stop"]
                supersedes = stop_event_id
            if action in ("=", "move") and kind in ("amend", "tp_ladder") and p["tps"]:
                payload["tps"] = {"old": [t["level"] for t in tps], "new": [t["level"] for t in p["tps"]]}
                tps = list(p["tps"])
            if action in ("=", "move") and kind == "reduce":
                payload["claimed_reduce"] = {"fraction": (p["size_hint"] or {}).get("fraction") if p["size_hint"] else None}
            if action == "move" and nc == "claimed_closed":
                exit_observed = True
                claimed_outcome = {"kind": "close_claimed", "at": p["event_time"], "source_version_id": p["source_version_id"], "execution_contract_version": None}
            elif action == "move" and np_ in ("cancelled", "expired") and claimed_outcome is None:
                claimed_outcome = {"kind": "cancel" if np_ == "cancelled" else "expire", "at": (expire_at if e["synth"] else p["event_time"]), "source_version_id": p["source_version_id"], "execution_contract_version": None}
            if kind == "delete_notice":
                target_mid = mvd.get(p["source_version_id"], {}).get("reply_to_message_id")
                observed = [r["source_version_id"] for r in mvd.values() if r.get("channel_id") == root["channel_id"] and (r.get("source_id") or {}).get("message_id") == target_mid and r.get("time_grade") == "V" and r.get("available_at") is not None and p["available_at"] is not None and r["available_at"] < p["available_at"]]
                payload["delete_evidence"] = {"target_message_id": target_mid, "observed_version_ids": sorted(observed)}
                deleted_any = deleted_any or bool(observed)
            if action not in ("I", "desc", "L", "R"):
                p_state, c_state = np_, nc
            event_id = stable_id("ev", episode_id, e["pid"], kind, i)[:32]
            if (e["root"] and not is_orphan) or (kind in ("amend", "stop_move") and p["stop"] is not None and action in ("=", "move")):
                stop_event_id = event_id
            if action != "I" and action != "R":
                for field_name in ("entry", "stop", "tps"):
                    if p.get(field_name):
                        field_events[field_name] = event_id
            grades.append(mvd.get(p["source_version_id"], {}).get("time_grade", "U"))
            if t_dec is not None and decision_visible(e["edge_available_at"], t_dec):
                dec.update({"state": (p_state, c_state), "stop": stop, "tps": [dict(t) for t in tps]})
                dec["events"].append((e["pid"], kind, action))
                dec["invalid"] += int(action == "I")
                dec["grades"].append(grades[-1])
                dec["reasons"] = sorted(set(dec["reasons"]) | ({Reason.LIFECYCLE_INVALID} if action == "I" else set()))
            ev_rows.append({"event_id": event_id, "episode_id": episode_id, "graph_version": graph_version, "channel_id": root["channel_id"], "kind": kind, "event_seq": i,
                            "extract_id": p["extract_id"], "plan_id": e["pid"], "source_version_id": p["source_version_id"], "supersedes_event_id": supersedes,
                            "superseded": False, "superseded_at": None,
                            "link_method": LINK_METHOD_PUBLIC.get(e["method"]) if e["method"] else None, "link_confidence": None if e["root"] else (0.9 if e["strength"] == "strong" else 0.5),
                            "edge_available_at": e["edge_available_at"], "dependency_refs": e["deps"], "payload": json.dumps(payload, ensure_ascii=False, default=str),
                            "event_time": expire_at if e["synth"] else p["event_time"], "available_at": p["available_at"], "ingested_at": ingested_at})
        horizon = (root["available_at"] + timedelta(seconds=horizon_s)) if root["available_at"] else None
        right_censored, censor_at, censor_reason = False, None, None
        if p_state in ("none", "active") and c_state != "claimed_closed" and horizon is not None:
            right_censored, censor_at, censor_reason = True, min(horizon, obs_end), Reason.LABEL_RIGHT_CENSORED
        elig = json.loads(root["eligibility_by_estimand"]) if root.get("eligibility_by_estimand") else {}
        elig["entry_decision"] = t_dec is not None and root["instrument_id"] is not None and elig.get("execution", True)
        elig["outcome"] = claimed_outcome is not None  # A13：有作者终态（全平/取消/到期）→ true，否则 false；决策视图恒 false
        if is_orphan or replay_required or h1:
            elig["execution"] = False
        if h1:
            elig["entry_decision"] = False  # 仅最终编辑版：原始入场隔离，不复活为可执行入场（S02）
        if replay_required:
            elig = {k: False for k in ELIG_KEYS}
        dec_plan = _order_plan(root, dec["stop"], dec["tps"], root.get("expires_after_s")) if (t_dec is not None) else None
        if dec_plan is None and t_dec is not None:
            elig["entry_decision"] = False
            elig["execution"] = False
        dec_grade = weakest(dec["grades"]) if dec["state"] else None
        dec_group = None
        dec_audit_stratum = None
        if t_dec is not None:
            # r4 V02: recompute only the then-known copy graph. Future bridges cannot join old families.
            known_mv = mv.filter(pl.col("available_at") < t_dec)
            known_groups = _dedup.dedup_frame(known_mv, ingested_at=ingested_at)[0]
            own = known_groups.filter(pl.col("source_version_id") == root["source_version_id"])
            if own.height:
                own_row = own.row(0, named=True)
                if own_row["usable_for_cluster"]:
                    dec_group = own_row["duplicate_group_id"]
                if own_row["dup_kind"] == "repost_same_channel":
                    dec_audit_stratum = "repost_same_channel"
            if dec_group is None:
                dec_group = "dg-" + stable_id("dg", root["source_version_id"])[:16]
        root_elig = json.loads(root["eligibility_by_estimand"]) if root.get("eligibility_by_estimand") else {}
        if price_a_star is None or t_dec is None or price_a_star >= t_dec:
            root_elig["price_check"] = False
        dec_elig = {"description": True, "outcome": False, "original_entry": root_elig.get("original_entry", True), "price_check": root_elig.get("price_check", True),
                    "execution": bool(root_elig.get("execution", True)) and not is_orphan and not h1 and dec_plan_possible(root),
                    "entry_decision": t_dec is not None and root["instrument_id"] is not None and bool(root_elig.get("execution", True)) and dec_plan_possible(root)}
        if decision_replay_required:
            dec_elig = {k: False for k in ELIG_KEYS}
        # 簇（契约 §9.7 A5）：v0 经济簇 = 可用于簇的复制组（近似组 usable_for_cluster=false 不算），否则单机会自成一簇（= episode_id）；决策视图用 t_dec 前可见成员
        dgr = dg_rows.get(root["source_version_id"])
        desc_group = dgr["duplicate_group_id"] if (dgr and dgr.get("usable_for_cluster", True) and len(dg_members.get(dgr["duplicate_group_id"], [])) > 1) else None
        cluster_id = desc_group or dec_group or episode_id
        dec_cluster_id = (dec_group or episode_id) if dec["state"] else None
        dec_deriv = stable_id("dec-deriv", graph_version, dec["events"], dec["state"], dec["stop"], [(t["level"], t.get("fraction")) for t in dec["tps"]], dec["invalid"], dec["reasons"], dec_grade, dec_group, dec_cluster_id, rule_versions) if dec["state"] else None
        snap = snapshot_hash(graph_version=graph_version, episode_id=episode_id, root_plan_id=root_id, t_dec=t_dec, dependency_ids=dep_refs,
                             fields={"order_plan": dec_plan, "state": dec["state"], "n_events": len(dec["events"]), "invalid": dec["invalid"], "reasons": dec["reasons"], "grade": dec_grade,
                                     "dup_group": dec_group, "cluster": dec_cluster_id, "audit_stratum": dec_audit_stratum, "elig": dec_elig, "delay": processing_delay_s}, rule_versions=rule_versions) if dec_plan else None
        deriv = stable_id("deriv", graph_version, [(e["pid"], e["kind"]) for e in evs], p_state, c_state, stop, [t["level"] for t in tps], rule_versions, obs_end)
        ep_rows.append({
            "episode_id": episode_id, "graph_version": graph_version, "predecessor_ids": [], "successor_ids": [], "channel_id": root["channel_id"], "trader_id": root_mv.get("author_id"),
            "instrument_id": root["instrument_id"], "side": root["side"], "entry_branch_id": root_id, "duplicate_group_id": dg_rows.get(root["source_version_id"], {}).get("duplicate_group_id"), "cluster_id": cluster_id,
            "author_plan_state": p_state, "author_claim_state": c_state, "entry_observed": entry_observed, "exit_observed": exit_observed, "left_truncated": is_orphan,
            "right_censored": right_censored, "censor_at": censor_at, "censor_reason": censor_reason, "time_grade_min": weakest(grades),
            "decision_eligible_at": decision_eligible_at, "t_dec": t_dec, "processing_delay_s": processing_delay_s, "order_plan": dec_plan, "decision_snapshot_hash": snap,
            "temporal_assumptions": json.dumps({"processing_delay_s": processing_delay_s, "horizon_s": horizon_s, "order_policy": "strict_lt", "root_grade": grades[0], "observation_end": obs_end.isoformat()}),
            "dec_temporal_assumptions": json.dumps({"processing_delay_s": processing_delay_s, "order_policy": "strict_lt", "root_grade": grades[0]}) if dec["state"] else None,
            "claimed_outcome": claimed_outcome, "reconstructed_outcome": None, "eligibility_by_estimand": _elig(elig),
            "audit_stratum": "repost_same_channel" if dgr and dgr["dup_kind"] == "repost_same_channel" else None, "dec_audit_stratum": dec_audit_stratum, "sampling_probability": None, "label_status": "unresolved", "signoffs": [], "derivation_hash": deriv, "is_tombstone": False, "tombstoned_at": None, "migration_reason": None,
            "dec_author_plan_state": dec["state"][0] if dec["state"] else None, "dec_author_claim_state": dec["state"][1] if dec["state"] else None, "dec_stop": dec["stop"] if dec["state"] else None,
            "dec_tps": [t["level"] for t in dec["tps"]] if dec["state"] else None, "dec_n_events": len(dec["events"]) if dec["state"] else None, "dec_n_invalid_transitions": dec["invalid"] if dec["state"] else None,
            "dec_reason_codes": dec["reasons"] if dec["state"] else None, "dec_time_grade_min": dec_grade, "dec_duplicate_group_id": dec_group, "dec_derivation_hash": dec_deriv,
            "dec_eligibility": _elig(dec_elig) if dec["state"] else _elig({}), "dec_cluster_id": dec_cluster_id, "dependency_refs": dep_refs,
            "deleted_after_observation_any": deleted_any,
            "root_plan_id": root_id, "root_source_version_id": root["source_version_id"], "root_message_id": root["message_id"], "replay_required": replay_required,
            "n_events": len(evs), "n_invalid_transitions": n_invalid, "reason_codes": sorted(set(reasons)), "rule_version": RULE_VERSION, "batch_id": batch_id,
            "event_time": root["event_time"], "available_at": max((e["edge_available_at"] for e in evs if e["edge_available_at"]), default=root["available_at"]), "ingested_at": ingested_at,
        })
        if a_star is None and not is_orphan:
            qrows.append(quarantine_row(batch_id=batch_id, object_kind="episode", object_id=episode_id, object_version=graph_version, partition_id=f"channel={root['channel_id']}", reason_codes=[Reason.DEPENDENCY_NOT_AVAILABLE], rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH, event_time=root["event_time"], available_at=root["available_at"], ingested_at=ingested_at, dependency_refs=dep_refs))
        if n_invalid or is_orphan:
            qrows.append(quarantine_row(batch_id=batch_id, object_kind="episode", object_id=episode_id, object_version=graph_version, partition_id=f"channel={root['channel_id']}",
                                        reason_codes=[Reason.LIFECYCLE_INVALID] if n_invalid else [Reason.PARENT_MISSING], rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH,
                                        source_refs={"root_plan_id": root_id}, event_time=root["event_time"], available_at=root["available_at"], ingested_at=ingested_at,
                                        field_path="lifecycle", observed_value_ref=f"{p_state},{c_state}", expected_contract="ADR-G1 §3 转移表"))
    for pid in unresolved:
        p = plans[pid]
        qrows.append(quarantine_row(batch_id=batch_id, object_kind="canonical_plan", object_id=pid, object_version=p["source_version_id"], partition_id=f"channel={p['channel_id']}",
                                    reason_codes=sorted(set([Reason.ENTRY_LINK_AMBIGUOUS] + adj[pid]["reason_codes"])), rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH, source_refs={"message_id": p["message_id"]},
                                    event_time=p["event_time"], available_at=p["available_at"], ingested_at=ingested_at, field_path="link", observed_value_ref="unresolved", expected_contract="ADR-G1 §4"))
    # A terminal predecessor stays immutable; the new proposal owns a new episode and branch.
    for episode in ep_rows:
        root = plans[episode["root_plan_id"]]
        source = mvd.get(root["source_version_id"], {})
        parent_mid = source.get("reply_to_message_id")
        if parent_mid is None:
            explicit = _extract.re.search(r"重开(?:原单|旧单)?\s*[#＃]\s*(\d+)", source.get("text", ""))
            if explicit:
                parent_mid = int(explicit.group(1))
        if parent_mid is None or root["available_at"] is None:
            continue
        predecessors = []
        for prior in ep_rows:
            if prior["channel_id"] != episode["channel_id"] or prior["root_message_id"] != parent_mid or prior["episode_id"] == episode["episode_id"]:
                continue
            if prior["instrument_id"] != episode["instrument_id"] or prior["side"] != episode["side"] or not episode["trader_id"] or prior["trader_id"] != episode["trader_id"]:
                continue
            evidence = [e for e in ev_rows if e["episode_id"] == prior["episode_id"] and e["edge_available_at"] is not None and e["edge_available_at"] < root["available_at"]]
            if not evidence:
                continue
            last = max(evidence, key=lambda e: (e["edge_available_at"], e["event_seq"]))
            state = json.loads(last["payload"])["to"]
            if state[0] in ("cancelled", "expired") or state[1] == "claimed_closed":
                predecessors.append(prior["episode_id"])
        if len(predecessors) == 1:
            episode["predecessor_ids"] = predecessors
            episode["migration_reason"] = "reopen"
            for event in ev_rows:
                if event["episode_id"] == episode["episode_id"] and event["event_seq"] == 0:
                    payload = json.loads(event["payload"])
                    payload.update(reopen=True, predecessor_ids=predecessors)
                    event["payload"] = json.dumps(payload, sort_keys=True)
    from decimal import Decimal
    from .lake import q12

    def _dec(v):
        return None if v is None else Decimal(q12(v))

    for r in ep_rows:
        op = r["order_plan"]
        if op:
            for e in op["entries"]:
                e["price_lo"], e["price_hi"], e["fraction"] = _dec(e["price_lo"]), _dec(e["price_hi"]), _dec(e["fraction"])
            if op["stop"]:
                op["stop"]["price"] = _dec(op["stop"]["price"])
            for t in op["tps"]:
                t["level"], t["fraction"] = _dec(t["level"]), _dec(t["fraction"])
            op["sizing"]["qty"] = _dec(op["sizing"]["qty"])
        r["dec_stop"] = _dec(r["dec_stop"])
        r["dec_tps"] = [_dec(x) for x in r["dec_tps"]] if r["dec_tps"] is not None else None
    episodes = pl.DataFrame(ep_rows, schema=EPISODE_SCHEMA) if ep_rows else pl.DataFrame(schema=EPISODE_SCHEMA)
    events = pl.DataFrame(ev_rows, schema=EVENT_SCHEMA) if ev_rows else pl.DataFrame(schema=EVENT_SCHEMA)
    # 记账（层 6）：输入 = 全部 canonical_plan（parser 行）；输出 = episode；管理归并 → merge 映射；未解决 → review
    ledgers: dict[tuple[int, str], LayerLedger] = {}
    root_of: dict[str, str] = {}
    for r in ep_rows:
        root_of[r["root_plan_id"]] = r["episode_id"]
        for pid in members[r["root_plan_id"]]:
            root_of[pid] = r["episode_id"]
    for p in cp.iter_rows(named=True):
        pid = p["plan_id"]
        y = str(p["event_time"].year) if p["event_time"] else "unknown"
        led = ledgers.setdefault((p["channel_id"], y), LayerLedger(6, {"channel_id": p["channel_id"], "year": y}, "canonical_plan", "episode"))
        if pid in root_of:
            led.mark(pid, "ok", [])
            led.map(pid, root_of[pid], "one_to_one" if pid in members else "merge")
        elif pid in unresolved:
            led.mark(pid, "review", [Reason.ENTRY_LINK_AMBIGUOUS])
            led.map(pid, None, "excluded", [Reason.ENTRY_LINK_AMBIGUOUS])
        else:
            led.mark(pid, "review", [Reason.NOT_SIGNAL])
            led.map(pid, None, "excluded", [Reason.NOT_SIGNAL])
    summary = {"graph_version": graph_version, "n_episodes": episodes.height, "n_events": events.height, "n_orphan_roots": len(orphan_roots), "n_unresolved": len(unresolved),
               "plan_states": {k: v for k, v in sorted(episodes.group_by("author_plan_state").len().iter_rows())} if episodes.height else {},
               "claim_states": {k: v for k, v in sorted(episodes.group_by("author_claim_state").len().iter_rows())} if episodes.height else {},
               "n_invalid_transitions": int(episodes["n_invalid_transitions"].sum()) if episodes.height else 0, "n_decision_roots": int(episodes["t_dec"].is_not_null().sum()) if episodes.height else 0,
               "processing_delay_s": processing_delay_s, "observation_end": obs_end.isoformat(), "rule_versions": rule_versions, "batch_id": batch_id}
    return episodes, events, qrows, ledgers, summary


def _sig(df: pl.DataFrame, key: str) -> str:
    """Portable q12 semantic hashing, independent of Polars' native row-hash implementation."""
    cols = [c for c in df.columns if c != "ingested_at" and df.schema[c] not in (pl.Float32, pl.Float64)]
    return stable_id(df.select(cols).sort(key).to_dicts())


def input_hash_of(layout: Layout, *, ingested_at: datetime | None = None, registry_version: str = "registry-synthetic-v1", **kw) -> str:
    """图版本输入 hash：全部 parser plan 列、候选边全列、裁决全列、复制组全列、规则版本、延迟/终点/H（S10）。"""
    ingested_at = ingested_at or now_utc()
    cp = pl.read_parquet(layout.canonical_plan)
    cb = pl.read_parquet(layout.silver_dir / "candidate_edges.parquet")
    jd = pl.read_parquet(layout.silver_dir / "adjudications.parquet")
    dg = pl.read_parquet(layout.duplicate_group) if layout.duplicate_group.exists() else None
    mv = pl.read_parquet(layout.message_version)
    ex = pl.read_parquet(layout.extracted_event) if layout.extracted_event.exists() else pl.DataFrame()
    mv_used = mv  # album members and intermediate parents are structural inputs too
    if not registry_version and "registry_version" in cp.columns and cp.height:
        registry_version = cp["registry_version"][0] or ""
    rule_versions = {"normalize": _normalize.RULE_VERSION, "dedup": _dedup.RULE_VERSION, "extract": _extract.RULE_VERSION, "validate": _validate.RULE_VERSION, "linker": _linker.RULE_VERSION, "lifecycle": RULE_VERSION, "registry": registry_version}
    obs_end = kw.get("observation_end") or max((t for t in cp["available_at"].to_list() if t is not None), default=ingested_at)
    return stable_id("gin", _sig(cp, "plan_id"), _sig(cb, "candidate_id"), _sig(jd, "request_id"), _sig(dg, "source_version_id") if dg is not None else 0, _sig(mv_used, "source_version_id"), _sig(ex, "extract_id") if ex.height else "",
                     rule_versions, kw.get("processing_delay_s", DEFAULT_PROCESSING_DELAY_S), obs_end.isoformat(), kw.get("horizon_s", HORIZON_S))


def run(layout: Layout, *, graph_version: str, ingested_at: datetime | None = None, registry_version: str = "registry-synthetic-v1", **kw) -> dict[str, Any]:
    if is_tombstoned(layout, graph_version):
        raise RuntimeError(f"graph_version={graph_version} 已 tombstone，不可重写；请用新版本号")
    ingested_at = ingested_at or now_utc()
    cp = pl.read_parquet(layout.canonical_plan)
    mv = pl.read_parquet(layout.message_version)
    cb = pl.read_parquet(layout.silver_dir / "candidate_edges.parquet")
    jd = pl.read_parquet(layout.silver_dir / "adjudications.parquet")
    dg = pl.read_parquet(layout.duplicate_group) if layout.duplicate_group.exists() else None
    input_hash = input_hash_of(layout, ingested_at=ingested_at, registry_version=registry_version, **kw)
    old = read_manifest(layout, graph_version)
    if old is not None and old.get("input_hash") != input_hash:
        raise RuntimeError(f"graph_version={graph_version} 已发布且输入不同（不可变派生版本）：换新版本号；不提供 force")
    episodes, events, qrows, ledgers, summary = build_graph(cp, mv, cb, jd, dg, graph_version=graph_version, registry_version=registry_version, ingested_at=ingested_at, **kw)
    layout.ensure()
    old_ep = pl.read_parquet(layout.episode(graph_version)) if layout.episode(graph_version).exists() else None
    old_ev = pl.read_parquet(layout.episode_event(graph_version)) if layout.episode_event(graph_version).exists() else None
    write_parquet_atomic(preserve_ingested_at(episodes, old_ep, "episode_id"), layout.episode(graph_version))
    write_parquet_atomic(preserve_ingested_at(events, old_ev, "event_id"), layout.episode_event(graph_version))
    append_quarantine(layout.quarantine_path, qrows)
    batch_id = summary["batch_id"]
    lrows, maps = [], []
    for key in sorted(ledgers, key=lambda k: (k[0], k[1])):
        row, _ = loss_row_from_ledger(ledgers[key], batch_id=batch_id, cum_excluded_prev=cum_prev(layout, batch_id, (1, 2, 3, 4, 5), ledgers[key].stratum), rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH, clocks=(episodes["event_time"].max() if episodes.height else None, episodes["available_at"].max() if episodes.height else None, ingested_at))
        lrows.append(row)
        maps += mapping_rows(ledgers[key], batch_id=batch_id, rule_version=RULE_VERSION, ingested_at=ingested_at)
    write_loss(layout.loss(batch_id), lrows, replace_layers={6})
    write_mapping(layout.mapping(batch_id, 6), maps)
    from .graph import record_migration
    for episode in episodes.iter_rows(named=True):
        if episode["migration_reason"] == "reopen":
            record_migration(layout, old_graph_version=graph_version, new_graph_version=graph_version, predecessor_ids=episode["predecessor_ids"], successor_ids=[episode["episode_id"]], reason="reopen", approved_by=RULE_VERSION, at=episode["t_dec"] or ingested_at)
    publish_manifest(layout, graph_version, input_hash=input_hash, rule_versions=summary["rule_versions"], assumptions={"processing_delay_s": summary["processing_delay_s"], "observation_end": summary["observation_end"], "horizon_s": kw.get("horizon_s", HORIZON_S)},
                     counts={"episodes": episodes.height, "events": events.height, "decision_roots": summary["n_decision_roots"]}, built_at=ingested_at)
    summary["input_hash"] = input_hash
    summary["paths"] = {"episode": str(layout.episode(graph_version)), "episode_event": str(layout.episode_event(graph_version))}
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="链接后重放双轨状态机，落 gold/episode 与 episode_event 并发布 manifest")
    ap.add_argument("--graph-version", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out")
    g.add_argument("--lake-root")
    a = ap.parse_args(argv)
    layout = Layout.flat(a.out) if a.out else Layout.from_root(a.lake_root)
    print(json.dumps(run(layout, graph_version=a.graph_version), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
