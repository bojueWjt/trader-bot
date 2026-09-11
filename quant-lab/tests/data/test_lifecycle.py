"""D-07 双轨状态机 + 决策图 + 发布：ADR §3 108 格独立期望、五反例（含逐例快照哈希/隔离/损耗）、决策视图对未来扰动全列不变、
依赖推迟拒收、发布 manifest 屏障、同名版本拒改、观察终点与到期守卫、tombstone 拒读、幂等。"""
from __future__ import annotations

import itertools
import json
import pathlib
import shutil
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from quant_lab.data import api, lifecycle
from quant_lab.data.graph import DEFAULT_PROCESSING_DELAY_S, decision_visible, manifest_path, t_dec_of, tombstone_graph, verify_manifest
from quant_lab.data.lake import Layout, read_quarantine
from quant_lab.data.lifecycle import C_STATES, P_STATES, transition

FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample"
LLM_FIX = pathlib.Path(__file__).parent / "fixtures" / "llm_recorded" / "extract_v1.json"
OCR_FIX = pathlib.Path(__file__).parent / "fixtures" / "llm_recorded" / "ocr_v1.json"
ANCH = json.loads((FIX / "ANCHORS.json").read_text())
PEER, A = ANCH["peers"], ANCH["anchors"]
T_BUILD = datetime(2026, 9, 11, tzinfo=UTC)

# ---- ADR §3 转移表：独立手写期望（不从实现生成）。格式 "P,C" 或动作符号。
P_ABBR = {"none": "N", "active": "A", "cancelled": "X", "expired": "E"}
C_ABBR = {"unknown": "U", "claimed_open": "O", "claimed_closed": "C"}
GOLDEN = {
    ("N", "U"): {"entry_proposal": "A,U", "amend": "L", "cancel": "I", "expire": "I", "entry_claimed": "N,O", "add": "=", "stop_move": "=", "tp_ladder": "=", "reduce": "=", "close_claimed": "N,C", "correction": "R"},
    ("N", "O"): {"entry_proposal": "J", "amend": "L", "cancel": "I", "expire": "I", "entry_claimed": "I", "add": "=", "stop_move": "=", "tp_ladder": "=", "reduce": "=", "close_claimed": "N,C", "correction": "R"},
    ("N", "C"): {"entry_proposal": "J", "amend": "I", "cancel": "I", "expire": "I", "entry_claimed": "J", "add": "I", "stop_move": "I", "tp_ladder": "I", "reduce": "I", "close_claimed": "I", "correction": "R"},
    ("A", "U"): {"entry_proposal": "J", "amend": "=", "cancel": "X,U", "expire": "E,U", "entry_claimed": "A,O", "add": "=", "stop_move": "=", "tp_ladder": "=", "reduce": "=", "close_claimed": "A,C", "correction": "R"},
    ("A", "O"): {"entry_proposal": "J", "amend": "=", "cancel": "X,O", "expire": "E,O", "entry_claimed": "I", "add": "=", "stop_move": "=", "tp_ladder": "=", "reduce": "=", "close_claimed": "A,C", "correction": "R"},
    ("A", "C"): {"entry_proposal": "J", "amend": "I", "cancel": "I", "expire": "I", "entry_claimed": "J", "add": "I", "stop_move": "I", "tp_ladder": "I", "reduce": "I", "close_claimed": "I", "correction": "R"},
    ("X", "U"): {"entry_proposal": "J", "amend": "I", "cancel": "I", "expire": "I", "entry_claimed": "X,O", "add": "I", "stop_move": "I", "tp_ladder": "I", "reduce": "I", "close_claimed": "X,C", "correction": "R"},
    ("X", "O"): {"entry_proposal": "J", "amend": "I", "cancel": "I", "expire": "I", "entry_claimed": "I", "add": "I", "stop_move": "I", "tp_ladder": "I", "reduce": "I", "close_claimed": "X,C", "correction": "R"},
    ("X", "C"): {"entry_proposal": "J", "amend": "I", "cancel": "I", "expire": "I", "entry_claimed": "J", "add": "I", "stop_move": "I", "tp_ladder": "I", "reduce": "I", "close_claimed": "I", "correction": "R"},
    ("E", "U"): {"entry_proposal": "J", "amend": "I", "cancel": "I", "expire": "I", "entry_claimed": "E,O", "add": "I", "stop_move": "I", "tp_ladder": "I", "reduce": "I", "close_claimed": "E,C", "correction": "R"},
    ("E", "O"): {"entry_proposal": "J", "amend": "I", "cancel": "I", "expire": "I", "entry_claimed": "I", "add": "I", "stop_move": "I", "tp_ladder": "I", "reduce": "I", "close_claimed": "E,C", "correction": "R"},
    ("E", "C"): {"entry_proposal": "J", "amend": "I", "cancel": "I", "expire": "I", "entry_claimed": "J", "add": "I", "stop_move": "I", "tp_ladder": "I", "reduce": "I", "close_claimed": "I", "correction": "R"},
}
P_FULL = {v: k for k, v in P_ABBR.items()}
C_FULL = {v: k for k, v in C_ABBR.items()}


