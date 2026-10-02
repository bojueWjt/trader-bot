"""G1 对外接口（契约 research-schema §7 + §9）与端到端构建 CLI。

- `load_episodes(graph_version, *, decision_graph=True)`：决策视图 = 由 t_dec 前可见边重放出的决策列（P/C、stop/tps、n_events、
  reason_codes、time_grade_min、复制组、派生哈希、eligibility），终态/结果/censor 不出现；只返回有决策根且用途准入
  （eligibility.entry_decision 且无致命原因）的行（S01/S07）。
- `load_episode_events(graph_version)`：决策视图，只含 edge_available_at < 所属 episode 的 t_dec 的边（顺序未知严格 <）。
  描述图事件另用 `load_episode_events_description(graph_version)`（签名不改契约函数）。
- `loss_table(batch_id)`（`latest` 便利入口会在结果里带实际 batch_id）、`quarantine(flow, *, status=None)`。
- 读前校验发布 manifest（存在、published、文件 hash 一致、未 tombstone）；缺/不符/撤销一律拒读（S11）。
- 湖根目录默认由 QUANT_LAB_DATA_ROOT 决定；只读访问可显式传入 Layout。
"""
from __future__ import annotations

import argparse
from collections.abc import Iterable
import json
import os
import pathlib
from datetime import datetime
from typing import Any

import polars as pl

from .graph import resolve_alias, stale_episodes, verify_manifest
from .lake import Layout, read_quarantine
from .reasons import FATAL

DECISION_DROP = ["dec_author_plan_state", "dec_author_claim_state", "dec_stop", "dec_tps", "dec_n_events", "dec_n_invalid_transitions", "dec_reason_codes", "dec_time_grade_min",
                 "dec_duplicate_group_id", "dec_derivation_hash", "dec_eligibility", "dec_cluster_id", "dec_temporal_assumptions", "dec_audit_stratum"]


def _layout() -> Layout:
    return Layout.from_root(None)


def load_episodes(graph_version: str, *, decision_graph: bool = True, layout: Layout | None = None) -> pl.DataFrame:
    layout = _layout() if layout is None else layout
    graph_version = resolve_alias(layout, graph_version)
    verify_manifest(layout, graph_version)
    df = pl.read_parquet(layout.episode(graph_version))
    stale = stale_episodes(layout, graph_version)
    if stale:
        df = df.filter(~pl.col("episode_id").is_in(list(stale)))
    if not decision_graph:
        return df
    d = df.filter(pl.col("t_dec").is_not_null() & pl.col("entry_observed") & pl.col("dec_eligibility").struct.field("entry_decision").fill_null(False))
    d = d.filter(~pl.col("dec_reason_codes").list.eval(pl.element().is_in([str(x) for x in FATAL])).list.any().fill_null(False))
    d = d.with_columns(
        pl.col("dec_author_plan_state").alias("author_plan_state"), pl.col("dec_author_claim_state").alias("author_claim_state"),
        pl.col("dec_stop").alias("stop_at_t_dec"), pl.col("dec_tps").alias("tps_at_t_dec"),
        pl.col("dec_n_events").alias("n_events"), pl.col("dec_n_invalid_transitions").alias("n_invalid_transitions"), pl.col("dec_reason_codes").alias("reason_codes"),
        pl.col("dec_time_grade_min").alias("time_grade_min"), pl.col("dec_duplicate_group_id").alias("duplicate_group_id"), pl.col("dec_derivation_hash").alias("derivation_hash"),
        pl.col("dec_audit_stratum").alias("audit_stratum"), pl.col("dec_eligibility").alias("eligibility_by_estimand"), pl.col("dec_cluster_id").alias("cluster_id"), pl.col("dec_temporal_assumptions").alias("temporal_assumptions"),
        pl.lit(False).alias("deleted_after_observation_any"), pl.lit(False).alias("exit_observed"), pl.lit(False).alias("right_censored"), pl.lit(False).alias("replay_required"),
        pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("censor_at"), pl.lit(None, dtype=pl.String).alias("censor_reason"),
        pl.lit(None, dtype=df.schema["claimed_outcome"]).alias("claimed_outcome"), pl.lit(None, dtype=df.schema["reconstructed_outcome"]).alias("reconstructed_outcome"),
        pl.col("t_dec").alias("available_at"),
    ).drop(DECISION_DROP)
    # 契约 §9.7 A5：决策视图三列非空是硬约束；违反即拒绝返回（不静默）
    for col in ("instrument_id", "t_dec", "cluster_id"):
        if d[col].null_count():
            raise LookupError(f"决策视图 {col} 含 null（{d[col].null_count()} 行）：违反 research-schema §9.7，拒绝返回")
    return d


