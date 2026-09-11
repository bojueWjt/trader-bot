"""层 2 去重与跨频道复制组（合并稿 C.2 Telegram 2；ADR-G1 §2.3 / §4.1；review-G1-P1 S05 / A03）。

规则（RULE_VERSION）：
- **精确重复只定义在同一 source_id 的相同观察上**（层 1 已按 source_version_id 折叠并计 dup_ref）。不同 message_id 的同文，即使同频道 72h 内，
  也只建复制候选组（dup_kind=repost_same_channel，弱），不折叠、不减种子：重发身份需要证据（人工/裁决）确认后才可合并。
- 跨频道相同内容 → 复制候选组：有可解析转发指针（forward_from.peer_id+message_id 命中湖内版本）→ dup_kind=forward，方向已知；
  无指针 → exact_cross_channel + COPY_LINK_AMBIGUOUS。
- 近重复 = simhash 预筛 + 数字 token 集合完全相同 + ratio ≥ NEAR_RATIO（无数字文本 ≥ NEAR_RATIO_NONUM）。阈值未经开发夹具校准
  （ADR U-03）：只建候选组、永远弱边、`usable_for_cluster=false`，研究侧的簇/折不得使用未校准近似组（A03）。
- 空文本、服务消息、同一 source_id 的多版本不参与。不同文本永不因同币同向并组。
组 id = stable_id("dg", 组内最早可知成员的 source_version_id)；追加成员不改组 id；组成员关系随全批变化，决策视图不消费本表。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from typing import Any

import polars as pl
from rapidfuzz import fuzz

from .lake import LayerLedger, Layout, append_quarantine, cum_prev, loss_row_from_ledger, mapping_rows, now_utc, preserve_ingested_at, quarantine_row, schema_hash, stable_id, write_loss, write_mapping, write_parquet_atomic
from .reasons import Reason

RULE_VERSION = "tg2-dedup-v0.3"
REPOST_WINDOW_S = 72 * 3600  # 同频道同文在此窗内才算"疑似重发"候选（待人工/裁决确认，不折叠）；超窗视为独立消息
HAMMING_MAX = 12
NEAR_RATIO = 80.0
NEAR_RATIO_NONUM = 90.0
MIN_NEAR_CHARS = 20

DUPLICATE_GROUP_SCHEMA: dict[str, Any] = {
    "source_version_id": pl.String,
    "channel_id": pl.Int64,
    "message_id": pl.Int64,
    "message_type": pl.String,
    "content_hash": pl.String,
    "media_hashes": pl.List(pl.String),
    "simhash": pl.UInt64,
    "duplicate_group_id": pl.String,
    "canonical_version_id": pl.String,
    "is_canonical": pl.Boolean,
    "dup_kind": pl.String,  # none | repost_same_channel | exact_cross_channel | forward | near
    "origin_version_id": pl.String,
    "origin_ref": pl.String,
    "direction_known": pl.Boolean,
    "similarity": pl.Float64,
    "usable_for_cluster": pl.Boolean,
    "reason_codes": pl.List(pl.String),
    "rule_version": pl.String,
    "batch_id": pl.String,
    "event_time": pl.Datetime("us", "UTC"),
    "available_at": pl.Datetime("us", "UTC"),
    "ingested_at": pl.Datetime("us", "UTC"),
}
SCHEMA_HASH = schema_hash(DUPLICATE_GROUP_SCHEMA)

_WS = re.compile(r"[\s　]+")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_PUNCT = re.compile(r"[，。！？、：；“”‘’（）()\[\]【】,.!?:;\"'\-—–…·~～_*#@>]+")


def number_tokens(t: str) -> tuple[str, ...]:
    return tuple(_NUM.findall(t))


def text_key(t: str) -> str:
    return _PUNCT.sub("", _WS.sub("", t)).lower()


def simhash64(t: str) -> int:
    if not t:
        return 0
    grams = [t[i : i + 3] for i in range(max(1, len(t) - 2))]
    v = [0] * 64
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "big")
        for b in range(64):
            v[b] += 1 if (h >> b) & 1 else -1
    out = 0
    for b in range(64):
        if v[b] > 0:
            out |= 1 << b
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class _DSU:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def dedup_frame(mv: pl.DataFrame, *, ingested_at: datetime | None = None) -> tuple[pl.DataFrame, list[dict[str, Any]], dict[tuple[int, str], LayerLedger], dict[str, Any]]:
    ingested_at = ingested_at or now_utc()
    batch_id = mv["batch_id"][0] if mv.height else "tg-empty"
    base = mv.filter(pl.col("message_type") == "message").with_columns(pl.col("source_id").struct.field("message_id").alias("message_id")).sort(["available_at", "sequence", "source_version_id"], nulls_last=True)
    rows = base.select("source_version_id", "channel_id", "message_id", "message_type", "content_hash", "media_hashes", "text", "forward_from", "event_time", "available_at", "sequence", "time_grade", "message_date").to_dicts()
    by_svid = {r["source_version_id"]: r for r in rows}
    key_of = {r["source_version_id"]: text_key(r["text"]) for r in rows}
    sim_of = {s: simhash64(k) for s, k in key_of.items()}
    by_source: dict[tuple[int, int], list[str]] = {}
    for r in rows:
        by_source.setdefault((r["channel_id"], r["message_id"]), []).append(r["source_version_id"])
    dsu = _DSU()
    dup_kind = {s: "none" for s in by_svid}
    origin: dict[str, str | None] = {s: None for s in by_svid}
    origin_ref: dict[str, dict | None] = {s: None for s in by_svid}
    direction = {s: False for s in by_svid}
    similarity = {s: 1.0 for s in by_svid}
    reasons: dict[str, set[str]] = {s: set() for s in by_svid}

    buckets: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["content_hash"] is None or r["text"] == "":
            continue
        buckets.setdefault((r["content_hash"], tuple(r["media_hashes"] or [])), []).append(r)
    for members in buckets.values():
        if len(members) < 2:
            continue
        head = members[0]
        for r in members[1:]:
            s = r["source_version_id"]
            if r["message_id"] == head["message_id"] and r["channel_id"] == head["channel_id"]:
                continue  # 同 source 多版本：版本链，不是重复
            if r["channel_id"] == head["channel_id"]:
                ha, ra = head["available_at"], r["available_at"]
                if ha is None or ra is None or abs((ra - ha).total_seconds()) > REPOST_WINDOW_S:
                    continue  # 超窗同文：独立消息，不入组
                dsu.union(head["source_version_id"], s)
                dup_kind[s] = "repost_same_channel"  # 候选，不折叠（S05）
                reasons[s].add(Reason.COPY_LINK_AMBIGUOUS)
            else:
                dsu.union(head["source_version_id"], s)
                dup_kind[s] = "exact_cross_channel"
                reasons[s].add(Reason.COPY_LINK_AMBIGUOUS)
    for r in rows:
        f = r["forward_from"]
        if not f:
            continue
        s = r["source_version_id"]
        origin_ref[s] = f
        pid, mid = f.get("peer_id"), f.get("message_id")
        if pid is not None and mid is not None and (pid, mid) in by_source:
            o = by_source[(pid, mid)][0]
            origin[s], direction[s] = o, True
            dsu.union(o, s)
            dup_kind[s] = "forward"
            reasons[s].discard(Reason.COPY_LINK_AMBIGUOUS)
        elif dup_kind[s] == "none" and f.get("name"):
            reasons[s].add(Reason.COPY_LINK_AMBIGUOUS)
    cand = [r for r in rows if len(key_of[r["source_version_id"]]) >= MIN_NEAR_CHARS]
    for i, a in enumerate(cand):
        sa = a["source_version_id"]
        for b in cand[i + 1 :]:
            sb = b["source_version_id"]
            if a["channel_id"] == b["channel_id"] and a["message_id"] == b["message_id"]:
                continue
            if key_of[sa] == key_of[sb] or hamming(sim_of[sa], sim_of[sb]) > HAMMING_MAX:
                continue
            ratio = fuzz.ratio(key_of[sa], key_of[sb])
            na, nb = number_tokens(a["text"]), number_tokens(b["text"])
            if na != nb or ratio < (NEAR_RATIO if na else NEAR_RATIO_NONUM):
                continue
            later = sb if (b["available_at"] or ingested_at) >= (a["available_at"] or ingested_at) else sa
            dsu.union(sa, sb)
            if dup_kind[later] == "none":
                dup_kind[later] = "near"
                similarity[later] = ratio / 100.0
                reasons[later].add(Reason.COPY_LINK_AMBIGUOUS)
    root_first: dict[str, str] = {}
    for r in rows:
        root_first.setdefault(dsu.find(r["source_version_id"]), r["source_version_id"])
    out_rows = []
    for r in rows:
        s = r["source_version_id"]
        first = root_first[dsu.find(s)]
        out_rows.append({
            "source_version_id": s, "channel_id": r["channel_id"], "message_id": r["message_id"], "message_type": r["message_type"], "content_hash": r["content_hash"],
            "media_hashes": r["media_hashes"], "simhash": sim_of[s], "duplicate_group_id": "dg-" + stable_id("dg", first)[:16], "canonical_version_id": s, "is_canonical": True,
            "dup_kind": dup_kind[s], "origin_version_id": origin[s], "origin_ref": json.dumps(origin_ref[s], ensure_ascii=False, default=str) if origin_ref[s] else None,
            "direction_known": direction[s], "similarity": similarity[s], "usable_for_cluster": dup_kind[s] in ("none", "forward", "exact_cross_channel", "repost_same_channel"),
            "reason_codes": sorted(reasons[s]), "rule_version": RULE_VERSION, "batch_id": batch_id, "event_time": r["event_time"], "available_at": r["available_at"], "ingested_at": ingested_at,
        })
    df = pl.DataFrame(out_rows, schema=DUPLICATE_GROUP_SCHEMA) if out_rows else pl.DataFrame(schema=DUPLICATE_GROUP_SCHEMA)
    qrows = []
    for o in out_rows:
        if not o["reason_codes"]:
            continue
        qrows.append(quarantine_row(batch_id=batch_id, object_kind="message_version", object_id=f"{o['channel_id']}:{o['message_id']}", object_version=o["source_version_id"],
                                    partition_id=f"channel={o['channel_id']}", reason_codes=o["reason_codes"], rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH,
                                    source_refs={"duplicate_group_id": o["duplicate_group_id"], "dup_kind": o["dup_kind"]}, event_time=o["event_time"], available_at=o["available_at"],
                                    ingested_at=ingested_at, field_path="content_hash", observed_value_ref=o["duplicate_group_id"], expected_contract="research-schema §4 COPY_LINK_AMBIGUOUS"))
    ledgers: dict[tuple[int, str], LayerLedger] = {}
    for r in rows:
        year = str(r["message_date"].year) if r["message_date"] else "unknown"
        led = ledgers.setdefault((r["channel_id"], year), LayerLedger(2, {"channel_id": r["channel_id"], "year": year}, "message_version", "canonical_version"))
        s = r["source_version_id"]
        led.mark(s, "review" if dup_kind[s] != "none" and dup_kind[s] != "forward" else "ok", sorted(reasons[s]))
        led.map(s, s, "one_to_one", sorted(reasons[s]))
    for row in mv.filter(pl.col("message_type") != "message").iter_rows(named=True):
        year = str(row["message_date"].year) if row["message_date"] else "unknown"
        led = ledgers.setdefault((row["channel_id"], year), LayerLedger(2, {"channel_id": row["channel_id"], "year": year}, "message_version", "canonical_version"))
        led.mark(row["source_version_id"], "review", [Reason.NOT_SIGNAL])
        led.map(row["source_version_id"], None, "excluded", [Reason.NOT_SIGNAL])
    summary = {
        "batch_id": batch_id, "n_input": mv.height, "n_canonical": int(df["is_canonical"].sum()) if df.height else 0,
        "n_groups": df["duplicate_group_id"].n_unique() if df.height else 0,
        "n_multi_member_groups": int(df.group_by("duplicate_group_id").len().filter(pl.col("len") > 1).height) if df.height else 0,
        "dup_kind_dist": {k: v for k, v in sorted(df.group_by("dup_kind").len().iter_rows())} if df.height else {},
        "rule_version": RULE_VERSION, "thresholds": {"hamming_max": HAMMING_MAX, "near_ratio": NEAR_RATIO, "near_ratio_nonum": NEAR_RATIO_NONUM, "calibrated": False},
    }
    return df, qrows, ledgers, summary


def run(layout: Layout, *, ingested_at: datetime | None = None) -> dict[str, Any]:
    ingested_at = ingested_at or now_utc()
    mv = pl.read_parquet(layout.message_version)
    df, qrows, ledgers, summary = dedup_frame(mv, ingested_at=ingested_at)
    layout.ensure()
    old = pl.read_parquet(layout.duplicate_group) if layout.duplicate_group.exists() else None
    write_parquet_atomic(preserve_ingested_at(df, old, "source_version_id"), layout.duplicate_group)
    append_quarantine(layout.quarantine_path, qrows)
    batch_id = summary["batch_id"]
    lrows, maps = [], []
    for key in sorted(ledgers, key=lambda k: (k[0], k[1])):
        row, _ = loss_row_from_ledger(ledgers[key], batch_id=batch_id, cum_excluded_prev=cum_prev(layout, batch_id, (1,), ledgers[key].stratum), rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH, clocks=(df["event_time"].max() if df.height else None, df["available_at"].max() if df.height else None, ingested_at))
        lrows.append(row)
        maps += mapping_rows(ledgers[key], batch_id=batch_id, rule_version=RULE_VERSION, ingested_at=ingested_at)
    write_loss(layout.loss(batch_id), lrows, replace_layers={2})
    write_mapping(layout.mapping(batch_id, 2), maps)
    summary["paths"] = {"duplicate_group": str(layout.duplicate_group)}
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="层 2 去重：message_version → duplicate_group")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out")
    g.add_argument("--lake-root")
    a = ap.parse_args(argv)
    layout = Layout.flat(a.out) if a.out else Layout.from_root(a.lake_root)
    print(json.dumps(run(layout), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