def test_transition_table_matches_independent_golden():
    n = 0
    for (pa, ca), cols in GOLDEN.items():
        p, c = P_FULL[pa], C_FULL[ca]
        for kind, exp in cols.items():
            np_, nc, action = transition(p, c, kind)
            n += 1
            if "," in exp:
                ep, ec = exp.split(",")
                assert (np_, nc, action) == (P_FULL[ep], C_FULL[ec], "move"), (pa, ca, kind)
            else:
                assert action == exp and (np_, nc) == (p, c), (pa, ca, kind)
    assert n == 12 * 11
    for p, c in itertools.product(P_STATES, C_STATES):
        for k in ("analysis", "result_post", "chatter", "undecidable", "delete_notice"):
            assert transition(p, c, k) == (p, c, "desc")
        assert transition(p, c, "unknown_kind")[2] == "I"


def test_t_dec_and_visibility():
    t = datetime(2024, 4, 2, 10, 21, tzinfo=UTC)
    assert t_dec_of(t) == t + timedelta(seconds=DEFAULT_PROCESSING_DELAY_S)
    with pytest.raises(ValueError):
        t_dec_of(t, 0)
    assert decision_visible(t, t_dec_of(t)) and not decision_visible(t_dec_of(t), t_dec_of(t))
    assert decision_visible(t_dec_of(t), t_dec_of(t), order_known=True) and not decision_visible(None, t_dec_of(t))


def _build(root: pathlib.Path, fixture: pathlib.Path = FIX, gv: str = "fixture-v1", **kw):
    layout = Layout.from_root(root)
    res = api.build(fixture, layout, graph_version=gv, llm_fixture=LLM_FIX, ocr_fixture=OCR_FIX, ingested_at=T_BUILD, **kw)
    return layout, res


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("lifecycle") / "root")


@pytest.fixture(scope="module")
def ep(lake):
    return pl.read_parquet(lake[0].episode("fixture-v1"))


@pytest.fixture(scope="module")
def ev(lake):
    return pl.read_parquet(lake[0].episode_event("fixture-v1"))


def _ep(ep, peer, mid):
    d = ep.filter((pl.col("channel_id") == PEER[peer]) & (pl.col("root_message_id") == mid))
    assert d.height == 1, (peer, mid, d.height)
    return d.row(0, named=True)


def _events(ev, episode_id):
    return ev.filter(pl.col("episode_id") == episode_id).sort("event_seq")


def _loss_layer6(lake):
    return pl.read_parquet(lake[0].loss(lake[1]["normalize"]["batch_id"])).filter(pl.col("layer") == 6)


def test_ce1_edited_sl_not_backfilled_and_not_revived(ep, ev, lake):
    """反例 1：导出侧 H1 只见最终版 → 不是决策根（不复活为可执行入场），描述图保留；实收侧 V 版本链 10:25 快照 SL=61000，10:26 编辑不进决策图。"""
    r = _ep(ep, "A", A["CE1_edited_sl"])
    assert r["time_grade_min"] == "H1" and r["t_dec"] is None and r["decision_snapshot_hash"] is None and r["order_plan"] is None
    assert r["eligibility_by_estimand"]["entry_decision"] is False and r["eligibility_by_estimand"]["execution"] is False and r["eligibility_by_estimand"]["original_entry"] is False
    assert "EDIT_ORIGINAL_UNAVAILABLE" in r["reason_codes"] and r["author_plan_state"] == "active"
    d = _ep(ep, "D", A["D_live_edit_v1_v2"])
    assert d["t_dec"] == datetime(2024, 4, 2, 10, 25, 1, tzinfo=UTC) and d["dec_stop"] == 61000.0 and d["order_plan"]["stop"]["price"] == 61000.0 and d["dec_n_events"] == 1
    evs = _events(ev, d["episode_id"])
    amend = evs.filter(pl.col("kind") == "amend").row(0, named=True)
    assert amend["edge_available_at"] == datetime(2024, 4, 2, 10, 26, tzinfo=UTC) and amend["link_method"] == "plan_ref"
    payload = json.loads(amend["payload"])
    assert payload["basis"] == "same_source_version_chain" and payload["stop"] == {"old": "61000.000000000000", "new": "60800.000000000000"} and amend["supersedes_event_id"] == evs["event_id"][0]
    assert amend["available_at"] == datetime(2024, 4, 2, 10, 26, tzinfo=UTC)  # EE.available_at = 源版本 available_at
    assert d["decision_snapshot_hash"] and d["author_plan_state"] == "active"
    q = read_quarantine(lake[0].quarantine_path)
    assert q.filter((pl.col("object_kind") == "canonical_plan") & pl.col("all_reason_codes").list.contains("EDIT_ORIGINAL_UNAVAILABLE")).height >= 1


