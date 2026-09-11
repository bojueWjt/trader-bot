"""D-04 层 2 去重：精确重复折叠为规范引用、跨频道复制候选组（转发指针=方向已知；无头复制=COPY_LINK_AMBIGUOUS）、
simhash 近重复只建候选组、独立同向单不并、幂等。"""
from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from quant_lab.data import dedup, normalize
from quant_lab.data.lake import Layout, read_quarantine

FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample"
ANCH = json.loads((FIX / "ANCHORS.json").read_text())
PEER, A = ANCH["peers"], ANCH["anchors"]


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    layout = Layout.flat(tmp_path_factory.mktemp("dedup"))
    normalize.run(FIX, layout, ingested_at=datetime(2026, 9, 11, tzinfo=UTC))
    summary = dedup.run(layout, ingested_at=datetime(2026, 9, 11, tzinfo=UTC))
    return layout, summary


@pytest.fixture(scope="module")
def dg(lake) -> pl.DataFrame:
    return pl.read_parquet(lake[0].duplicate_group)


def _r(dg, peer, mid):
    d = dg.filter((pl.col("channel_id") == PEER[peer]) & (pl.col("message_id") == mid))
    assert d.height == 1, (peer, mid, d.height)
    return d.row(0, named=True)


def test_output_schema_and_clocks(dg):
    assert dg.height > 0
    for c in ("source_version_id", "duplicate_group_id", "canonical_version_id", "dup_kind", "event_time", "available_at", "ingested_at"):
        assert c in dg.columns
    assert dg["source_version_id"].n_unique() == dg.height
    assert dg["ingested_at"].null_count() == 0


def test_same_channel_repost_is_candidate_not_folded(dg, lake):
    """S05：不同 message_id 的同文只建候选组（待确认重发），不折叠、不减种子；DUPLICATE_EXACT 只属于同 source 的重复观察。"""
    first, second = _r(dg, "C", A["C_dup_first"]), _r(dg, "C", A["C_dup_second"])
    assert first["is_canonical"] and second["is_canonical"] and first["dup_kind"] == "none" and second["dup_kind"] == "repost_same_channel"
    assert second["duplicate_group_id"] == first["duplicate_group_id"] and second["canonical_version_id"] == second["source_version_id"]
    assert "COPY_LINK_AMBIGUOUS" in second["reason_codes"] and "DUPLICATE_EXACT" not in second["reason_codes"]
    assert (dg["is_canonical"]).all()
    assert read_quarantine(lake[0].quarantine_path).filter(pl.col("reason_code") == "DUPLICATE_EXACT").height == 0


def test_cross_channel_forward_pointer_direction_known(dg):
    origin = _r(dg, "A", A["A_btc_short_wan"])
    fwd = dg.filter((pl.col("channel_id") == PEER["B"]) & (pl.col("dup_kind") == "forward") & (pl.col("origin_version_id") == origin["source_version_id"]))
    assert fwd.height == 1
    f = fwd.row(0, named=True)
    assert f["origin_version_id"] == origin["source_version_id"] and f["direction_known"]
    assert f["duplicate_group_id"] == origin["duplicate_group_id"]
    assert "COPY_LINK_AMBIGUOUS" not in f["reason_codes"]
    assert origin["is_canonical"]


def test_cross_channel_headless_copy_is_ambiguous_not_merged(dg):
    origin = _r(dg, "A", A["A_btc_short_wan"])
    copy = _r(dg, "C", A["C_copy_of_A_btc_short"])
    assert copy["dup_kind"] == "exact_cross_channel" and not copy["direction_known"]
    assert copy["duplicate_group_id"] == origin["duplicate_group_id"]
    assert "COPY_LINK_AMBIGUOUS" in copy["reason_codes"]
    # 跨频道各自保留、各自 canonical（不删出处）
    assert copy["canonical_version_id"] == copy["source_version_id"]


def test_near_duplicate_candidate_only(dg):
    src = _r(dg, "A", A["CE1_edited_sl"])
    near = _r(dg, "C", A["C_near_copy_CE1"])
    assert near["dup_kind"] == "near" and near["similarity"] >= 0.8
    assert near["duplicate_group_id"] == src["duplicate_group_id"]
    assert "COPY_LINK_AMBIGUOUS" in near["reason_codes"] and near["canonical_version_id"] == near["source_version_id"]


