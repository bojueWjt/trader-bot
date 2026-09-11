"""D-07 链接器：reply 强边沿父链到根、版本链 amend、窗口弱边唯一才 link、多候选 unresolved+ENTRY_LINK_AMBIGUOUS、缺父 PARENT_MISSING、同向双单不并。"""
from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from quant_lab.data import dedup, extract, linker, normalize, validate
from quant_lab.data.lake import Layout

FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample"
ANCH = json.loads((FIX / "ANCHORS.json").read_text())
PEER, A = ANCH["peers"], ANCH["anchors"]
T0 = datetime(2024, 6, 10, 9, 1, tzinfo=UTC)
INST = "BTCUSDT-PERP.BINANCE-UM"


def _cp_row(plan_id, mid, kind, at, side="long", inst=INST, extractor="parser"):
    return {"plan_id": plan_id, "extract_id": "ex-" + plan_id, "source_version_id": "sv-" + plan_id, "channel_id": 1, "message_id": mid, "extractor_name": extractor, "kind": kind,
            "symbol_raw": "BTC", "instrument_id": inst, "side": side, "available_at": at, "event_time": (at - timedelta(seconds=60)) if at else None, "batch_id": "tg-test", "time_grade": "H0"}


def _mv_row(plan_id, mid, reply=None, seq=None, version_no=1, text=""):
    return {"source_version_id": "sv-" + plan_id, "channel_id": 1, "source_id": {"peer_id": 1, "message_id": mid}, "reply_to_message_id": reply, "sequence": seq, "version_no": version_no, "text": text or plan_id}


def _frames(cp_rows, mv_rows):
    cp = pl.DataFrame(cp_rows).with_columns(pl.col("available_at").cast(pl.Datetime("us", "UTC")), pl.col("event_time").cast(pl.Datetime("us", "UTC")))
    mv = pl.DataFrame(mv_rows, schema={"source_version_id": pl.String, "channel_id": pl.Int64, "source_id": pl.Struct({"peer_id": pl.Int64, "message_id": pl.Int64}), "reply_to_message_id": pl.Int64, "sequence": pl.Int64, "version_no": pl.Int32, "text": pl.String})
    return cp, mv


def test_window_unique_weak_links_but_two_roots_unresolved():
    cp, mv = _frames(
        [_cp_row("r1", 10, "entry_proposal", T0), _cp_row("m1", 11, "stop_move", T0 + timedelta(hours=2)),
         _cp_row("r2", 20, "entry_proposal", T0 + timedelta(hours=5)), _cp_row("m2", 21, "reduce", T0 + timedelta(hours=6))],
        [_mv_row("r1", 10), _mv_row("m1", 11), _mv_row("r2", 20), _mv_row("m2", 21)])
    cb, jd, s = linker.build_candidates(cp, mv)
    a = {r["from_plan_id"]: r for r in jd.iter_rows(named=True)}
    assert a["m1"]["action"] == "link" and cb.filter((pl.col("from_plan_id") == "m1") & pl.col("selected"))["method"][0] == "window"
    assert a["m2"]["action"] == "unresolved" and "ENTRY_LINK_AMBIGUOUS" in a["m2"]["reason_codes"]  # 同向双单：不就近强并
    assert cb.filter(pl.col("from_plan_id") == "m2").height == 2 and not cb.filter(pl.col("from_plan_id") == "m2")["selected"].any()


def test_reply_strong_beats_window_and_follows_parent_chain():
    cp, mv = _frames(
        [_cp_row("r1", 10, "entry_proposal", T0), _cp_row("r2", 20, "entry_proposal", T0 + timedelta(hours=1)),
         _cp_row("m1", 30, "stop_move", T0 + timedelta(hours=2)), _cp_row("m2", 31, "close_claimed", T0 + timedelta(hours=3))],
        [_mv_row("r1", 10), _mv_row("r2", 20), _mv_row("m1", 30, reply=20), _mv_row("m2", 31, reply=30)])
    cb, jd, s = linker.build_candidates(cp, mv)
    sel = {r["from_plan_id"]: r for r in cb.filter(pl.col("selected")).iter_rows(named=True)}
    assert sel["m1"]["to_plan_id"] == "r2" and sel["m1"]["method"] == "reply" and sel["m1"]["strength"] == "strong"
    assert sel["m2"]["to_plan_id"] == "r2" and sel["m2"]["method"] == "reply"  # 沿父链 31→30→20
    assert sel["m1"]["edge_available_at"] == T0 + timedelta(hours=2)
    # S03：中间父帖是依赖：父帖 30 若晚于子事件可知，子边 edge_available_at 取父帖时刻；依赖引用含中间父帖
    assert sel["m2"]["edge_available_at"] == T0 + timedelta(hours=3) and "sv-m1" in sel["m2"]["dependency_refs"]


