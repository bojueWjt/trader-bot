"""单老师 L0 描述性重放，放在 market：只编排 G1→G2，不改变 G3 研究代码摘要。

python -m quant_lab.market.l0_replay --graph-version GV --channel ID --out DIR
行情缺省 DATA_ROOT/lake/market；不联网、不补行情/规则。live 策略只按根原文措辞变换执行计划副本。
G2 仍要求已体检分区（manifest.check_status）及 instrument_rules 历史规则；缺失会计入覆盖排除。

v8（F1/F4/§4 第二遍）：
- 无止损计划只在无止损策略下执行：区间取近端单笔限价，按每腿固定名义定量（fixed_qty），规则取不到记 NOSTOP_RULES_UNRESOLVED；
- live v4 读 episode 的 stop_rule/stop_base，并按 stop_source_version_id 加载止损所在消息的原文；
- repost_of/amend_of 指向别单的 episode 在第二遍按「原单家族当时是否在场」判定（REPOST_OF_LIVE_PLAN / AMEND_*）；
- overall/by/cumulative_R/censor_counts 只统计按风险定量的行；无止损行在 blocks.nostop 里按金额（U）统计，合计只有金额。
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
from decimal import ROUND_DOWN, Decimal
import json
from pathlib import Path

import polars as pl

from quant_lab.data.api import load_episodes, load_message_texts
from quant_lab.data.graph import resolve_alias
from quant_lab.data.lake import Layout, write_parquet_atomic
from quant_lab.data.sources import canonical_peer_id
from quant_lab.market.contract import (
    EVIDENCE_CENSORS, FILL_KINDS, MANAGEMENT_KINDS, RATIO_QUANTUM, TERMINAL_KINDS, ContractError, ExecutionRequest,
    ManagementAction, build_request, canonical_json, derived_t_start, management_stats, resolve_policy,
)
from quant_lab.market.execution import load_market_from_lake, simulate_batch
from quant_lab.market.live_profile import PROFILE_VERSION, RULES, SOURCES, apply_live_profile, quantity_fractions
from quant_lab.market.partition_check import load_rules, rule_at
from quant_lab.market.vision import LakePaths


def _covered(row: dict) -> bool:
    return all(row[key] is True for key in ("mark_ok", "funding_ok", "rules_ok", "bars_ok"))


def _evaluable(row: dict) -> bool:
    return row["censor_reason"] is None and _covered(row) and row["net_R"] is not None


def metrics(rows: list[dict]) -> dict:
    """成交率分母为全部请求；净 R 包含未成交零收益；胜率仅使用完整的已成交样本。"""
    evaluated = [r for r in rows if _evaluable(r)]
    filled = [r for r in rows if r["filled_qty"] > 0]
    closed_filled = [r for r in evaluated if r["filled_qty"] > 0]
    total = sum((r["net_R"] for r in evaluated), Decimal(0))
    return {
        "n_trades": len(rows), "n_filled": len(filled), "n_evaluable": len(evaluated),
        "n_evaluable_filled": len(closed_filled),
        "fill_rate": len(filled) / len(rows) if rows else None,
        "mean_net_R": float(total / len(evaluated)) if evaluated else None,
        "win_rate": sum(r["net_R"] > 0 for r in closed_filled) / len(closed_filled) if closed_filled else None,
        "sum_net_R": float(total),
        "n_censored": sum(r["censor_reason"] is not None for r in rows),
        "n_coverage_excluded": sum(not _covered(r) or r["censor_reason"] in EVIDENCE_CENSORS for r in rows),
    }


SIZING_RISK = "risk"          # 按风险预算定量（有止损计划；旧策略全部是这一类）
SIZING_NOSTOP = "nostop"      # v8 F1：无止损计划按固定名义定量，net_R 只是 net_U / B 的金额等价，不是风险等价


def _risk_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("sizing_basis", SIZING_RISK) == SIZING_RISK]


def summarize(table: pl.DataFrame) -> dict:
    all_rows = table.sort(["t_dec", "episode_id"]).to_dicts()
    rows = _risk_rows(all_rows)
    grouped = {}
    for name, key in (("side", lambda r: r["side"]), ("instrument", lambda r: r["instrument"]),
                      ("year", lambda r: str(r["t_dec"].year))):
        groups = {}
        for row in rows:
            groups.setdefault(key(row), []).append(row)
        grouped[name] = {k: metrics(v) for k, v in sorted(groups.items())}
    cumulative = []
    value = Decimal(0)
    for row in rows:
        if _evaluable(row):
            value += row["net_R"]
            cumulative.append({"episode_id": row["episode_id"], "t_dec": row["t_dec"].isoformat(),
                               "net_R": float(row["net_R"]), "cumulative_R": float(value)})
    return {"overall": metrics(rows), "by": grouped, "cumulative_R": cumulative,
            "censor_counts": dict(sorted(Counter(r["censor_reason"] for r in rows if r["censor_reason"] is not None).items())),
            "coverage_failure_counts": {key: sum(r[key] is not True for r in all_rows)
                                        for key in ("mark_ok", "funding_ok", "rules_ok", "bars_ok")},
            "blocks": blocks(all_rows)}


def _quantile(values: list[float], q: float) -> float | None:
    """Linear interpolation between order statistics (numpy default); None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _net_U(row: dict) -> Decimal:
    return row["net_R"] * Decimal(str(row["risk_budget"]))