def test_ce2_unfilled_tp_does_not_close(ep, ev):
    """反例 2（ADR 原输入）：'TP1 到了' 是结果帖，状态不变；'到达 3000 止盈一半' 只是声称减量；exit_observed=false，G1 不生成成交。"""
    r = _ep(ep, "A", A["CE2_unfilled_tp"])
    assert r["author_plan_state"] == "active" and r["author_claim_state"] == "unknown" and r["exit_observed"] is False
    assert r["right_censored"] and r["censor_reason"] == "LABEL_RIGHT_CENSORED" and r["claimed_outcome"] is None
    assert r["order_plan"]["entries"][0]["price_lo"] == 2900.0 and r["order_plan"]["entries"][0]["fraction"] is None and all(t["fraction"] is None for t in r["order_plan"]["tps"])
    evs = _events(ev, r["episode_id"])
    assert evs["kind"].to_list() == ["entry_proposal", "reduce"]  # result_post 不是信号类，不进作者轨事件
    assert json.loads(evs["payload"][1])["action"] == "=" and r["dec_n_events"] == 1


def test_ce3_timeout_only_expires_and_expire_respects_observation_end(ep, ev, tmp_path):
    r = _ep(ep, "A", A["CE3_timeout"])
    assert r["author_plan_state"] == "expired" and r["author_claim_state"] == "unknown" and r["exit_observed"] is False and not r["right_censored"]
    evs = _events(ev, r["episode_id"])
    ex = evs.filter(pl.col("kind") == "expire").row(0, named=True)
    assert json.loads(ex["payload"])["synthesized"] is True and ex["edge_available_at"] == datetime(2024, 5, 21, 9, 0, tzinfo=UTC)
    assert r["order_plan"]["expiry"]["entry_ttl_s"] == 86400 and r["dec_author_plan_state"] == "active"
    # 观察终点早于到期：不生成 expire，P 仍 active，right_censored 于观察终点（S09）
    layout = _LAKE[0]
    cpdf, mv, cb, jd, dg = (pl.read_parquet(layout.canonical_plan), pl.read_parquet(layout.message_version), pl.read_parquet(layout.silver_dir / "candidate_edges.parquet"),
                            pl.read_parquet(layout.silver_dir / "adjudications.parquet"), pl.read_parquet(layout.duplicate_group))
    obs_end = datetime(2024, 5, 20, 10, 0, tzinfo=UTC)
    eps, evs2, _, _, _ = lifecycle.build_graph(cpdf, mv, cb, jd, dg, graph_version="obs-test", ingested_at=T_BUILD, observation_end=obs_end)
    r2 = eps.filter((pl.col("channel_id") == PEER["A"]) & (pl.col("root_message_id") == A["CE3_timeout"])).row(0, named=True)
    assert r2["author_plan_state"] == "active" and r2["right_censored"] and r2["censor_at"] == obs_end
    assert evs2.filter((pl.col("episode_id") == r2["episode_id"]) & (pl.col("kind") == "expire")).height == 0
    # 无期限、无后续 → 只 right_censored，P 仍 active
    assert ep.filter((pl.col("channel_id") == PEER["B"]) & pl.col("right_censored") & (pl.col("author_plan_state") == "active")).height >= 1


_LAKE: list = []


@pytest.fixture(scope="module", autouse=True)
def _keep_lake(lake):
    _LAKE.clear()
    _LAKE.append(lake[0])


def test_ce4_dual_same_side_not_merged_and_ambiguous_mgmt_unresolved(ep, ev, lake):
    a, b = _ep(ep, "A", A["CE4_dual_1"]), _ep(ep, "A", A["CE4_dual_2"])
    assert a["episode_id"] != b["episode_id"] and a["entry_branch_id"] != b["entry_branch_id"]
    ea, eb = _events(ev, a["episode_id"]), _events(ev, b["episode_id"])
    assert ea["kind"].to_list() == ["entry_proposal", "stop_move"] and eb["kind"].to_list() == ["entry_proposal", "reduce"]
    assert json.loads(ea["payload"][1])["stop"]["new"] == "60000.000000000000" and b["order_plan"]["stop"]["price"] == 57800.0
    assert a["order_plan"]["stop"]["price"] == 59200.0 and a["dec_stop"] == 59200.0
    # 无指针的 155 未挂到任何一单，且进 quarantine ENTRY_LINK_AMBIGUOUS（致命）
    cp = pl.read_parquet(lake[0].canonical_plan)
    pid = cp.filter((pl.col("channel_id") == PEER["A"]) & (pl.col("message_id") == A["CE4_ambiguous_mgmt"]) & (pl.col("extractor_name") == "parser"))["plan_id"][0]
    assert ev.filter(pl.col("plan_id") == pid).height == 0
    q = read_quarantine(lake[0].quarantine_path).filter((pl.col("object_id") == pid) & (pl.col("reason_code") == "ENTRY_LINK_AMBIGUOUS"))
    assert q.height == 1 and q["severity"][0] == "fatal"
    assert _loss_layer6(lake)["n_review"].sum() >= 1


