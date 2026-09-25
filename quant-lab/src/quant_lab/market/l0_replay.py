"""单老师 L0 描述性重放，放在 market：只编排 G1→G2，不改变 G3 研究代码摘要。

python -m quant_lab.market.l0_replay --graph-version GV --channel ID --out DIR
行情缺省 DATA_ROOT/lake/market；不联网、不解释原文、不改执行契约或补行情/规则。
G2 仍要求已体检分区（manifest.check_status）及 instrument_rules 历史规则；缺失会计入覆盖排除。
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path

import polars as pl

from quant_lab.data.api import load_episodes
from quant_lab.data.graph import resolve_alias
from quant_lab.data.lake import Layout, write_parquet_atomic
from quant_lab.data.sources import canonical_peer_id
from quant_lab.market.contract import EVIDENCE_CENSORS, ContractError, build_request, resolve_policy
from quant_lab.market.execution import load_market_from_lake, simulate_batch


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
    """把无价格的 market_ref 入场补成 t_dec 时的 as-of 标记价；取不到就返回原因码，由调用方计入覆盖排除。"""
    plan = row.get("order_plan")
    entries = (plan or {}).get("entries") or []
    if not any(e.get("kind") == "market_ref" and e.get("price_lo") is None for e in entries):
        return row, None
    if row.get("t_dec") is None or row.get("instrument_id") is None:
        return None, "MARKET_REF_UNRESOLVED"
    mark = marks.mark_at(row["instrument_id"], row["t_dec"])
    if mark.price is None:
        return None, f"MARKET_REF_UNRESOLVED:{mark.reason or 'NO_MARK'}"
    fixed = [dict(e, price_lo=mark.price, price_hi=mark.price) if e.get("kind") == "market_ref" and e.get("price_lo") is None else e
             for e in entries]
    return dict(row, order_plan=dict(plan, entries=fixed)), None


def replay(*, graph_version: str, channel: int, out: str | Path, market_lake: str | Path | None = None,
           policy_version: str = "base-v1", risk_budget: Decimal = Decimal("100")) -> dict:
    layout = Layout.from_root(None)
    graph_version = resolve_alias(layout, graph_version)
    channel = canonical_peer_id(channel, "channel")
    lake = Path(market_lake) if market_lake is not None else layout.quarantine_path.parent.parent / "lake" / "market"
    lake = lake.expanduser().resolve()
    policy = resolve_policy(policy_version)
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
    requests, replay_exclusions, market_ref_resolved = [], Counter(), []
    for row in episodes.iter_rows(named=True):
        fixed, why = resolve_market_refs(row, marks)
        if fixed is None:
            replay_exclusions[why] += 1
            continue
        if fixed is not row:
            market_ref_resolved.append(row["episode_id"])
        try:
            requests.append(build_request(fixed, policy_version=policy.version, policy_hash=policy.content_hash,
                                          risk_budget=risk_budget, market_manifest="l0-active-silver",
                                          cost_scenario="base", path_scenario="primary"))
        except (ContractError, ValueError) as exc:   # 单笔计划不合契约：记原因码继续，不让整批中断
            replay_exclusions[f"PLAN_CONTRACT_INVALID:{type(exc).__name__}"] += 1
    market_hashes = {}

    def resolver(request):
        view = load_market_from_lake(request, lake_root=lake)
        market_hashes[request.episode_id] = view.manifest_hash
        return view

    results = simulate_batch(requests, kernel="A", resolver=resolver)
    plans = episodes.select("episode_id", pl.col("instrument_id").alias("instrument"), "side", "order_plan")
    table = results.join(plans, on="episode_id", how="left").with_columns(
        pl.col("order_plan").struct.field("entries").alias("entries"),
        pl.col("order_plan").struct.field("stop").struct.field("price").alias("stop"),
        pl.col("order_plan").struct.field("tps").alias("targets"),
    ).drop("order_plan", "canonical_events").with_columns(
        pl.col("episode_id").is_in(market_ref_resolved).alias("market_ref_resolved"),   # 参考价由 t_dec as-of 标记价补出
    ).sort(["t_dec", "episode_id"])
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
                               "note": "原文只写「现价 / 附近」无价格的入场，参考价取 t_dec 时 as-of 标记价，仅用于定仓；成交由内核在 t_start 按市价撮合"},
        **summarize(table),
    }
    target = Path(out)
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
    args = parser.parse_args(argv)
    report = replay(graph_version=args.graph_version, channel=args.channel, out=args.out,
                    market_lake=args.market_lake, policy_version=args.policy, risk_budget=args.risk_budget)
    print("claim_status: descriptive_only")
    print(json.dumps(report["overall"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
