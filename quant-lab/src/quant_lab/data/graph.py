"""决策图闭包、快照哈希、发布 manifest / tombstone / 迁移（ADR-G1 §6 / §8；review-G1-P1 S01/S03/S10/S11）。

- `closure_available_at(deps)`：A* = max(available_at)；任一依赖未知 → None（拒相关决策）。
- `t_dec_of(A*, delay_s)`：冻结处理延迟必须为正。
- `decision_visible(edge_available_at, t_dec, order_known=False)`：顺序未知取严格 <。
- `snapshot_hash(...)`：决策快照哈希，覆盖全部决策列，不含 ingested_at。
- 发布 manifest：gold/_manifest/<graph_version>.json 记录 EP/EE 文件 sha256、输入 hash、规则/假设；`verify_manifest` 校验文件 hash；
  缺 manifest / hash 不符 / tombstone → 研究 API 拒读。同名版本不同输入 → 拒写（无 force）。
- tombstone / migration 表追加式；旧 Parquet 不改。
"""
from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime, timedelta
from typing import Any, Iterable

import polars as pl

from .lake import Layout, now_utc, sha256_file, stable_id, write_parquet_atomic

DEFAULT_PROCESSING_DELAY_S = 1  # 冻结正值（看板 03:20 note 与 ADR §6.2）

