"""层 1 归一：原始导出 → 不可变 bronze/message_version（三时钟、版本表、相册、媒体清单、quarantine、损耗表）。

规则版本 RULE_VERSION；同输入同规则重放：message_version 追加去重（按 source_version_id），quarantine 按唯一键去重，
损耗表按 (batch, layer) 覆盖；已入湖对象保留首次 ingested_at（缓存命中不更新）。

时间级（合并稿 A.1；review-G1-P1 S02）：
- V  ：有 first_seen_at（实收）。available_at = first_seen_at；event_time = 版本事件时刻（发布或编辑）。
- H0 ：历史导出、未编辑。available_at = message_date + freeze_delay（假设，记 temporal_assumptions）；event_time = message_date。
- H1 ：历史导出、仅见最终编辑版。原文不可见；该最终版**首次可证明完整可见**的时刻 = 导出快照时刻（export_manifest.json.exported_at）；
       无快照证据 → available_at 未知（VERSION_TIME_UNKNOWN）。edit 时刻只作 event_time，绝不当作可知时刻。
- U  ：时间单位无从确定（TIME_UNIT_INVALID），三时钟 event/available 为空。
版本身份（S10）：source_version_id = sha256(source_id, content_hash, media_hashes, version_evidence, evidence_time)，
evidence_time 为该证据的时刻（edit_date→last_edit_at；live_receive→first_seen_at；export_snapshot→exported_at 或 null）。
追加不重编号：新版本 version_no = 该 source 已有最大值 + 1；若新版本的可知时刻早于已有版本 → KEY_DUPLICATE_OR_ORDER 隔离（CR-02，不改旧行）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import unicodedata
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from .lake import (
    LayerLedger,
    Layout,
    append_quarantine,
    loss_row_from_ledger,
    mapping_rows,
    now_utc,
    preserve_ingested_at,
    quarantine_row,
    schema_hash,
    sha256_text,
    stable_id,
    write_loss,
    write_mapping,
    write_parquet_atomic,
)
from .reasons import Reason
from .sources import RawMessage, discover, read_all

RULE_VERSION = "tg1-normalize-v0.5"
DEFAULT_FREEZE_DELAY_S = 60
ALBUM_WINDOW_S = 1
VERSION_EVIDENCE = ("export_snapshot", "edit_date", "live_receive")  # 契约 §9.1 enum（album_infer 单列布尔，不拼串）

MESSAGE_VERSION_SCHEMA: dict[str, Any] = {
    "source_id": pl.Struct({"peer_id": pl.Int64, "message_id": pl.Int64}),
    "channel_id": pl.Int64,
    "channel_name": pl.String,
    "message_type": pl.String,
    "source_version_id": pl.String,
    "version_no": pl.Int32,
    "sequence": pl.Int64,
    "content_hash": pl.String,
    "text": pl.String,
    "text_raw": pl.String,
    "text_entities": pl.String,
    "media_hashes": pl.List(pl.String),
    "media_uris": pl.List(pl.String),
    "media_kinds": pl.List(pl.String),
    "reply_to_message_id": pl.Int64,
    "forward_from": pl.Struct({"name": pl.String, "peer_id": pl.Int64, "message_id": pl.Int64, "date": pl.Datetime("us", "UTC")}),
    "grouped_id": pl.Int64,
    "album_inferred": pl.Boolean,
    "message_date": pl.Datetime("us", "UTC"),
    "last_edit_at": pl.Datetime("us", "UTC"),
    "first_seen_at": pl.Datetime("us", "UTC"),
    "snapshot_at": pl.Datetime("us", "UTC"),
    "asserted_event_time": pl.Datetime("us", "UTC"),
    "time_grade": pl.String,
    "survival_scope": pl.String,
    "deleted_after_observation": pl.Boolean,
    "version_evidence": pl.String,
    "evidence_time": pl.Datetime("us", "UTC"),
    "temporal_assumptions": pl.String,
    "raw_hash": pl.String,
    "raw_uri": pl.String,
    "raw_index": pl.Int64,
    "author_id": pl.String,
    "event_time": pl.Datetime("us", "UTC"),
    "available_at": pl.Datetime("us", "UTC"),
    "ingested_at": pl.Datetime("us", "UTC"),
    "quality_status": pl.String,
    "reason_codes": pl.List(pl.String),
    "batch_id": pl.String,
}
SCHEMA_HASH = schema_hash(MESSAGE_VERSION_SCHEMA)


def normalize_text(t: str) -> str:
    return unicodedata.normalize("NFC", t.replace("\r\n", "\n").replace("\r", "\n"))


def _infer_albums(msgs: list[RawMessage]) -> dict[tuple[int, int], int]:
    """TDesktop 导出无 grouped_id 时按启发式推断相册：同频道、同发送者、时间差 ≤1s、id 连续、都带图片。"""
    out: dict[tuple[int, int], int] = {}
    by_ch: dict[int, list[RawMessage]] = {}
    for m in msgs:
        if m.grouped_id is None and m.date_unixtime is not None and any(r.kind == "photo" for r in m.media):
            by_ch.setdefault(m.channel_id, []).append(m)
    for ch, lst in by_ch.items():
        lst.sort(key=lambda m: (m.date_unixtime, m.message_id))
        group: list[RawMessage] = []

        def flush() -> None:
            if len(group) >= 2:
                gid = min(g.message_id for g in group)
                for g in group:
                    out[(ch, g.message_id)] = gid

        for m in lst:
            if group and m.from_id == group[-1].from_id and m.message_id == group[-1].message_id + 1 and abs(m.date_unixtime - group[-1].date_unixtime) <= ALBUM_WINDOW_S:
                group.append(m)
            else:
                flush()
                group = [m]
        flush()
    return out


def _copy_media(m: RawMessage, media_dir: pathlib.Path) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    hashes, uris, kinds, missing, rejected = [], [], [], [], []
    for ref in m.media:
        if ref.reject_reason and ref.reject_reason != "not_included":
            rejected.append(f"{ref.rel_path}:{ref.reject_reason}")
            continue
        if not ref.exists or not ref.sha256:
            missing.append(ref.rel_path)
            continue
        ext = pathlib.Path(ref.rel_path).suffix.lower() or ".bin"
        name = f"{ref.sha256}{ext}"
        dst = media_dir / name
        if not dst.exists():
            media_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ref.abs_path, dst)
        hashes.append(ref.sha256)
        uris.append(name)
        kinds.append(ref.kind)
    return hashes, uris, kinds, missing, rejected


def normalize_messages(
    msgs: list[RawMessage],
    layout: Layout,
    *,
    freeze_delay_s: int = DEFAULT_FREEZE_DELAY_S,
    ingested_at: datetime | None = None,
) -> tuple[pl.DataFrame, list[dict[str, Any]], dict[tuple[int, str], LayerLedger], str]:
    """核心：RawMessage 列表 → (message_version df, quarantine rows, 分层记账, batch_id)。"""
    ingested_at = ingested_at or now_utc()
    raw_hashes = sorted({m.raw_hash for m in msgs})
    batch_id = "tg-" + stable_id(raw_hashes, RULE_VERSION, freeze_delay_s)[:12]
    delay = timedelta(seconds=freeze_delay_s)
    albums = _infer_albums(msgs)
    # 同一原始文件内同 message_id 且同证据时刻却内容不同 = 键冲突（多版本快照有不同证据时刻，不算冲突）
    seen: dict[tuple[int, int, str], int] = {}
    conflict_keys: set[tuple[int, int, str]] = set()
    _sig: dict[tuple, set[str]] = {}
    for m in msgs:
        et = m.first_seen_at or m.last_edit_at or m.message_date
        k = (m.channel_id, m.message_id, m.raw_hash, et)
        _sig.setdefault(k, set()).add(sha256_text(normalize_text(m.text)))
    for k, hs in _sig.items():
        if len(hs) > 1:
            conflict_keys.add(k[:3])
    for m in msgs:
        seen[(m.channel_id, m.message_id, m.raw_hash)] = 2 if (m.channel_id, m.message_id, m.raw_hash) in conflict_keys else 1

    ordered_exports = set()
    by_export = {}
    for message in msgs:
        if message.first_seen_at is None and message.raw_uri.endswith("result.json"):
            by_export.setdefault((message.channel_id, message.raw_hash), []).append(message)
    for export_key, observations in by_export.items():
        ordered = sorted(observations, key=lambda m: m.raw_index)
        # An exact duplicate observation adds no ordering evidence.
        unique = list(dict.fromkeys((m.raw_index, m.message_id) for m in ordered))
        ids = [mid for _, mid in unique]
        if len(ids) > 1 and all(a < b for a, b in zip(ids, ids[1:])):
            ordered_exports.add(export_key)

    rows: list[dict[str, Any]] = []
    qrows: list[dict[str, Any]] = []
    ledgers: dict[tuple[int, str], LayerLedger] = {}

    def ledger(ch: int, year: str) -> LayerLedger:
        return ledgers.setdefault((ch, year), LayerLedger(1, {"channel_id": ch, "year": year}, "message_observation", "message_version"))

    for m in msgs:
        reasons: list[str] = []
        qextra: dict[str, Any] = {}
        text = normalize_text(m.text)
        content_hash = sha256_text(text)
        hashes, uris, kinds, missing, rejected = _copy_media(m, layout.media_dir)
        if missing or rejected:
            reasons.append(Reason.MEDIA_MISSING)
            qextra = {"field_path": "media", "observed_value_ref": ";".join(missing + rejected), "unknown_reason": "path_rejected" if rejected else None}
        if m.time_unit_problem:
            reasons.append(Reason.TIME_UNIT_INVALID)
            qextra = {"field_path": "date_unixtime", "observed_value_ref": m.time_unit_problem, "unknown_reason": m.time_unit_problem}
        if m.edit_time_problem:
            reasons.append(Reason.VERSION_TIME_UNKNOWN)
            qextra = {"field_path": "edited_unixtime", "observed_value_ref": m.edit_time_problem, "unknown_reason": m.edit_time_problem}
        if m.unknown_keys or m.message_type not in ("message", "service"):
            reasons.append(Reason.SCHEMA_DRIFT)
            qextra = {"field_path": "message", "observed_value_ref": ",".join(m.unknown_keys) or f"type={m.message_type}"}
        if seen[(m.channel_id, m.message_id, m.raw_hash)] > 1:
            reasons.append(Reason.KEY_DUPLICATE_OR_ORDER)
            qextra = {"field_path": "id", "observed_value_ref": f"{m.message_id} x{seen[(m.channel_id, m.message_id, m.raw_hash)]}"}

        sequence = None
        sequence_source = None
        if isinstance(m.sequence_evidence, int) and not isinstance(m.sequence_evidence, bool):
            sequence = m.sequence_evidence
            sequence_source = "observation_sidecar"
        elif (m.channel_id, m.raw_hash) in ordered_exports and m.first_seen_at is None and m.edited_unixtime is None:
            sequence = m.message_id
            sequence_source = "message_id_monotonic_within_export"
        assumptions: dict[str, Any] = {"freeze_delay_s": freeze_delay_s, "sequence_source": sequence_source, "order_policy": "strict_lt_when_equal"}
        edited = m.edited_unixtime is not None or m.edit_time_problem is not None
        if m.first_seen_at is not None:
            grade, evidence, evidence_time = "V", "live_receive", m.first_seen_at
            if edited:
                evidence, evidence_time = "edit_date", m.last_edit_at
            event_time, available_at = (m.last_edit_at or m.message_date), m.first_seen_at
            assumptions["clock"] = "first_seen_at"
        elif m.time_unit_problem:
            grade, evidence, evidence_time = "U", "export_snapshot", m.export_snapshot_at
            event_time = available_at = None
            assumptions["clock"] = "unknown"
        elif edited:
            grade, evidence, evidence_time = "H1", "edit_date", m.last_edit_at
            event_time = m.last_edit_at
            available_at = m.export_snapshot_at  # 首次可证明该最终版完整可见 = 导出快照；无快照证据 → 未知
            assumptions["clock"] = "export_snapshot_at" if m.export_snapshot_at else "unknown"
            assumptions["edit_original_unavailable"] = True
            assumptions["original_message_date"] = m.message_date.isoformat() if m.message_date else None
            if m.export_snapshot_at is None:
                reasons.append(Reason.VERSION_TIME_UNKNOWN)
                qextra = qextra or {"field_path": "available_at", "observed_value_ref": "H1_without_export_snapshot", "unknown_reason": "no_export_manifest"}
        else:
            grade, evidence, evidence_time = "H0", "export_snapshot", m.export_snapshot_at
            event_time = m.message_date
            available_at = m.message_date + delay
            assumptions["clock"] = "message_date+freeze_delay"
            assumptions["unedited_assumed_from_export"] = True
        if m.quote_only:
            grade = "H2"
            assumptions["clock"] = "quote_observed_original_unavailable"
            assumptions["original_visible"] = False
        gid = m.grouped_id
        inferred = False
        if gid is None and (m.channel_id, m.message_id) in albums:
            gid = albums[(m.channel_id, m.message_id)]
            inferred = True
            assumptions["album_inferred"] = True
        svid = stable_id(m.channel_id, m.message_id, content_hash, hashes, evidence, evidence_time)
        fwd = None
        if m.forwarded_from or m.forwarded_from_id:
            fwd = {"name": m.forwarded_from, "peer_id": m.forwarded_from_id, "message_id": m.forwarded_from_message_id,
                   "date": datetime.fromtimestamp(m.forwarded_date_unixtime, tz=ingested_at.tzinfo) if m.forwarded_date_unixtime else None}
        survival = "unknown"
        if grade == "V" and m.cohort_id:
            survival = "watched_cohort"
            assumptions["cohort_id"] = m.cohort_id
        elif grade in ("H0", "H1"):
            survival = "historical_survivor"
        rows.append({
            "source_id": {"peer_id": m.channel_id, "message_id": m.message_id}, "channel_id": m.channel_id, "channel_name": m.channel_name, "message_type": m.message_type,
            "source_version_id": svid, "version_no": 0, "sequence": sequence, "content_hash": content_hash, "text": text, "text_raw": m.text,
            "text_entities": json.dumps(m.text_entities, ensure_ascii=False), "media_hashes": hashes, "media_uris": uris, "media_kinds": kinds,
            "reply_to_message_id": m.reply_to_message_id, "forward_from": fwd, "grouped_id": gid, "album_inferred": inferred,
            "message_date": m.message_date, "last_edit_at": m.last_edit_at, "first_seen_at": m.first_seen_at, "snapshot_at": m.snapshot_at or m.export_snapshot_at,
            "asserted_event_time": None, "time_grade": grade, "survival_scope": survival, "deleted_after_observation": None,
            "version_evidence": evidence, "evidence_time": evidence_time, "temporal_assumptions": json.dumps(assumptions, ensure_ascii=False, sort_keys=True),
            "raw_hash": m.raw_hash, "raw_uri": m.raw_uri, "raw_index": m.raw_index, "author_id": m.from_id,
            "event_time": event_time, "available_at": available_at, "ingested_at": ingested_at,
            "quality_status": "quarantined" if reasons else "ok", "reason_codes": sorted(set(reasons)), "batch_id": batch_id,
        })
        if reasons:
            qrows.append(quarantine_row(
                batch_id=batch_id, object_kind="message_version", object_id=f"{m.channel_id}:{m.message_id}", object_version=svid, partition_id=f"channel={m.channel_id}",
                reason_codes=reasons, rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH, raw_uri=m.raw_uri, raw_hash=m.raw_hash,
                source_refs={"peer_id": m.channel_id, "message_id": m.message_id, "raw_index": m.raw_index}, event_time=event_time, available_at=available_at,
                ingested_at=ingested_at, expected_contract="research-schema §2 bronze/message_version", **qextra,
            ))

    df = pl.DataFrame(rows, schema=MESSAGE_VERSION_SCHEMA) if rows else pl.DataFrame(schema=MESSAGE_VERSION_SCHEMA)
    dropped_by_svid: dict[str, int] = {}
    if df.height:
        df = df.sort(["available_at", "raw_index"], nulls_last=True)
        dup_obs = df.filter(pl.col("source_version_id").is_duplicated()).group_by("source_version_id").len().with_columns((pl.col("len") - 1).alias("_dup"))
        dropped_by_svid = dict(zip(dup_obs["source_version_id"].to_list(), dup_obs["_dup"].to_list())) if dup_obs.height else {}
        df = df.unique(subset=["source_version_id"], keep="first", maintain_order=True)
        df = df.with_columns(pl.struct(["available_at", "event_time", "source_version_id"]).rank("ordinal").over(["channel_id", "source_id"]).cast(pl.Int32).alias("version_no"))
    # 记账（互斥：quarantine > dup_ref > ok）
    for r in df.iter_rows(named=True):
        year = str(r["message_date"].year) if r["message_date"] else "unknown"
        led = ledger(r["channel_id"], year)
        obj = f"{r['channel_id']}:{r['source_id']['message_id']}:{r['raw_hash'][:8]}:{r['raw_index']}:{r['source_version_id']}"
        led.mark(obj, "quarantine" if r["quality_status"] == "quarantined" else "ok", r["reason_codes"])
        led.map(obj, r["source_version_id"], "one_to_one", r["reason_codes"])
        for k in range(dropped_by_svid.get(r["source_version_id"], 0)):
            dobj = f"{obj}:dup{k}"
            led.mark(dobj, "dup_ref", [Reason.DUPLICATE_EXACT])
            led.map(dobj, r["source_version_id"], "duplicate_ref", [Reason.DUPLICATE_EXACT])
    return df, qrows, ledgers, batch_id


def run(fixture_dir: pathlib.Path, layout: Layout, *, freeze_delay_s: int = DEFAULT_FREEZE_DELAY_S, ingested_at: datetime | None = None) -> dict[str, Any]:
    """读导出 → 归一 → 落盘（追加去重、不重编号、保留首次 ingested_at）→ 损耗/映射 → manifest。"""
    layout.ensure()
    ingested_at = ingested_at or now_utc()
    msgs = list(read_all(fixture_dir))
    df, qrows, ledgers, batch_id = normalize_messages(msgs, layout, freeze_delay_s=freeze_delay_s, ingested_at=ingested_at)
    added = df.height
    if layout.message_version.exists():
        old = pl.read_parquet(layout.message_version)
        new = df.join(old.select("source_version_id"), on="source_version_id", how="anti")
        added = new.height
        if new.height:
            # 不重编号：新版本序号 = 已有最大 + 顺位；若新版本可知时刻早于已有最大可知时刻 → KEY_DUPLICATE_OR_ORDER（CR-02）
            mx = old.group_by(["channel_id", "source_id"]).agg(pl.col("version_no").max().alias("_max"), pl.col("available_at").max().alias("_max_avail"))
            new = new.join(mx, on=["channel_id", "source_id"], how="left").with_columns(pl.col("_max").fill_null(0))
            new = new.sort(["available_at", "raw_index"], nulls_last=True).with_columns(
                (pl.col("_max") + pl.struct(["available_at", "event_time", "source_version_id"]).rank("ordinal").over(["channel_id", "source_id"]).cast(pl.Int32)).alias("version_no"))
            late = new.filter(pl.col("_max_avail").is_not_null() & pl.col("available_at").is_not_null() & (pl.col("available_at") < pl.col("_max_avail")))
            for r in late.iter_rows(named=True):
                qrows.append(quarantine_row(batch_id=batch_id, object_kind="message_version", object_id=f"{r['channel_id']}:{r['source_id']['message_id']}", object_version=r["source_version_id"],
                                            partition_id=f"channel={r['channel_id']}", reason_codes=[Reason.KEY_DUPLICATE_OR_ORDER], rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH,
                                            raw_uri=r["raw_uri"], raw_hash=r["raw_hash"], event_time=r["event_time"], available_at=r["available_at"], ingested_at=ingested_at,
                                            field_path="version_no", observed_value_ref="late_version_earlier_than_existing", expected_contract="ADR-G1 CR-02 不重编号"))
            new = new.drop(["_max", "_max_avail"])
            merged = pl.concat([old, new.select(old.columns)], how="vertical")
        else:
            merged = old
        write_parquet_atomic(merged, layout.message_version)
    else:
        write_parquet_atomic(df, layout.message_version)
    q = append_quarantine(layout.quarantine_path, qrows)
    lrows, maps = [], []
    for key in sorted(ledgers, key=lambda k: (k[0], k[1])):
        led = ledgers[key]
        row, _ = loss_row_from_ledger(led, batch_id=batch_id, cum_excluded_prev=set(), rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH,
                                      clocks=(df.filter(pl.col("channel_id") == key[0])["event_time"].max() if df.height else None, df["available_at"].max() if df.height else None, ingested_at))
        lrows.append(row)
        maps += mapping_rows(led, batch_id=batch_id, rule_version=RULE_VERSION, ingested_at=ingested_at)
    write_loss(layout.loss(batch_id), lrows, replace_layers={1})
    write_mapping(layout.mapping(batch_id, 1), maps)
    manifest = {
        "batch_id": batch_id, "rule_version": RULE_VERSION, "schema_hash": SCHEMA_HASH, "freeze_delay_s": freeze_delay_s,
        "inputs": [{"kind": k, "path": str(p.relative_to(fixture_dir))} for k, p in discover(fixture_dir)], "raw_hashes": sorted({m.raw_hash for m in msgs}),
        "n_messages": len(msgs), "n_versions": df.height, "n_versions_added": added, "n_quarantine_rows": len(qrows),
        "time_grade_dist": {k: v for k, v in sorted(df.group_by("time_grade").len().iter_rows())} if df.height else {}, "ingested_at": ingested_at.isoformat(),
    }
    mdir = layout.bronze_dir / "_manifest"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{batch_id}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest["quarantine_total"] = q.height
    manifest["paths"] = {"message_version": str(layout.message_version), "quarantine": str(layout.quarantine_path), "loss": str(layout.loss(batch_id))}
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="层 1 归一：导出目录 → bronze/message_version")
    ap.add_argument("--fixture", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out")
    g.add_argument("--lake-root")
    ap.add_argument("--freeze-delay", type=int, default=DEFAULT_FREEZE_DELAY_S)
    a = ap.parse_args(argv)
    layout = Layout.flat(a.out) if a.out else Layout.from_root(a.lake_root)
    print(json.dumps(run(pathlib.Path(a.fixture), layout, freeze_delay_s=a.freeze_delay), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
