"""单老师 L0 描述性重放，放在 market：只编排 G1→G2，不改变 G3 研究代码摘要。

python -m quant_lab.market.l0_replay --graph-version GV --channel ID --out DIR
行情缺省 DATA_ROOT/lake/market；不联网、不补行情/规则。live 策略只按根原文措辞变换执行计划副本。
G2 仍要求已体检分区（manifest.check_status）及 instrument_rules 历史规则；缺失会计入覆盖排除。
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path

import polars as pl

from quant_lab.data.api import load_episodes, load_message_texts
from quant_lab.data.graph import resolve_alias
from quant_lab.data.lake import Layout, write_parquet_atomic
from quant_lab.data.sources import canonical_peer_id
from quant_lab.market.contract import (
    EVIDENCE_CENSORS, MANAGEMENT_KINDS, ContractError, ExecutionRequest, ManagementAction,
    build_request, canonical_json, derived_t_start, management_stats, resolve_policy,
)
from quant_lab.market.execution import load_market_from_lake, simulate_batch
from quant_lab.market.live_profile import PROFILE_VERSION, RULES, SOURCES, apply_live_profile
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


def summarize(table: pl.DataFrame) -> dict:
    rows = table.sort(["t_dec", "episode_id"]).to_dicts()
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
            "coverage_failure_counts": {key: sum(r[key] is not True for r in rows)
                                        for key in ("mark_ok", "funding_ok", "rules_ok", "bars_ok")}}


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


def prepare_execution(row: dict, *, policy, text: str | None = None, tick_size: Decimal | None = None) -> tuple[dict, dict | None]:
    """Apply the optional profile before build_request, preserving the source row."""
    if not policy.live_execution_profile:
        return row, None
    if tick_size is None:
        audit = {"profile": PROFILE_VERSION, "status": "rules_unresolved", "rule_counts": dict.fromkeys(RULES, 0),
                 "source_version_id": row.get("root_source_version_id"), "root_text_resolved": text is not None,
                 "execution_plan": row["order_plan"]}
        return row, audit
    plan, audit = apply_live_profile(row["order_plan"], text, tick_size)
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
        root_texts = load_message_texts(episodes["root_source_version_id"].to_list())
    for row in episodes.iter_rows(named=True):
        fixed, why = resolve_market_refs(row, marks)
        if fixed is None:
            replay_exclusions[why] += 1
            continue
        if fixed is not row:
            market_ref_resolved.append(row["episode_id"])
        if fixed.get("stale_quote_as_limit"):
            stale_quote_limits.append(row["episode_id"])
        if fixed.get("order_plan") is None:
            # 决策时刻已定但凑不成计划（方向或品种缺失等）：与"没有止损"分开计，否则会把解析缺口算成老师没给止损。
            replay_exclusions["PLAN_NOT_EXECUTABLE"] += 1
            continue
        if fixed["order_plan"].get("stop") is None:
            # 按风险预算定仓要止损距离；原文没给止损的计划无法折成 R，单独计数，不混进契约错误。
            replay_exclusions["PLAN_NO_STOP"] += 1
            continue
        try:
            execution_row, audit = fixed, None
            if policy.live_execution_profile:
                inst = fixed["instrument_id"]
                if inst not in rule_cache:
                    rule_cache[inst] = load_rules(LakePaths(lake), inst)
                # Live variants have latency_s=0. Use only rules known at the
                # decision, just as market_ref uses the decision's as-of mark.
                rule = rule_at(rule_cache[inst], inst, fixed["t_dec"])
                tick = None
                if rule is not None and rule.get("status") == "TRADING" and rule.get("tick_size") is not None:
                    value = Decimal(str(rule["tick_size"]))
                    if value.is_finite() and value > 0:
                        tick = value
                execution_row, audit = prepare_execution(fixed, policy=policy,
                    text=root_texts.get(row["root_source_version_id"]), tick_size=tick)
            requests.append(build_request(execution_row, policy_version=policy.version, policy_hash=policy.content_hash,
                                          risk_budget=risk_budget, market_manifest="l0-active-silver",
                                          cost_scenario="base", path_scenario="primary"))
            if audit is not None:
                live_records[row["episode_id"]] = audit
        except (ContractError, ValueError) as exc:   # 单笔计划不合契约：记原因码继续，不让整批中断
            replay_exclusions[f"PLAN_CONTRACT_INVALID:{type(exc).__name__}"] += 1
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

    results = simulate_batch(requests, kernel="A", resolver=resolver)
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
        },
        "g1_exclusions": {"n_entry_episodes": descriptions.height, "n_excluded": excluded.height,
                          "reason_counts": dict(sorted(exclusion_reasons.items())),
                          "n_coverage_excluded": coverage_excluded},
        "replay_exclusions": {"n_decision_episodes": episodes.height, "n_replayed": len(requests),
                              "reason_counts": dict(sorted(replay_exclusions.items()))},
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
    for key in ("overall", "by", "g1_exclusions", "censor_counts", "coverage_failure_counts", "cumulative_R"):
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