def nostop_metrics(rows: list[dict]) -> dict:
    """无止损块（金额口径）：成交率分母为全部请求；金额只用完整覆盖且未删失的样本。"""
    evaluated = [r for r in rows if _evaluable(r)]
    filled = [r for r in rows if r["filled_qty"] > 0]
    closed_filled = [r for r in evaluated if r["filled_qty"] > 0]
    net = [_net_U(r) for r in evaluated]
    total = sum(net, Decimal(0))
    mae = [-float(r["mae_U"]) for r in rows if r["filled_qty"] > 0 and r.get("mae_U") is not None]
    mae_pct = [-float(r["mae_pct_notional"]) for r in rows if r["filled_qty"] > 0 and r.get("mae_pct_notional") is not None]
    censored = [r for r in rows if r["censor_reason"] is not None]
    mtm = {r["episode_id"]: (None if r.get("mtm_U_at_censor") is None else float(r["mtm_U_at_censor"])) for r in censored}
    budgets = {Decimal(str(r["risk_budget"])) for r in rows}
    return {
        "n_trades": len(rows), "n_filled": len(filled), "n_evaluable": len(evaluated),
        "n_evaluable_filled": len(closed_filled),
        "fill_rate": len(filled) / len(rows) if rows else None,
        "sum_net_U": float(total),
        "mean_net_U": float(total / len(evaluated)) if evaluated else None,
        "median_net_U": _quantile([float(v) for v in net], 0.5),
        "win_rate": sum(_net_U(r) > 0 for r in closed_filled) / len(closed_filled) if closed_filled else None,
        "mean_net_U_over_B": float(sum((r["net_R"] for r in evaluated), Decimal(0)) / len(evaluated)) if evaluated else None,
        "risk_budget_B": [str(b) for b in sorted(budgets)],
        "adverse_excursion_U": {"p50": _quantile(mae, 0.5), "p95": _quantile(mae, 0.95), "max": max(mae) if mae else None},
        "adverse_excursion_pct_notional": {"p95": _quantile(mae_pct, 0.95), "max": max(mae_pct) if mae_pct else None},
        "n_censored": len(censored),
        "mtm_U_at_censor": {"sum": float(sum(v for v in mtm.values() if v is not None)) if censored else 0.0,
                            "n_unavailable": sum(v is None for v in mtm.values()),
                            "by_episode": dict(sorted(mtm.items()))},
        "n_coverage_excluded": sum(not _covered(r) or r["censor_reason"] in EVIDENCE_CENSORS for r in rows),
    }


NOSTOP_GROUP_COLUMNS = ("nostop_kind", "venue_hint", "legs_gt3", "time_ref_promoted")


def concurrency(rows: list[dict]) -> dict:
    """无止损在场名义（成交量 × 均价 × 合约乘数）的峰值与时刻；未平仓的删失单按删失时刻结束。只作诊断。"""
    edges = []
    for r in rows:
        if r["filled_qty"] <= 0 or r.get("entry_notional_U") is None or r["position_open_at"] is None:
            continue
        end = r["position_close_at"] or r.get("censor_at")
        notional = Decimal(str(r["entry_notional_U"]))
        edges.append((r["position_open_at"], 1, notional))
        if end is not None:
            edges.append((end, 0, -notional))        # 同刻先平后开
    level, peak, peak_at = Decimal(0), Decimal(0), None
    for at, _, delta in sorted(edges, key=lambda e: (e[0], e[1])):
        level += delta
        if level > peak:
            peak, peak_at = level, at
    return {"peak_notional_U": float(peak), "peak_at": None if peak_at is None else peak_at.isoformat(),
            "note": "单笔内核看不到组合层；按成交时刻叠加在场名义，只作诊断"}


def blocks(rows: list[dict]) -> dict:
    """有止损（R）与无止损（U）分块；合计只有金额，不出合计均值与胜率。"""
    risk = _risk_rows(rows)
    nostop = [r for r in rows if r.get("sizing_basis") == SIZING_NOSTOP]
    by = {}
    for column in NOSTOP_GROUP_COLUMNS:
        if not any(column in r for r in nostop):
            continue
        groups = {}
        for r in nostop:
            groups.setdefault(str(r.get(column)), []).append(r)
        by[column] = {k: nostop_metrics(v) for k, v in sorted(groups.items())}
    def sum_U(part):
        # 没有 risk_budget 的行不能换成金额：整项为空，而不是当成 0（无法比较 ≠ 相等）。
        evaluated = [r for r in part if _evaluable(r)]
        if any(r.get("risk_budget") is None for r in evaluated):
            return None
        return sum((_net_U(r) for r in evaluated), Decimal(0))

    risk_U, nostop_U = sum_U(risk), sum_U(nostop)
    return {
        "with_stop": metrics(risk),
        "nostop": {**nostop_metrics(nostop), "by": by},
        "total": {"sum_net_U": None if risk_U is None or nostop_U is None else float(risk_U + nostop_U),
                  "sum_net_U_with_stop": None if risk_U is None else float(risk_U),
                  "sum_net_U_nostop": None if nostop_U is None else float(nostop_U)},
        "concurrency": concurrency(nostop),
    }


#: 参考价入场（原文「现价附近」「市价」等，G1 只记 market_ref、不写价格——G1 不猜市价）在重放时的参考价来源。
#: 取决策时刻 t_dec 已知的最新标记价（严格 as-of，不前视），只用于按风险预算定仓；成交仍由内核按市价在 t_start 撮合。
MARKET_REF_SOURCE = "mark_asof_t_dec"