def test_ce5_early_management_left_truncated(ep, ev, lake):
    r = _ep(ep, "A", A["CE5_orphan_mgmt"])
    assert r["left_truncated"] and r["entry_observed"] is False and r["author_plan_state"] == "none"
    assert r["author_claim_state"] == "claimed_closed" and r["exit_observed"] is True
    assert r["n_invalid_transitions"] == 0 and "PARENT_MISSING" in r["reason_codes"]
    assert r["order_plan"] is None and r["t_dec"] is None and r["eligibility_by_estimand"]["execution"] is False and r["claimed_outcome"]["kind"] == "close_claimed"
    assert _events(ev, r["episode_id"])["kind"].to_list() == ["stop_move", "close_claimed"]
    q = read_quarantine(lake[0].quarantine_path).filter((pl.col("object_kind") == "episode") & (pl.col("object_id") == r["episode_id"]))
    assert q.height == 1 and q["reason_code"][0] == "PARENT_MISSING"


def test_five_counterexamples_snapshot_hashes_are_stable_and_distinct(ep):
    hs = {k: _ep(ep, "A", A[k])["decision_snapshot_hash"] for k in ("CE2_unfilled_tp", "CE3_timeout", "CE4_dual_1", "CE4_dual_2")}
    assert all(hs.values()) and len(set(hs.values())) == 4
    assert _ep(ep, "A", A["CE1_edited_sl"])["decision_snapshot_hash"] is None and _ep(ep, "A", A["CE5_orphan_mgmt"])["decision_snapshot_hash"] is None


def test_decision_events_only_before_t_dec(ep, ev):
    j = ev.join(ep.select("episode_id", "t_dec"), on="episode_id")
    dec_ev = j.filter(pl.col("t_dec").is_not_null() & (pl.col("edge_available_at") < pl.col("t_dec")))
    assert dec_ev.height >= 5 and (dec_ev["edge_available_at"] < dec_ev["t_dec"]).all()
    assert (dec_ev.group_by("episode_id").len()["len"] == 1).all() and (dec_ev["event_seq"] == 0).all()
    assert j.filter(pl.col("t_dec").is_not_null() & (pl.col("edge_available_at") >= pl.col("t_dec"))).height >= 10


def _with_env(monkeypatch, root):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(root))


def test_api_views_and_manifest_barrier(lake, monkeypatch, tmp_path):
    # 在副本湖上做屏障/篡改/tombstone 试验，不污染模块级共享湖
    src_root = lake[0].bronze_dir.parents[2]
    root = tmp_path / "copy"
    shutil.copytree(src_root, root)
    layout = Layout.from_root(root)
    _with_env(monkeypatch, root)
    d = api.load_episodes("fixture-v1")
    assert d.height >= 5 and d["t_dec"].is_not_null().all() and (~d["exit_observed"]).all() and d["claimed_outcome"].null_count() == d.height
    assert not any(c.startswith("dec_") for c in d.columns)
    assert set(d["author_plan_state"]) == {"active"} and set(d["author_claim_state"]) == {"unknown"} and (d["n_events"] == 1).all()
    assert not d["eligibility_by_estimand"].struct.field("outcome").any()  # A13：决策视图 outcome 恒 false（不泄漏终态）
    assert d["eligibility_by_estimand"].struct.field("entry_decision").all()
    # 契约 §9.7 A5：决策视图 instrument_id / t_dec / cluster_id 非空；单机会自成一簇（cluster_id == episode_id）
    for col in ("instrument_id", "t_dec", "cluster_id"):
        assert d[col].null_count() == 0, col
    single = d.group_by("cluster_id").len().filter(pl.col("len") == 1)  # r4 V02: stable source-family anchor even for a singleton.
    assert single.height >= 1
    full = api.load_episodes("fixture-v1", decision_graph=False)
    assert full["cluster_id"].null_count() == 0
    assert full.height > d.height and full.filter(pl.col("exit_observed")).height >= 5
    # 有致命原因的根不进决策视图（S07）
    assert not d["reason_codes"].list.eval(pl.element().is_in(["UNIT_SCALE_CONFLICT", "SYMBOL_TIME_INVALID", "ENTRY_LINK_AMBIGUOUS"])).list.any().any()
    evd = api.load_episode_events("fixture-v1")
    jj = evd.join(full.select("episode_id", "t_dec"), on="episode_id")
    assert (jj["edge_available_at"] < jj["t_dec"]).all() and api.load_episode_events_description("fixture-v1").height > evd.height
    # manifest 屏障：篡改 EP 文件 → 拒读；缺 manifest → 拒读
    mp = manifest_path(layout, "fixture-v1")
    ep_path = layout.episode("fixture-v1")
    original = ep_path.read_bytes()
    pl.read_parquet(ep_path).head(3).write_parquet(ep_path)
    with pytest.raises(LookupError):
        api.load_episodes("fixture-v1")
    ep_path.write_bytes(original)
    verify_manifest(layout, "fixture-v1")
    saved = mp.read_text()
    mp.unlink()
    with pytest.raises(LookupError):
        api.load_episodes("fixture-v1")
    mp.write_text(saved)
    # tombstone → 研究 API 拒读；旧 Parquet 原样保留；manifest 状态 tombstoned
    tombstone_graph(layout, "fixture-v1", reason_code="CONSENT_REVOKED", approved_by="test", successor_graph_version="fixture-v2", migration_reason="test")
    with pytest.raises(LookupError):
        api.load_episodes("fixture-v1")
    assert ep_path.exists() and json.loads(mp.read_text())["status"] == "tombstoned"