def load_episode_events(graph_version: str, *, layout: Layout | None = None) -> pl.DataFrame:
    layout = _layout() if layout is None else layout
    graph_version = resolve_alias(layout, graph_version)
    verify_manifest(layout, graph_version)
    ev = pl.read_parquet(layout.episode_event(graph_version))
    stale = stale_episodes(layout, graph_version)
    if stale:
        ev = ev.filter(~pl.col("episode_id").is_in(list(stale)))
    ep = pl.read_parquet(layout.episode(graph_version)).select("episode_id", "t_dec")
    j = ev.join(ep, on="episode_id", how="inner")
    visible = j.filter(pl.col("t_dec").is_not_null() & (pl.col("edge_available_at") < pl.col("t_dec")))
    if "superseded_at" in visible.columns:
        known = pl.col("superseded_at").is_not_null() & (pl.col("superseded_at") < pl.col("t_dec"))
        visible = visible.with_columns(known.alias("superseded"), pl.when(known).then(pl.col("superseded_at")).otherwise(None).alias("superseded_at"))
    return visible.drop("t_dec")


def load_episode_events_description(graph_version: str, *, layout: Layout | None = None) -> pl.DataFrame:
    layout = _layout() if layout is None else layout
    graph_version = resolve_alias(layout, graph_version)
    verify_manifest(layout, graph_version)
    ev = pl.read_parquet(layout.episode_event(graph_version))
    stale = stale_episodes(layout, graph_version)
    return ev.filter(~pl.col("episode_id").is_in(list(stale))) if stale else ev


def load_message_texts(source_version_ids: Iterable[str], *, layout: Layout | None = None) -> dict[str, str]:
    """Read exact requested bronze versions, never the latest version of a message.

    Missing/null or conflicting duplicate text is unresolved, not guessed. This
    read-only accessor changes no extraction or decision-graph semantics.
    """
    ids = {value for value in source_version_ids if value is not None}
    layout = _layout() if layout is None else layout
    path = layout.message_version
    if not ids or not path.exists():
        return {}
    rows = pl.scan_parquet(path).filter(pl.col("source_version_id").is_in(sorted(ids))).select(
        "source_version_id", "text").collect()
    candidates: dict[str, set[str | None]] = {}
    for version, text in rows.iter_rows():
        candidates.setdefault(version, set()).add(text)
    return {version: next(iter(texts)) for version, texts in candidates.items()
            if len(texts) == 1 and None not in texts}


def loss_table(batch_id: str) -> pl.DataFrame:
    layout = _layout()
    if batch_id == "latest":
        files = sorted((p for p in layout.loss_dir.glob("tg-*.parquet") if "__map_" not in p.name), key=lambda p: p.stat().st_mtime)
        if not files:
            raise FileNotFoundError(f"无损耗表：{layout.loss_dir}")
        return pl.read_parquet(files[-1])
    return pl.read_parquet(layout.loss(batch_id))


def quarantine(flow: str, *, status: str | None = None) -> pl.DataFrame:
    layout = _layout()
    path = layout.quarantine_path if flow == "telegram" else layout.quarantine_path.parent / f"{flow}.parquet"
    q = read_quarantine(path)
    return q.filter(pl.col("status") == status) if status else q