def resolve_market_refs(row: dict, marks) -> tuple[dict | None, str | None]:
    """验证计划止损与报价，再把全部 market_ref 入场统一定仓于 t_dec 时的 as-of 标记价；取不到就返回原因码，由调用方计入覆盖排除。"""
    plan = row.get("order_plan")
    entries = (plan or {}).get("entries") or []
    has_market = any(e.get("kind") == "market_ref" for e in entries)
    stop = plan.get("stop") if plan else None
    if not has_market and stop is None:
        return row, None
    if row.get("t_dec") is None or row.get("instrument_id") is None:
        return None, "MARKET_REF_UNRESOLVED"
    mark = marks.mark_at(row["instrument_id"], row["t_dec"])
    if mark.price is None:
        return None, f"MARKET_REF_UNRESOLVED:{mark.reason or 'NO_MARK'}"
    if stop is not None:
        stop_price = Decimal(str(stop["price"]))
        side = plan.get("side", row.get("side"))
        stale = mark.price <= stop_price if side == "long" else mark.price >= stop_price
        if stale:
            return None, "PLAN_STALE"
        # A quoted CMP that is stale by more than 0.25R becomes a resting limit at the quote: a follower who sees the price
        # has moved places the order at the teacher's level and is filled only if the market comes back within the entry TTL
        # (or at once, as taker, when the limit is already marketable).
        stale_quote = []
        for entry in entries:
            quote = entry.get("price_lo")
            if entry.get("kind") == "market_ref" and quote is not None:
                quote = Decimal(str(quote))
                if abs(quote - mark.price) > Decimal("0.25") * abs(quote - stop_price):
                    stale_quote.append(id(entry))
        if stale_quote:
            entries = [dict(e, kind="limit", price_hi=e["price_lo"], tif="GTC", post_only=False) if id(e) in stale_quote else e for e in entries]
            has_market = any(e.get("kind") == "market_ref" for e in entries)
            row = dict(row, order_plan=dict(plan, entries=entries), stale_quote_as_limit=True)
            plan = row["order_plan"]
    if not has_market:
        return row, None
    fixed = [dict(e, price_lo=mark.price, price_hi=mark.price) if e.get("kind") == "market_ref" else e for e in entries]
    return dict(row, order_plan=dict(plan, entries=fixed)), None


def stop_meta_of(row: dict) -> dict | None:
    """v8 rows carry a stop_rule key (value may be empty) → live v4; v7 rows have no key → v3 logic unchanged."""
    if "stop_rule" not in row:
        return None
    return {"stop_rule": row["stop_rule"], "stop_base": row.get("stop_base")}


def prepare_execution(row: dict, *, policy, text: str | None = None, tick_size: Decimal | None = None,
                      stop_text: str | None = None) -> tuple[dict, dict | None]:
    """Apply the optional profile before build_request, preserving the source row."""
    if not policy.live_execution_profile:
        return row, None
    if tick_size is None:
        audit = {"profile": PROFILE_VERSION, "status": "rules_unresolved", "rule_counts": dict.fromkeys(RULES, 0),
                 "source_version_id": row.get("root_source_version_id"), "root_text_resolved": text is not None,
                 "execution_plan": row["order_plan"]}
        return row, audit
    plan, audit = apply_live_profile(row["order_plan"], text, tick_size, stop_meta=stop_meta_of(row), stop_text=stop_text)
    audit["source_version_id"] = row.get("root_source_version_id")
    audit["root_text_resolved"] = text is not None
    return dict(row, order_plan=plan), audit


def load_followup_actions(path: Path) -> list[dict]:
    """Missing/invalid follow input fails the whole run, including an empty channel."""
    if not path.is_file():
        raise ContractError(f"follow_teacher requires followup actions file: {path}")
    table = pl.read_parquet(path)
    required = {"message_id", "episode_id", "available_at", "action", "fraction", "stop_price",
                "to_entry", "uncertain", "episode_ambiguity"}
    if not required.issubset(table.columns) or not ({"channel_id", "channel"} & set(table.columns)):
        raise ContractError(f"followup actions schema invalid: {path}")
    return table.to_dicts()


def attach_management(requests: list[ExecutionRequest], rows: list[dict], *, policy, channel: int,
                      graph_version: str) -> tuple[list[ExecutionRequest], dict]:
    """Every row is adopted once or discarded for one reason; no evidence text propagated.

    Equal times use message_id then instruction_id/canonical payload, never row order.
    """
    by_episode = {req.episode_id: req for req in requests}
    grouped = {req.episode_id: [] for req in requests}
    discarded, read_kinds, adopted_kinds = Counter(), Counter(), Counter()
    defaulted = 0
    for row in rows:
        kind = row["action"]
        read_kinds[kind] += 1
        req = by_episode.get(row["episode_id"])
        reason = None
        source_channel = row.get("channel_id", row.get("channel"))
        if source_channel != channel:
            reason = "channel_mismatch"
        elif row.get("graph_version", graph_version) != graph_version:
            reason = "graph_version_mismatch"
        elif row["episode_ambiguity"] not in (None, ""):
            reason = "episode_ambiguity"
        elif row["uncertain"] is not False:
            reason = "uncertain"
        elif req is None:
            reason = "episode_not_replayed"
        elif kind == "none":
            reason = "none"
        if reason is not None:
            discarded[reason] += 1
            continue
        try:
            available = row["available_at"]
            if available is None or available.tzinfo is None:
                raise ContractError("followup available_at must be timezone aware")
            if available <= req.t_dec:
                discarded["at_or_before_t_dec"] += 1
                continue
            if available >= req.horizon_end:
                discarded["at_or_after_horizon"] += 1
                continue
            at = derived_t_start(available, policy)
            if at >= req.horizon_end:
                discarded["execution_at_or_after_horizon"] += 1
                continue
            action = ManagementAction(at=at, kind=kind, fraction=row["fraction"], stop_price=row["stop_price"],
                                      to_entry=row["to_entry"], source_message_id=row["message_id"])
        except (ContractError, ValueError, TypeError, AttributeError):
            discarded["invalid_contract"] += 1
            continue
        tie = (row.get("instruction_id") or "", canonical_json(action))
        grouped[req.episode_id].append((action, tie))
        adopted_kinds[kind] += 1
        if kind == "reduce" and action.fraction is None:
            defaulted += 1
    in_channel = len(rows) - discarded["channel_mismatch"]
    if in_channel and discarded["graph_version_mismatch"] == in_channel:
        # 本频道整表都是别的图（常见是建表时别名没解析）：逐行丢光等于没跟单，整批报错。
        found = sorted({str(row.get("graph_version")) for row in rows
                        if row.get("channel_id", row.get("channel")) == channel})
        raise ContractError(f"followup actions built for graph_version {found[:3]}, replay is {graph_version}")
    attached = []
    for req in requests:
        ordered = sorted(grouped[req.episode_id], key=lambda pair: (pair[0].at, pair[0].source_message_id, pair[1]))
        attached.append(ExecutionRequest.model_validate({**req.model_dump(), "management": [a for a, _ in ordered]}))
    n_adopted = sum(adopted_kinds.values())
    return attached, {"n_read": len(rows), "n_adopted": n_adopted, "n_discarded": sum(discarded.values()),
                      "discard_reason_counts": dict(sorted(discarded.items())),
                      "read_kind_counts": dict(sorted(read_kinds.items())),
                      "kind_counts": {kind: adopted_kinds[kind] for kind in MANAGEMENT_KINDS},
                      "n_reduce_fraction_defaulted": defaulted}