TOMBSTONE_SCHEMA = {
    "object_ref": pl.String, "object_version": pl.String, "migration_id": pl.String, "reason_code": pl.String, "successor_refs": pl.List(pl.String),
    "approved_by": pl.String, "revoked_at": pl.Datetime("us", "UTC"), "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}
MIGRATION_SCHEMA = {
    "migration_id": pl.String, "old_graph_version": pl.String, "new_graph_version": pl.String, "predecessor_ids": pl.List(pl.String), "successor_ids": pl.List(pl.String),
    "migration_reason": pl.String, "approved_by": pl.String, "effective_at": pl.Datetime("us", "UTC"), "event_time": pl.Datetime("us", "UTC"), "available_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}


def closure_available_at(deps: Iterable[datetime | None]) -> datetime | None:
    deps = list(deps)
    if not deps or any(d is None for d in deps):
        return None
    return max(deps)


def dependency_closure(root: str, nodes: dict[str, dict[str, Any]]) -> tuple[datetime | None, list[str]]:
    """White/grey/black DFS. Missing nodes/clocks and cycles invalidate the entire closure."""
    colour = {}
    visited = []
    times = []
    valid = True

    def visit(ref):
        nonlocal valid
        state = colour.get(ref, "white")
        if state == "grey":
            valid = False
            return
        if state == "black":
            return
        colour[ref] = "grey"
        node = nodes.get(ref)
        visited.append(ref)
        if node is None:
            valid = False
        else:
            clock = node.get("available_at")
            if not isinstance(clock, datetime):
                valid = False
            else:
                times.append(clock)
            for child in node.get("dependencies", []):
                visit(child)
        colour[ref] = "black"

    visit(root)
    if not valid or not times:
        return None, sorted(set(visited))
    return max(times), sorted(set(visited))


def plan_dependencies(root: dict, versions: dict[str, dict], rule_versions: dict[str, str], *, purpose: str = "entry_decision") -> tuple[datetime | None, list[str]]:
    """Structural source/album/reply, field, rule and purpose-specific validation dependencies."""
    from datetime import UTC
    source = root["source_version_id"]
    nodes = {}
    by_message = {}
    for ref, row in versions.items():
        identity = row.get("source_id") or {}
        by_message.setdefault((row.get("channel_id"), identity.get("message_id")), []).append(ref)
        deps = []
        for media in row.get("media_hashes") or []:
            media_ref = f"media:{ref}:{media}"
            nodes[media_ref] = {"available_at": row.get("available_at"), "dependencies": []}
            deps.append(media_ref)
        nodes[ref] = {"available_at": row.get("available_at"), "dependencies": deps}
    # Attach necessary album media to every source, without introducing album cycles.
    for ref, row in versions.items():
        group = row.get("grouped_id")
        if group is None:
            continue
        for member_ref, member in versions.items():
            if member.get("channel_id") != row.get("channel_id") or member.get("grouped_id") != group:
                continue
            for media in member.get("media_hashes") or []:
                nodes[ref]["dependencies"].append(f"media:{member_ref}:{media}")
    # Reply evidence is recursively traversed.
    for ref, row in versions.items():
        parent = row.get("reply_to_message_id")
        if parent is None:
            continue
        candidates = by_message.get((row.get("channel_id"), parent), [])
        if not candidates:
            nodes[ref]["dependencies"].append(f"missing-parent:{row.get('channel_id')}:{parent}")
            continue
        known = [r for r in candidates if versions[r].get("available_at") is not None and row.get("available_at") is not None and versions[r]["available_at"] <= row["available_at"]]
        if known:
            candidates = [max(known, key=lambda r: versions[r]["available_at"])]
        nodes[ref]["dependencies"].extend(candidates)
    dependencies = [source]
    row = versions.get(source, {})
    group = row.get("grouped_id")
    if group is not None:
        dependencies.extend(ref for ref, member in versions.items() if member.get("channel_id") == row.get("channel_id") and member.get("grouped_id") == group)
    # The named historical processing model is a frozen assumption, not a claimed historical runtime.
    for name, version in sorted(rule_versions.items()):
        ref = f"rule:{name}:{version}"
        nodes[ref] = {"available_at": datetime(1970, 1, 1, tzinfo=UTC) if version else None, "dependencies": []}
        dependencies.append(ref)
    for name in ("entry", "stop", "tps", "size_hint"):
        if root.get(name) is None:
            continue
        ref = f"field:{root['extract_id']}:{name}"
        nodes[ref] = {"available_at": root.get("available_at"), "dependencies": [source]}
        dependencies.append(ref)
    checks = json.loads(root.get("checks") or "{}")
    for dep in checks.get("dependencies", []):
        if dep.get("purpose", "entry_decision") not in ("all", purpose):
            continue
        ref = dep["ref"]
        clock = dep.get("available_at")
        if isinstance(clock, str):
            clock = datetime.fromisoformat(clock)
        nodes[ref] = {"available_at": clock, "dependencies": dep.get("dependencies", [])}
        dependencies.append(ref)
    extract_ref = root["extract_id"]
    nodes[extract_ref] = {"available_at": root.get("available_at"), "dependencies": dependencies}
    nodes[root["plan_id"]] = {"available_at": root.get("available_at"), "dependencies": [extract_ref]}
    return dependency_closure(root["plan_id"], nodes)


def t_dec_of(a_star: datetime | None, delay_s: int = DEFAULT_PROCESSING_DELAY_S) -> datetime | None:
    if a_star is None:
        return None
    if delay_s <= 0:
        raise ValueError("processing_delay_s 必须为冻结正值（0 会让同刻依赖在严格 < 下全部不可用，ADR §6.2）")
    return a_star + timedelta(seconds=delay_s)


def decision_visible(edge_available_at: datetime | None, t_dec: datetime | None, *, order_known: bool = False) -> bool:
    if edge_available_at is None or t_dec is None:
        return False
    return edge_available_at <= t_dec if order_known else edge_available_at < t_dec


def snapshot_hash(*, graph_version: str, episode_id: str, root_plan_id: str, t_dec: datetime | None, dependency_ids: list[str], fields: dict[str, Any], rule_versions: dict[str, str]) -> str:
    return stable_id("snapshot", graph_version, episode_id, root_plan_id, t_dec, sorted(dependency_ids), fields, rule_versions)


# ---------------------------------------------------------------- manifest 发布/校验
def manifest_path(layout: Layout, graph_version: str) -> pathlib.Path:
    return layout.gold_dir / "_manifest" / f"{graph_version}.json"


REQUIRED_MANIFEST_KEYS = ("graph_version", "input_hash", "rule_versions", "assumptions", "files", "counts", "graph_kind", "status", "built_at")


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def manifest_value_errors(doc: dict[str, Any]) -> list[str]:
    """T01 值级校验：input_hash 64 hex；rule_versions 非空 str→str；assumptions dict；files 非空 name→64hex；counts 非空 str→非负 int；graph_kind/status 固定枚举；built_at ISO 时间。"""
    errs: list[str] = []
    if not isinstance(doc.get("graph_version"), str) or not doc["graph_version"]:
        errs.append("graph_version")
    if not isinstance(doc.get("input_hash"), str) or not _HEX64.match(doc["input_hash"]):
        errs.append("input_hash")
    rv = doc.get("rule_versions")
    if not isinstance(rv, dict) or not rv or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in rv.items()):
        errs.append("rule_versions")
    if not isinstance(doc.get("assumptions"), dict):
        errs.append("assumptions")
    files = doc.get("files")
    if not isinstance(files, dict) or not files or not all(isinstance(k, str) and isinstance(v, str) and _HEX64.match(v) for k, v in files.items()):
        errs.append("files")
    counts = doc.get("counts")
    if not isinstance(counts, dict) or not counts or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts.values()):
        errs.append("counts")
    if doc.get("graph_kind") != "description+decision_snapshot":
        errs.append("graph_kind")
    if doc.get("status") not in ("published", "tombstoned", "staging"):
        errs.append("status")
    try:
        datetime.fromisoformat(str(doc.get("built_at")))
    except (TypeError, ValueError):
        errs.append("built_at")
    return errs


