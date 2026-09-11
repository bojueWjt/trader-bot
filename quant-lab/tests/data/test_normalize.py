"""D-03 层 1 归一：夹具 → message_version 非空、三时钟齐、时间级、相册、媒体、quarantine、损耗表、幂等。"""
from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from quant_lab.data import normalize
from quant_lab.data.lake import Layout, read_quarantine
from quant_lab.data.sources import discover, read_all

FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample"
ANCH = json.loads((FIX / "ANCHORS.json").read_text())
PEER = ANCH["peers"]
A = ANCH["anchors"]


@pytest.fixture(scope="module")
def lake(tmp_path_factory) -> tuple[Layout, dict]:
    out = tmp_path_factory.mktemp("norm")
    layout = Layout.flat(out)
    summary = normalize.run(FIX, layout, ingested_at=datetime(2026, 9, 11, tzinfo=UTC))
    return layout, summary


@pytest.fixture(scope="module")
def mv(lake) -> pl.DataFrame:
    return pl.read_parquet(lake[0].message_version)


def test_fixture_spec():
    """≥3 频道 × ≥60 条。"""
    kinds = discover(FIX)
    assert sum(1 for k, _ in kinds if k == "tdesktop") >= 3
    per = {}
    for m in read_all(FIX):
        per[m.channel_id] = per.get(m.channel_id, 0) + 1
    big = [n for n in per.values() if n >= 60]
    assert len(big) >= 3, per


def test_message_version_nonempty_three_clocks(mv):
    assert mv.height > 180
    for col in ("event_time", "available_at", "ingested_at"):
        assert col in mv.columns and mv.schema[col] == pl.Datetime("us", "UTC")
    ok = mv.filter(pl.col("time_grade") != "U")
    assert ok["event_time"].null_count() == 0
    assert ok["available_at"].null_count() == 0
    assert mv["ingested_at"].null_count() == 0
    assert mv["source_version_id"].n_unique() == mv.height


def _row(mv, peer_key, mid, version=None):
    d = mv.filter((pl.col("channel_id") == PEER[peer_key]) & (pl.col("source_id").struct.field("message_id") == mid))
    if version is not None:
        d = d.filter(pl.col("version_no") == version)
    assert d.height == 1, (peer_key, mid, d.height)
    return d.row(0, named=True)


def test_h0_available_at_is_publish_plus_delay(mv):
    r = _row(mv, "A", A["A_eth_1"])
    assert r["time_grade"] == "H0"
    assert r["available_at"] - r["event_time"] == timedelta(seconds=normalize.DEFAULT_FREEZE_DELAY_S)
    assert json.loads(r["temporal_assumptions"])["unedited_assumed_from_export"] is True


def test_h1_clock_is_export_snapshot_not_edit_time(mv):
    """反例 1（导出侧，S02）：仅见最终编辑版；SL=60800 首次可证明完整可见 = 导出快照时刻，绝不是 10:00 也不是 10:20。"""
    r = _row(mv, "A", A["CE1_edited_sl"])
    assert r["time_grade"] == "H1" and r["version_evidence"] == "edit_date" and r["evidence_time"] == datetime(2024, 4, 2, 10, 20, tzinfo=UTC)
    assert r["event_time"] == datetime(2024, 4, 2, 10, 20, tzinfo=UTC)
    assert r["available_at"] == datetime(2025, 3, 1, tzinfo=UTC) and r["snapshot_at"] == datetime(2025, 3, 1, tzinfo=UTC)
    ta = json.loads(r["temporal_assumptions"])
    assert ta["edit_original_unavailable"] is True and ta["original_message_date"] == "2024-04-02T10:00:00+00:00" and ta["clock"] == "export_snapshot_at"
    assert "60800" in r["text"]


def test_h1_without_export_snapshot_is_time_unknown(tmp_path):
    """无导出快照证据的 H1：available_at 未知 + VERSION_TIME_UNKNOWN（不用 edit_date 回填）。"""
    import shutil
    fx = tmp_path / "fx"
    shutil.copytree(FIX / "AlphaSignals", fx / "AlphaSignals")
    (fx / "AlphaSignals" / "export_manifest.json").unlink()
    layout = Layout.flat(tmp_path / "lake")
    normalize.run(fx, layout, ingested_at=datetime(2026, 9, 11, tzinfo=UTC))
    d = pl.read_parquet(layout.message_version)
    r = d.filter((pl.col("source_id").struct.field("message_id") == A["CE1_edited_sl"])).row(0, named=True)
    assert r["time_grade"] == "H1" and r["available_at"] is None and "VERSION_TIME_UNKNOWN" in r["reason_codes"]