def _positive(value) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() and number > 0 else None


def nostop_shape(plan: dict) -> dict:
    """无止损计划的形状（v8 F1，同实盘 read_api 的区间单）：区间腿改成近端单笔限价（多单上沿、空单下沿）。"""
    long = plan["side"] == "long"
    entries = []
    for entry in plan["entries"]:
        if entry["kind"] == "ladder":
            near = entry["price_hi"] if long else entry["price_lo"]
            entries.append(dict(entry, kind="limit", price_lo=near, price_hi=near))
        else:
            entries.append(dict(entry))
    return dict(plan, entries=entries)


def nostop_size(plan: dict, policy, risk_budget: Decimal, multiplier: Decimal) -> tuple[dict, dict]:
    """每腿名义 N_i = leg_k×B（或 plan_k×B/n），q_i = N_i/(p_i×mult)；写成 fixed_qty 总量 + 数量分配比例（余量并入末腿）。

    价格用 live 变换（含让点、取整）之后的最终价格；市价腿用 t_dec as-of 标记价。内核按 step 取整并做 LOT/MIN_NOTIONAL/保证金拒单。
    """
    legs = plan["entries"]
    n = len(legs)
    if policy.nostop_leg_notional_k is not None:
        notionals = [policy.nostop_leg_notional_k * risk_budget] * n
    else:
        notionals = [policy.nostop_plan_notional_k * risk_budget / n] * n
    quantities = []
    for notional, leg in zip(notionals, legs):
        price = _positive(leg.get("price_lo"))
        if price is None:
            raise ContractError("nostop sizing needs a positive price on every leg")
        quantities.append(notional / (price * multiplier))
    total = sum(quantities).quantize(RATIO_QUANTUM, rounding=ROUND_DOWN)
    fractions = quantity_fractions(quantities)
    sized = dict(plan, entries=[dict(leg, fraction=f) for leg, f in zip(legs, fractions)],
                 sizing={"mode": "fixed_qty", "qty": total})
    info = {"sizing_basis": SIZING_NOSTOP, "nostop_notional_U": sum(notionals, Decimal(0)), "legs_n": n,
            "legs_gt3": n > 3, "multiplier": multiplier}
    return sized, info


def prepare_episode_request(row: dict, *, marks, lake: Path, policy, risk_budget: Decimal,
                            text: str | None = None, rule_cache: dict | None = None, stop_text: str | None = None,
                            info: dict | None = None,
                            ) -> tuple[ExecutionRequest | None, dict | None, str | None, bool, bool]:
    """Shared L0 single-episode construction; return request, audit, exclusion, resolution flags.

    Management is attached separately by attach_management, exactly as in replay.
    Source rows and policy identities remain unchanged. ``info`` (optional) receives the sizing basis
    (risk / nostop), the planned stopless notional, leg count and contract multiplier for the trades table.
    """
    info = {} if info is None else info
    fixed, why = resolve_market_refs(row, marks)
    if fixed is None:
        return None, None, why, False, False
    resolved = fixed is not row
    stale = bool(fixed.get("stale_quote_as_limit"))
    if fixed.get("order_plan") is None:
        return None, None, "PLAN_NOT_EXECUTABLE", resolved, stale
    nostop = fixed["order_plan"].get("stop") is None
    if nostop and not policy.nostop_enabled:
        return None, None, "PLAN_NO_STOP", resolved, stale
    rule_cache = {} if rule_cache is None else rule_cache
    legs = len(fixed["order_plan"].get("entries") or [])
    info.update({"sizing_basis": SIZING_RISK, "nostop_notional_U": None, "legs_n": legs, "legs_gt3": legs > 3,
                 "multiplier": None})

    def decision_rule():
        inst = fixed["instrument_id"]
        if inst not in rule_cache:
            rule_cache[inst] = load_rules(LakePaths(lake), inst)
        # Live variants have latency_s=0. Use only rules known at the
        # decision, just as market_ref uses the decision's as-of mark.
        return rule_at(rule_cache[inst], inst, fixed["t_dec"])

    multiplier = None
    if nostop:
        # 无止损定量需要当时的交易规则（与 live 开关无关）；取不到不能和合约错误混在一起（无法比较 ≠ 相等）。
        rule = decision_rule() if fixed.get("instrument_id") is not None and fixed.get("t_dec") is not None else None
        multiplier = None if rule is None else _positive(rule.get("multiplier"))
        if rule is None or rule.get("status") != "TRADING" or _positive(rule.get("tick_size")) is None or multiplier is None:
            return None, None, "NOSTOP_RULES_UNRESOLVED", resolved, stale
        fixed = dict(fixed, order_plan=nostop_shape(fixed["order_plan"]))
    try:
        execution_row, audit = fixed, None
        if policy.live_execution_profile:
            rule = decision_rule()
            tick = None
            if rule is not None and rule.get("status") == "TRADING" and rule.get("tick_size") is not None:
                value = Decimal(str(rule["tick_size"]))
                if value.is_finite() and value > 0:
                    tick = value
            execution_row, audit = prepare_execution(fixed, policy=policy,
                text=text, tick_size=tick, stop_text=stop_text)
        if nostop:
            plan, sizing = nostop_size(execution_row["order_plan"], policy, risk_budget, multiplier)
            execution_row = dict(execution_row, order_plan=plan)
            info.update(sizing)
        request = build_request(execution_row, policy_version=policy.version, policy_hash=policy.content_hash,
                                risk_budget=risk_budget, market_manifest="l0-active-silver",
                                cost_scenario="base", path_scenario="primary")
        return request, audit, None, resolved, stale
    except (ContractError, ValueError) as exc:
        return None, None, f"PLAN_CONTRACT_INVALID:{type(exc).__name__}", resolved, stale


