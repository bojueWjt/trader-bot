"""quant_lab.market.quarantine —— data/quarantine/market.parquet 的共享写入（合并稿 C.3 字段组）。

quarantine_id = uuid5(partition, object_id, object_version, rule_version, reason_code, **source_sha256**)：
同源同规则重跑不增记录；新源版本同对象同原因是新记录（S06）。
"""
from __future__ import annotations

import datetime as dt
import uuid

import polars as pl

QUARANTINE_SCHEMA = {
    "quarantine_id": pl.Utf8, "batch_id": pl.Utf8, "object_kind": pl.Utf8, "object_id": pl.Utf8,
    "object_version": pl.Utf8, "partition_id": pl.Utf8, "raw_uri": pl.Utf8, "raw_hash": pl.Utf8,
    "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
    "reason_code": pl.Utf8, "all_reason_codes": pl.List(pl.Utf8), "severity": pl.Utf8, "field_path": pl.Utf8,
    "observed_value_ref": pl.Utf8, "expected_contract": pl.Utf8, "rule_version": pl.Utf8, "schema_hash": pl.Utf8,
    "status": pl.Utf8,
}


def quarantine_id(pid: str, obj_id: str, obj_ver: str, rule_version: str, reason: str, source_sha256: str | None) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{pid}|{obj_id}|{obj_ver}|{rule_version}|{reason}|{source_sha256 or ''}"))


def quarantine_path(lake) -> "pathlib.Path":  # noqa: F821
    return lake.root.parent.parent / "quarantine" / "market.parquet"


def append_quarantine(lake, qs: list[dict]) -> int:
    """按 quarantine_id 去重追加（含本批内部去重）。返回新增条数。"""
    from quant_lab.market.vision import atomic_write_parquet
    if not qs:
        return 0
    new = pl.DataFrame(qs, schema=QUARANTINE_SCHEMA).unique(subset=["quarantine_id"], keep="first", maintain_order=True)
    p = quarantine_path(lake)
    if p.exists():
        old = pl.read_parquet(p)
        new = new.filter(~pl.col("quarantine_id").is_in(old["quarantine_id"].implode()))
        if new.height == 0:
            return 0
        merged = pl.concat([old, new], how="vertical")
    else:
        merged = new
    atomic_write_parquet(p, merged)
    return new.height


def conflict_records(pid: str, rows: pl.DataFrame, *, key: str, source_sha256: str, rule_version: str) -> list[dict]:
    """同键异值冲突：每个候选行一条记录，object_version 绑定该行内容摘要。"""
    import hashlib
    out = []
    for r in rows.to_dicts():
        content = hashlib.sha256(repr(sorted((k, str(v)) for k, v in r.items() if k != "ingested_at")).encode()).hexdigest()[:16]
        oid = f"{r.get('instrument_id')}|{r.get('interval', '')}|{r[key].isoformat()}"
        out.append({
            "quarantine_id": quarantine_id(pid, oid, content, rule_version, "KEY_DUPLICATE_OR_ORDER", source_sha256),
            "batch_id": "", "object_kind": "bar", "object_id": oid, "object_version": content, "partition_id": pid,
            "raw_uri": None, "raw_hash": source_sha256, "event_time": r.get("close_time") or r.get(key), "available_at": r.get("available_at"),
            "ingested_at": r.get("ingested_at"), "reason_code": "KEY_DUPLICATE_OR_ORDER", "all_reason_codes": ["KEY_DUPLICATE_OR_ORDER"],
            "severity": "error", "field_path": key, "observed_value_ref": str({k: str(r[k]) for k in ("open", "high", "low", "close") if k in r}),
            "expected_contract": "同键唯一；冲突候选全部隔离，不 first-wins", "rule_version": rule_version, "schema_hash": "", "status": "open",
        })
    return out