def required_files(layout: Layout, graph_version: str) -> dict[str, pathlib.Path]:
    return {layout.episode(graph_version).name: layout.episode(graph_version), layout.episode_event(graph_version).name: layout.episode_event(graph_version)}


def publish_manifest(layout: Layout, graph_version: str, *, input_hash: str, rule_versions: dict[str, str], assumptions: dict[str, Any], counts: dict[str, int], built_at: datetime) -> dict[str, Any]:
    """单一发布指针：EP/EE 两份文件缺一不发布；写入文件 hash 清单（T01）。"""
    mp = manifest_path(layout, graph_version)
    req = required_files(layout, graph_version)
    missing = [n for n, p in req.items() if not p.exists()]
    if missing:
        raise RuntimeError(f"发布拒绝：缺文件 {missing}")
    files = {n: sha256_file(p) for n, p in req.items()}
    prev = read_manifest(layout, graph_version)
    if prev and prev.get("input_hash") == input_hash and prev.get("built_at"):
        built_at_iso = prev["built_at"]  # 同输入幂等重跑：发布元数据不原地变更（A9 第 2 条）
    else:
        built_at_iso = built_at.isoformat()
    doc = {"graph_version": graph_version, "input_hash": input_hash, "rule_versions": rule_versions, "assumptions": assumptions, "files": files, "counts": counts,
           "graph_kind": "description+decision_snapshot", "status": "published", "built_at": built_at_iso}
    mp.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(mp)
    update_index(layout, {"graph_version": graph_version, "status": "published", "built_at": doc["built_at"], "input_hash": input_hash})
    return doc


def index_path(layout: Layout) -> pathlib.Path:
    return layout.gold_dir / "_manifest" / "index.json"


def read_index(layout: Layout) -> list[dict[str, Any]]:
    ip = index_path(layout)
    return json.loads(ip.read_text()) if ip.exists() else []


def update_index(layout: Layout, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """契约 §9.10.2 A9：gold/_manifest/index.json —— [{graph_version, status, built_at, tombstoned_at?, successor_graph_version?, input_hash}]，按 built_at 升序，随发布原子更新。"""
    idx = [e for e in read_index(layout) if e.get("graph_version") != entry["graph_version"]]
    old = next((e for e in read_index(layout) if e.get("graph_version") == entry["graph_version"]), {})
    idx.append({**old, **entry})
    idx.sort(key=lambda e: e.get("built_at") or "")
    ip = index_path(layout)
    ip.parent.mkdir(parents=True, exist_ok=True)
    tmp = ip.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, indent=2, ensure_ascii=False))
    tmp.replace(ip)
    return idx