# ---------------------------------------------------------------------------
# v8 §4 G2 第二遍：重发与改单按原单家族当时是否在场判定
# ---------------------------------------------------------------------------
SECOND_PASS_EXCLUSIONS = ("REPOST_OF_LIVE_PLAN", "AMEND_REQUIRES_FOLLOW", "AMEND_TARGET_FILLED")


def link_of(row: dict) -> tuple[str | None, str | None]:
    """(kind, target episode) from G1 plan_merge columns; repost wins if both are set."""
    for kind, column in (("repost", "repost_of"), ("amend", "amend_of")):
        target = row.get(column)
        if target not in (None, ""):
            return kind, str(target)
    return None, None


def alive_at(events: list[dict], t: dt.datetime) -> bool:
    """Only events strictly before t: a working entry order, or an open position not yet closed."""
    prior = [e for e in events if e["ts"] < t]
    terminal = {e["order_id"] for e in prior if e["kind"] in TERMINAL_KINDS}
    if any(e["kind"] == "accepted" and e["leg"] == "entry" and e["order_id"] not in terminal for e in prior):
        return True
    entered = sum((e["qty"] for e in prior if e["kind"] in FILL_KINDS and e["leg"] == "entry"), Decimal(0))
    exited = sum((e["qty"] for e in prior if e["kind"] in FILL_KINDS and e["leg"] in ("sl", "tp", "close")), Decimal(0))
    return entered - exited > 0 and not any(e["kind"] == "closed" for e in prior)


def entry_filled_before(events: list[dict], t: dt.datetime) -> bool:
    return any(e["kind"] in FILL_KINDS and e["leg"] == "entry" and e["ts"] < t for e in events)


def second_pass(first: pl.DataFrame, pending: list[tuple[ExecutionRequest, str, str]], *, follow: bool,
                run) -> tuple[list[pl.DataFrame], Counter, dict]:
    """Causal second pass over repost/amend requests in (t_dec, episode_id) order.

    family(T) = {T} ∪ executed reposts of it. A target skipped here is replaced by its nearest executed
    ancestor along the link chain; a target with no execution result (excluded in G1 or L0) makes E an
    independent plan that starts a new family. ``run(request)`` simulates one request into a one-row frame.
    """
    events = {r["episode_id"]: r["canonical_events"] for r in first.select("episode_id", "canonical_events").to_dicts()}
    family = {eid: eid for eid in events}
    members = {eid: [eid] for eid in events}
    skipped: dict[str, str] = {}
    excluded, decisions, parts = Counter(), {}, []
    for req, kind, target in sorted(pending, key=lambda item: (item[0].t_dec, item[0].episode_id)):
        seen = set()
        while target in skipped and target not in seen:
            seen.add(target)
            target = skipped[target]
        eid = req.episode_id
        reason = None
        if target not in events:
            decision = "independent_no_target_result"
        elif kind == "repost":
            if any(alive_at(events[m], req.t_dec) for m in members[family[target]]):
                reason = "REPOST_OF_LIVE_PLAN"
            decision = "repost_family_ended"
        else:
            if not follow:
                reason = "AMEND_REQUIRES_FOLLOW"
            elif entry_filled_before(events[target], req.t_dec):
                reason = "AMEND_TARGET_FILLED"
            decision = "amend_target_unfilled"
        if reason is not None:
            excluded[reason] += 1
            skipped[eid] = target
            decisions[eid] = {"kind": kind, "target": target, "decision": reason}
            continue
        frame = run(req)
        parts.append(frame)
        events[eid] = frame["canonical_events"].to_list()[0] if frame.height else []
        root = family[target] if kind == "repost" and decision == "repost_family_ended" else eid
        family[eid] = root
        members.setdefault(root, [])
        if eid not in members[root]:
            members[root].append(eid)
        decisions[eid] = {"kind": kind, "target": target, "decision": decision, "family": root}
    return parts, excluded, decisions


#: G1 episode 列，存在时原样透传到 trades（G2 用 .get 读，合入先后无硬依赖；edit_delay_s 等只作诊断）。
PASSTHROUGH_COLUMNS = ("nostop_kind", "venue_hint", "triage_verdict", "plan_link_kind", "family_id", "stop_rule",
                       "time_ref_promoted", "promotion_scope", "signal_age_s", "edit_delay_s", "edit_may_contain_outcome")
DEC_COLUMN = pl.Decimal(38, 12)
DERIVED_COLUMNS = {"sizing_basis": pl.String, "nostop_notional_U": DEC_COLUMN, "legs_n": pl.Int32, "legs_gt3": pl.Boolean,
                   "mae_U": DEC_COLUMN, "mae_pct_notional": DEC_COLUMN, "entry_notional_U": DEC_COLUMN,
                   "censor_at": pl.Datetime("us", "UTC"), "mtm_U_at_censor": DEC_COLUMN}