def test_decision_view_invariant_to_future_events_full_row(tmp_path, monkeypatch):
    """S01：追加未来的平仓/管理/复制/补父消息后重建：旧根的决策视图整行（含 decision_snapshot_hash、derivation_hash、n_events、
    reason_codes、time_grade_min、复制组、eligibility）不变；描述视图允许变。"""
    fx = tmp_path / "fx"
    shutil.copytree(FIX, fx)
    la, _ = _build(tmp_path / "a", fx)
    rj = fx / "AlphaSignals" / "result.json"
    doc = json.loads(rj.read_text())
    base = int(datetime(2024, 6, 14, 10, tzinfo=UTC).timestamp())  # 晚于全部锚点根
    doc["messages"] += [
        {"id": 990, "type": "message", "date": "2024-06-14T10:00:00", "date_unixtime": str(base), "from": "Alpha Signals", "from_id": "channel2000000001", "reply_to_message_id": A["CE2_unfilled_tp"], "text": "ETH 多单全部止盈离场。", "text_entities": []},
        {"id": 991, "type": "message", "date": "2024-06-14T11:00:00", "date_unixtime": str(base + 3600), "from": "Alpha Signals", "from_id": "channel2000000001", "reply_to_message_id": A["CE4_dual_1"], "text": "第一单 BTC 多止损上移到 60500。", "text_entities": []},
        {"id": 992, "type": "message", "date": "2024-06-14T12:00:00", "date_unixtime": str(base + 7200), "from": "Alpha Signals", "from_id": "channel2000000001", "text": next(m for m in doc["messages"] if m["id"] == A["CE2_unfilled_tp"])["text"], "text_entities": []},
    ]
    rj.write_text(json.dumps(doc, ensure_ascii=False))
    lb, _ = _build(tmp_path / "b", fx)
    _with_env(monkeypatch, tmp_path / "a")
    da, fa = api.load_episodes("fixture-v1").sort("root_message_id"), api.load_episodes("fixture-v1", decision_graph=False)
    _with_env(monkeypatch, tmp_path / "b")
    db, fb = api.load_episodes("fixture-v1").sort("root_message_id"), api.load_episodes("fixture-v1", decision_graph=False)
    for mid in (A["CE2_unfilled_tp"], A["CE4_dual_1"], A["CE4_dual_2"]):
        key = (pl.col("channel_id") == PEER["A"]) & (pl.col("root_message_id") == mid)
        ra, rb = da.filter(key).drop("ingested_at", "batch_id"), db.filter(key).drop("ingested_at", "batch_id")
        assert ra.height == rb.height == 1
        diff = [c for c in ra.columns if ra[c].to_list() != rb[c].to_list()]
        assert diff == [], (mid, diff)
    key = (pl.col("channel_id") == PEER["A"]) & (pl.col("root_message_id") == A["CE2_unfilled_tp"])
    assert fa.filter(key)["exit_observed"][0] is False and fb.filter(key)["exit_observed"][0] is True and fb.filter(key)["n_events"][0] == fa.filter(key)["n_events"][0] + 1
    assert fa.filter(key)["derivation_hash"][0] != fb.filter(key)["derivation_hash"][0]


def test_dependency_delay_rejects_old_snapshot(tmp_path):
    """S03：推迟根版本可知时刻 / 处理延迟改变 → 快照必须变；根依赖未知 → 无决策。"""
    fx = tmp_path / "fx"
    shutil.copytree(FIX, fx)
    la, ra = _build(tmp_path / "a", fx)
    epa = pl.read_parquet(la.episode("fixture-v1"))
    h_a = epa.filter((pl.col("channel_id") == PEER["A"]) & (pl.col("root_message_id") == A["CE2_unfilled_tp"]))["decision_snapshot_hash"][0]
    # 处理延迟 1→2：快照哈希与 t_dec 都变（同名版本输入不同 → 拒写，需新版本号）
    layout_b = Layout.from_root(tmp_path / "a")
    with pytest.raises(RuntimeError):
        lifecycle.run(layout_b, graph_version="fixture-v1", ingested_at=T_BUILD, processing_delay_s=2)
    s2 = lifecycle.run(layout_b, graph_version="fixture-v1-d2", ingested_at=T_BUILD, processing_delay_s=2)
    epb = pl.read_parquet(layout_b.episode("fixture-v1-d2"))
    rb = epb.filter((pl.col("channel_id") == PEER["A"]) & (pl.col("root_message_id") == A["CE2_unfilled_tp"])).row(0, named=True)
    assert rb["decision_snapshot_hash"] != h_a and rb["t_dec"] == datetime(2024, 5, 6, 8, 1, 2, tzinfo=UTC)
    # 根版本时钟未知（去掉 date_unixtime）→ 无决策根
    rj = fx / "AlphaSignals" / "result.json"
    doc = json.loads(rj.read_text())
    for m in doc["messages"]:
        if m["id"] == A["CE2_unfilled_tp"]:
            m["date_unixtime"] = "n/a"
    rj.write_text(json.dumps(doc, ensure_ascii=False))
    lc, _ = _build(tmp_path / "c", fx)
    rc = pl.read_parquet(lc.episode("fixture-v1")).filter((pl.col("channel_id") == PEER["A"]) & (pl.col("root_message_id") == A["CE2_unfilled_tp"]))
    assert rc.height == 1 and rc["t_dec"][0] is None and rc["decision_snapshot_hash"][0] is None


