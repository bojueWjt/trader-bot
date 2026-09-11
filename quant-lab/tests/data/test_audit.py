"""D-09 标注与分级放行：OC 表数值（合并稿 B.4 逐位）、Wilson、放行决策（致命零容忍/全审/重验上限）、全窗口抽样框与 π 加权召回、金标分歧率门、Label Studio 模板与任务导出、元数据。"""
from __future__ import annotations

import math
import pathlib
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from quant_lab.data import api, audit
from quant_lab.data.audit import (AcceptancePlan, acceptance_decision, binom_accept, build_frame, dataset_metadata, disagreement_report, frame_hash, hypergeom_accept,
                                  label_studio_tasks, sample_identity, sample_windows, weighted_recall, wilson, window_of)

SID = sample_identity([f"e{i}" for i in range(200)])
REV = ("reviewer-a",)


def _acc(**kw):
    kw.setdefault("sample_ids_hash", SID)
    kw.setdefault("reviewer_ids", REV)
    return acceptance_decision(**kw)
from quant_lab.data.lake import Layout

FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample"


def test_oc_table_matches_merged_plan():
    assert math.isclose(binom_accept(200, 3, 0.005), 0.981319, abs_tol=5e-7)
    assert math.isclose(binom_accept(400, 0, 0.005), 0.134658, abs_tol=5e-7)
    assert math.isclose(binom_accept(200, 3, 0.03), 0.147151, abs_tol=5e-7)
    assert math.isclose(binom_accept(400, 0, 0.03), 0.00000511, abs_tol=5e-9)
    assert math.isclose(binom_accept(200, 3, 0.01), 0.858034, abs_tol=5e-7) and math.isclose(binom_accept(400, 0, 0.001), 0.670186, abs_tol=5e-7)
    # 有限批超几何 ≠ 二项，大批量趋近
    assert abs(hypergeom_accept(100000, 500, 200, 3) - binom_accept(200, 3, 0.005)) < 1e-3
    assert hypergeom_accept(300, 3, 200, 3) > binom_accept(200, 3, 0.01)


def test_wilson_two_sided():
    lo, hi = wilson(0, 400)
    assert abs(lo) < 1e-12 and math.isclose(hi, 0.0095126, abs_tol=5e-7)
    assert math.isclose(wilson(0, 60)[1], 0.0601739, abs_tol=5e-7) and math.isclose(wilson(0, 200)[1], 0.0188460, abs_tol=5e-7)
    assert math.isclose(wilson(1, 400)[1], 0.0140236, abs_tol=5e-7)
    with pytest.raises(ValueError):
        wilson(0, 0)


def test_acceptance_decisions():
    plan = AcceptancePlan()
    assert _acc(n_sampled=200, errors_general=3, errors_fatal=0, plan=plan).result == "pass"
    r = _acc(n_sampled=200, errors_general=4, errors_fatal=0, plan=plan)
    assert r.result == "fail" and r.interval[1] > 0.02
    assert _acc(n_sampled=200, errors_general=0, errors_fatal=1, plan=plan).result == "fail"  # 致命零容忍
    assert _acc(n_sampled=150, errors_general=0, errors_fatal=0, plan=plan, batch_size=150).result == "full_audit_required"
    assert _acc(n_sampled=150, errors_general=0, errors_fatal=0, plan=plan).result == "insufficient"
    assert _acc(n_sampled=200, errors_general=0, errors_fatal=0, plan=plan, attempt=3).result == "insufficient"  # 最多一次重验
    ok = _acc(n_sampled=200, errors_general=2, errors_fatal=0, plan=plan)
    assert ok.p_hat == 0.01 and ok.interval_method == "wilson_95_two_sided"  # 接受后仍记录非零错误估计
    # S14：缺固定样本身份 / 缺独立复核人 / 复核人=产线作者 → not_run，不能 pass
    assert acceptance_decision(n_sampled=200, errors_general=0, errors_fatal=0, plan=plan).result == "not_run"
    assert acceptance_decision(n_sampled=200, errors_general=0, errors_fatal=0, plan=plan, sample_ids_hash=SID, reviewer_ids=()).result == "not_run"
    assert acceptance_decision(n_sampled=200, errors_general=0, errors_fatal=0, plan=plan, sample_ids_hash=SID, reviewer_ids=("x",), producer_ids=("x",)).result == "not_run"


def test_frame_sampling_and_weighted_recall():
    t0, t1 = datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 3, 11, tzinfo=UTC)
    frame = build_frame([1, 2], t0, t1)
    assert len(frame) == 2 * 40 and frame[0].start == t0 and frame[1].start == t0 + timedelta(hours=6)
    s1, s2 = sample_windows(frame, seed=1), sample_windows(frame, seed=1)
    assert s1 == s2 and len(s1) == 60 and all(math.isclose(pi, 30 / 40) for pi in s1.values())  # 每频道 30 窗，π=30/40
    assert frame_hash(frame) == frame_hash(build_frame([1, 2], t0, t1))
    w = window_of(frame, 1, datetime(2024, 3, 2, 7, 30, tzinfo=UTC))
    assert w.start == datetime(2024, 3, 2, 6, tzinfo=UTC)
    # 全部窗口抽中（π=1）时等于普通召回；"同窗 BTC 识别、ETH 漏掉"
    full = sample_windows(frame, per_channel=40)
    key = w.key
    truth = [(key, "btc-1"), (key, "eth-1"), (frame[9].key, "sol-1")]
    r = weighted_recall(full, truth, {"btc-1", "sol-1"})
    assert r["status"] == "ok" and math.isclose(r["recall"], 2 / 3) and r["n_true"] == 3
    # π 不等时按 1/π 加权
    part = {key: 0.5, frame[9].key: 1.0}
    r2 = weighted_recall(part, truth, {"btc-1", "sol-1"})
    assert math.isclose(r2["recall"], (2 + 1) / (4 + 1))
    assert weighted_recall(part, [], {"x"})["status"] == "insufficient"
    # 未抽中窗口的机会不进分母
    r3 = weighted_recall({key: 1.0}, truth, {"btc-1"})
    assert r3["n_true"] == 2 and math.isclose(r3["recall"], 0.5)
    # S14：零机会的负窗口也参与重采样计数
    three = {key: 1.0, frame[9].key: 1.0, frame[12].key: 1.0}
    r4 = weighted_recall(three, truth, {"btc-1", "sol-1"})
    assert r4["n_windows"] == 3 and r4["n_windows_with_opportunity"] == 2


