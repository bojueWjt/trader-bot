"""Shared plan selection; evidence approval is set only by llm_extract."""
from __future__ import annotations

import json
import polars as pl

MODES = ("parser", "llm", "reconciled")


def llm_eligible(row: dict) -> bool:
    checks = json.loads(row.get("checks") or "{}")
    return (row["extractor_name"] == "llm" and checks.get("llm_evidence_valid") is True
            and row["kind"] != "undecidable")


def approved_sources(cp: pl.DataFrame, ex: pl.DataFrame | None = None) -> set[str]:
    approved = {r["source_version_id"] for r in cp.iter_rows(named=True) if llm_eligible(r)}
    if ex is not None:
        for row in ex.iter_rows(named=True):
            row["extractor_name"] = row["extractor"]["name"]
            if llm_eligible(row):
                approved.add(row["source_version_id"])
    return approved


def select_plans(cp: pl.DataFrame, plan_source: str = "parser", ex: pl.DataFrame | None = None) -> pl.DataFrame:
    if plan_source not in MODES:
        raise ValueError(f"invalid plan_source: {plan_source}")
    if plan_source == "parser":
        return cp.filter(pl.col("extractor_name") == "parser")
    rows = cp.to_dicts()
    approved = approved_sources(cp, ex)
    ids = [r["plan_id"] for r in rows if llm_eligible(r) or (
        plan_source == "reconciled" and r["extractor_name"] == "parser"
        and r["source_version_id"] not in approved)]
    return cp.filter(pl.col("plan_id").is_in(ids))


def disagreements(cp: pl.DataFrame, ex: pl.DataFrame | None = None) -> dict[str, list[str]]:
    groups: dict[str, dict[str, list[dict]]] = {}
    rows = cp.to_dicts()
    represented = {r["extract_id"] for r in rows}
    if ex is not None:
        for row in ex.iter_rows(named=True):
            if row["extract_id"] not in represented:
                row["extractor_name"] = row["extractor"]["name"]
                rows.append(row)
    for row in rows:
        if row["extractor_name"] == "parser" or llm_eligible(row):
            groups.setdefault(row["source_version_id"], {}).setdefault(row["extractor_name"], []).append(row)
    result = {}
    for source, paths in groups.items():
        if "parser" not in paths or "llm" not in paths:
            continue
        different = []
        for field in ("side", "stop", "entry", "entries"):
            values = [{json.dumps(r.get(field), sort_keys=True, default=str) for r in paths[name]}
                      for name in ("parser", "llm")]
            if values[0] != values[1]:
                different.append(field)
        if different:
            result[source] = different
    return result