def test_immutable_graph_version_and_idempotent(lake):
    layout, res = lake
    before = pl.read_parquet(layout.episode("fixture-v1"))
    s2 = lifecycle.run(layout, graph_version="fixture-v1", ingested_at=datetime(2026, 9, 12, tzinfo=UTC))
    after = pl.read_parquet(layout.episode("fixture-v1"))
    assert sorted(after["derivation_hash"]) == sorted(before["derivation_hash"]) and sorted(after["decision_snapshot_hash"].drop_nulls()) == sorted(before["decision_snapshot_hash"].drop_nulls())
    assert set(after["ingested_at"]) == {T_BUILD} and s2["input_hash"] == res["lifecycle"]["input_hash"]
    cp = pl.read_parquet(layout.canonical_plan)
    cp.filter(~((pl.col("extractor_name") == "parser") & (pl.col("kind") == "entry_proposal")).cum_sum().eq(1)).write_parquet(layout.canonical_plan)  # 删一条 parser 提议
    with pytest.raises(RuntimeError):
        lifecycle.run(layout, graph_version="fixture-v1")
    cp.write_parquet(layout.canonical_plan)
    # 同名版本改选边也拒（输入 hash 含选边）
    cbp = layout.silver_dir / "candidate_edges.parquet"
    cb = pl.read_parquet(cbp)
    cb.with_columns(pl.lit(False).alias("selected")).write_parquet(cbp)
    with pytest.raises(RuntimeError):
        lifecycle.run(layout, graph_version="fixture-v1")
    cb.write_parquet(cbp)


def test_loss_six_layers_conserved(lake):
    layout, res = lake
    loss = pl.read_parquet(layout.loss(res["normalize"]["batch_id"]))
    assert set(loss["layer"]) == {1, 2, 3, 4, 5, 6}
    assert (loss["input_n"] == loss["n_ok"] + loss["n_review"] + loss["n_quarantine"] + loss["n_dup_ref"]).all()
    assert (loss.group_by("layer").agg(pl.col("output_n").sum())["output_n"] > 0).all()
    assert loss["ingested_at"].null_count() == 0 and loss["cum_excluded_weight"].null_count() == loss.height
    for L in range(1, 7):
        assert layout.mapping(res["normalize"]["batch_id"], L).exists()
    q = read_quarantine(layout.quarantine_path)
    assert {"episode", "canonical_plan", "message_version", "extracted_event"} <= set(q["object_kind"])


def test_t01_manifest_strictness_and_alias_publish(tmp_path, monkeypatch):
    """T01：files 为空/只有一份/graph_version 不一致/缺必备字段 → 拒读；T04：别名重发布不删旧版本。"""
    fx = FIX
    la, res = _build(tmp_path / "a", fx)
    _with_env(monkeypatch, tmp_path / "a")
    mp = manifest_path(la, "fixture-v1")
    good = json.loads(mp.read_text())
    for bad in ({**good, "files": {}}, {**good, "files": {k: v for k, v in list(good["files"].items())[:1]}}, {**good, "graph_version": "other"}, {k: v for k, v in good.items() if k != "assumptions"}):
        mp.write_text(json.dumps(bad))
        with pytest.raises(LookupError):
            api.load_episodes("fixture-v1")
    mp.write_text(json.dumps(good))
    assert api.load_episodes("fixture-v1").height >= 5
    # 别名发布：不同输入（改 processing_delay）以别名 fixture-v1 重发布 → 新不可变版本 + 别名移动；旧版本文件与 manifest 原样
    old_bytes = la.episode("fixture-v1").read_bytes()
    from quant_lab.data.graph import resolve_alias, set_alias
    s2 = lifecycle.run(la, graph_version="fixture-v1@delay2", ingested_at=T_BUILD, processing_delay_s=2)
    set_alias(la, "fixture-v1", "fixture-v1@delay2")
    assert resolve_alias(la, "fixture-v1") == "fixture-v1@delay2"
    d2 = api.load_episodes("fixture-v1")
    assert (d2["processing_delay_s"] == 2).all() and la.episode("fixture-v1").read_bytes() == old_bytes and manifest_path(la, "fixture-v1").exists()