def test_disagreement_report_gates():
    a = [{"episode_id": f"e{i}", "instrument_id": "BTC", "side": "long", "entry": "1", "stop": "2", "kind": "entry_proposal"} for i in range(50)]
    b = [dict(x) for x in a]
    b[0]["side"] = "short"  # 1/50 = 2% 关键字段分歧（恰在门上）
    rep = disagreement_report(a, b)
    assert rep["n"] == 50 and rep["verdict"] == "pass" and math.isclose(rep["field_disagreement"]["side"], 0.02)
    b[1]["stop"] = "3"
    b[2]["unresolved"] = True
    rep = disagreement_report(a, b)
    assert rep["verdict"] == "pause" and rep["unresolved_rate"] == 0.02 and rep["whole_episode_rate"] == 2 / 50
    assert disagreement_report([], [])["status"] == "insufficient"
    # S14：只有 episode_id 的空标签 / 缺配对 / 单边缺字段 不能 pass
    empty = [{"episode_id": f"e{i}"} for i in range(10)]
    assert disagreement_report(empty, [dict(x) for x in empty])["status"] == "insufficient"
    assert disagreement_report(a, a[:40])["status"] == "insufficient"
    c = [dict(x) for x in a]
    del c[0]["stop"]
    rep2 = disagreement_report(a, c)
    assert rep2["status"] == "ok" and rep2["unresolved_rate"] == 1 / 50


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    layout = Layout.flat(tmp_path_factory.mktemp("audit"))
    api.build(FIX, layout, graph_version="fixture-v1", ingested_at=datetime(2026, 9, 11, tzinfo=UTC))
    return layout


def test_label_studio_template_and_tasks(lake):
    assert "<Repeater" in audit.LABEL_STUDIO_CONFIG and "$events" in audit.LABEL_STUDIO_CONFIG
    ep = pl.read_parquet(lake.episode("fixture-v1"))
    ev = pl.read_parquet(lake.episode_event("fixture-v1"))
    mv = pl.read_parquet(lake.message_version)
    tasks = label_studio_tasks(ep, ev, mv)
    assert len(tasks) == ep.height and all(t["data"]["root_text"] is not None for t in tasks)
    multi = [t for t in tasks if t["data"]["events"]]
    assert multi and all(set(t["data"]) == {"episode_id", "channel_id", "root_text", "events"} for t in tasks)  # 盲标：不带模型品种/归属/预测（S14）
    s = str(tasks)
    assert "reconstructed_outcome" not in s and "mark_price" not in s and "author_plan_state" not in s and "instrument_id" not in s


def test_dataset_metadata_fields(lake):
    frame = build_frame([1], datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 3, 2, tzinfo=UTC))
    res = _acc(n_sampled=200, errors_general=1, errors_fatal=0)
    md = dataset_metadata(batch_id="tg-x", frame=frame, plan=AcceptancePlan(), seed=1, result=res, review_hours=3.5, excluded_layers=["low_confidence"], low_confidence_rule="confidence_bucket=low", attempt_history=["v1:pass"], sample_ids_hash=SID, reviewer_ids=REV)
    for k in ("audit_plan_version", "batch_id", "frame_hash", "n", "c", "seed", "error_count_by_severity", "p_hat", "interval_method", "interval", "acceptance_result", "attempt_history", "review_hours", "excluded_layers", "low_confidence_rule"):
        assert k in md
    assert md["risk_acknowledged_by_user"] is False and md["acceptance_result"] == "pass" and md["sample_ids_hash"] == SID and md["reviewer_ids"] == ["reviewer-a"]


def test_s14_residuals():
    a = [{"episode_id": f"e{i}", "instrument_id": None, "side": None, "entry": None, "stop": None, "kind": None} for i in range(10)]
    rep = disagreement_report(a, [dict(x) for x in a])
    assert rep["status"] == "insufficient" or rep["verdict"] != "pass"
    assert _acc(n_sampled=201, errors_general=0, errors_fatal=0).result == "insufficient"
    assert _acc(n_sampled=200, errors_general=0, errors_fatal=0, attempt=0).result == "insufficient"
    assert acceptance_decision(n_sampled=200, errors_general=0, errors_fatal=0, sample_ids_hash="", reviewer_ids=("",)).result == "not_run"


def test_s14_partial_key_fields_not_pass():
    a = [{"episode_id": f"e{i}", "kind": "entry_proposal"} for i in range(50)]
    rep = disagreement_report(a, [dict(x) for x in a])
    assert rep["status"] == "insufficient" or rep["verdict"] != "pass"