def build(fixture_dir: str | os.PathLike | None, layout: Layout, *, graph_version: str, llm_fixture: str | os.PathLike | None = None, ocr_fixture: str | os.PathLike | None = None,
          adjudicator_fixture: str | os.PathLike | None = None, ingested_at: datetime | None = None, alias: bool = False, llm: str | None = None,
          market_lake: str | os.PathLike | None = None, market: str = "fixture",
          export_dir: str | os.PathLike | None = None, channel: int | None = None, plan_source: str = "parser",
          edit_visible_at_last_edit: bool = False, chart_fixture: str | os.PathLike | None = None) -> dict[str, Any]:
    """端到端：归一 → 去重 → 抽取 → 行情校验 → 链接 → 生命周期 + 发布。
    仅显式 market=real 或 market_lake 启用真实行情，默认保持夹具行为。
    alias=True：把 graph_version 当别名，实际发布不可变版本 `<alias>@<input_hash[:8]>` 并把别名指过去；旧版本原样保留，不删除（T04）。"""
    from . import dedup, extract, lifecycle, linker, normalize, validate
    from .graph import set_alias
    from .llm import RecordedClient, extraction_client

    chart = extract.load_chart_fixture(chart_fixture) if chart_fixture is not None else None
    from .plan_source import MODES
    if plan_source not in MODES:
        raise ValueError(f"invalid plan_source: {plan_source}")
    if llm is not None:
        extraction_client(llm=llm, llm_fixture=llm_fixture)  # 闸门先于归一写盘。
    from .lake import stable_id
    from .sources import canonical_peer_id, ingest_pull_dir, load_channel_whitelist, read_all

    if market not in ("fixture", "real"):
        raise ValueError("market 必须为 fixture 或 real")
    real = market_lake is not None or market == "real"
    root = Layout.from_root(None).quarantine_path.parent.parent
    selected = canonical_peer_id(channel, "channel") if channel is not None else None
    whitelist_path = root / "import" / "telegram" / "channels.txt"
    allowed = load_channel_whitelist(whitelist_path)
    source = pathlib.Path(fixture_dir) if fixture_dir is not None else root / "import" / "telegram"
    normalize_kwargs = {}
    if real or export_dir is not None:
        from .harvest import _data_root, scan_inputs
        root = _data_root()
        inputs = scan_inputs(root, export_dir=export_dir if export_dir is not None else source,
                             require_whitelist=True, channel=selected)
        source = inputs["source"]
        allowed = inputs["allowed"]
        pull_msgs, _ = ingest_pull_dir(inputs["default_export"] / "pull", allowed_peer_ids=allowed, root=inputs["default_export"])
        normalize_kwargs = {"tdesktop_only": True, "allowed_peer_ids": allowed,
                            "source_messages": inputs["scan"].messages, "extra_messages": pull_msgs}
    elif allowed is not None or selected is not None:
        if allowed is not None and selected is not None and selected not in allowed:
            raise ValueError("CHANNEL_NOT_WHITELISTED")
        if selected is not None:
            allowed = frozenset({selected})
        normalize_kwargs = {"source_messages": list(read_all(source, allowed_peer_ids=allowed))}
    # 使用独立派生工作集，避免累计 harvest 湖或前一次其他老师构建混入本次图。
    if real or export_dir is not None or allowed is not None or chart is not None:
        scope = stable_id("build-scope", sorted(allowed) if allowed is not None else [], str(source.resolve()),
                          *(["edit-visible"] if edit_visible_at_last_edit else []),
                          *(["chart", chart["sha256"]] if chart is not None else []))[:16]
        work = layout.gold_dir.parent / "_build" / scope
        scoped = Layout(work / "bronze", work / "silver", layout.gold_dir,
                        work / "_loss", layout.quarantine_path)
        layout = scoped
    provider = None
    lifecycle_kwargs = {"plan_source": plan_source}
    if real:
        from .market_lake import LakeMarket
        provider = LakeMarket(market_lake if market_lake is not None else root / "lake" / "market")
        lifecycle_kwargs["registry_version"] = provider.registry.version
    out: dict[str, Any] = {}
    out["normalize"] = normalize.run(source, layout, ingested_at=ingested_at, edit_visible_at_last_edit=edit_visible_at_last_edit, **normalize_kwargs)
    out["dedup"] = dedup.run(layout, ingested_at=ingested_at)
    out["extract"] = extract.run(layout, llm_fixture=llm_fixture, ocr_fixture=ocr_fixture, ingested_at=ingested_at, llm=llm,
                                 **({"chart_fixture": chart_fixture} if chart_fixture is not None else {}))
    if provider is None:
        out["validate"] = validate.run(layout, ingested_at=ingested_at, synthetic=True)
    else:
        out["validate"] = validate.run(layout, ingested_at=ingested_at, marks=provider, registry=provider.registry)
    out["linker"] = linker.run(layout, plan_source=plan_source, adjudicator=RecordedClient.from_file(adjudicator_fixture) if adjudicator_fixture else None, ingested_at=ingested_at)
    if alias:
        ih = lifecycle.input_hash_of(layout, ingested_at=ingested_at, **lifecycle_kwargs)
        gv = f"{graph_version}@{ih[:8]}"
        out["lifecycle"] = lifecycle.run(layout, graph_version=gv, ingested_at=ingested_at, **lifecycle_kwargs)
        set_alias(layout, graph_version, gv)
        out["lifecycle"]["alias"] = {graph_version: gv}
    else:
        out["lifecycle"] = lifecycle.run(layout, graph_version=graph_version, ingested_at=ingested_at, **lifecycle_kwargs)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="quant_lab.data API / 构建（默认夹具，显式接入真实行情）")
    ap.add_argument("--build", action="store_true")
    source_group = ap.add_mutually_exclusive_group()
    source_group.add_argument("--fixture")
    source_group.add_argument("--export-dir", help="整号 TDesktop 导出目录；pull 固定从数据根 import/telegram/pull 读取")
    ap.add_argument("--market", choices=["fixture", "real"], default="fixture")
    ap.add_argument("--market-lake", help="显式启用真实行情；--market real 缺省取 DATA_ROOT/lake/market")
    ap.add_argument("--channel", type=int, help="只构建该频道（白名单内的 Telegram peer id）")
    ap.add_argument("--plan-source", choices=["parser", "llm", "reconciled"], default="parser")
    ap.add_argument("--graph-version", default="fixture-v1")
    ap.add_argument("--edit-visible-at-last-edit", action="store_true",
                    help="敏感性口径：编辑过的历史消息按最后编辑时刻可见（默认按导出快照，编辑版不作决策根）")
    providers = ap.add_mutually_exclusive_group()
    providers.add_argument("--llm-fixture")
    providers.add_argument("--llm", choices=["grok"], help="显式使用本机 grok；另需 QUANT_LAB_ALLOW_LLM=1")
    ap.add_argument("--ocr-fixture")
    ap.add_argument("--chart-fixture", help="外部 chart-read-v1 JSON；只补 v2 LLM 单个当下开仓缺失的止损/止盈")
    ap.add_argument("--adjudicator-fixture")
    ap.add_argument("--alias", action="store_true", help="graph-version 作别名：发布不可变版本 <alias>@<hash8> 并移动别名，不删旧版本")
    ap.add_argument("--loss", help="batch_id 或 latest：打印损耗表每层一行")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out")
    g.add_argument("--lake-root")
    a = ap.parse_args(argv)
    if a.lake_root:
        os.environ["QUANT_LAB_DATA_ROOT"] = str(a.lake_root)
    layout = Layout.flat(a.out) if a.out else Layout.from_root(None)
    if a.build:
        if not a.fixture and not a.export_dir and a.market != "real" and a.market_lake is None:
            ap.error("--build 需要 --fixture、--export-dir 或显式真实行情模式")
        try:
            res = build(a.fixture, layout, graph_version=a.graph_version, llm_fixture=a.llm_fixture, ocr_fixture=a.ocr_fixture, adjudicator_fixture=a.adjudicator_fixture, alias=a.alias, llm=a.llm,
                        market_lake=a.market_lake, market=a.market, export_dir=a.export_dir, channel=a.channel, plan_source=a.plan_source,
                        edit_visible_at_last_edit=a.edit_visible_at_last_edit, chart_fixture=a.chart_fixture)
        except (PermissionError, ValueError) as exc:
            ap.error(str(exc))
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ("paths", "inputs", "raw_hashes", "items")} for k, v in res.items()}, ensure_ascii=False, indent=2, default=str))
        return 0
    if a.loss:
        files = sorted((p for p in layout.loss_dir.glob("tg-*.parquet") if "__map_" not in p.name), key=lambda p: p.stat().st_mtime)
        df = pl.read_parquet(files[-1]) if a.loss == "latest" else pl.read_parquet(layout.loss(a.loss))
        for r in df.sort(["layer", "stratum"]).iter_rows(named=True):
            print(f"batch={r['batch_id']} layer={r['layer']} name={r['layer_name']} stratum={r['stratum']} input_n={r['input_n']} output_n={r['output_n']} n_ok={r['n_ok']} n_review={r['n_review']} n_quarantine={r['n_quarantine']} n_dup_ref={r['n_dup_ref']} cum_excluded={r['cum_excluded_ids']} primary={r['primary_reason_dist']}")
        if df.height:
            from .audit import repost_audit_report
            print("repost_audit=" + json.dumps(repost_audit_report(layout, df["batch_id"][0]), sort_keys=True))
        return 0
    ap.print_help()
    return 0


__all__ = ["load_episodes", "load_episode_events", "load_episode_events_description", "load_message_texts", "loss_table", "quarantine", "build"]

if __name__ == "__main__":
    raise SystemExit(main())
