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
    approved = {r["source_version_id"] for r in cp.iter_rows(named=True) if llm_eligible(r) and json.loads(r.get("checks") or "{}").get("schema_version") != 2}
    if ex is not None:
        for row in ex.iter_rows(named=True):
            row["extractor_name"] = row["extractor"]["name"]
            if llm_eligible(row) and json.loads(row.get("checks") or "{}").get("schema_version") != 2:
                approved.add(row["source_version_id"])
    return approved


def action_overridden(row, llm_rows):
    """Match semantics, not model array position, when both sides name a symbol."""
    for other in llm_rows:
        if row["source_version_id"] != other["source_version_id"]:
            continue
        checks = json.loads(other.get("checks") or "{}")
        if checks.get("schema_version") != 2 or not llm_eligible(other):
            continue
        symbol, other_symbol = row.get("symbol_raw"), other.get("symbol_raw")
        if symbol and other_symbol:
            # Registry aliases are preferred below; unknown symbols use literal spelling.
            left_id, right_id = row.get("instrument_id"), other.get("instrument_id")
            left = symbol.upper().removeprefix("#").removesuffix("USDT")
            right = other_symbol.upper().removeprefix("#").removesuffix("USDT")
            if left_id and right_id:
                left, right = left_id, right_id
            if left != right:
                continue
            if row.get("side") and other.get("side") and row["side"] != other["side"]:
                continue
            return True
        if row.get("branch_index", 0) == other.get("branch_index", 0):
            return True
    return False


def descriptive_only(row):
    checks = json.loads(row.get("checks") or "{}")
    return checks.get("schema_version") == 2 and checks.get("time_ref") != "now"


def select_plans(cp: pl.DataFrame, plan_source: str = "parser", ex: pl.DataFrame | None = None) -> pl.DataFrame:
    if plan_source not in MODES:
        raise ValueError(f"invalid plan_source: {plan_source}")
    if plan_source == "parser":
        return cp.filter(pl.col("extractor_name") == "parser")
    rows = cp.to_dicts()
    approved = approved_sources(cp, ex)
    candidates = list(rows)
    if ex is not None:
        candidates += [dict(r, extractor_name=r["extractor"]["name"]) for r in ex.iter_rows(named=True)]
    by_source = {}
    for candidate in candidates:
        by_source.setdefault(candidate["source_version_id"], []).append(candidate)
    ids = [r["plan_id"] for r in rows if llm_eligible(r) or (
        plan_source == "reconciled" and r["extractor_name"] == "parser"
        and r["source_version_id"] not in approved) and not (r["extractor_name"] == "parser" and action_overridden(r, by_source[r["source_version_id"]]))]
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