def test_v_grade_live_versions(mv):
    """Telethon 实收：同一消息两次观察 → 两个版本，version_no 按 available_at；首次实收 10:25 的版本 SL=61000。"""
    v1 = _row(mv, "D", A["D_live_edit_v1_v2"], version=1)
    v2 = _row(mv, "D", A["D_live_edit_v1_v2"], version=2)
    assert v1["time_grade"] == v2["time_grade"] == "V"
    assert v1["available_at"] == datetime(2024, 4, 2, 10, 25, tzinfo=UTC) and "61000" in v1["text"]
    assert v2["available_at"] == datetime(2024, 4, 2, 10, 26, tzinfo=UTC) and "60800" in v2["text"]
    assert v2["event_time"] == datetime(2024, 4, 2, 10, 20, tzinfo=UTC)
    assert v1["survival_scope"] == "watched_cohort" and json.loads(v1["temporal_assumptions"])["cohort_id"] == "delta-watch-2024Q2"  # 有 cohort 证据才是 watched
    assert v1["source_version_id"] != v2["source_version_id"]


def test_albums_inferred_and_explicit(mv):
    inf = mv.filter((pl.col("channel_id") == PEER["B"]) & (pl.col("grouped_id") == A["B_album_inferred"]))
    assert inf.height == 3 and inf["album_inferred"].all()
    assert inf.filter(pl.col("text") != "").height == 1
    exp = mv.filter((pl.col("channel_id") == PEER["B"]) & (pl.col("grouped_id") == 13000000001))
    assert exp.height == 2 and not exp["album_inferred"].any()
    assert set(mv["version_evidence"]) <= {"export_snapshot", "edit_date", "live_receive"}  # 冻结 enum，不拼串
    assert json.loads(inf["temporal_assumptions"][0])["album_inferred"] is True


def test_media_copied_by_hash(mv, lake):
    r = _row(mv, "B", A["B_pure_image"])
    assert r["text"] == "" and len(r["media_hashes"]) == 1 and r["media_kinds"] == ["photo"]
    p = lake[0].media_dir / r["media_uris"][0]
    assert p.is_file() and r["media_uris"][0].startswith(r["media_hashes"][0])


def test_forward_pointer_and_reply(mv):
    r = _row(mv, "C", A["C_fwd_of_A_eth_1"])
    assert r["forward_from"]["name"] == "Alpha Signals"
    assert r["forward_from"]["peer_id"] == PEER["A"] and r["forward_from"]["message_id"] == A["A_eth_1"]
    r2 = _row(mv, "A", A["CE1_edited_sl"] + 1)
    assert r2["reply_to_message_id"] == A["CE1_edited_sl"]


def test_entities_flattened(mv):
    r = _row(mv, "C", A["C_entities"])
    assert r["text"] == "BTC 方向：做多 入场：61000-61200 止损 60000 止盈 63000"
    ents = json.loads(r["text_entities"])
    assert {e["type"] for e in ents} == {"bold", "code"} and ents[1]["offset"] == r["text"].index("61000")


def test_quarantine_reasons_and_isolation_not_deletion(mv, lake):
    q = read_quarantine(lake[0].quarantine_path)
    assert q.height >= 3
    by = {r["object_id"]: r for r in q.iter_rows(named=True)}
    bad = f"{PEER['C']}:{A['C_bad_time']}"
    assert by[bad]["reason_code"] == "TIME_UNIT_INVALID" and by[bad]["severity"] == "general"
    assert by[f"{PEER['C']}:{A['C_missing_photo']}"]["reason_code"] == "MEDIA_MISSING"
    assert by[f"{PEER['C']}:{A['C_unknown_key']}"]["reason_code"] == "SCHEMA_DRIFT"
    # 隔离不删除：坏时间消息仍在 bronze，三时钟为空、time_grade=U
    r = _row(mv, "C", A["C_bad_time"])
    assert r["quality_status"] == "quarantined" and r["time_grade"] == "U" and r["event_time"] is None and r["available_at"] is None
    assert q.filter(pl.col("status") != "open").height == 0
    assert q.select(["object_id", "object_version", "rule_version", "reason_code"]).n_unique() == q.height


