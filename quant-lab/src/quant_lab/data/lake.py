"""研究湖布局、原子写、quarantine 与损耗表（contracts/research-schema.md §2/§5/§6）。

所有层共用：
- `Layout`：路径解析。`Layout.from_root()` 读环境变量 `QUANT_LAB_DATA_ROOT`（默认 quant-lab/data）；
  `Layout.flat(out)` 把整条 telegram 流平铺到一个目录（CLI `--out` 与测试用）。
- `write_parquet_atomic`：临时文件 + os.replace，幂等。
- `append_quarantine`：唯一键 (object_id, object_version, rule_version, reason_code)，同输入同规则重跑不产生重复记录。
- `write_loss`：每层一行（按 stratum 展开），主原因互斥、全部原因另存。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

import polars as pl

from .reasons import primary_reason, severity_of_all

QUARANTINE_KEY = ("object_id", "object_version", "rule_version", "reason_code")

QUARANTINE_SCHEMA: dict[str, pl.DataType] = {
    "quarantine_id": pl.String,
    "batch_id": pl.String,
    "object_kind": pl.String,
    "object_id": pl.String,
    "object_version": pl.String,
    "partition_id": pl.String,
    "raw_uri": pl.String,
    "raw_hash": pl.String,
    "source_refs": pl.String,  # json
    "event_time": pl.Datetime("us", "UTC"),
    "available_at": pl.Datetime("us", "UTC"),
    "ingested_at": pl.Datetime("us", "UTC"),
    "unknown_reason": pl.String,
    "reason_code": pl.String,
    "all_reason_codes": pl.List(pl.String),
    "severity": pl.String,
    "field_path": pl.String,
    "observed_value_ref": pl.String,
    "expected_contract": pl.String,
    "rule_version": pl.String,
    "schema_hash": pl.String,
    "status": pl.String,
    "eligibility_by_estimand": pl.String,  # json
    "reviewer_ids": pl.List(pl.String),
    "decision_reason": pl.String,
    "decision_at": pl.Datetime("us", "UTC"),
    "successor_ref": pl.String,
    "dependency_refs": pl.List(pl.String),
    "affected_manifest_ids": pl.List(pl.String),
    "invalidation_job_id": pl.String,
    "consent_id": pl.String,
    "retention_deadline": pl.Datetime("us", "UTC"),
}

LOSS_SCHEMA: dict[str, pl.DataType] = {
    "batch_id": pl.String,
    "flow": pl.String,
    "layer": pl.Int32,
    "layer_name": pl.String,
    "stratum": pl.String,  # json {"channel_id":..,"year":..}
    "input_unit": pl.String,
    "input_n": pl.Int64,
    "output_unit": pl.String,
    "output_n": pl.Int64,
    "n_ok": pl.Int64,
    "n_review": pl.Int64,
    "n_quarantine": pl.Int64,
    "n_dup_ref": pl.Int64,
    "primary_reason_dist": pl.String,  # json
    "cum_excluded_ids": pl.Int64,
    "cum_excluded_weight": pl.Float64,
    "rule_version": pl.String,
    "schema_hash": pl.String,
    "mapping_hash": pl.String,
    "event_time": pl.Datetime("us", "UTC"),
    "available_at": pl.Datetime("us", "UTC"),
    "ingested_at": pl.Datetime("us", "UTC"),
}

#: 映射账（合并稿 C.3：一入多出 / 多入一出 / 引用 / 排除走映射，不拿 output-input 相减）
MAP_SCHEMA: dict[str, pl.DataType] = {
    "mapping_id": pl.String,
    "batch_id": pl.String,
    "layer": pl.Int32,
    "stratum": pl.String,
    "input_ref": pl.String,
    "output_ref": pl.String,
    "relation": pl.String,  # one_to_one | split | merge | duplicate_ref | excluded
    "status": pl.String,  # 输入对象在本层的互斥状态 ok | review | quarantine | dup_ref
    "reason_codes": pl.List(pl.String),
    "rule_version": pl.String,
    "event_time": pl.Datetime("us", "UTC"),
    "available_at": pl.Datetime("us", "UTC"),
    "ingested_at": pl.Datetime("us", "UTC"),
}

LAYER_NAMES = {1: "normalize", 2: "dedup", 3: "extract", 4: "canonicalize", 5: "market_check", 6: "link"}


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


DECIMAL_SCALE = 12
D12 = pl.Decimal(38, DECIMAL_SCALE)


def q12(v: Any) -> str | None:
    """契约 §9.10.1 A8 第 3 条：数值进哈希前规范化为定标度 12 的十进制字符串（不去尾零、无科学计数法、负号在前）。"""
    if v is None:
        return None
    from decimal import Decimal, ROUND_HALF_EVEN
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return format(d.quantize(Decimal(1).scaleb(-DECIMAL_SCALE), rounding=ROUND_HALF_EVEN), "f")


def _canon(obj: Any) -> Any:
    """哈希输入规范化：float/Decimal → q12 串；递归处理 dict/list/tuple。"""
    from decimal import Decimal
    if isinstance(obj, bool) or obj is None or isinstance(obj, (str, int)):
        return obj
    if isinstance(obj, (float, Decimal)):
        return q12(obj)
    if isinstance(obj, dict):
        return {k: _canon(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    return obj


def stable_id(*parts: Any) -> str:
    """确定性 id：sha256(json.dumps(规范化后的 parts, sort_keys))。数值按 A8 定标度 12 串。"""
    return sha256_text(json.dumps(_canon(parts), sort_keys=True, ensure_ascii=False, default=str))


def schema_hash(schema: dict[str, Any]) -> str:
    return sha256_text(json.dumps({k: str(v) for k, v in schema.items()}, sort_keys=True))


@dataclass
class Layout:
    """telegram 流的全部落盘路径。"""

    bronze_dir: pathlib.Path
    silver_dir: pathlib.Path
    gold_dir: pathlib.Path
    loss_dir: pathlib.Path
    quarantine_path: pathlib.Path
    media_dir: pathlib.Path = field(init=False)

    def __post_init__(self) -> None:
        self.media_dir = self.bronze_dir / "media"

    @classmethod
    def from_root(cls, root: str | os.PathLike | None = None) -> "Layout":
        if root is None:
            root = os.environ.get("QUANT_LAB_DATA_ROOT")
        if root is None:
            root = pathlib.Path(__file__).resolve().parents[3] / "data"
        r = pathlib.Path(root)
        tg = r / "lake" / "telegram"
        return cls(tg / "bronze", tg / "silver", tg / "gold", tg / "_loss", r / "quarantine" / "telegram.parquet")

    @classmethod
    def flat(cls, out: str | os.PathLike) -> "Layout":
        o = pathlib.Path(out)
        return cls(o, o, o, o / "_loss", o / "_quarantine" / "telegram.parquet")

    def ensure(self) -> "Layout":
        for d in (self.bronze_dir, self.silver_dir, self.gold_dir, self.loss_dir, self.media_dir, self.quarantine_path.parent):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # 表文件
    @property
    def message_version(self) -> pathlib.Path:
        return self.bronze_dir / "message_version.parquet"

    @property
    def duplicate_group(self) -> pathlib.Path:
        return self.silver_dir / "duplicate_group.parquet"

    @property
    def extracted_event(self) -> pathlib.Path:
        return self.silver_dir / "extracted_event.parquet"

    @property
    def canonical_plan(self) -> pathlib.Path:
        return self.silver_dir / "canonical_plan.parquet"

    def episode(self, graph_version: str) -> pathlib.Path:
        return self.gold_dir / f"episode__{graph_version}.parquet"

    def episode_event(self, graph_version: str) -> pathlib.Path:
        return self.gold_dir / f"episode_event__{graph_version}.parquet"

    def loss(self, batch_id: str) -> pathlib.Path:
        return self.loss_dir / f"{batch_id}.parquet"

    def mapping(self, batch_id: str, layer: int) -> pathlib.Path:
        return self.loss_dir / f"{batch_id}__map_L{layer}.parquet"


def write_parquet_atomic(df: pl.DataFrame, path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".parquet")
    os.close(fd)
    try:
        df.write_parquet(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def empty_quarantine() -> pl.DataFrame:
    return pl.DataFrame(schema=QUARANTINE_SCHEMA)


def read_quarantine(path: pathlib.Path) -> pl.DataFrame:
    if not path.exists():
        return empty_quarantine()
    return pl.read_parquet(path)


def quarantine_row(
    *,
    batch_id: str,
    object_kind: str,
    object_id: str,
    object_version: str,
    partition_id: str,
    reason_codes: list[str],
    rule_version: str,
    schema_hash_: str,
    raw_uri: str | None = None,
    raw_hash: str | None = None,
    source_refs: dict | list | None = None,
    event_time: datetime | None = None,
    available_at: datetime | None = None,
    ingested_at: datetime | None = None,
    unknown_reason: str | None = None,
    field_path: str | None = None,
    observed_value_ref: str | None = None,
    expected_contract: str | None = None,
    dependency_refs: list[str] | None = None,
    severity_override: str | None = None,
) -> dict[str, Any]:
    """一条隔离记录（主原因 = reason_code；全部原因 all_reason_codes）。severity_override='fatal' 用于契约 §4 '任何方向/品种/归属错' 但无专属码的情形。"""
    reason = primary_reason(reason_codes)
    assert reason is not None
    return {
        "quarantine_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"ql/{object_id}/{object_version}/{rule_version}/{reason}")),
        "batch_id": batch_id,
        "object_kind": object_kind,
        "object_id": object_id,
        "object_version": object_version,
        "partition_id": partition_id,
        "raw_uri": raw_uri,
        "raw_hash": raw_hash,
        "source_refs": json.dumps(source_refs or {}, ensure_ascii=False, default=str),
        "event_time": event_time,
        "available_at": available_at,
        "ingested_at": ingested_at,
        "unknown_reason": unknown_reason,
        "reason_code": reason,
        "all_reason_codes": sorted(set(reason_codes)),
        "severity": "fatal" if severity_override == "fatal" else severity_of_all(list(reason_codes)),
        "field_path": field_path,
        "observed_value_ref": observed_value_ref,
        "expected_contract": expected_contract,
        "rule_version": rule_version,
        "schema_hash": schema_hash_,
        "status": "open",
        "eligibility_by_estimand": "{}",
        "reviewer_ids": [],
        "decision_reason": None,
        "decision_at": None,
        "successor_ref": None,
        "dependency_refs": dependency_refs or [],
        "affected_manifest_ids": [],
        "invalidation_job_id": None,
        "consent_id": None,
        "retention_deadline": None,
    }


def append_quarantine(path: pathlib.Path, rows: list[dict[str, Any]]) -> pl.DataFrame:
    """按唯一键去重后追加；已存在的记录原位不改（旧记录不篡改）。返回追加后的全表。"""
    existing = read_quarantine(path)
    if not rows:
        return existing
    new = pl.DataFrame(rows, schema=QUARANTINE_SCHEMA)
    if existing.height:
        new = new.join(existing.select(QUARANTINE_KEY).unique(), on=list(QUARANTINE_KEY), how="anti")
    new = new.unique(subset=list(QUARANTINE_KEY), keep="first", maintain_order=True)
    out = pl.concat([existing, new], how="vertical") if existing.height else new
    write_parquet_atomic(out, path)
    return out


@dataclass
class LayerLedger:
    """一层一分层的互斥记账：每个输入对象恰好落入 ok / review / quarantine / dup_ref 之一（S12 守恒）。

    - `status[obj] = (state, reasons)`；`outputs` 计输出对象数；`mappings` 记 input→output 关系。
    - 累计排除 `cum_excluded` 由上游传入的集合并上本层排除集合（对象 id 并集，不逐层重数）。
    """

    layer: int
    stratum: dict[str, Any]
    input_unit: str
    output_unit: str
    status: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    outputs: set[str] = field(default_factory=set)
    mappings: list[tuple[str, str | None, str, list[str]]] = field(default_factory=list)

    def mark(self, obj: str, state: str, reasons: list[str] | None = None) -> None:
        assert state in ("ok", "review", "quarantine", "dup_ref"), state
        prev = self.status.get(obj)
        rs = sorted(set((prev[1] if prev else []) + list(reasons or [])))
        order = {"ok": 0, "review": 1, "dup_ref": 2, "quarantine": 3}
        st = state if prev is None or order[state] >= order[prev[0]] else prev[0]
        self.status[obj] = (st, rs)

    def map(self, input_ref: str, output_ref: str | None, relation: str, reasons: list[str] | None = None) -> None:
        self.mappings.append((input_ref, output_ref, relation, list(reasons or [])))
        if output_ref is not None:
            self.outputs.add(output_ref)

    def excluded(self) -> set[str]:
        return {o for o, (st, _) in self.status.items() if st in ("review", "quarantine", "dup_ref")} | {i for i, _, relation, _ in self.mappings if relation == "excluded"}

    def counts(self) -> dict[str, int]:
        c = {"ok": 0, "review": 0, "quarantine": 0, "dup_ref": 0}
        for st, _ in self.status.values():
            c[st] += 1
        return c


def loss_row_from_ledger(led: LayerLedger, *, batch_id: str, cum_excluded_prev: set[str], rule_version: str, schema_hash_: str, clocks: tuple[datetime | None, datetime | None, datetime], flow: str = "telegram") -> tuple[dict[str, Any], set[str]]:
    """返回 (损耗行, 本层累计排除集合)。守恒：input_n == ok+review+quarantine+dup_ref。权重未知 → null（不填零）。"""
    c = led.counts()
    dist: dict[str, int] = {}
    for st, rs in led.status.values():
        if st in ("quarantine", "dup_ref", "review") and rs:
            base = [r.split(":")[0] for r in rs]  # "CODE:basis" 细分（A10）只影响 dist 键，不影响优先级
            pr = primary_reason(base)
            full = next((r for r in rs if r.split(":")[0] == pr and ":" in r), pr)
            dist[full] = dist.get(full, 0) + 1
    cum = set(cum_excluded_prev) | led.excluded()
    mapping_hash = stable_id("map", led.layer, sorted((i, o, r, tuple(rs)) for i, o, r, rs in led.mappings))
    n_in = len(led.status)
    row = {
        "batch_id": batch_id, "flow": flow, "layer": led.layer, "layer_name": LAYER_NAMES[led.layer],
        "stratum": json.dumps(led.stratum, sort_keys=True, ensure_ascii=False),
        "input_unit": led.input_unit, "input_n": n_in, "output_unit": led.output_unit, "output_n": len(led.outputs),
        "n_ok": c["ok"], "n_review": c["review"], "n_quarantine": c["quarantine"], "n_dup_ref": c["dup_ref"],
        "primary_reason_dist": json.dumps(dict(sorted(dist.items())), ensure_ascii=False),
        "cum_excluded_ids": len(cum), "cum_excluded_weight": None,
        "rule_version": rule_version, "schema_hash": schema_hash_, "mapping_hash": mapping_hash,
        "event_time": clocks[0], "available_at": clocks[1], "ingested_at": clocks[2],
    }
    assert row["input_n"] == row["n_ok"] + row["n_review"] + row["n_quarantine"] + row["n_dup_ref"], row
    return row, cum


def mapping_rows(led: LayerLedger, *, batch_id: str, rule_version: str, ingested_at: datetime) -> list[dict[str, Any]]:
    return [
        {"mapping_id": stable_id("mp", batch_id, led.layer, i, o, r)[:32], "batch_id": batch_id, "layer": led.layer, "stratum": json.dumps(led.stratum, sort_keys=True, ensure_ascii=False), "input_ref": i, "output_ref": o, "relation": r,
         "status": led.status.get(i, ("ok", []))[0], "reason_codes": rs, "rule_version": rule_version, "event_time": None, "available_at": None, "ingested_at": ingested_at}
        for i, o, r, rs in led.mappings
    ]


def write_mapping(path: pathlib.Path, rows: list[dict[str, Any]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=MAP_SCHEMA) if rows else pl.DataFrame(schema=MAP_SCHEMA)
    write_parquet_atomic(df, path)
    return df


def read_cum_excluded(path: pathlib.Path, layer: int) -> dict[str, set[str]]:
    """从某层映射账恢复各 stratum 的累计排除集合（excluded/duplicate_ref 的 input_ref 并集），按 stratum json 键。"""
    if not path.exists():
        return {}
    m = pl.read_parquet(path).filter(pl.col("status").is_in(["review", "quarantine", "dup_ref"]) | (pl.col("relation") == "excluded"))
    out: dict[str, set[str]] = {}
    for st, i, o, rel, status in zip(m["stratum"].to_list(), m["input_ref"].to_list(), m["output_ref"].to_list(), m["relation"].to_list(), m["status"].to_list()):
        # 用下一层命名空间表达（隔离对象仍有输出时取 output_ref；重复引用/无输出取 input_ref），跨层并集才不重数
        out.setdefault(st, set()).add(i)
    return out


def cum_prev(layout: "Layout", batch_id: str, layers: Iterable[int], stratum: dict[str, Any]) -> set[str]:
    """前几层同 stratum 的累计排除并集。"""
    key = json.dumps(stratum, sort_keys=True, ensure_ascii=False)
    out: set[str] = set()
    for L in layers:
        out |= read_cum_excluded(layout.mapping(batch_id, L), L).get(key, set())
    return out


def preserve_ingested_at(new: pl.DataFrame, old: pl.DataFrame | None, key: str) -> pl.DataFrame:
    """重跑同对象保留首次入湖时刻（ADR §7：ingested_at 首次落湖，缓存命中不更新）。"""
    if old is None or not old.height or key not in old.columns:
        return new
    first = old.select(key, pl.col("ingested_at").alias("_first_ingested"))
    j = new.join(first, on=key, how="left")
    return j.with_columns(pl.coalesce([pl.col("_first_ingested"), pl.col("ingested_at")]).alias("ingested_at")).drop("_first_ingested")


def write_loss(path: pathlib.Path, rows: list[dict[str, Any]], *, replace_layers: set[int] | None = None) -> pl.DataFrame:
    """写损耗表：同 batch 同层重跑覆盖该层的行（幂等），其他层保留。"""
    new = pl.DataFrame(rows, schema=LOSS_SCHEMA) if rows else pl.DataFrame(schema=LOSS_SCHEMA)
    if path.exists():
        old = pl.read_parquet(path)
        layers = replace_layers or set(new["layer"].to_list())
        old = old.filter(~pl.col("layer").is_in(list(layers))) if layers else old
        new = pl.concat([old, new], how="vertical").sort(["layer", "stratum"])
    write_parquet_atomic(new, path)
    return new