def rebuild_index(layout: Layout) -> list[dict[str, Any]]:
    """从 _manifest/*.json 回填版本索引（用于索引上线前已发布的版本；幂等）。"""
    mdir = layout.gold_dir / "_manifest"
    for mp in sorted(mdir.glob("*.json")):
        if mp.name in ("index.json", "_alias.json"):
            continue
        doc = json.loads(mp.read_text())
        if not isinstance(doc, dict) or "graph_version" not in doc:
            continue
        entry = {"graph_version": doc["graph_version"], "status": doc.get("status", "unknown"), "built_at": doc.get("built_at"), "input_hash": doc.get("input_hash")}
        if doc.get("tombstone_migration_id"):
            entry["tombstoned_at"] = entry.get("tombstoned_at")
        update_index(layout, entry)
    return read_index(layout)


def consumable_versions(layout: Layout) -> list[str]:
    return [e["graph_version"] for e in read_index(layout) if e.get("status") == "published"]


def read_manifest(layout: Layout, graph_version: str) -> dict[str, Any] | None:
    mp = manifest_path(layout, graph_version)
    return json.loads(mp.read_text()) if mp.exists() else None


def alias_path(layout: Layout) -> pathlib.Path:
    return layout.gold_dir / "_manifest" / "_alias.json"


def resolve_alias(layout: Layout, name: str) -> str:
    """别名（如 fixture-v1）→ 当前指向的不可变 graph_version；无别名则原样返回。"""
    ap = alias_path(layout)
    if ap.exists():
        return json.loads(ap.read_text()).get(name, name)
    return name


def set_alias(layout: Layout, name: str, graph_version: str) -> None:
    ap = alias_path(layout)
    doc = json.loads(ap.read_text()) if ap.exists() else {}
    doc[name] = graph_version
    ap.parent.mkdir(parents=True, exist_ok=True)
    tmp = ap.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(ap)


def verify_manifest(layout: Layout, graph_version: str) -> dict[str, Any]:
    """研究 API 读前校验（T01 加严）：manifest 存在且必备字段齐、graph_version 一致、status=published、EP/EE 两份文件都在清单且 hash 一致、未 tombstone。失败抛 LookupError。"""
    doc = read_manifest(layout, graph_version)
    if doc is None:
        raise LookupError(f"graph_version={graph_version} 无发布 manifest：未提交/未发布的图不可消费")
    if not isinstance(doc, dict) or any(k not in doc for k in REQUIRED_MANIFEST_KEYS):
        raise LookupError(f"graph_version={graph_version} manifest 缺必备字段：视为未发布")
    errs = manifest_value_errors(doc)
    if errs:
        raise LookupError(f"graph_version={graph_version} manifest 字段值非法：{errs}")
    if doc.get("graph_version") != graph_version:
        raise LookupError(f"graph_version={graph_version} manifest 内 graph_version={doc.get('graph_version')} 不一致，拒读")
    if doc.get("status") != "published":
        raise LookupError(f"graph_version={graph_version} 状态 {doc.get('status')}，不可消费")
    if is_tombstoned(layout, graph_version):
        raise LookupError(f"graph_version={graph_version} 已 tombstone：研究 API 拒读（审计重放请按旧 manifest 走审计入口）")
    files = doc.get("files") or {}
    req = required_files(layout, graph_version)
    if set(files) != set(req):
        raise LookupError(f"graph_version={graph_version} manifest 文件清单 {sorted(files)} ≠ 必需 {sorted(req)}，拒读")
    for name, h in files.items():
        p = req[name]
        if not p.exists() or sha256_file(p) != h:
            raise LookupError(f"graph_version={graph_version} 文件 {name} 与 manifest hash 不符或缺失：混版本/损坏，拒读")
    try:
        episodes = pl.read_parquet(layout.episode(graph_version))
        events = pl.read_parquet(layout.episode_event(graph_version))
        actual = {"episodes": episodes.height, "events": events.height,
                  "decision_roots": int(episodes["t_dec"].is_not_null().sum())}
    except Exception as error:
        raise LookupError("manifest parquet contents unreadable") from error
    if doc["counts"] != actual:
        raise LookupError(f"manifest counts mismatch: {doc['counts']} != {actual}")
    return doc


def _tomb_path(layout: Layout) -> pathlib.Path:
    return layout.gold_dir / "_tombstones.parquet"


def _mig_path(layout: Layout) -> pathlib.Path:
    return layout.gold_dir / "_migrations.parquet"