def test_service_messages_kept(mv):
    s = mv.filter(pl.col("message_type") == "service")
    assert s.height >= 4 and (s["text"] == "").all()


def test_loss_table_layer1(lake):
    layout, summary = lake
    loss = pl.read_parquet(layout.loss(summary["batch_id"]))
    assert loss.height >= 4 and (loss["layer"] == 1).all()
    assert loss["input_n"].sum() == summary["n_messages"]
    assert loss["output_n"].sum() == summary["n_versions"]
    assert loss["n_quarantine"].sum() >= 3
    strata = [json.loads(s) for s in loss["stratum"]]
    assert {s["year"] for s in strata} >= {"2024", "2025"}
    dist = {}
    for d in loss["primary_reason_dist"]:
        for k, v in json.loads(d).items():
            dist[k] = dist.get(k, 0) + v
    assert dist.get("TIME_UNIT_INVALID") == 1 and dist.get("MEDIA_MISSING") == 1
    # 守恒 + 三时钟（S12）
    assert (loss["input_n"] == loss["n_ok"] + loss["n_review"] + loss["n_quarantine"] + loss["n_dup_ref"]).all()
    assert loss["ingested_at"].null_count() == 0 and loss["cum_excluded_weight"].null_count() == loss.height  # 权重未知留空，不填零


def test_idempotent_rerun(lake):
    """同输入同规则重跑：版本不增、隔离不重复、批次 id 相同。"""
    layout, s1 = lake
    before = pl.read_parquet(layout.message_version)
    q_before = read_quarantine(layout.quarantine_path).height
    s2 = normalize.run(FIX, layout, ingested_at=datetime(2026, 9, 12, tzinfo=UTC))
    after = pl.read_parquet(layout.message_version)
    assert s2["batch_id"] == s1["batch_id"] and s2["n_versions_added"] == 0
    assert after.height == before.height
    assert read_quarantine(layout.quarantine_path).height == q_before
    assert after.sort("source_version_id")["source_version_id"].to_list() == before.sort("source_version_id")["source_version_id"].to_list()


def test_time_grade_distribution(lake):
    d = lake[1]["time_grade_dist"]
    assert d.get("H0", 0) > 150 and d.get("H1", 0) >= 2 and d.get("V", 0) >= 7 and d.get("U", 0) == 1


def test_media_path_escape_rejected(tmp_path):
    """S16：绝对路径 / ../ / 根外 symlink 一律不读不复制；根内媒体正常；缺媒体隔离。"""
    import os
    from quant_lab.data.sources import read_tdesktop_export, safe_media_path
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_bytes(b"SECRET-DO-NOT-COPY")
    chat = tmp_path / "chat"
    (chat / "photos").mkdir(parents=True)
    (chat / "photos" / "ok.png").write_bytes(b"\x89PNG-ok")
    os.symlink(sentinel, chat / "photos" / "link.png")
    msgs = [
        {"id": 1, "type": "message", "date": "2024-01-01T00:00:00", "date_unixtime": "1704067200", "from": "x", "from_id": "channel1", "photo": str(sentinel), "text": "abs"},
        {"id": 2, "type": "message", "date": "2024-01-01T00:00:00", "date_unixtime": "1704067201", "from": "x", "from_id": "channel1", "photo": "../sentinel.txt", "text": "dotdot"},
        {"id": 3, "type": "message", "date": "2024-01-01T00:00:00", "date_unixtime": "1704067202", "from": "x", "from_id": "channel1", "photo": "photos/link.png", "text": "symlink"},
        {"id": 4, "type": "message", "date": "2024-01-01T00:00:00", "date_unixtime": "1704067203", "from": "x", "from_id": "channel1", "photo": "photos/ok.png", "text": "ok"},
    ]
    (chat / "result.json").write_text(json.dumps({"name": "c", "type": "public_channel", "id": 1, "messages": msgs}))
    got = {m.message_id: m for m in read_tdesktop_export(chat, root=tmp_path)}
    assert got[1].media[0].reject_reason == "absolute_path" and got[2].media[0].reject_reason == "parent_escape" and got[3].media[0].reject_reason == "symlink_escape"
    assert all(m.media[0].sha256 is None for m in (got[1], got[2], got[3])) and got[4].media[0].sha256 is not None
    layout = Layout.flat(tmp_path / "lake")
    normalize.run(tmp_path, layout, ingested_at=datetime(2026, 9, 11, tzinfo=UTC))
    copied = {p.read_bytes() for p in layout.media_dir.glob("*")}
    assert b"SECRET-DO-NOT-COPY" not in copied and len(copied) == 1
    q = read_quarantine(layout.quarantine_path)
    assert q.filter(pl.col("reason_code") == "MEDIA_MISSING").height == 3 and set(q.filter(pl.col("reason_code") == "MEDIA_MISSING")["unknown_reason"]) == {"path_rejected"}
    assert safe_media_path(chat, "photos/ok.png")[1] is None