def test_t02_explicit_tp_fraction_retained(lake):
    """T02：作者明确给出的 TP fraction 原样保留在快照；未知仍 null。"""
    layout = lake[0]
    cp = pl.read_parquet(layout.canonical_plan)
    row = cp.filter((pl.col("channel_id") == PEER["A"]) & (pl.col("message_id") == A["CE2_unfilled_tp"]) & (pl.col("extractor_name") == "parser")).row(0, named=True)
    tps = [dict(t) for t in row["tps"]]
    tps[0]["fraction"] = 0.25
    cp2 = cp.with_columns(pl.when(pl.col("plan_id") == row["plan_id"]).then(pl.lit(tps, dtype=cp.schema["tps"])).otherwise(pl.col("tps")).alias("tps"))
    mv, cb, jd, dg = (pl.read_parquet(layout.message_version), pl.read_parquet(layout.silver_dir / "candidate_edges.parquet"), pl.read_parquet(layout.silver_dir / "adjudications.parquet"), pl.read_parquet(layout.duplicate_group))
    eps, *_ = lifecycle.build_graph(cp2, mv, cb, jd, dg, graph_version="t02", ingested_at=T_BUILD)
    r = eps.filter(pl.col("root_plan_id") == row["plan_id"]).row(0, named=True)
    assert r["order_plan"]["tps"][0]["fraction"] == 0.25 and r["order_plan"]["tps"][1]["fraction"] is None
    base = pl.read_parquet(layout.episode("fixture-v1")).filter(pl.col("root_plan_id") == row["plan_id"]).row(0, named=True)
    assert base["decision_snapshot_hash"] != r["decision_snapshot_hash"]  # fraction 进快照 hash


def test_expire_event_keeps_source_clock(ev, ep):
    r = _ep(ep, "A", A["CE3_timeout"])
    ex = _events(ev, r["episode_id"]).filter(pl.col("kind") == "expire").row(0, named=True)
    assert ex["available_at"] == r["available_at"] or ex["available_at"] == datetime(2024, 5, 20, 9, 1, tzinfo=UTC)
    assert ex["edge_available_at"] == datetime(2024, 5, 21, 9, 0, tzinfo=UTC)


def test_a8_decimal_columns_and_hash_normalization(lake, ep):
    """契约 §9.10.1 A8：gold 数值列 Decimal(38,12)；哈希前定标度 12 串（Decimal('6') 与 Decimal('6.000000000000') 同哈希）。"""
    from decimal import Decimal
    from quant_lab.data.lake import D12, q12, stable_id
    sch = ep.schema
    op = sch["order_plan"]
    assert op.fields[2].dtype.inner.fields[1].dtype == D12 and op.fields[3].dtype.fields[0].dtype == D12 and op.fields[4].dtype.inner.fields[0].dtype == D12
    assert sch["dec_stop"] == D12 and sch["dec_tps"] == pl.List(D12)
    ex = pl.read_parquet(lake[0].extracted_event).schema
    assert ex["tps"].inner.fields[0].dtype == D12 and ex["size_hint"].fields[0].dtype == D12
    assert q12(Decimal("6")) == "6.000000000000" == q12(6.0) == q12("6") and q12(-0.1) == "-0.100000000000"
    assert stable_id("x", {"p": Decimal("6")}) == stable_id("x", {"p": Decimal("6.000000000000")}) == stable_id("x", {"p": 6.0})
    r = _ep(ep, "A", A["CE2_unfilled_tp"])
    assert r["order_plan"]["entries"][0]["price_lo"] == Decimal("2900.000000000000")


def test_a13_eligibility_six_keys_non_null(lake, ep, monkeypatch):
    for k in ("description", "entry_decision", "execution", "original_entry", "price_check", "outcome"):
        assert ep["eligibility_by_estimand"].struct.field(k).null_count() == 0 and ep["dec_eligibility"].struct.field(k).null_count() == 0
    closed = _ep(ep, "A", A["CE5_orphan_mgmt"])
    assert closed["eligibility_by_estimand"]["outcome"] is True and closed["claimed_outcome"]["kind"] == "close_claimed"
    expired = _ep(ep, "A", A["CE3_timeout"])
    assert expired["eligibility_by_estimand"]["outcome"] is True and expired["claimed_outcome"]["kind"] == "expire"
    active = _ep(ep, "A", A["CE2_unfilled_tp"])
    assert active["eligibility_by_estimand"]["outcome"] is False and active["claimed_outcome"] is None
    assert set(ep["claimed_outcome"].struct.field("kind").drop_nulls()) <= set(lifecycle.CLAIMED_OUTCOME_KINDS)
    _with_env(monkeypatch, lake[0].bronze_dir.parents[2])
    d = api.load_episodes("fixture-v1")
    for k in ("description", "entry_decision", "execution", "original_entry", "price_check", "outcome"):
        assert d["eligibility_by_estimand"].struct.field(k).null_count() == 0
    assert not d["eligibility_by_estimand"].struct.field("outcome").any()  # 决策视图恒 false