def read_tombstones(layout: Layout) -> pl.DataFrame:
    p = _tomb_path(layout)
    return pl.read_parquet(p) if p.exists() else pl.DataFrame(schema=TOMBSTONE_SCHEMA)


def read_migrations(layout: Layout) -> pl.DataFrame:
    p = _mig_path(layout)
    return pl.read_parquet(p) if p.exists() else pl.DataFrame(schema=MIGRATION_SCHEMA)


def is_tombstoned(layout: Layout, graph_version: str) -> bool:
    t = read_tombstones(layout)
    return bool(t.height and t.filter(pl.col("object_ref") == f"graph_version:{graph_version}").height)


def tombstone_graph(layout: Layout, graph_version: str, *, reason_code: str, approved_by: str, successor_graph_version: str | None = None, migration_reason: str = "", at: datetime | None = None) -> str:
    at = at or now_utc()
    mig_id = stable_id("mig", graph_version, successor_graph_version, reason_code, migration_reason)
    t = read_tombstones(layout)
    row = pl.DataFrame([{"object_ref": f"graph_version:{graph_version}", "object_version": graph_version, "migration_id": mig_id, "reason_code": reason_code,
                         "successor_refs": [f"graph_version:{successor_graph_version}"] if successor_graph_version else [], "approved_by": approved_by, "revoked_at": at,
                         "event_time": at, "available_at": at, "ingested_at": at}], schema=TOMBSTONE_SCHEMA)
    if not t.filter(pl.col("migration_id") == mig_id).height:
        write_parquet_atomic(pl.concat([t, row]) if t.height else row, _tomb_path(layout))
    record_migration(layout, old_graph_version=graph_version, new_graph_version=successor_graph_version, predecessor_ids=[], successor_ids=[], reason=migration_reason or reason_code, approved_by=approved_by, at=at, migration_id=mig_id)
    mp = manifest_path(layout, graph_version)
    built_at = None
    if mp.exists():  # 发布指针标 tombstoned（文件保留，审计可读）
        doc = json.loads(mp.read_text())
        doc["status"] = "tombstoned"
        doc["tombstone_migration_id"] = mig_id
        built_at = doc.get("built_at")
        mp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    update_index(layout, {"graph_version": graph_version, "status": "tombstoned", "built_at": built_at, "tombstoned_at": at.isoformat(), "successor_graph_version": successor_graph_version})
    return mig_id


def record_migration(layout: Layout, *, old_graph_version: str, new_graph_version: str | None, predecessor_ids: list[str], successor_ids: list[str], reason: str, approved_by: str, at: datetime | None = None, migration_id: str | None = None) -> str:
    at = at or now_utc()
    mig_id = migration_id or stable_id("mig", old_graph_version, new_graph_version, sorted(predecessor_ids), sorted(successor_ids), reason)
    m = read_migrations(layout)
    row = pl.DataFrame([{"migration_id": mig_id, "old_graph_version": old_graph_version, "new_graph_version": new_graph_version, "predecessor_ids": predecessor_ids, "successor_ids": successor_ids,
                         "migration_reason": reason, "approved_by": approved_by, "effective_at": at, "event_time": at, "available_at": at, "ingested_at": at}], schema=MIGRATION_SCHEMA)
    if not m.filter(pl.col("migration_id") == mig_id).height:
        write_parquet_atomic(pl.concat([m, row]) if m.height else row, _mig_path(layout))
    return mig_id


def successors_of(layout: Layout, episode_id: str) -> list[str]:
    m = read_migrations(layout)
    if not m.height:
        return []
    out: list[str] = []
    for r in m.filter(pl.col("predecessor_ids").list.contains(episode_id)).iter_rows(named=True):
        out.extend(r["successor_ids"])
    return sorted(set(out))


def stale_episodes(layout: Layout, graph_version: str) -> set[str]:
    """被迁移标记为前驱的 episode（旧图中已失效，消费方拒命中）。"""
    m = read_migrations(layout)
    if not m.height:
        return set()
    out: set[str] = set()
    for r in m.filter(pl.col("old_graph_version") == graph_version).iter_rows(named=True):
        if r.get("migration_reason") == "reopen":
            continue
        out |= set(r["predecessor_ids"])
    return out