def test_intermediate_parent_later_than_child_delays_edge():
    cp, mv = _frames(
        [_cp_row("r1", 10, "entry_proposal", T0), _cp_row("mid", 20, "stop_move", T0 + timedelta(days=1)), _cp_row("child", 30, "close_claimed", T0 + timedelta(hours=1))],
        [_mv_row("r1", 10), _mv_row("mid", 20, reply=10), _mv_row("child", 30, reply=20)])
    cb, jd, _ = linker.build_candidates(cp, mv)
    e = cb.filter((pl.col("from_plan_id") == "child") & pl.col("selected")).row(0, named=True)
    assert e["edge_available_at"] == T0 + timedelta(days=1)  # 不能早于中间父帖可知
    # 依赖时钟未知 → 边不可用（DEPENDENCY_NOT_AVAILABLE），不 link
    cp2, mv2 = _frames([_cp_row("r1", 10, "entry_proposal", T0), _cp_row("mid", 20, "stop_move", None), _cp_row("child", 30, "close_claimed", T0 + timedelta(hours=1))],
                       [_mv_row("r1", 10), _mv_row("mid", 20, reply=10), _mv_row("child", 30, reply=20)])
    cb2, jd2, _ = linker.build_candidates(cp2, mv2)
    row = cb2.filter(pl.col("from_plan_id") == "child").row(0, named=True)
    assert "DEPENDENCY_NOT_AVAILABLE" in row["reason_codes"] and not row["selected"]
    assert jd2.filter(pl.col("from_plan_id") == "child")["action"][0] == "unresolved"


def test_reply_guard_instrument_side_and_cycle():
    """S05：reply 指向品种/方向不符的根 → 不是强边（ENTRY_LINK_AMBIGUOUS）；环 → LIFECYCLE_INVALID。"""
    eth = "ETHUSDT-PERP.BINANCE-UM"
    cp, mv = _frames([_cp_row("r1", 10, "entry_proposal", T0), {**_cp_row("m1", 11, "stop_move", T0 + timedelta(hours=1), side="short", inst=eth), "symbol_raw": "ETH"}],
                     [_mv_row("r1", 10), _mv_row("m1", 11, reply=10)])
    cb, jd, _ = linker.build_candidates(cp, mv)
    row = cb.filter(pl.col("from_plan_id") == "m1").row(0, named=True)
    assert row["strength"] == "weak" and "ENTRY_LINK_AMBIGUOUS" in row["reason_codes"] and jd["action"][0] == "unresolved"
    cp2, mv2 = _frames([_cp_row("r1", 10, "entry_proposal", T0), _cp_row("a", 11, "stop_move", T0 + timedelta(hours=1)), _cp_row("b", 12, "stop_move", T0 + timedelta(hours=2))],
                       [_mv_row("r1", 10), _mv_row("a", 11, reply=12), _mv_row("b", 12, reply=11)])
    cb2, jd2, _ = linker.build_candidates(cp2, mv2)
    assert "LIFECYCLE_INVALID" in jd2.filter(pl.col("from_plan_id") == "a")["reason_codes"][0]


def test_window_same_instant_not_linked_and_plan_ref_cutoff():
    cp, mv = _frames([_cp_row("r1", 10, "entry_proposal", T0), _cp_row("m1", 11, "stop_move", T0)], [_mv_row("r1", 10), _mv_row("m1", 11)])
    cb, jd, _ = linker.build_candidates(cp, mv)
    assert cb.height == 0 and jd["action"][0] == "new_episode"  # 同刻顺序未知：不算"之前"
    # plan_ref 只查事件可知时刻之前的根
    cp2, mv2 = _frames([_cp_row("r1", 10, "entry_proposal", T0 + timedelta(hours=5)), _cp_row("m1", 11, "stop_move", T0 + timedelta(hours=1))], [_mv_row("r1", 10), _mv_row("m1", 11)])
    ex = pl.DataFrame({"extract_id": ["ex-r1", "ex-m1"], "plan_ref": ["7", "7"]})
    cb2, jd2, _ = linker.build_candidates(cp2, mv2, ex)
    assert cb2.filter(pl.col("method") == "plan_ref").height == 0