def test_same_source_multi_observation_is_versions_not_key_conflict(lake):
    """同文件内同 message_id 的两次观察（不同实收时刻/内容）= 版本链，不是 KEY_DUPLICATE_OR_ORDER。"""
    q = read_quarantine(lake[0].quarantine_path)
    assert q.filter(pl.col("reason_code") == "KEY_DUPLICATE_OR_ORDER").height == 0


def test_append_does_not_renumber_and_keeps_first_ingested(tmp_path):
    """追加晚到的更早版本：旧行 version_no/ingested_at 不改，新行序号递增并记 KEY_DUPLICATE_OR_ORDER（CR-02）。"""
    import shutil
    fx = tmp_path / "fx"
    shutil.copytree(FIX / "DeltaLive", fx / "DeltaLive")
    layout = Layout.flat(tmp_path / "lake")
    t1 = datetime(2026, 9, 11, tzinfo=UTC)
    normalize.run(fx, layout, ingested_at=t1)
    before = pl.read_parquet(layout.message_version).filter(pl.col("source_id").struct.field("message_id") == A["D_live_edit_v1_v2"]).sort("version_no")
    assert before["version_no"].to_list() == [1, 2]
    # 晚到一条更早的实收观察（10:24，内容为 v1 文本但另一时刻）
    line = json.loads((fx / "DeltaLive" / "live.jsonl").read_text().splitlines()[0])
    line["first_seen_at"] = "2024-04-02T10:24:00+00:00"
    line["snapshot_at"] = line["first_seen_at"]
    (fx / "DeltaLive" / "late.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n")
    normalize.run(fx, layout, ingested_at=datetime(2026, 9, 12, tzinfo=UTC))
    after = pl.read_parquet(layout.message_version).filter(pl.col("source_id").struct.field("message_id") == A["D_live_edit_v1_v2"]).sort("version_no")
    assert after["version_no"].to_list() == [1, 2, 3] and after["ingested_at"][0] == t1 and after["ingested_at"][2] == datetime(2026, 9, 12, tzinfo=UTC)
    assert after.filter(pl.col("version_no") <= 2).select("source_version_id", "available_at").equals(before.select("source_version_id", "available_at"))
    q = read_quarantine(layout.quarantine_path)
    assert q.filter((pl.col("reason_code") == "KEY_DUPLICATE_OR_ORDER") & (pl.col("object_version") == after["source_version_id"][2])).height == 1


def test_media_path_windows_separators(tmp_path):
    """G0 抽验补充：反斜杠父级 `..\\SECRET.txt`、`C:\\x`、`\\\\server\\x` 一律拒绝；根内反斜杠路径归一后可用。"""
    from quant_lab.data.sources import safe_media_path
    chat = tmp_path / "chat"
    (chat / "photos").mkdir(parents=True)
    (chat / "photos" / "ok.png").write_bytes(b"x")
    assert safe_media_path(chat, "..\\SECRET.txt")[1] == "parent_escape"
    assert safe_media_path(chat, "photos\\..\\..\\SECRET.txt")[1] == "parent_escape"
    assert safe_media_path(chat, "C:\\Windows\\x")[1] == "absolute_path"
    assert safe_media_path(chat, "\\\\server\\share\\x")[1] == "absolute_path"
    real, why = safe_media_path(chat, "photos\\ok.png")
    assert why is None and real == (chat / "photos" / "ok.png").resolve()