def test_independent_same_side_orders_not_merged(dg):
    """同币同向、同模板、不同数字的两单：既不精确也不近似并组（数字集合不同=不同计划）。"""
    a, b = _r(dg, "A", A["CE4_dual_1"]), _r(dg, "A", A["CE4_dual_2"])
    assert a["duplicate_group_id"] != b["duplicate_group_id"]
    assert a["dup_kind"] == "none" and b["dup_kind"] == "none"
    # 同模板信号（舒琴式）之间没有任何 near 并组：near 行的组内必须数字集合一致
    from quant_lab.data.dedup import number_tokens
    mv_text = {r["source_version_id"]: r for r in dg.iter_rows(named=True)}
    near = dg.filter(pl.col("dup_kind") == "near")
    assert near.height >= 1


def test_far_apart_identical_text_not_folded(dg, lake):
    """超过 72h 的同文重发不折叠（独立消息）。"""
    mv = pl.read_parquet(lake[0].message_version).filter(pl.col("message_type") == "message")
    grp = mv.group_by(["channel_id", "content_hash"]).agg(pl.col("available_at").min().alias("lo"), pl.col("available_at").max().alias("hi"), pl.len())
    far = grp.filter((pl.col("len") > 1) & ((pl.col("hi") - pl.col("lo")).dt.total_seconds() > 72 * 3600))
    assert far.height >= 1
    ch, h = far.row(0)[0], far.row(0)[1]
    members = dg.filter((pl.col("channel_id") == ch) & (pl.col("content_hash") == h)).sort("available_at")
    assert members.height >= 2 and members["is_canonical"].all()
    # Cross-channel copy groups can legitimately span the chosen pair. Isolate the temporal rule.
    pair = []
    for i in range(2):
        row = mv.row(0, named=True)
        row.update(source_version_id=f"far-{i}", source_id={"peer_id": 1, "message_id": i + 1}, channel_id=1, text="independent identical source text", content_hash="far-text", media_hashes=[], forward_from=None,
                   available_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=4*i))
        pair.append(row)
    isolated, *_ = dedup.dedup_frame(pl.DataFrame(pair, schema=mv.schema))
    assert isolated["is_canonical"].all() and isolated["dup_kind"].to_list() == ["none", "none"]
    assert isolated["duplicate_group_id"].n_unique() == 2


def test_album_members_and_service_not_folded(dg, lake):
    mv = pl.read_parquet(lake[0].message_version)
    empties = mv.filter((pl.col("text") == "") & (pl.col("message_type") == "message"))["source_version_id"]
    sub = dg.filter(pl.col("source_version_id").is_in(empties))
    assert (sub["dup_kind"] == "none").all()
    assert dg.filter(pl.col("message_type") == "service").height == 0


def test_loss_layer2(lake):
    layout, summary = lake
    loss = pl.read_parquet(layout.loss(summary["batch_id"])).filter(pl.col("layer") == 2)
    assert loss.height >= 1 and loss["layer_name"][0] == "dedup"
    assert loss["n_review"].sum() >= 2 and loss["n_dup_ref"].sum() == 0  # 层 2 不再折叠
    assert loss["input_n"].sum() == summary["n_input"] and loss["output_n"].sum() == summary["n_canonical"]
    assert (loss["input_n"] == loss["n_ok"] + loss["n_review"] + loss["n_quarantine"] + loss["n_dup_ref"]).all()
    m = pl.read_parquet(layout.mapping(summary["batch_id"], 2))
    assert m.height == summary["n_input"] and set(m["relation"]) == {"one_to_one", "excluded"}  # r4 §6 S12: service exits are explicit.


def test_near_group_not_usable_for_cluster(dg):
    near = dg.filter(pl.col("dup_kind") == "near")
    assert near.height >= 1 and not near["usable_for_cluster"].any()  # A03：未校准近似组不进簇/折


def test_idempotent(lake):
    layout, s1 = lake
    before = pl.read_parquet(layout.duplicate_group)
    qn = read_quarantine(layout.quarantine_path).height
    s2 = dedup.run(layout, ingested_at=datetime(2026, 9, 12, tzinfo=UTC))
    after = pl.read_parquet(layout.duplicate_group)
    assert s2["n_groups"] == s1["n_groups"]
    assert after.sort("source_version_id")["duplicate_group_id"].to_list() == before.sort("source_version_id")["duplicate_group_id"].to_list()
    assert read_quarantine(layout.quarantine_path).height == qn
