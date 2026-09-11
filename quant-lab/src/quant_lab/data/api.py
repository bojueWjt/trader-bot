"""G1 对外接口（契约 research-schema §7 + §9）与端到端构建 CLI。

- `load_episodes(graph_version, *, decision_graph=True)`：决策视图 = 由 t_dec 前可见边重放出的决策列（P/C、stop/tps、n_events、
  reason_codes、time_grade_min、复制组、派生哈希、eligibility），终态/结果/censor 不出现；只返回有决策根且用途准入
  （eligibility.entry_decision 且无致命原因）的行（S01/S07）。
- `load_episode_events(graph_version)`：决策视图，只含 edge_available_at < 所属 episode 的 t_dec 的边（顺序未知严格 <）。
  描述图事件另用 `load_episode_events_description(graph_version)`（签名不改契约函数）。
- `loss_table(batch_id)`（`latest` 便利入口会在结果里带实际 batch_id）、`quarantine(flow, *, status=None)`。
- 读前校验发布 manifest（存在、published、文件 hash 一致、未 tombstone）；缺/不符/撤销一律拒读（S11）。
- 湖根目录只由 QUANT_LAB_DATA_ROOT 决定（契约 §9.1）。
"""
from __future__ import annotations

import argparse
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


def load_episodes(graph_version: str, *, decision_graph: bool = True) -> pl.DataFrame:
    layout = _layout()
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


def load_episode_events(graph_version: str) -> pl.DataFrame:
    layout = _layout()
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


def load_episode_events_description(graph_version: str) -> pl.DataFrame:
    layout = _layout()
    graph_version = resolve_alias(layout, graph_version)
    verify_manifest(layout, graph_version)
    ev = pl.read_parquet(layout.episode_event(graph_version))
    stale = stale_episodes(layout, graph_version)
    return ev.filter(~pl.col("episode_id").is_in(list(stale))) if stale else ev


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


def build(fixture_dir: str | os.PathLike, layout: Layout, *, graph_version: str, llm_fixture: str | os.PathLike | None = None, ocr_fixture: str | os.PathLike | None = None,
          adjudicator_fixture: str | os.PathLike | None = None, ingested_at: datetime | None = None, alias: bool = False) -> dict[str, Any]:
    """端到端（夹具模式）：归一 → 去重 → 抽取 → 规范化/行情校验（合成桩）→ 链接 → 生命周期 + 发布。
    alias=True：把 graph_version 当别名，实际发布不可变版本 `<alias>@<input_hash[:8]>` 并把别名指过去；旧版本原样保留，不删除（T04）。"""
    from . import dedup, extract, lifecycle, linker, normalize, validate
    from .graph import set_alias
    from .llm import RecordedClient

    out: dict[str, Any] = {}
    out["normalize"] = normalize.run(pathlib.Path(fixture_dir), layout, ingested_at=ingested_at)
    out["dedup"] = dedup.run(layout, ingested_at=ingested_at)
    out["extract"] = extract.run(layout, llm_fixture=llm_fixture, ocr_fixture=ocr_fixture, ingested_at=ingested_at)
    out["validate"] = validate.run(layout, ingested_at=ingested_at, synthetic=True)
    out["linker"] = linker.run(layout, adjudicator=RecordedClient.from_file(adjudicator_fixture) if adjudicator_fixture else None, ingested_at=ingested_at)
    if alias:
        ih = lifecycle.input_hash_of(layout, ingested_at=ingested_at)
        gv = f"{graph_version}@{ih[:8]}"
        out["lifecycle"] = lifecycle.run(layout, graph_version=gv, ingested_at=ingested_at)
        set_alias(layout, graph_version, gv)
        out["lifecycle"]["alias"] = {graph_version: gv}
    else:
        out["lifecycle"] = lifecycle.run(layout, graph_version=graph_version, ingested_at=ingested_at)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="quant_lab.data API / 构建（夹具模式）")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--fixture")
    ap.add_argument("--graph-version", default="fixture-v1")
    ap.add_argument("--llm-fixture")
    ap.add_argument("--ocr-fixture")
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
        if not a.fixture:
            ap.error("--build 需要 --fixture")
        res = build(a.fixture, layout, graph_version=a.graph_version, llm_fixture=a.llm_fixture, ocr_fixture=a.ocr_fixture, adjudicator_fixture=a.adjudicator_fixture, alias=a.alias)
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


__all__ = ["load_episodes", "load_episode_events", "load_episode_events_description", "loss_table", "quarantine", "build"]

if __name__ == "__main__":
    raise SystemExit(main())