def test_a9_index_and_a12_cluster_sharing(lake, tmp_path, monkeypatch):
    from quant_lab.data.graph import consumable_versions, read_index, tombstone_graph
    root = tmp_path / "copy"
    shutil.copytree(lake[0].bronze_dir.parents[2], root)
    lay = Layout.from_root(root)
    idx = read_index(lay)
    assert [e["graph_version"] for e in idx] == ["fixture-v1"] and idx[0]["status"] == "published" and idx[0]["input_hash"]
    lifecycle.run(lay, graph_version="fixture-v1@x", ingested_at=T_BUILD + timedelta(hours=1), processing_delay_s=3)
    tombstone_graph(lay, "fixture-v1", reason_code="CONSENT_REVOKED", approved_by="t", successor_graph_version="fixture-v1@x")
    idx = {e["graph_version"]: e for e in read_index(lay)}
    assert consumable_versions(lay) == ["fixture-v1@x"] and idx["fixture-v1"]["status"] == "tombstoned" and idx["fixture-v1"]["successor_graph_version"] == "fixture-v1@x" and idx["fixture-v1"]["tombstoned_at"]
    assert [e["built_at"] for e in read_index(lay)] == sorted(e["built_at"] for e in read_index(lay))
    # A12：同频道重发候选与原帖共享 cluster_id（有效样本量不膨胀）
    monkeypatch.setattr(api, "_layout", lambda: lake[0])
    ep = api.load_episodes("fixture-v1")  # r4 §7: default view, nonempty matched pairs.
    dg = pl.read_parquet(lake[0].duplicate_group)
    candidates = dg.filter(pl.col("dup_kind") == "repost_same_channel")
    checked = 0
    for group in candidates["duplicate_group_id"].unique():
        members = dg.filter(pl.col("duplicate_group_id") == group)["source_version_id"].to_list()
        pair = ep.filter(pl.col("root_source_version_id").is_in(members))
        if pair.height < 2:
            continue
        assert pair["cluster_id"].n_unique() == 1
        checked += 1
    assert checked > 0 and ep.height > 0  # r4 §7: no empty-set success.


def test_a15_reconstructed_outcome_seven_kinds_and_censor_reason(ep):
    from quant_lab.data.lifecycle import RECONSTRUCTED_OUTCOME_KINDS, RECONSTRUCTED_OUTCOME_SCHEMA, validate_reconstructed_outcome
    assert set(RECONSTRUCTED_OUTCOME_KINDS) == {"filled_closed", "unfilled_expired", "stopped", "tp_hit", "right_censored", "unevaluable", "rejected"}
    assert ep.schema["reconstructed_outcome"] == RECONSTRUCTED_OUTCOME_SCHEMA and "censor_reason" in [f.name for f in ep.schema["reconstructed_outcome"].fields]
    assert ep["reconstructed_outcome"].null_count() == ep.height  # G1 初次发布不回填
    assert validate_reconstructed_outcome({"kind": "unevaluable", "censor_reason": "MARK_STALE"}) == []
    assert validate_reconstructed_outcome({"kind": "unevaluable", "censor_reason": None}) == ["unevaluable_without_censor_reason"]
    assert validate_reconstructed_outcome({"kind": "right_censored_legacy"}) == ["bad_kind:'right_censored_legacy'"]
    assert set(lifecycle.CLAIMED_OUTCOME_KINDS) == {"close_claimed", "cancel", "expire"}


def test_t01_manifest_value_schema(tmp_path, monkeypatch):
    la, _ = _build(tmp_path / "a", FIX)
    _with_env(monkeypatch, tmp_path / "a")
    mp = manifest_path(la, "fixture-v1")
    good = json.loads(mp.read_text())
    for k, bad in (("input_hash", None), ("input_hash", "zz"), ("rule_versions", {}), ("rule_versions", None), ("assumptions", None), ("counts", {}), ("counts", {"episodes": -1}), ("graph_kind", "other"), ("built_at", "not-a-time"), ("files", {"episode__fixture-v1.parquet": "nothex", "episode_event__fixture-v1.parquet": good["files"]["episode_event__fixture-v1.parquet"]})):
        mp.write_text(json.dumps({**good, k: bad}))
        with pytest.raises(LookupError):
            api.load_episodes("fixture-v1")
    mp.write_text(json.dumps(good))
    assert api.load_episodes("fixture-v1").height >= 5


def test_s10_mv_clock_change_changes_input_hash(tmp_path):
    la, ra = _build(tmp_path / "a", FIX)
    h1 = lifecycle.input_hash_of(la, ingested_at=T_BUILD)
    mvp = la.message_version
    mv = pl.read_parquet(mvp)
    root_sv = pl.read_parquet(la.episode("fixture-v1")).filter((pl.col("channel_id") == PEER["A"]) & (pl.col("root_message_id") == A["CE2_unfilled_tp"]))["root_source_version_id"][0]
    mv.with_columns(pl.when(pl.col("source_version_id") == root_sv).then(pl.col("available_at") + timedelta(days=1)).otherwise(pl.col("available_at")).alias("available_at")).write_parquet(mvp)
    assert lifecycle.input_hash_of(la, ingested_at=T_BUILD) != h1
    with pytest.raises(RuntimeError):
        lifecycle.run(la, graph_version="fixture-v1", ingested_at=T_BUILD)