def test_llm_adjudication_recorded_and_rejected():
    """S13：多弱候选交录制裁决；选边必须来自输入候选，伪造候选/证据 → unresolved。"""
    from quant_lab.data.llm import SCHEMA_NAME_ADJ, AdjudicationRequest, RecordedClient, build_adjudicate_prompt, record_key
    cp, mv = _frames([_cp_row("r1", 10, "entry_proposal", T0), _cp_row("r2", 20, "entry_proposal", T0 + timedelta(hours=5)), _cp_row("m2", 21, "reduce", T0 + timedelta(hours=6))],
                     [_mv_row("r1", 10, text="BTC 多 60000"), _mv_row("r2", 20, text="BTC 多 58500"), _mv_row("m2", 21, text="第二单止盈一半")])
    cb0, _, _ = linker.build_candidates(cp, mv)
    weak = [{k: c[k] for k in ("candidate_id", "to_plan_id", "method", "strength", "evidence")} for c in cb0.filter(pl.col("from_plan_id") == "m2").iter_rows(named=True)]
    ctx_plans = [("m2", 21), ("r1", 10), ("r2", 20)]
    ctx = [{"peer_id": 1, "message_id": mid, "source_version_id": "sv-" + pid, "text": pid} for pid, mid in ctx_plans]
    # 用 linker 相同的上下文构造（顺序：事件在前，其余按 available_at）
    ctx = [{"peer_id": 1, "message_id": 21, "source_version_id": "sv-m2", "text": "第二单止盈一半", "available_at": (T0 + timedelta(hours=6)).isoformat()},
           {"peer_id": 1, "message_id": 10, "source_version_id": "sv-r1", "text": "BTC 多 60000", "available_at": T0.isoformat()},
           {"peer_id": 1, "message_id": 20, "source_version_id": "sv-r2", "text": "BTC 多 58500", "available_at": (T0 + timedelta(hours=5)).isoformat()}]
    from quant_lab.data.lake import stable_id
    req = AdjudicationRequest(request_id=stable_id("adjreq", "m2", linker.RULE_VERSION)[:32], event_plan_id="m2", candidates=weak, context=ctx, decision_cutoff=(T0 + timedelta(hours=6)).isoformat(), purpose="description")
    system, user = build_adjudicate_prompt(req)
    key = record_key(system, user, SCHEMA_NAME_ADJ)
    good = next(c for c in weak if c["to_plan_id"] == "r2")
    fixtures = {key: {"response": {"action": "link", "selected_candidate_id": good["candidate_id"], "evidence_message_ids": [{"peer_id": 1, "message_id": 20, "source_version_id": "sv-r2"}],
                                    "rejected_candidate_ids": [c["candidate_id"] for c in weak if c is not good], "reason_codes": [], "needs_more_context": False}}}
    cb, jd, s = linker.build_candidates(cp, mv, adjudicator=RecordedClient(fixtures))
    a = jd.filter(pl.col("from_plan_id") == "m2").row(0, named=True)
    assert a["action"] == "link" and a["provider"] == "llm:recorded" and cb.filter(pl.col("selected") & (pl.col("from_plan_id") == "m2"))["to_plan_id"][0] == "r2"
    bad = {key: {"response": {"action": "link", "selected_candidate_id": "cb-forged", "evidence_message_ids": [{"peer_id": 1, "message_id": 999, "source_version_id": "sv-x"}], "rejected_candidate_ids": [], "reason_codes": []}}}
    cb2, jd2, _ = linker.build_candidates(cp, mv, adjudicator=RecordedClient(bad))
    assert jd2.filter(pl.col("from_plan_id") == "m2")["action"][0] == "unresolved"
    # 无 provider（gated）→ unresolved
    cb3, jd3, _ = linker.build_candidates(cp, mv, adjudicator=None)
    assert jd3.filter(pl.col("from_plan_id") == "m2")["action"][0] == "unresolved"


def test_parent_missing_and_out_of_window():
    cp, mv = _frames(
        [_cp_row("r1", 10, "entry_proposal", T0), _cp_row("m1", 50, "stop_move", T0 + timedelta(days=5))],
        [_mv_row("r1", 10), _mv_row("m1", 50, reply=49)])
    cb, jd, _ = linker.build_candidates(cp, mv)
    a = jd.filter(pl.col("from_plan_id") == "m1").row(0, named=True)
    assert a["action"] == "new_episode" and "PARENT_MISSING" in a["reason_codes"] and cb.height == 0