def _dec12(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(RATIO_QUANTUM)


def censor_time(req: ExecutionRequest, row: dict) -> dt.datetime | None:
    """标签删失 = 观察窗或持仓上限终点（取先到者）；证据删失 = 最后一个事件时刻（内核此后不再有证据）。"""
    if row["censor_reason"] is None:
        return None
    if row["censor_reason"] == "LABEL_RIGHT_CENSORED":
        end = req.horizon_end
        hold = req.order_plan.expiry.max_holding_s
        if hold is not None and row["position_open_at"] is not None:
            end = min(end, row["position_open_at"] + dt.timedelta(seconds=hold))
        return end
    events = row["canonical_events"]
    return max(e["ts"] for e in events) if events else req.resolved_t_start(resolve_policy(req.policy_version))


def derived_columns(results: pl.DataFrame, requests: dict, sizing_info: dict, marks) -> dict:
    """Per-trade G2 columns: sizing basis, nostop notional, MAE in U, entry notional and censor-time mark-to-market."""
    out = {}
    for row in results.to_dicts():
        eid = row["episode_id"]
        req = requests[eid]
        info = sizing_info.get(eid, {})
        budget = Decimal(str(row["risk_budget"]))
        basis = info.get("sizing_basis", SIZING_RISK)
        notional = info.get("nostop_notional_U")
        mae_U = None if row["mae_R"] is None else row["mae_R"] * budget
        mult = info.get("multiplier")
        entry_notional = None
        if row["filled_qty"] > 0 and row["entry_avg_price"] is not None and mult is not None:
            entry_notional = row["filled_qty"] * row["entry_avg_price"] * mult
        censor_at = censor_time(req, row)
        mtm = None
        if basis == SIZING_NOSTOP and censor_at is not None:
            mtm = Decimal(0) if row["filled_qty"] == 0 else None
            if row["filled_qty"] > 0 and row["entry_avg_price"] is not None and mult is not None:
                exited = sum((e["qty"] for e in row["canonical_events"]
                              if e["kind"] in FILL_KINDS and e["leg"] in ("sl", "tp", "close")), Decimal(0))
                residual = row["filled_qty"] - exited
                sign = 1 if req.order_plan.side == "long" else -1
                unrealized = Decimal(0)
                mark_price = row["entry_avg_price"] if residual == 0 else None
                if residual != 0:
                    mark = marks.mark_at(req.order_plan.instrument_id, censor_at)
                    mark_price = mark.price
                if mark_price is not None:
                    unrealized = (Decimal(str(mark_price)) - row["entry_avg_price"]) * residual * sign * mult
                    mtm = row["gross_pnl"] + unrealized - row["fees"] + row["funding"]
        out[eid] = {
            "sizing_basis": basis, "nostop_notional_U": _dec12(notional),
            "legs_n": info.get("legs_n", len(req.order_plan.entries)),
            "legs_gt3": info.get("legs_gt3", len(req.order_plan.entries) > 3),
            "mae_U": _dec12(mae_U),
            "mae_pct_notional": _dec12(mae_U * 100 / notional) if basis == SIZING_NOSTOP and mae_U is not None and notional else None,
            "entry_notional_U": _dec12(entry_notional), "censor_at": censor_at, "mtm_U_at_censor": _dec12(mtm),
        }
    return out


def replay(*, graph_version: str, channel: int, out: str | Path, market_lake: str | Path | None = None,
           policy_version: str = "base-v1", risk_budget: Decimal = Decimal("100"),
           followup_actions: str | Path | None = None) -> dict:
    layout = Layout.from_root(None)
    graph_version = resolve_alias(layout, graph_version)
    channel = canonical_peer_id(channel, "channel")
    lake = Path(market_lake) if market_lake is not None else layout.quarantine_path.parent.parent / "lake" / "market"
    lake = lake.expanduser().resolve()
    policy = resolve_policy(policy_version)
    followup_path = Path(followup_actions).expanduser().resolve() if followup_actions is not None else layout.silver_dir / "followup_action.parquet"
    followup_rows = load_followup_actions(followup_path) if policy.follow_teacher else []
    episodes = load_episodes(graph_version, decision_graph=True).filter(pl.col("channel_id") == channel)
    descriptions = load_episodes(graph_version, decision_graph=False).filter(
        (pl.col("channel_id") == channel) & pl.col("entry_observed")
    )
    excluded = descriptions.filter(~pl.col("episode_id").is_in(episodes["episode_id"].to_list()))
    # 描述图只用于计损耗，绝不拿描述图的终态/止损/目标构造执行请求。
    exclusion_reasons = Counter()
    coverage_excluded = 0
    for row in excluded.iter_rows(named=True):
        codes = row["dec_reason_codes"] or []
        if not codes:
            # 没有决策时刻的根（如 SYMBOL_TIME_INVALID）没有决策原因，使用描述原因计损耗。
            codes = row["reason_codes"] or []
        for code in set(codes):
            exclusion_reasons[code] += 1
        if not codes:
            exclusion_reasons["G1_DECISION_INELIGIBLE"] += 1
        if set(codes) & set(EVIDENCE_CENSORS) or row["instrument_id"] is None:
            coverage_excluded += 1
    from quant_lab.data.market_lake import LakeMarket
    marks = LakeMarket(lake)
    requests, replay_exclusions, market_ref_resolved, stale_quote_limits = [], Counter(), [], []
    live_records, rule_cache = {}, {}
    root_texts = {}
    if policy.live_execution_profile:
        root_ids = [value for value in episodes["root_source_version_id"].to_list() if value is not None]
        # v8 F4：止损可能在另一条消息里（补充止损），按行另读 stop_source_version_id 的原文。
        stop_ids = ([value for value in episodes["stop_source_version_id"].to_list() if value is not None]
                    if "stop_source_version_id" in episodes.columns else [])
        root_texts = load_message_texts(root_ids + sorted(set(stop_ids) - set(root_ids)))
        if root_ids and not any(value in root_texts for value in root_ids):
            # 一条原文都读不到 = 数据根缺 bronze（构建常把它留在 _build/<hash>），不是「老师都写精确价」；
            # 静默按无原文跑会让让点口径一次都不让（10-02 所有 live 结果因此作废）。
            raise ContractError(f"live profile read no root message text; expected bronze at {layout.message_version}")
    sizing_info, pending, rows_by_episode = {}, [], {}
    for row in episodes.iter_rows(named=True):
        info = {}
        request, audit, why, resolved, stale = prepare_episode_request(
            row, marks=marks, lake=lake, policy=policy, risk_budget=risk_budget,
            text=root_texts.get(row["root_source_version_id"]), rule_cache=rule_cache,
            stop_text=root_texts.get(row.get("stop_source_version_id")), info=info)
        if resolved:
            market_ref_resolved.append(row["episode_id"])
        if stale:
            stale_quote_limits.append(row["episode_id"])
        if request is None:
            replay_exclusions[why] += 1
            continue
        requests.append(request)
        sizing_info[row["episode_id"]] = info
        rows_by_episode[row["episode_id"]] = row
        if audit is not None:
            live_records[row["episode_id"]] = audit
    market_hashes = {}
    followup_report = None
    if policy.follow_teacher:
        requests, followup_report = attach_management(requests, followup_rows, policy=policy, channel=channel,
                                                     graph_version=graph_version)
        followup_report["path"] = str(followup_path)

    def resolver(request):
        view = load_market_from_lake(request, lake_root=lake)
        market_hashes[request.episode_id] = view.manifest_hash
        return view

    links = {req.episode_id: link_of(rows_by_episode[req.episode_id]) for req in requests}
    pending = [(req, *links[req.episode_id]) for req in requests if links[req.episode_id][0] is not None]
    results = simulate_batch([req for req in requests if links[req.episode_id][0] is None], kernel="A", resolver=resolver)
    parts, second_excluded, second_decisions = second_pass(
        results, pending, follow=policy.follow_teacher,
        run=lambda req: simulate_batch([req], kernel="A", resolver=resolver))
    if parts:
        results = pl.concat([results, *parts], how="vertical")
    replay_exclusions.update(second_excluded)
    for eid, decision in second_decisions.items():
        if decision["decision"] in SECOND_PASS_EXCLUSIONS:
            live_records.pop(eid, None)
    n_replayed = results.height
    by_request = {req.episode_id: req for req in requests}
    derived = derived_columns(results, by_request, sizing_info, marks)
    teacher_stats = {row["episode_id"]: management_stats(row["canonical_events"])
                     for row in results.select("episode_id", "canonical_events").to_dicts()}
    plans = episodes.select("episode_id", pl.col("instrument_id").alias("instrument"), "side", "order_plan")
    table = results.join(plans, on="episode_id", how="left").with_columns(
        pl.col("order_plan").struct.field("entries").alias("entries"),
        pl.col("order_plan").struct.field("stop").struct.field("price").alias("stop"),
        pl.col("order_plan").struct.field("tps").alias("targets"),
    ).drop("order_plan", "canonical_events").with_columns(
        pl.col("episode_id").is_in(market_ref_resolved).alias("market_ref_resolved"),   # 参考价由 t_dec as-of 标记价补出
        pl.col("episode_id").is_in(stale_quote_limits).alias("stale_quote_as_limit"),   # 过时现价改为报价处限价挂单
    ).sort(["t_dec", "episode_id"])
    table = table.with_columns(
        pl.Series("n_teacher_actions_executed", [teacher_stats[eid]["n_executed"] for eid in table["episode_id"]], dtype=pl.Int64),
        pl.Series("last_teacher_action_kind", [teacher_stats[eid]["last_kind"] for eid in table["episode_id"]], dtype=pl.String),
    )
    table = table.with_columns(*[pl.Series(name, [derived[eid][name] for eid in table["episode_id"]], dtype=dtype)
                                  for name, dtype in DERIVED_COLUMNS.items()])
    passthrough = [name for name in PASSTHROUGH_COLUMNS if name in episodes.columns and name not in table.columns]
    if passthrough:
        table = table.join(episodes.select("episode_id", *passthrough), on="episode_id", how="left").sort(["t_dec", "episode_id"])
    if pending:
        table = table.with_columns(pl.Series("second_pass_decision",
            [second_decisions.get(eid, {}).get("decision") for eid in table["episode_id"]], dtype=pl.String))
    if policy.live_execution_profile:
        table = table.with_columns(pl.Series("live_execution_json",
            [canonical_json(live_records[eid]) for eid in table["episode_id"]], dtype=pl.String))
    report = {
        "claim_status": "descriptive_only", "graph_version": graph_version, "channel": channel,
        "kernel": "A", "policy_version": policy.version, "policy_hash": policy.content_hash,
        "policy": policy.model_dump(mode="json"), "risk_budget": str(risk_budget),
        "cost_scenario": "base", "path_scenario": "primary", "market_lake": str(lake),
        "market_manifest_hashes": market_hashes,
        "metric_definitions": {
            "fill_rate": "有成交数量的请求 / 全部决策请求（含删失样本的已观察成交）",
            "mean_net_R": "完整覆盖且未删失的净 R 均值，包含未成交的 0 R",
            "win_rate": "完整覆盖、未删失且已成交样本中 net_R > 0 的比例",
            "cumulative_R": "完整覆盖且未删失样本按 t_dec、episode_id 累加；不是组合权益曲线",
            "coverage_counts": "覆盖排除与删失可能重叠；各原因计数也可能重叠",
            "scope": "overall/by/cumulative_R/censor_counts 只统计 sizing_basis=risk 的行（按风险预算定量）；"
                     "无止损行见 blocks.nostop（金额 U = net_R × risk_budget）；blocks.total 只有金额合计，不出合计均值与胜率",
            "nostop_adverse_excursion": "无止损块最大不利浮亏的幅度 −mae_U（U）及其占计划名义的百分比 −mae_pct_notional",
            "mtm_U_at_censor": "删失的无止损单在删失时刻（标签删失=观察窗或持仓上限终点，证据删失=最后一个事件时刻）"
                               "按 as-of 标记价计的净值：已实现 + 未实现 − 费用 + 资金费",
        },
        "g1_exclusions": {"n_entry_episodes": descriptions.height, "n_excluded": excluded.height,
                          "reason_counts": dict(sorted(exclusion_reasons.items())),
                          "n_coverage_excluded": coverage_excluded},
        "replay_exclusions": {"n_decision_episodes": episodes.height, "n_replayed": n_replayed,
                              "reason_counts": dict(sorted(replay_exclusions.items()))},
        "second_pass": {"n_pending": len(pending),
                        "n_executed": sum(d["decision"] not in SECOND_PASS_EXCLUSIONS for d in second_decisions.values()),
                        "decision_counts": dict(sorted(Counter(d["decision"] for d in second_decisions.values()).items()))},
        "market_ref_entries": {"n_resolved": len(market_ref_resolved), "reference_source": MARKET_REF_SOURCE,
                               "n_stale_quote_as_limit": len(stale_quote_limits),
                               "note": "全部市价腿（含通过 0.25R 报价门的现价腿），参考价取 t_dec 时 as-of 标记价，仅用于定仓；成交由内核在 t_start 按市价撮合"},
        **summarize(table),
    }
    if policy.live_execution_profile:
        report["live_execution_profile"] = {
            "profile": PROFILE_VERSION, "sources": SOURCES,
            "n_applied": sum(a["status"] == "applied" for a in live_records.values()),
            "n_rules_unresolved": sum(a["status"] == "rules_unresolved" for a in live_records.values()),
            "n_root_text_unresolved": sum(a.get("root_text_resolved") is False for a in live_records.values()),
            "rule_counts": {rule: sum(a["rule_counts"][rule] for a in live_records.values()) for rule in RULES},
            "count_units": "成功构造请求：入场/止盈按腿，止损按计划，zone/双明确点位按组，tick 按改变的价格",
            "allocation": "zone: qty ∝ risk_share / stop_distance；双明确点位: qty ∝ 1 / price；总风险和 lot 取整沿用内核 A",
        }
    target = Path(out)
    if followup_report is not None:
        ignored = Counter()
        for stats in teacher_stats.values():
            ignored.update(stats["ignored_counts"])
        followup_report["execution"] = {
            "n_processed": sum(s["n_processed"] for s in teacher_stats.values()),
            "n_not_processed": followup_report["n_adopted"] - sum(s["n_processed"] for s in teacher_stats.values()),
            "n_executed": sum(s["n_executed"] for s in teacher_stats.values()),
            "kind_counts": {kind: sum(s["kind_counts"][kind] for s in teacher_stats.values()) for kind in MANAGEMENT_KINDS},
            "ignored_counts": dict(sorted(ignored.items())),
            "n_reduce_fraction_defaulted": sum(s["n_reduce_fraction_defaulted"] for s in teacher_stats.values()),
        }
        report["follow_teacher"] = followup_report
    target.mkdir(parents=True, exist_ok=True)
    write_parquet_atomic(table, target / "trades.parquet")
    (target / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["claim_status: descriptive_only", "", f"graph_version: {graph_version}", f"channel: {channel}",
             f"policy: {policy.version}", f"policy_hash: {policy.content_hash}",
             f"kernel: A; cost: base; path: primary; risk_budget: {risk_budget}", "",
             "政策未给 TTL/比例时用 G2 基线兜底；观察窗由 G2 build_request 推导。",
             "缺行情或历史交易规则时保留删失，不填补；结果仅为描述。", ""]
    for key, definition in report["metric_definitions"].items():
        lines.append(f"- {key}: {definition}")
    # 只输出由数值、身份与原因码组成的白名单字段，禁止拷贝消息原文。
    for key in ("overall", "by", "g1_exclusions", "censor_counts", "coverage_failure_counts", "cumulative_R", "blocks"):
        lines.extend(["", f"### {key}", "", "```json", json.dumps(report[key], ensure_ascii=False, indent=2), "```"])
    if policy.live_execution_profile:
        lines.extend(["", "### live_execution_profile", "", "```json",
                      json.dumps(report["live_execution_profile"], ensure_ascii=False, indent=2), "```"])
    if followup_report is not None:
        lines.extend(["", "### follow_teacher", "", "```json",
                      json.dumps(followup_report, ensure_ascii=False, indent=2), "```"])
    (target / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-version", required=True)
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--market-lake")
    parser.add_argument("--policy", default="base-v1")
    parser.add_argument("--risk-budget", type=Decimal, default=Decimal("100"))
    parser.add_argument("--followup-actions")
    args = parser.parse_args(argv)
    report = replay(graph_version=args.graph_version, channel=args.channel, out=args.out,
                    market_lake=args.market_lake, policy_version=args.policy, risk_budget=args.risk_budget,
                    followup_actions=args.followup_actions)
    print("claim_status: descriptive_only")
    print(json.dumps(report["overall"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
