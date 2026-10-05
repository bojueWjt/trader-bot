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
import re
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from . import dedup as _dedup
from . import extract as _extract
from . import linker as _linker
from . import normalize as _normalize
from . import validate as _validate
from .graph import DEFAULT_PROCESSING_DELAY_S, closure_available_at, dependency_index, plan_dependencies, decision_visible, is_tombstoned, publish_manifest, read_manifest, snapshot_hash, t_dec_of
from .lake import D12, LayerLedger, Layout, append_quarantine, cum_prev, loss_row_from_ledger, mapping_rows, now_utc, preserve_ingested_at, quarantine_row, schema_hash, stable_id, write_loss, write_mapping, write_parquet_atomic
from .reasons import Reason
from .close_stop import parse_close_stop
from . import cx_v2 as _cx_v2
from . import plan_merge as _plan_merge

RULE_VERSION = "tg-lifecycle-v0.8"  # v8: plan_merge (F2/F6), signal age (F8), triage sidecar gating (F3), plan_link table.
HORIZON_S = 7 * 24 * 3600
STALE_SIGNAL_S = 1800  # 规则 14：signal_age_s > 1800 → STALE_SIGNAL_30M
EDIT_OUTCOME_S = 300  # edit_may_contain_outcome：编辑晚于发帖 5 分钟以上且带结果词
TRIAGE_FILE = "nostop_triage.parquet"
#: 冻结 sidecar schema（方案 §4 / F3）：主键 (source_version_id, branch_index)；其余列按名读取，不 import cx_triage。
TRIAGE_KEY = ("source_version_id", "branch_index")
TRIAGE_VERDICTS = ("new_entry", "not_entry", "uncertain")
#: D4 劝阻词（§1）：「不要跟/别跟」后面紧跟 风/在/大/着 的不算；「别」是词内字（区别跟、特别跟、级别跟、分别跟、识别跟）时也不算
CONTRACT_DISCOURAGED_RE = re.compile(r"合约先别做|合约先等|先别开合约|不建议跟|不许合约|(?:不要|(?<![区特级分识鉴性派类])别)跟(?![风在大着])")
#: Cash 观察帖：「关注区域 …… 失效 ……」（方案 F3/D6 的原词，与 D 组 cx_triage 的审计正则同一口径）。G1 不知道频道名，
#: 规则对所有频道生效，各频道命中数写进构建报告（v8_report.cash_watch_post_by_channel），由 G0 核对是否只出现在 Cash。
CASH_WATCH_RE = re.compile(r"关注区域")
CASH_WATCH_INVALIDATION_RE = re.compile(r"失效")
#: F12 分级止损（不建模，只计数 staged_stop_dropped）：同一段落里止损标签后紧跟两个以上不同价位，或明写分批/分级止损。
#: 只认「止损/防守/SL 后面直接是数字」，「止损上移至 X」这类移损不算；这是计数上界，不改任何单。
STAGED_STOP_VALUE_RE = re.compile(r"(?:止损|防守|SL|stop)\s*(?:位|价)?\s*[:：]?\s*(\d+(?:\.\d+)?)", re.I)
STAGED_STOP_WORDS_RE = re.compile(r"分批止损|分级止损|第[一二三123]止损|止损[一二三123]\s*[:：]")
LOW_LEVERAGE_RE = re.compile(r"1倍|一倍|低倍|不会爆仓|不设止损|不用止损")
NOSTOP_HINT_KINDS = ("zero_distance_break", "reference_level", "ambiguous_break", "relative_ambiguous")
EXECUTION_BLOCKING = frozenset({Reason.SAME_PLAN_COMPANION, Reason.SAME_PLAN_RESTATEMENT, Reason.STALE_SIGNAL_30M, Reason.STALE_EDIT_30M,
                                Reason.TRIAGE_NOT_ENTRY, Reason.TRIAGE_UNCERTAIN, Reason.TRIAGE_MISSING, Reason.TRIAGE_INVALID,
                                Reason.CASH_WATCH_POST, Reason.CONTRACT_DISCOURAGED, Reason.PROMOTED_WIDE_ONLY})

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
    "stop": pl.Struct({"price": D12, "trigger": pl.String, "timeframe": pl.String}),
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
    # v8 §4 episode 新列（全部进入决策快照哈希）
    "plan_group_id": pl.String, "family_id": pl.String, "dup_of": pl.String, "plan_link_kind": pl.String,
    "repost_of": pl.String, "amend_of": pl.String, "reentry_of": pl.String, "reentry_parent_stop": D12,
    "stop_rule": pl.String, "stop_base": D12, "stop_source_version_id": pl.String,
    "nostop_kind": pl.String, "venue_hint": pl.String, "triage_verdict": pl.String, "triage_reason": pl.String, "promotion_scope": pl.String,
    "signal_age_s": pl.Int64, "edit_delay_s": pl.Int64, "signal_anchor": pl.String,
    "edit_original_unavailable": pl.Boolean, "edit_may_contain_outcome": pl.Boolean, "entry_legs_n": pl.Int32, "time_ref_promoted": pl.String,
    "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}