def test_same_source_version_chain_is_amend():
    cp, mv = _frames([_cp_row("v1", 10, "entry_proposal", T0), _cp_row("v2", 10, "entry_proposal", T0 + timedelta(minutes=1))],
                     [_mv_row("v1", 10, version_no=1), _mv_row("v2", 10, version_no=2)])
    cb, jd, _ = linker.build_candidates(cp, mv)
    sel = cb.filter(pl.col("selected")).row(0, named=True)
    assert sel["from_plan_id"] == "v2" and sel["to_plan_id"] == "v1" and sel["method"] == "same_source" and sel["strength"] == "strong"


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    layout = Layout.flat(tmp_path_factory.mktemp("linker"))
    ts = datetime(2026, 9, 11, tzinfo=UTC)
    normalize.run(FIX, layout, ingested_at=ts)
    dedup.run(layout, ingested_at=ts)
    extract.run(layout, ingested_at=ts)
    validate.run(layout, ingested_at=ts, synthetic=True)
    s = linker.run(layout, ingested_at=ts)
    return layout, s


def test_fixture_links(lake):
    layout, s = lake
    cb = pl.read_parquet(layout.silver_dir / "candidate_edges.parquet")
    cp = pl.read_parquet(layout.canonical_plan)
    pid = {(r["channel_id"], r["message_id"]): r["plan_id"] for r in cp.filter(pl.col("extractor_name") == "parser").iter_rows(named=True)}
    sel = {r["from_plan_id"]: r for r in cb.filter(pl.col("selected")).iter_rows(named=True)}
    # 反例 4：153→150、154→152 各自归属，不串
    assert sel[pid[(PEER["A"], A["CE4_dual_2"] + 1)]]["to_plan_id"] == pid[(PEER["A"], A["CE4_dual_1"])]
    assert sel[pid[(PEER["A"], A["CE4_dual_2"] + 2)]]["to_plan_id"] == pid[(PEER["A"], A["CE4_dual_2"])]
    # 窗口弱边也生成但未被选（150/152 互为对方 72h 内同向候选）
    w = cb.filter((pl.col("from_plan_id") == pid[(PEER["A"], A["CE4_dual_2"] + 1)]) & (pl.col("method") == "window"))
    assert w.height >= 1 and not w["selected"].any()
    assert s["n_roots"] >= 20 and s["actions"]["link"] >= 10
    # 反例 4 无指针配对：155 "BTC 多单止损上移到 59500" 在 150/152 都 72h 内 → unresolved + ENTRY_LINK_AMBIGUOUS
    jd = pl.read_parquet(layout.silver_dir / "adjudications.parquet")
    a = jd.filter(pl.col("from_plan_id") == pid[(PEER["A"], A["CE4_ambiguous_mgmt"])]).row(0, named=True)
    assert a["action"] == "unresolved" and "ENTRY_LINK_AMBIGUOUS" in a["reason_codes"]
    assert cb.filter(pl.col("from_plan_id") == pid[(PEER["A"], A["CE4_ambiguous_mgmt"])]).height == 2


def test_adjudication_rejects_evidence_after_cutoff_or_without_clock():
    """S13：截止后 / 无可知时钟的上下文不进提示；引用它们的证据一律拒收 → unresolved。"""
    from quant_lab.data.llm import SCHEMA_NAME_ADJ, AdjudicationRequest, RecordedClient, adjudicate, build_adjudicate_prompt, record_key, sent_subsets
    cands = [{"candidate_id": "c1", "to_plan_id": "r1", "method": "window", "strength": "weak", "evidence": "{}"}, {"candidate_id": "c2", "to_plan_id": "r2", "method": "window", "strength": "weak", "evidence": "{}"}]
    future = {"peer_id": 1, "message_id": 99, "source_version_id": "sv-future", "text": "后来的平仓", "available_at": (T0 + timedelta(days=2)).isoformat()}
    noclock = {"peer_id": 1, "message_id": 98, "source_version_id": "sv-noclock", "text": "无时钟"}
    req = AdjudicationRequest(request_id="rq", event_plan_id="m", candidates=cands, context=[future, noclock], decision_cutoff=(T0 + timedelta(hours=6)).isoformat())
    filtered = AdjudicationRequest(**{**req.__dict__, "context": []})
    system, user = build_adjudicate_prompt(filtered)
    key = record_key(system, user, SCHEMA_NAME_ADJ)
    resp = {"action": "link", "selected_candidate_id": "c1", "evidence_message_ids": [{"peer_id": 1, "message_id": 99, "source_version_id": "sv-future"}], "rejected_candidate_ids": ["c2"], "reason_codes": []}
    res = adjudicate(req, client=RecordedClient({key: {"response": resp}}))
    assert res.action == "unresolved" and "evidence_not_in_context" in res.note
    assert sent_subsets(filtered)[1] == []