#: gold/plan_link__<gv>.parquet：每个入场分支和每条补充一行（方案 §4）
PLAN_LINK_SCHEMA: dict[str, Any] = {
    "channel_id": pl.Int64, "message_id": pl.Int64, "source_version_id": pl.String, "branch_index": pl.Int32,
    "episode_id": pl.String, "kept_episode_id": pl.String, "family_id": pl.String, "plan_link_kind": pl.String,
    "target_message_id": pl.Int64, "gap_s": pl.Int64, "synthetic_action": pl.String, "synthetic_at": pl.Datetime("us", "UTC"),
    "synthetic_stop_price": D12, "merge_version": pl.String,
    "supplement_status": pl.String,
    "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}
#: plan_link.supplement_status（只在补充止损行上有值；plan_link_kind 只取 §4 冻结枚举）：
#:   merged（kind=supplement）、late_stop（kind=late_stop，合成 move_stop）、stop_move（根已有止损，属移损，交跟单；kind 空）、
#:   rejected:<reason>（F6 三道校验或上游换算不过；kind 空）


def plan_link_signature(frame: pl.DataFrame) -> str:
    """Content signature of a plan_link table: every column but ingested_at, independent of row order, so rebuilding the
    same graph version gives the same signature and a stale or foreign table does not."""
    cols = [c for c in frame.columns if c != "ingested_at"]
    rows = sorted(json.dumps(r, sort_keys=True, ensure_ascii=False, default=str) for r in frame.select(cols).to_dicts())
    return stable_id("plan-link", rows)
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


def _order_plan(root: dict[str, Any], stop: float | None, tps: list[dict], expires_after_s: int | None,
                stop_meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """冻结计划快照：只写有原文来源的字段；缺入场方式 → None（不猜市价）；缺分配比例留 null（由 G2 policy 展开，S06）。

    v8 F4：stop_meta 来自止损所在那条消息（rule = silver checks.stop_rule.rule；injected = 止损来自别的消息）。
    有 silver 标记或注入的止损时不再按本条的条件止损映射置空；close_from_clause 以收盘触发。"""
    if root["instrument_id"] is None or root["side"] not in ("long", "short"):
        return None
    e = root["entry"]
    mode = root.get("entry_mode") or ("price" if e else "unknown")
    if e is None and mode != "market_ref":
        return None
    if e is None:
        entries = [{"kind": "market_ref", "price_lo": None, "price_hi": None, "fraction": None, "tif": "IOC", "post_only": False}]
    elif e["kind"] == "market_ref":
        entries = [{"kind": "market_ref", "price_lo": e["lo"], "price_hi": e["hi"], "fraction": None, "tif": "IOC", "post_only": False}]
    elif e["kind"] == "ladder" and root["entries"]:
        entries = [{"kind": "limit", "price_lo": x, "price_hi": x, "fraction": None, "tif": "GTC", "post_only": False} for x in root["entries"]]
    elif e["kind"] == "zone":
        entries = [{"kind": "ladder", "price_lo": e["lo"], "price_hi": e["hi"], "fraction": None, "tif": "GTC", "post_only": False}]
    else:
        entries = [{"kind": "limit", "price_lo": e["lo"], "price_hi": e["hi"], "fraction": None, "tif": "GTC", "post_only": False}]
    stop_plan = {"price": stop, "trigger": "mark"} if stop is not None else None
    stop_meta = stop_meta or {}
    if stop_plan is not None and stop_meta.get("rule") == "close_from_clause":
        if not stop_meta.get("timeframe"):
            return None  # a close condition without its timeframe is never turned into an intraday price: no plan
        stop_plan = {"price": stop, "trigger": "close", "timeframe": stop_meta["timeframe"]}
    checks = json.loads(root.get("checks") or "{}")
    # checks.action keeps the quoted values; a price whose unit was inherited (cx_v2.inherited_prices) is rescaled here.
    rescale = Decimal(str(checks.get("unit_rescaled", {}).get("factor", 1)))
    factor = {u["field"]: Decimal(u["factor"]) for u in checks.get("unit_inherited", [])}
    if checks.get("schema_version") == 2:
        action = checks.get("action", {})
        source_entry = action.get("entry")
        if source_entry and source_entry["kind"] == "ladder":
            # An unpriced CMP leg is filled from the as-of mark at t_dec (l0_replay); an unpriced limit is not guessable.
            if any(level.get("price") is None and level.get("kind") != "market_ref" for level in source_entry["levels"]):
                return None
            entries = []
            for i, level in enumerate(source_entry["levels"]):
                price = Decimal(level["price"]["value"]) * factor.get(f"entry.levels[{i}].price", 1) if level.get("price") is not None else None
                if price is not None:
                    price *= rescale
                fraction = level.get("fraction")
                entries.append(dict(kind=level["kind"], price_lo=price, price_hi=price,
                                    fraction=Decimal(fraction["value"]) / 100 if fraction else None,
                                    tif="IOC" if level["kind"] == "market_ref" else "GTC", post_only=False))
        source_stop = action.get("stop")
        marked = bool(stop_meta.get("injected") or stop_meta.get("rule") or checks.get("stop_rule"))
        if source_stop and source_stop["kind"] == "condition" and not marked:
            atom = source_stop.get("price")
            level = Decimal(atom["value"]) * factor.get("stop.price", 1) if atom is not None else None
            if level is not None:
                level *= rescale
            close_stop = parse_close_stop(source_stop.get("condition"), level)
            stop_plan = None
            if close_stop is not None:
                stop_plan = {"price": close_stop["level"], "trigger": "close", "timeframe": close_stop["timeframe"]}
    dropped = []
    if stop_plan is not None:
        kept = []
        for leg in entries:
            price = leg["price_lo"] if root["side"] == "long" else leg["price_hi"]
            ok = price is None or (price > stop_plan["price"] if root["side"] == "long" else price < stop_plan["price"])
            if ok:
                kept.append(leg)
            else:
                dropped.append({"price": str(price), "reason": "beyond_stop"})
        entries = kept
    if not entries:
        return None
    if dropped:
        checks["entries_dropped_direction"] = dropped
    fractions = [leg["fraction"] for leg in entries]
    if all(f is not None for f in fractions) and sum(fractions) > 0 and sum(fractions) != 1:
        total = sum(fractions)
        for leg in entries:
            leg["fraction"] /= total
        entries[-1]["fraction"] = Decimal(1) - sum(leg["fraction"] for leg in entries[:-1])
        checks["fractions_normalized"] = {"total": str(total), "basis": "retained_entries"}
    priced = [leg["price_hi"] if root["side"] == "long" else leg["price_lo"] for leg in entries if leg["price_lo"] is not None]
    if priced:
        edge = max(priced) if root["side"] == "long" else min(priced)
        kept = []
        for tp in tps:
            ok = tp["level"] > edge if root["side"] == "long" else tp["level"] < edge
            if ok:
                kept.append(tp)
            else:
                checks.setdefault("tps_dropped_direction", []).append({"level": str(tp["level"]), "reason": "wrong_entry_side"})
        tps = kept
    root["checks"] = json.dumps(checks, ensure_ascii=False, sort_keys=True)
    return {"instrument_id": root["instrument_id"], "side": root["side"], "entries": entries,
            "stop": stop_plan,
            "tps": [{"level": t["level"], "fraction": t.get("fraction")} for t in tps],
            "sizing": {"mode": "risk_budget", "qty": None}, "expiry": {"entry_ttl_s": int(expires_after_s) if expires_after_s else None, "max_holding_s": None}, "reduce_only_exit": True}


def triage_index(triage: pl.DataFrame | None) -> dict[tuple[str, int], dict[str, Any]] | None:
    """Frozen sidecar schema (F3): one row per (source_version_id, branch_index); None = no sidecar (stage 1)."""
    if triage is None:
        return None
    missing = [c for c in (*TRIAGE_KEY, "verdict") if c not in triage.columns]
    if missing:
        raise ValueError(f"triage sidecar 缺列 {missing}（冻结 schema：主键 source_version_id+branch_index，另有 verdict 等）")
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for row in triage.iter_rows(named=True):
        key = (row["source_version_id"], int(row["branch_index"] or 0))
        if key in out:
            raise ValueError(f"triage sidecar 主键重复：{key}")
        out[key] = row
    return out


#: frozen sidecar exclusion_code → (label, reason); None = an ok new_entry
_TRIAGE_EXCLUSIONS = {"TRIAGE_NOT_ENTRY": ("not_entry", Reason.TRIAGE_NOT_ENTRY), "TRIAGE_UNCERTAIN": ("uncertain", Reason.TRIAGE_UNCERTAIN),
                      "TRIAGE_INVALID": ("invalid", Reason.TRIAGE_INVALID), "TRIAGE_MISSING": ("missing", Reason.TRIAGE_MISSING)}


def triage_status(row: dict[str, Any] | None) -> tuple[str, Reason | None]:
    """(verdict label, blocking reason in the main scope), read by the frozen sidecar schema (F3 / D cx_triage._sidecar_schema).

    status decides first: missing (no recording, abstain, no prompt; verdict is null there) → TRIAGE_MISSING; invalid
    (failed a deterministic check) → TRIAGE_INVALID. Only an ok row's verdict is read. MISSING and INVALID are never
    folded into each other (uncomparable is not equal). When the row carries exclusion_code it must agree with what
    status/verdict say; a disagreement or an unknown status/verdict is an invalid row."""
    if row is None:
        return "missing", Reason.TRIAGE_MISSING
    status = str(row.get("status") or "").lower()
    verdict = row.get("verdict")
    if status in ("missing", "abstain", "abstained"):
        out = ("missing", Reason.TRIAGE_MISSING)
    elif status in ("invalid", "rejected") or row.get("invalid") is True or row.get("valid") is False:
        out = ("invalid", Reason.TRIAGE_INVALID)
    elif status not in ("", "ok") or verdict not in TRIAGE_VERDICTS:
        return "invalid", Reason.TRIAGE_INVALID
    elif verdict == "new_entry":
        out = ("new_entry", None)
    elif verdict == "not_entry":
        out = ("not_entry", Reason.TRIAGE_NOT_ENTRY)
    else:
        out = ("uncertain", Reason.TRIAGE_UNCERTAIN)
    if "exclusion_code" in row:
        code = row.get("exclusion_code")
        expected = _TRIAGE_EXCLUSIONS.get(code, ("new_entry", None)) if code is None or code in _TRIAGE_EXCLUSIONS else None
        if expected != out:
            return "invalid", Reason.TRIAGE_INVALID
    return out


def staged_stop(seg: str) -> bool:
    """F12: the paragraph states more than one stop level (only the decision stop is modelled)."""
    values = {Decimal(v) for v in STAGED_STOP_VALUE_RE.findall(seg or "")}
    return len(values) >= 2 or bool(STAGED_STOP_WORDS_RE.search(seg or ""))


def _decimal(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _segment_text(text: str, checks: dict[str, Any], symbol_raw: str | None) -> str:
    """The branch's own paragraph (stop_rules.segment), whole text when it cannot be cut."""
    action = checks.get("action") if checks.get("schema_version") == 2 else None
    if not action or not text:
        return text or ""
    from . import stop_rules
    try:
        bounds = stop_rules.segment(text, dict(action, symbol_raw=symbol_raw))
    except (KeyError, TypeError, ValueError):
        bounds = None
    return text[bounds[0]:bounds[1]] if bounds else text


def _merge_item(p: dict[str, Any], mvd: dict[str, dict], t_post: dict[tuple, datetime], *, supplement_of: str | None = None,
                conditional: bool = False) -> _plan_merge.Item | None:
    if p.get("available_at") is None:
        return None
    m = mvd.get(p["source_version_id"], {})
    checks = json.loads(p.get("checks") or "{}")
    text = m.get("text") or ""
    seg = _segment_text(text, checks, p.get("symbol_raw"))
    e = p.get("entry")
    legs: list[Decimal] = []
    market_ref = False
    if p.get("entries"):
        legs = [Decimal(str(v)) for v in p["entries"]]
    elif e and e.get("kind") != "market_ref" and e.get("lo") is not None:
        legs = sorted({Decimal(str(e["lo"])), Decimal(str(e["hi"]))})
    if e is None and p.get("entry_mode") == "market_ref" or (e and e.get("kind") == "market_ref"):
        market_ref = True
    action = checks.get("action") or {}
    if (action.get("entry") or {}).get("kind") == "ladder" and any(l.get("kind") == "market_ref" for l in action["entry"].get("levels", [])):
        market_ref = True
    stop = _decimal(p.get("stop"))
    compact = re.sub(r"\s", "", text)
    return _plan_merge.Item(
        pid=p["plan_id"], svid=p["source_version_id"], channel=p["channel_id"], message_id=p["message_id"], t_vis=p["available_at"],
        t_post=t_post.get((p["channel_id"], p["message_id"]), p["available_at"]), author=m.get("author_id"), reply_to=m.get("reply_to_message_id"),
        sequence=m.get("sequence"), inst=p.get("instrument_id"), side=p.get("side"), branch_index=int(p.get("branch_index") or 0),
        legs=tuple(legs), market_ref=market_ref, mark=_decimal(p.get("mark_price")), entry_kind=(e or {}).get("kind"), stop=stop,
        quote=_decimal(e["lo"]) if e and e.get("kind") == "market_ref" and e.get("lo") is not None else None,
        tps=tuple(t["level"] for t in (p.get("tps") or [])), text=text, segment=seg,
        has_media=bool(m.get("media_hashes") or m.get("media_kinds")) or bool(checks.get("chart_fill")),
        is_title=not legs and stop is None and len(compact) <= 40, card=_plan_merge.is_card(seg),
        venue_words=bool(_plan_merge.VENUE_WORDS.search(seg)), reentry_words=bool(_plan_merge.REENTRY_WORDS.search(seg)),
        supplement_of=supplement_of, conditional=conditional)


def _edit_delay(row: dict[str, Any]) -> int | None:
    """Edit delay of one H1 version: last_edit_at − message_date; 0 for other grades; None = unknown.

    Under --edit-visible-at-post event_time is the original post time, so it says nothing about the edit: a row without
    a recorded edit_delay_s there (edit time missing or inconsistent, edit_delay_unknown) is unknown, never 0."""
    if row.get("time_grade") != "H1":
        return 0
    ta = json.loads(row.get("temporal_assumptions") or "{}")
    if ta.get("edit_delay_s") is not None:
        return int(ta["edit_delay_s"])
    if ta.get("edit_delay_unknown") or ta.get("edit_visible_at_post"):
        return None
    if row.get("event_time") is not None and row.get("message_date") is not None:
        return max(0, _plan_merge.whole_seconds(row["event_time"] - row["message_date"]))
    return None


def _edit_too_late(delay: int | None, limit: int | None) -> bool:
    """-v8e pool removal: over the limit, or an unknown delay (an unknown edit time is not a short one)."""
    return limit is not None and (delay is None or delay > limit)


def _stop_meta(src: dict[str, Any] | None, *, injected: bool) -> dict[str, Any]:
    """Live v4 stop columns from the plan that supplied the decision stop (方案 §4：stop_rule / stop_base / stop_source_version_id)."""
    if src is None:
        return {"rule": None, "base": None, "svid": None, "timeframe": None, "injected": injected}
    checks = json.loads(src.get("checks") or "{}")
    rule = checks.get("stop_rule") or {}
    if checks.get("stop_from_mark"):
        return {"rule": "r9_fuzzy_break", "base": _decimal(checks["stop_from_mark"]["mark"]), "svid": src["source_version_id"], "timeframe": None, "injected": injected}
    if checks.get("stop_relative_resolved"):
        return {"rule": "relative", "base": None, "svid": src["source_version_id"], "timeframe": None, "injected": injected}
    if rule.get("rule"):
        return {"rule": rule["rule"], "base": _decimal(rule.get("base")), "svid": src["source_version_id"], "timeframe": rule.get("timeframe"), "injected": injected}
    return {"rule": "supplement" if injected else None, "base": None, "svid": src["source_version_id"], "timeframe": None, "injected": injected}


def _rule_versions(registry_version: str, *, triage_version: str = "", wide: bool = False, supplement_window_s: int = _plan_merge.DEFAULT_SUPPLEMENT_S,
                   max_edit_delay_s: int | None = None) -> dict[str, str]:
    versions = {"normalize": _normalize.RULE_VERSION, "dedup": _dedup.RULE_VERSION, "extract": _extract.RULE_VERSION, "validate": _validate.RULE_VERSION,
                "linker": _linker.RULE_VERSION, "lifecycle": RULE_VERSION, "registry": registry_version, "plan_merge": _plan_merge.PLAN_MERGE_VERSION}
    if triage_version:
        versions["triage"] = triage_version
    variant = variant_signature(wide=wide, supplement_window_s=supplement_window_s, max_edit_delay_s=max_edit_delay_s)
    if variant:
        versions["variant"] = variant
    return versions


def variant_signature(*, wide: bool = False, supplement_window_s: int = _plan_merge.DEFAULT_SUPPLEMENT_S, max_edit_delay_s: int | None = None) -> str:
    """Non-default variant switches as one string ('' for the main scope)."""
    parts = []
    if wide:
        parts.append("wide")
    if int(supplement_window_s) != _plan_merge.DEFAULT_SUPPLEMENT_S:
        parts.append(f"supplement_window_s={int(supplement_window_s)}")
    if max_edit_delay_s is not None:
        parts.append(f"max_edit_delay_s={int(max_edit_delay_s)}")
    return ";".join(parts)


def build_graph(cp: pl.DataFrame, mv: pl.DataFrame, cb: pl.DataFrame, jd: pl.DataFrame, dg: pl.DataFrame | None, *, graph_version: str, registry_version: str = "",
                processing_delay_s: int = DEFAULT_PROCESSING_DELAY_S, horizon_s: int = HORIZON_S, ingested_at: datetime | None = None,
                observation_end: datetime | None = None, plan_source: str = "parser", extracted_event: pl.DataFrame | None = None,
                triage: pl.DataFrame | None = None, triage_version: str = "", wide: bool = False,
                supplement_window_s: int = _plan_merge.DEFAULT_SUPPLEMENT_S, max_edit_delay_s: int | None = None) -> tuple[pl.DataFrame, pl.DataFrame, list[dict], dict[tuple[int, str], LayerLedger], dict[str, Any]]:
    """v8 变体开关：wide（-v8w：执行存疑/缺失分诊与宽口径升级）、supplement_window_s（-v8nw 取 0）、
    max_edit_delay_s（-v8e：编辑延迟超过它的 H1 版本先移出合并池再重算）。triage 为冻结 schema 的 sidecar（None = stage 1）。"""
    ingested_at = ingested_at or now_utc()
    batch_id = cp["batch_id"][0] if cp.height else "tg-empty"
    from .plan_source import select_plans, disagreements, descriptive_only
    plans = {r["plan_id"]: r for r in select_plans(cp, plan_source, extracted_event).iter_rows(named=True)}
    conflicts = disagreements(cp, extracted_event)
    mvd = {r["source_version_id"]: r for r in mv.iter_rows(named=True)}
    dep_index = dependency_index(mvd)  # mvd is read-only below
    dg_rows = {r["source_version_id"]: r for r in dg.iter_rows(named=True)} if dg is not None and dg.height else {}
    dg_members: dict[str, list[str]] = {}
    for r in dg_rows.values():
        dg_members.setdefault(r["duplicate_group_id"], []).append(r["source_version_id"])
    sel = {r["from_plan_id"]: r for r in cb.filter(pl.col("selected")).iter_rows(named=True) if r["from_plan_id"] in plans and r["to_plan_id"] in plans}
    adj = {r["from_plan_id"]: r for r in jd.iter_rows(named=True) if r["from_plan_id"] in plans}
    roots = [pid for pid, p in plans.items() if p["kind"] == "entry_proposal" and pid not in adj and not descriptive_only(p)]
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
    rule_versions = _rule_versions(registry_version, triage_version=triage_version, wide=wide, supplement_window_s=supplement_window_s,
                                   max_edit_delay_s=max_edit_delay_s)

    def key(pid: str) -> tuple:
        p = plans[pid]
        return (p["available_at"] or obs_end, mvd.get(p["source_version_id"], {}).get("sequence") or 0, pid)

    # ---------------------------------------------------------------- v8 pre-pass: executability, plan merge (F2/F6/F3/F8/F11)
    tri = triage_index(triage)
    stage = 2 if tri is not None else 1
    t_post: dict[tuple, datetime] = {}
    for r in mvd.values():
        ident = (r.get("channel_id"), (r.get("source_id") or {}).get("message_id"))
        if r.get("available_at") is not None and (ident not in t_post or r["available_at"] < t_post[ident]):
            t_post[ident] = r["available_at"]
    root_info: dict[str, dict[str, Any]] = {}
    for rid in roots:
        p = plans[rid]
        checks = json.loads(p.get("checks") or "{}")
        text = mvd.get(p["source_version_id"], {}).get("text") or ""
        seg = _segment_text(text, checks, p.get("symbol_raw"))
        promoted = checks.get("time_ref_promoted") or None
        blocks: list[str] = []
        if CONTRACT_DISCOURAGED_RE.search(seg):
            blocks.append(Reason.CONTRACT_DISCOURAGED)
        if CASH_WATCH_RE.search(seg) and CASH_WATCH_INVALIDATION_RE.search(seg):
            blocks.append(Reason.CASH_WATCH_POST)
        if promoted and promoted.get("scope") == "wide" and not wide:
            blocks.append(Reason.PROMOTED_WIDE_ONLY)
        needs_triage = p.get("stop") is None or bool(promoted)
        verdict, verdict_reason, triage_row = ("n/a", None, None) if not needs_triage else (None, None, None)
        if needs_triage and tri is not None:
            triage_row = tri.get((p["source_version_id"], int(p.get("branch_index") or 0)))
            verdict, verdict_reason = triage_status(triage_row)
            if verdict_reason is not None:
                blocks.append(verdict_reason)
        edit_delay = _edit_delay(mvd.get(p["source_version_id"], {}))
        stale_edit = _edit_too_late(edit_delay, max_edit_delay_s)
        if stale_edit:
            blocks.append(Reason.STALE_EDIT_30M)
        # The wide scope (-v8w) executes uncertain, missing and invalid triage; the code is still recorded.
        wide_ok = {Reason.TRIAGE_UNCERTAIN, Reason.TRIAGE_MISSING, Reason.TRIAGE_INVALID} if wide else set()
        blocking = [b for b in blocks if b not in wide_ok]
        root_info[rid] = {"checks": checks, "seg": seg, "promoted": promoted, "verdict": verdict, "triage": triage_row, "blocks": blocks,
                          "blocking": blocking, "stale_edit": stale_edit, "edit_delay": edit_delay}
    items: list[_plan_merge.Item] = []
    for rid in roots:
        if root_info[rid]["stale_edit"]:
            continue  # -v8e: out of the merge pool before merging (TR-3)
        it = _merge_item(plans[rid], mvd, t_post)
        if it is None:
            continue
        it.executable = not root_info[rid]["blocking"]
        tr = root_info[rid]["triage"]
        if tr is not None and root_info[rid]["verdict"] not in ("invalid", "missing"):
            it.relation = tr.get("relation")
            target = tr.get("relation_target_message_id")
            it.relation_target = int(target) if target is not None else None
        items.append(it)
    pooled = {it.pid for it in items}
    supplements, scale_rejected = [], {}
    for rid in roots:
        if rid not in pooled:
            continue
        for pid in members.get(rid, []):
            p = plans[pid]
            if p["kind"] not in ("stop_move", "amend") or p.get("stop") is None or sel[pid]["method"] == "same_source":
                continue
            if _edit_too_late(_edit_delay(mvd.get(p["source_version_id"], {})), max_edit_delay_s):
                continue
            it = _merge_item(p, mvd, t_post, supplement_of=rid)
            if it is None:
                continue
            if Reason.UNIT_SCALE_CONFLICT in (p.get("reason_codes") or []):
                scale_rejected[pid] = "scale_not_unique"
            supplements.append(it)
    conditionals = []
    for pid, p in plans.items():
        checks = json.loads(p.get("checks") or "{}")
        if p["kind"] == "entry_claimed" and checks.get("schema_version") == 2 and checks.get("op") == "open" \
                and checks.get("time_ref") == "conditional" and not checks.get("time_ref_promoted") and p.get("instrument_id"):
            if _edit_too_late(_edit_delay(mvd.get(p["source_version_id"], {})), max_edit_delay_s):
                continue  # -v8e: a long-edited conditional post is out of the pool like any other H1 version
            it = _merge_item(p, mvd, t_post, conditional=True)
            if it is not None:
                conditionals.append(it)
    for rid in roots:
        if rid in pooled and Reason.PROMOTED_WIDE_ONLY in root_info[rid]["blocking"]:
            it = _merge_item(plans[rid], mvd, t_post, conditional=True)
            if it is not None:
                conditionals.append(it)
    links, merge_counts = _plan_merge.merge(items, supplements, conditionals=conditionals, stage=stage, supplement_window_s=supplement_window_s,
                                            rejected_supplements=scale_rejected)
    for rid in roots:
        if rid not in links:  # out of the pool (v8e long edit, unknown clock): its own plan, never merged
            links[rid] = _plan_merge.Link(pid=rid, kind="root", group=rid, kept=rid, family=rid)

    def episode_of(pid: str | None) -> str | None:
        return None if pid is None else stable_id("ep", graph_version, pid)[:32]

    plan_link_rows: list[dict[str, Any]] = []
    staged_stop_roots: list[int] = []
    edit_unknown_roots: list[int] = []

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
        a_star, dep_refs = plan_dependencies(root, mvd, rule_versions, index=dep_index)
        price_a_star, price_refs = plan_dependencies(root, mvd, rule_versions, purpose="price_check", index=dep_index)
        # v8 F2/F6/C0：保留单的字段来自同计划的其他消息；t_dec 取用到字段的消息里最晚的可见时刻（§2 deps）。
        link = links.get(root_id) if not is_orphan else None
        info = root_info.get(root_id, {})
        eff = root
        stop_src, injected = (root if root.get("stop") is not None else None), False
        used = []
        if link is not None and link.kind == "conditional_confirmed" and link.conditional_parent in plans:
            parent = plans[link.conditional_parent]
            eff = dict(root, instrument_id=parent["instrument_id"], side=parent["side"], entry=parent["entry"], entries=parent["entries"],
                       entry_mode=parent.get("entry_mode"), checks=parent.get("checks"), stop=parent["stop"], tps=parent["tps"],
                       expires_after_s=parent.get("expires_after_s") or root.get("expires_after_s"))
            stop_src, injected = (parent if parent.get("stop") is not None else None), True
            used = [link.conditional_parent]
        elif link is not None and link.kept == root_id and link.dup_of is None:
            eff = dict(root)
            if link.stop_pid not in (None, root_id) and link.stop is not None:
                eff["stop"] = plans[link.stop_pid]["stop"] if link.stop_pid in plans else link.stop
                stop_src, injected = plans.get(link.stop_pid), True
            if link.tps_pid not in (None, root_id) and link.tps_pid in plans and not root.get("tps"):
                eff["tps"] = plans[link.tps_pid]["tps"]
            used = [pid for pid in link.providers if pid != root_id and pid in plans]
            if link.kind == "amend" and link.stop_pid not in (None, root_id) and root.get("stop") is None:
                used.append(link.stop_pid)
        for pid in used:
            more, refs = plan_dependencies(plans[pid], mvd, rule_versions, index=dep_index)
            a_star = None if (a_star is None or more is None) else max(a_star, more)
            dep_refs = sorted(set(dep_refs) | set(refs))
        # H1 默认不作决策根；仅当该版本在敏感性口径下已按最后编辑时刻（或 v8 主口径的原帖时刻）定可见时刻时放行。
        root_ta = json.loads(root_mv.get("temporal_assumptions") or "{}")
        h1 = root.get("time_grade") == "H1" and not (root_ta.get("edit_visible_at_last_edit") or root_ta.get("edit_visible_at_post"))
        if not is_orphan and not h1 and eff["instrument_id"] is not None:
            decision_eligible_at = a_star
            t_dec = t_dec_of(a_star, processing_delay_s)
        p_state, c_state = "none", "unknown"
        stop, tps = (eff["stop"], list(eff["tps"] or [])) if not is_orphan else (None, [])
        dec_stop_meta = _stop_meta(stop_src, injected=injected) if stop is not None else _stop_meta(None, injected=False)
        if link is not None and link.kind == "amend" and injected and stop is not None:
            dec_stop_meta["rule"] = "inherited_amend"  # F2：改单继承原单止损
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
        # v8 (F2/F3/F8/F11)：本口径下不执行的原因；全部写进 reason_codes，blocking 的同时关掉入场决策
        v8_codes = list(info.get("blocks", []))
        blocking = list(info.get("blocking", []))
        if link is not None and link.dup_of is not None:
            code = Reason.SAME_PLAN_RESTATEMENT if link.kind in _plan_merge.RESTATEMENT_KINDS else Reason.SAME_PLAN_COMPANION
            v8_codes.append(code)
            blocking.append(code)
        anchor_t = t_post.get((root["channel_id"], root["message_id"]), root["available_at"])
        signal_age = None
        if t_dec is not None and anchor_t is not None:
            age = t_dec - timedelta(seconds=processing_delay_s) - anchor_t
            signal_age = _plan_merge.whole_seconds(age)
            if age > timedelta(seconds=STALE_SIGNAL_S):
                v8_codes.append(Reason.STALE_SIGNAL_30M)
                blocking.append(Reason.STALE_SIGNAL_30M)
        reasons.extend(c for c in v8_codes if c not in reasons)
        stop_meta_now = dict(dec_stop_meta)
        grades = [root_mv.get("time_grade", "U")]
        claimed_outcome = None
        dec: dict[str, Any] = {"state": None, "stop": stop, "tps": [dict(t) for t in tps], "events": [], "invalid": 0, "grades": [grades[0]], "reasons": sorted(set(reasons))}
        for i, e in enumerate(evs):
            p = plans[e["pid"]]
            kind = e["kind"]
            description_only = descriptive_only(p)
            np_, nc, action = transition(p_state, c_state, "entry_proposal" if (e["root"] and not is_orphan) else kind)
            if description_only:
                np_, nc, action = p_state, c_state, "desc"
            if e["root"] and is_orphan:
                reasons.append(Reason.PARENT_MISSING)
            payload: dict[str, Any] = {"from": [p_state, c_state], "to": [np_, nc], "action": action, "invalid_transition": action == "I", "synthesized": e["synth"]}
            source_checks = json.loads(p.get("checks") or "{}")
            if source_checks.get("schema_version") == 2:
                payload["action_v2"] = source_checks.get("action")
                payload["mapping_issues"] = source_checks.get("mapping_issues", [])
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
            member_link = links.get(e["pid"]) if not e["root"] else None
            rejected = member_link is not None and member_link.supplement_rejected is not None
            if rejected:
                payload["supplement_rejected"] = member_link.supplement_rejected  # F6：三道校验不过的补充止损不取价
            if action in ("=", "move") and kind in ("amend", "stop_move") and p["stop"] is not None and not rejected:
                payload["stop"] = {"old": stop, "new": p["stop"]}
                had_stop = root.get("stop") is not None  # the root's own stop: a visible change is a move, not a supplement
                stop = p["stop"]
                supersedes = stop_event_id
                stop_meta_now = _stop_meta(p, injected=True)
                if had_stop and stop_meta_now["rule"] == "supplement":
                    stop_meta_now["rule"] = None  # a visible move of an existing stop is an explicit price
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
            if not description_only and action != "I" and action != "R":
                for field_name in ("entry", "stop", "tps"):
                    if p.get(field_name):
                        field_events[field_name] = event_id
            grades.append(mvd.get(p["source_version_id"], {}).get("time_grade", "U"))
            if not description_only and t_dec is not None and decision_visible(e["edge_available_at"], t_dec):
                dec.update({"state": (p_state, c_state), "stop": stop, "tps": [dict(t) for t in tps]})
                dec_stop_meta = dict(stop_meta_now)
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
        elig = json.loads(eff["eligibility_by_estimand"]) if eff.get("eligibility_by_estimand") else {}
        elig["entry_decision"] = t_dec is not None and eff["instrument_id"] is not None and elig.get("execution", True)
        elig["outcome"] = claimed_outcome is not None  # A13：有作者终态（全平/取消/到期）→ true，否则 false；决策视图恒 false
        if is_orphan or replay_required or h1:
            elig["execution"] = False
        if h1:
            elig["entry_decision"] = False  # 仅最终编辑版：原始入场隔离，不复活为可执行入场（S02）
        if blocking:
            elig["entry_decision"] = elig["execution"] = False
        if replay_required:
            elig = {k: False for k in ELIG_KEYS}
        dec_plan = _order_plan(eff, dec["stop"], dec["tps"], eff.get("expires_after_s"), stop_meta=dec_stop_meta) if (t_dec is not None) else None
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
        root_elig = json.loads(eff["eligibility_by_estimand"]) if eff.get("eligibility_by_estimand") else {}
        if price_a_star is None or t_dec is None or price_a_star >= t_dec:
            root_elig["price_check"] = False
        dec_elig = {"description": True, "outcome": False, "original_entry": root_elig.get("original_entry", True), "price_check": root_elig.get("price_check", True),
                    "execution": bool(root_elig.get("execution", True)) and not is_orphan and not h1 and dec_plan_possible(eff),
                    "entry_decision": t_dec is not None and eff["instrument_id"] is not None and bool(root_elig.get("execution", True)) and dec_plan_possible(eff)}
        if blocking:
            dec_elig["entry_decision"] = dec_elig["execution"] = False
        if decision_replay_required:
            dec_elig = {k: False for k in ELIG_KEYS}
        # v8 episode columns (§4)
        plan_stop = (dec_plan or {}).get("stop")
        promoted = info.get("promoted") or {}
        seg = info.get("seg") or ""
        tr = info.get("triage") or {}
        venue = _plan_merge.venue_of(seg)
        if venue == "unspecified" and tr.get("venue_hint") in ("perp", "spot", "coin_m", "unspecified"):
            venue = tr["venue_hint"]
        nostop_kind = None
        if dec_plan is not None and plan_stop is None:
            hint = json.loads(eff.get("checks") or "{}").get("nostop_hint")
            if hint in NOSTOP_HINT_KINDS:
                nostop_kind = hint
            elif venue == "spot":
                nostop_kind = "spot"
            elif LOW_LEVERAGE_RE.search(seg):
                nostop_kind = "declared_low_lev"
            else:
                nostop_kind = "contract_no_stop"
        dep_versions = [root["source_version_id"]] + [plans[pid]["source_version_id"] for pid in used if pid in plans]
        dep_rows = [mvd.get(v, {}) for v in dep_versions]
        delays = [_edit_delay(r) for r in dep_rows]
        edit_delay = None if any(d is None for d in delays) else max(delays, default=0)  # unknown → null, never 0
        h1_rows = [r for r in dep_rows if r.get("time_grade") == "H1"]
        edit_original_unavailable = any(json.loads(r.get("temporal_assumptions") or "{}").get("edit_original_unavailable") for r in h1_rows)
        # an unknown edit delay may hide an outcome as well as a long one does
        may_contain_outcome = any(_edit_too_late(_edit_delay(r), EDIT_OUTCOME_S) and _cx_v2.RESULT_WORDS.search(r.get("text") or "") for r in h1_rows)
        meta = dec_stop_meta if plan_stop is not None else _stop_meta(None, injected=False)
        if plan_stop is not None and staged_stop(seg):
            staged_stop_roots.append(root["message_id"])  # F12: not modelled, counted
        if edit_delay is None:
            edit_unknown_roots.append(root["message_id"])
        v8_cols = {
            "plan_group_id": episode_of(link.group) if link else None, "family_id": episode_of(link.family) if link else None,
            "dup_of": episode_of(link.dup_of) if link else None, "plan_link_kind": link.kind if link else None,
            "repost_of": episode_of(link.repost_of) if link else None, "amend_of": episode_of(link.amend_of) if link else None,
            "reentry_of": episode_of(link.reentry_of) if link else None, "reentry_parent_stop": link.reentry_parent_stop if link else None,
            "stop_rule": meta["rule"], "stop_base": meta["base"], "stop_source_version_id": meta["svid"],
            "nostop_kind": nostop_kind, "venue_hint": venue if not is_orphan else None, "triage_verdict": info.get("verdict"),
            "triage_reason": tr.get("reason") if tr else None, "promotion_scope": promoted.get("scope"),
            "signal_age_s": signal_age, "edit_delay_s": edit_delay, "signal_anchor": ("confirm" if link and link.kind == "conditional_confirmed" else "root") if t_dec is not None else None,
            "edit_original_unavailable": edit_original_unavailable, "edit_may_contain_outcome": bool(may_contain_outcome),
            "entry_legs_n": len(dec_plan["entries"]) if dec_plan else None, "time_ref_promoted": promoted.get("rule"),
        }
        # 簇（契约 §9.7 A5）：v0 经济簇 = 可用于簇的复制组（近似组 usable_for_cluster=false 不算），否则单机会自成一簇（= episode_id）；决策视图用 t_dec 前可见成员
        dgr = dg_rows.get(root["source_version_id"])
        desc_group = dgr["duplicate_group_id"] if (dgr and dgr.get("usable_for_cluster", True) and len(dg_members.get(dgr["duplicate_group_id"], [])) > 1) else None
        cluster_id = desc_group or dec_group or episode_id
        dec_cluster_id = (dec_group or episode_id) if dec["state"] else None
        dec_deriv = stable_id("dec-deriv", graph_version, dec["events"], dec["state"], dec["stop"], [(t["level"], t.get("fraction")) for t in dec["tps"]], dec["invalid"], dec["reasons"], dec_grade, dec_group, dec_cluster_id, rule_versions) if dec["state"] else None
        snap = snapshot_hash(graph_version=graph_version, episode_id=episode_id, root_plan_id=root_id, t_dec=t_dec, dependency_ids=dep_refs,
                             fields={"order_plan": dec_plan, "state": dec["state"], "n_events": len(dec["events"]), "invalid": dec["invalid"], "reasons": dec["reasons"], "grade": dec_grade,
                                     "dup_group": dec_group, "cluster": dec_cluster_id, "audit_stratum": dec_audit_stratum, "elig": dec_elig, "delay": processing_delay_s, "v8": v8_cols}, rule_versions=rule_versions) if dec_plan else None
        deriv = stable_id("deriv", graph_version, [(e["pid"], e["kind"]) for e in evs], p_state, c_state, stop, [t["level"] for t in tps], rule_versions, obs_end)
        ep_rows.append({
            "episode_id": episode_id, "graph_version": graph_version, "predecessor_ids": [], "successor_ids": [], "channel_id": root["channel_id"], "trader_id": root_mv.get("author_id"),
            "instrument_id": eff["instrument_id"], "side": eff["side"], "entry_branch_id": root_id, "duplicate_group_id": dg_rows.get(root["source_version_id"], {}).get("duplicate_group_id"), "cluster_id": cluster_id,
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
            **v8_cols,
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
    # v8 gold/plan_link: one row per entry branch and per supplement (F2/F6/F13 read it)
    for pid, link in sorted(links.items()):
        p = plans.get(pid)
        if p is None:
            continue
        owner = link.supplement_of or pid
        synth = link.synthetic[0] if link.synthetic else {}
        kind, supplement_status = link.kind, None
        if link.supplement_of is not None:
            if link.supplement_rejected is not None:
                kind, supplement_status = None, f"rejected:{link.supplement_rejected}"
            elif link.kind == "stop_move":
                kind, supplement_status = None, "stop_move"
            else:
                supplement_status = "merged" if link.kind == "supplement" else link.kind
        plan_link_rows.append({
            "channel_id": p["channel_id"], "message_id": p["message_id"], "source_version_id": p["source_version_id"], "branch_index": int(p.get("branch_index") or 0),
            "episode_id": episode_of(owner), "kept_episode_id": episode_of(synth.get("target") or link.kept or owner),
            "family_id": episode_of(link.family) if link.family else None, "plan_link_kind": kind, "supplement_status": supplement_status,
            "target_message_id": link.target_message_id, "gap_s": link.gap_s, "synthetic_action": synth.get("action"), "synthetic_at": synth.get("at"),
            "synthetic_stop_price": synth.get("stop"), "merge_version": _plan_merge.PLAN_MERGE_VERSION,
            "event_time": p["event_time"], "available_at": p["available_at"], "ingested_at": ingested_at})
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
        r["reentry_parent_stop"], r["stop_base"] = _dec(r["reentry_parent_stop"]), _dec(r["stop_base"])
    for row in plan_link_rows:
        row["synthetic_stop_price"] = _dec(row["synthetic_stop_price"])
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
    episode_sources = {}
    conflict_episodes = []
    used_by_episode = {}
    for event in ev_rows:
        used_by_episode.setdefault(event["episode_id"], set()).add(event["plan_id"])
    for episode in ep_rows:
        eid = episode["episode_id"]
        root_id = episode["root_plan_id"]
        used = used_by_episode.get(eid, {root_id})
        sources = sorted({plans[pid]["extractor_name"] for pid in used})
        episode_sources[eid] = {"root": plans[root_id]["extractor_name"], "sources": sources}
        if any(plans[pid]["source_version_id"] in conflicts for pid in used):
            conflict_episodes.append(eid)
    summary.update(plan_source=plan_source, episode_sources=episode_sources,
                   n_disagreements=len(conflicts), disagreements=conflicts,
                   n_disagreement_episodes=len(conflict_episodes), disagreement_episode_ids=sorted(conflict_episodes))
    merge_counts = dict(merge_counts, staged_stop_dropped=len(staged_stop_roots), edit_delay_unknown=len(edit_unknown_roots))
    summary["v8"] = v8_report(episodes, plan_link_rows, merge_counts, stage=stage)
    summary["v8"]["edit_delay_unknown_message_ids"] = sorted(set(edit_unknown_roots))
    summary["v8"]["staged_stop_message_ids"] = sorted(set(staged_stop_roots))
    summary["plan_link_rows"] = plan_link_rows
    return episodes, events, qrows, ledgers, summary


def v8_report(episodes: pl.DataFrame, plan_link_rows: list[dict], merge_counts: dict[str, int], *, stage: int) -> dict[str, Any]:
    """构建报告（方案 §8 第 5 步）：plan_link_kind、分诊、nostop_kind、stop_rule、STALE、PROMOTED_WIDE_ONLY、TRIAGE_MISSING 计数。"""
    def counts(column: str) -> dict[str, int]:
        if not episodes.height or column not in episodes.columns:
            return {}
        return {str(k): v for k, v in sorted(episodes.group_by(column).len().iter_rows(), key=lambda kv: str(kv[0]))}

    codes: dict[str, int] = {}
    for row in episodes.select("reason_codes").iter_rows() if episodes.height else []:
        for code in row[0] or []:
            if code in EXECUTION_BLOCKING:
                codes[code] = codes.get(code, 0) + 1
    kinds: dict[str, int] = {}
    supplements: dict[str, int] = {}
    for row in plan_link_rows:
        if row["plan_link_kind"] is not None:
            kinds[row["plan_link_kind"]] = kinds.get(row["plan_link_kind"], 0) + 1
        if row.get("supplement_status") is not None:
            supplements[row["supplement_status"]] = supplements.get(row["supplement_status"], 0) + 1
    cash_watch: dict[str, int] = {}
    for channel, row_codes in (episodes.select("channel_id", "reason_codes").iter_rows() if episodes.height else []):
        if Reason.CASH_WATCH_POST in (row_codes or []):
            cash_watch[str(channel)] = cash_watch.get(str(channel), 0) + 1
    decision = episodes.filter(pl.col("dec_eligibility").struct.field("entry_decision")) if episodes.height else episodes
    return {"stage": stage, "merge_version": _plan_merge.PLAN_MERGE_VERSION, "plan_link_kind": dict(sorted(kinds.items())),
            "supplement_status": dict(sorted(supplements.items())), "cash_watch_post_by_channel": dict(sorted(cash_watch.items())),
            "merge_counters": {k: v for k, v in sorted(merge_counts.items()) if not k.startswith("kind:")},
            "triage_verdict": counts("triage_verdict"), "nostop_kind": counts("nostop_kind"), "stop_rule": counts("stop_rule"),
            "reason_codes": dict(sorted(codes.items())), "n_decision_entries": decision.height,
            "n_decision_nostop": int(decision["nostop_kind"].is_not_null().sum()) if decision.height else 0}


def _sig(df: pl.DataFrame, key: str) -> str:
    """Portable q12 semantic hashing, independent of Polars' native row-hash implementation."""
    cols = [c for c in df.columns if c != "ingested_at" and df.schema[c] not in (pl.Float32, pl.Float64)]
    return stable_id(df.select(cols).sort(key).to_dicts())


def triage_path(layout: Layout):
    return layout.silver_dir / TRIAGE_FILE


def load_triage(layout: Layout) -> tuple[pl.DataFrame | None, str, str | None]:
    """The silver copy of the triage sidecar: (frame, rule version string, file sha256). No file → stage 1."""
    from .lake import sha256_file
    path = triage_path(layout)
    if not path.exists():
        return None, "", None
    frame = pl.read_parquet(path)
    sha = sha256_file(path)
    schemas = sorted({str(v) for v in frame["triage_schema"].to_list() if v is not None}) if "triage_schema" in frame.columns else []
    recordings = sorted({str(v) for v in frame["recording_version"].to_list() if v is not None}) if "recording_version" in frame.columns else []
    version = f"sidecar:{sha[:16]}|schema:{','.join(schemas) or '-'}|recording:{','.join(recordings) or '-'}"
    return frame, version, sha


def silver_signature(layout: Layout) -> str:
    """Every silver/bronze input of a graph (plans, edges, adjudications, copy groups, versions, extractions)."""
    cp = pl.read_parquet(layout.canonical_plan)
    cb = pl.read_parquet(layout.silver_dir / "candidate_edges.parquet")
    jd = pl.read_parquet(layout.silver_dir / "adjudications.parquet")
    dg = pl.read_parquet(layout.duplicate_group) if layout.duplicate_group.exists() else None
    mv = pl.read_parquet(layout.message_version)
    ex = pl.read_parquet(layout.extracted_event) if layout.extracted_event.exists() else pl.DataFrame()
    return stable_id("silver", _sig(cp, "plan_id"), _sig(cb, "candidate_id"), _sig(jd, "request_id"), _sig(dg, "source_version_id") if dg is not None else 0,
                     _sig(mv, "source_version_id"), _sig(ex, "extract_id") if ex.height else "")


VARIANT_KEYS = ("wide", "supplement_window_s", "max_edit_delay_s")


def input_hash_of(layout: Layout, *, ingested_at: datetime | None = None, registry_version: str = "registry-synthetic-v1", **kw) -> str:
    """图版本输入 hash：全部 parser plan 列、候选边全列、裁决全列、复制组全列、规则版本、延迟/终点/H（S10）；v8 加 sidecar 与变体开关。"""
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
    _, triage_version, _ = load_triage(layout)
    rule_versions = _rule_versions(registry_version, triage_version=triage_version, **{k: kw[k] for k in VARIANT_KEYS if k in kw})
    obs_end = kw.get("observation_end") or max((t for t in cp["available_at"].to_list() if t is not None), default=ingested_at)
    signature = stable_id("gin", _sig(cp, "plan_id"), _sig(cb, "candidate_id"), _sig(jd, "request_id"), _sig(dg, "source_version_id") if dg is not None else 0, _sig(mv_used, "source_version_id"), _sig(ex, "extract_id") if ex.height else "",
                     rule_versions, kw.get("processing_delay_s", DEFAULT_PROCESSING_DELAY_S), obs_end.isoformat(), kw.get("horizon_s", HORIZON_S))
    if kw.get("plan_source", "parser") != "parser":
        signature = stable_id(signature, "plan-source-v1", kw["plan_source"])
    return signature


def like_settings(layout: Layout, like: str) -> dict[str, Any]:
    """`--like <gv>`: read plan_source / registry / sidecar sha of a published graph and assert the silver inputs are the same."""
    from .graph import resolve_alias
    resolved = resolve_alias(layout, like)
    doc = read_manifest(layout, resolved)
    if doc is None:
        raise RuntimeError(f"--like {like}: 没有已发布的 manifest（{resolved}）")
    a = doc.get("assumptions") or {}
    for k in ("plan_source", "registry_version", "silver_signature"):
        if k not in a:
            raise RuntimeError(f"--like {resolved}: manifest 缺 {k}（v8 之前的图不能作变体基准）")
    now = silver_signature(layout)
    if now != a["silver_signature"]:
        raise RuntimeError(f"--like {resolved}: silver 输入签名不一致（{now[:12]} ≠ {a['silver_signature'][:12]}），变体只能重算同一 silver 的 gold")
    _, _, sha = load_triage(layout)
    if sha != a.get("triage_sidecar_sha256"):
        raise RuntimeError(f"--like {resolved}: triage sidecar sha 不一致（{sha} ≠ {a.get('triage_sidecar_sha256')}）")
    return {"plan_source": a["plan_source"], "registry_version": a["registry_version"], "like": resolved}


def run(layout: Layout, *, graph_version: str, ingested_at: datetime | None = None, registry_version: str = "registry-synthetic-v1",
        variant_only: bool = False, **kw) -> dict[str, Any]:
    """variant_only=True（lifecycle --like）：只写 gold、manifest、plan_link 与 plan_source 汇总，不写第 6 层损耗/映射、隔离与复核表。"""
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
    ex = pl.read_parquet(layout.extracted_event) if layout.extracted_event.exists() else None
    triage, triage_version, triage_sha = load_triage(layout)
    episodes, events, qrows, ledgers, summary = build_graph(cp, mv, cb, jd, dg, graph_version=graph_version, registry_version=registry_version, ingested_at=ingested_at,
                                                            extracted_event=ex, triage=triage, triage_version=triage_version, **kw)
    plan_link_rows = summary.pop("plan_link_rows")
    layout.ensure()
    old_ep = pl.read_parquet(layout.episode(graph_version)) if layout.episode(graph_version).exists() else None
    old_ev = pl.read_parquet(layout.episode_event(graph_version)) if layout.episode_event(graph_version).exists() else None
    write_parquet_atomic(preserve_ingested_at(episodes, old_ep, "episode_id"), layout.episode(graph_version))
    write_parquet_atomic(preserve_ingested_at(events, old_ev, "event_id"), layout.episode_event(graph_version))
    links = pl.DataFrame(plan_link_rows, schema=PLAN_LINK_SCHEMA) if plan_link_rows else pl.DataFrame(schema=PLAN_LINK_SCHEMA)
    write_parquet_atomic(links, plan_link_path(layout, graph_version))
    batch_id = summary["batch_id"]
    if not variant_only:
        append_quarantine(layout.quarantine_path, qrows)
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
    from .cx_batch import save_build_review, write_json
    if not variant_only:
        save_build_review(layout, graph_version, cp, mv, episodes, kw.get("plan_source", "parser"), events=events)
    summary["input_hash"] = input_hash
    write_json(layout.gold_dir / f"plan_source__{graph_version}.json", summary)
    assumptions = {"processing_delay_s": summary["processing_delay_s"], "observation_end": summary["observation_end"], "horizon_s": kw.get("horizon_s", HORIZON_S),
                   "plan_source": kw.get("plan_source", "parser"), "registry_version": registry_version, "silver_signature": silver_signature(layout),
                   "triage_sidecar_sha256": triage_sha, "plan_link_signature": plan_link_signature(links),
                   "wide": bool(kw.get("wide", False)), "supplement_window_s": int(kw.get("supplement_window_s", _plan_merge.DEFAULT_SUPPLEMENT_S)),
                   "max_edit_delay_s": kw.get("max_edit_delay_s"), "variant_only": variant_only}
    publish_manifest(layout, graph_version, input_hash=input_hash, rule_versions=summary["rule_versions"], assumptions=assumptions,
                     counts={"episodes": episodes.height, "events": events.height, "decision_roots": summary["n_decision_roots"]}, built_at=ingested_at)
    summary["paths"] = {"episode": str(layout.episode(graph_version)), "episode_event": str(layout.episode_event(graph_version)),
                        "plan_link": str(plan_link_path(layout, graph_version))}
    return summary


def plan_link_path(layout: Layout, graph_version: str):
    return layout.gold_dir / f"plan_link__{graph_version}.parquet"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="链接后重放双轨状态机，落 gold/episode 与 episode_event 并发布 manifest")
    ap.add_argument("--plan-source", choices=["parser", "llm", "reconciled"], help="不带 --like 时必填")
    ap.add_argument("--registry-version", help="不带 --like 时必填（品种登记版本）")
    ap.add_argument("--like", help="从该图的 manifest 读 plan_source/registry/sidecar sha，断言 silver 输入一致，只重算 gold（变体）")
    ap.add_argument("--wide", action="store_true", help="宽口径 -v8w：执行存疑/缺失分诊与只进宽口径的升级根")
    ap.add_argument("--supplement-window-s", type=int, default=_plan_merge.DEFAULT_SUPPLEMENT_S, help="补止损合并窗口（主口径 1800；-v8nw 取 0）")
    ap.add_argument("--max-edit-delay-s", type=int, help="-v8e：编辑延迟超过它的 H1 版本先移出合并池，不当决策根（STALE_EDIT_30M）")
    ap.add_argument("--graph-version", required=True)
    ap.add_argument("--alias", action="store_true", help="graph-version 作别名：发布不可变版本 <alias>@<hash8> 并移动别名")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out")
    g.add_argument("--lake-root")
    a = ap.parse_args(argv)
    layout = Layout.flat(a.out) if a.out else Layout.from_root(a.lake_root)
    variant = {"wide": a.wide, "supplement_window_s": a.supplement_window_s}
    if a.max_edit_delay_s is not None:
        variant["max_edit_delay_s"] = a.max_edit_delay_s
    if a.like:
        settings = like_settings(layout, a.like)
        if a.plan_source and a.plan_source != settings["plan_source"]:
            ap.error(f"--plan-source {a.plan_source} 与 --like 的 {settings['plan_source']} 不一致")
        if a.registry_version and a.registry_version != settings["registry_version"]:
            ap.error(f"--registry-version {a.registry_version} 与 --like 的 {settings['registry_version']} 不一致")
        plan_source, registry_version, variant_only = settings["plan_source"], settings["registry_version"], True
    else:
        if not a.plan_source or a.registry_version is None:
            ap.error("不带 --like 时必须显式给 --plan-source 和 --registry-version（不再默认 parser / registry-synthetic-v1）")
        plan_source, registry_version, variant_only = a.plan_source, a.registry_version, False
    kwargs = dict(plan_source=plan_source, **variant)
    gv = a.graph_version
    if a.alias:
        from .graph import set_alias
        ih = input_hash_of(layout, registry_version=registry_version, **kwargs)
        gv = f"{a.graph_version}@{ih[:8]}"
    out = run(layout, graph_version=gv, registry_version=registry_version, variant_only=variant_only, **kwargs)
    if a.alias:
        set_alias(layout, a.graph_version, gv)
        out["alias"] = {a.graph_version: gv}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
