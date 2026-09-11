"""D-06 层 4/5：品种当时身份、方向/档位、数量级门 δ≥ln3、合理性带 T_plaus、MARK_STALE（含 G2 asof 真实接缝）、H1 隔离、损耗与幂等。"""
from __future__ import annotations

import json
import math
import pathlib
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from quant_lab.data import dedup, extract, normalize, validate
from quant_lab.data.lake import Layout, read_quarantine
from quant_lab.data.market_stub import FrameMarks, PlausibilityCalibration, SyntheticMarks, fixture_marks, fixture_registry, instrument_id_for
from quant_lab.data.validate import canonicalize_row, market_check_row

FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample"
LLM_FIX = pathlib.Path(__file__).parent / "fixtures" / "llm_recorded" / "extract_v1.json"
ANCH = json.loads((FIX / "ANCHORS.json").read_text())
PEER, A = ANCH["peers"], ANCH["anchors"]
T0 = datetime(2024, 6, 10, 9, 1, tzinfo=UTC)
BTC = instrument_id_for("BTC")


def _ex_row(**kw):
    base = {"symbol_raw": "BTC", "side": "long", "entry": {"lo": 60000.0, "hi": 60300.0, "kind": "zone"}, "entries": [], "stop": 59200.0,
            "tps": [{"level": 62000.0, "fraction": None, "kind": "price"}, {"level": 61000.0, "fraction": None, "kind": "price"}],
            "available_at": T0, "time_grade": "H0", "kind": "entry_proposal"}
    base.update(kw)
    return base


def test_instrument_time_identity():
    reg = fixture_registry()
    assert reg.resolve("BTC", T0) == (BTC, "mapped")
    assert reg.resolve("ZRO", datetime(2024, 3, 5, tzinfo=UTC))[1] == "invalid_time"  # 上市前
    assert reg.resolve("ZRO", datetime(2024, 7, 1, tzinfo=UTC))[1] == "mapped"
    assert reg.resolve("MATIC", datetime(2024, 9, 1, tzinfo=UTC))[0] == instrument_id_for("MATIC")
    assert reg.resolve("MATIC", datetime(2024, 10, 1, tzinfo=UTC))[0] == instrument_id_for("POL")  # 改名后的当时身份
    assert reg.resolve("XYZ", T0)[1] == "unknown_symbol" and reg.resolve("BTC", None)[1] == "time_unknown"
    can, reasons, _ = canonicalize_row(_ex_row(symbol_raw="ZRO", available_at=datetime(2024, 3, 5, tzinfo=UTC)), registry=reg)
    assert "SYMBOL_TIME_INVALID" in reasons and can["instrument_id"] is None


def test_canonicalize_direction_and_tp_order():
    reg = fixture_registry()
    can, reasons, checks = canonicalize_row(_ex_row(), registry=reg)
    assert [t["level"] for t in can["tps"]] == [61000.0, 62000.0] and can["direction_ok"] and not reasons
    assert can["entry_ref"] == 60150.0
    can, reasons, checks = canonicalize_row(_ex_row(stop=61000.0), registry=reg)  # long 的 SL 在入场上方
    assert not can["direction_ok"] and "INTENT_AMBIGUOUS" in reasons and can["stop"] == 61000.0  # 只标不改
    can, _, checks = canonicalize_row(_ex_row(tps=[{"level": 5.0, "fraction": None, "kind": "pct"}]), registry=reg)
    assert math.isclose(can["tps"][0]["level"], 60150.0 * 1.05) and checks["tp_pct_converted"] == 1
    can, reasons, checks = canonicalize_row(_ex_row(time_grade="H1"), registry=reg)
    assert "EDIT_ORIGINAL_UNAVAILABLE" in reasons and checks["eligibility"]["original_entry"] is False


def test_scale_gate_ln3():
    marks = fixture_marks()
    reg = fixture_registry()
    can, _, _ = canonicalize_row(_ex_row(entry={"lo": 747.0, "hi": 747.0, "kind": "limit"}, stop=727.0, tps=[]), registry=reg)
    mk, reasons, checks = market_check_row(can, t_a=T0, marks=marks, t_plaus=None, channel_id=1)
    assert mk["scale_gate"] == "conflict" and "UNIT_SCALE_CONFLICT" in reasons and mk["delta_near"] > math.log(3)
    assert can["entry"]["lo"] == 747.0  # 不自动乘除
    can, _, _ = canonicalize_row(_ex_row(), registry=reg)
    mk, reasons, _ = market_check_row(can, t_a=T0, marks=marks, t_plaus=None, channel_id=1)
    assert mk["scale_gate"] == "ok" and mk["delta_near"] < 0.01 and "UNIT_SCALE_CONFLICT" not in reasons
    # A04：远端也逐一过门（近端合理、远端 ≥ 3 倍 → 冲突）
    wide, _, _ = canonicalize_row(_ex_row(entry={"lo": 60000.0, "hi": 600000.0, "kind": "zone"}, stop=59000.0, tps=[]), registry=reg)
    mk, reasons, _ = market_check_row(wide, t_a=T0, marks=marks, t_plaus=None, channel_id=1)
    assert mk["scale_gate"] == "conflict" and "UNIT_SCALE_CONFLICT" in reasons
    # SL 数量级错也触发
    can, _, _ = canonicalize_row(_ex_row(stop=59.2), registry=reg)
    mk, reasons, checks = market_check_row(can, t_a=T0, marks=marks, t_plaus=None, channel_id=1)
    assert "UNIT_SCALE_CONFLICT" in reasons and checks["scale_conflict_field"] == "stop"


def test_plausibility_band_frozen_or_insufficient():
    marks, reg = fixture_marks(), fixture_registry()
    can, _, _ = canonicalize_row(_ex_row(), registry=reg)
    mk, reasons, checks = market_check_row(can, t_a=T0, marks=marks, t_plaus=None, channel_id=1)
    assert mk["plausibility_status"] == "insufficient" and "ENTRY_MARK_DEVIATION" not in reasons  # 无冻结阈值不猜
    frozen = PlausibilityCalibration({(1, "zone"): 0.05}, version="cal-v1", frozen_at=T0 - timedelta(days=30), n_samples={(1, "zone"): 80})
    mk, reasons, _ = market_check_row(can, t_a=T0, marks=marks, t_plaus=frozen, channel_id=1)
    assert mk["plausibility_status"] == "ok" and mk["t_plaus"] == 0.05 and mk["calibration_version"] == "cal-v1"
    far, _, _ = canonicalize_row(_ex_row(entry={"lo": 55000.0, "hi": 55300.0, "kind": "zone"}, stop=54000.0, tps=[]), registry=reg)
    mk, reasons, _ = market_check_row(far, t_a=T0, marks=marks, t_plaus=frozen, channel_id=1)
    assert mk["plausibility_status"] == "deviation" and "ENTRY_MARK_DEVIATION" in reasons and 0.05 < mk["delta_near"] < math.log(3)
    pooled = PlausibilityCalibration({(0, "zone"): 0.2}, version="cal-pooled", frozen_at=T0 - timedelta(days=1), n_samples={(0, "zone"): 300})
    mk, _, checks = market_check_row(far, t_a=T0, marks=marks, t_plaus=pooled, channel_id=1)  # 合并分布回退
    assert mk["plausibility_status"] == "ok" and checks["plausibility"] == "pooled"
    # 未来才冻结的校准不可用（S04）；样本不足退到合并分布
    future = PlausibilityCalibration({(1, "zone"): 0.05}, version="cal-future", frozen_at=T0 + timedelta(days=1), n_samples={(1, "zone"): 80})
    mk, reasons, checks = market_check_row(far, t_a=T0, marks=marks, t_plaus=future, channel_id=1)
    assert mk["plausibility_status"] == "insufficient" and checks["plausibility"] == "calibration_not_yet_frozen" and "ENTRY_MARK_DEVIATION" not in reasons
    small = PlausibilityCalibration({(1, "zone"): 0.05, (0, "zone"): 0.2}, version="cal-small", frozen_at=T0 - timedelta(days=1), n_samples={(1, "zone"): 10, (0, "zone"): 300})
    mk, _, checks = market_check_row(far, t_a=T0, marks=marks, t_plaus=small, channel_id=1)
    assert mk["t_plaus"] == 0.2 and checks["plausibility"] == "pooled"
    # 无样本量记录的阈值不可用（S04 残余）
    nosamp = PlausibilityCalibration({(1, "zone"): 0.05}, version="cal-nosamp", frozen_at=T0 - timedelta(days=1))
    mk, _, checks = market_check_row(far, t_a=T0, marks=marks, t_plaus=nosamp, channel_id=1)
    assert mk["plausibility_status"] == "insufficient"
    # 裸字典 = 未冻结 → 不可用
    mk, _, _ = market_check_row(far, t_a=T0, marks=marks, t_plaus={(1, "zone"): 0.05}, channel_id=1)
    assert mk["plausibility_status"] == "insufficient"


def test_mark_stale_synthetic_gap_and_g2_asof_frame():
    marks, reg = fixture_marks(), fixture_registry()
    can, _, _ = canonicalize_row(_ex_row(), registry=reg)
    in_gap = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    mk, reasons, checks = market_check_row(can, t_a=in_gap, marks=marks, t_plaus=None, channel_id=1)
    assert "MARK_STALE" in reasons and mk["mark_price"] is None and mk["scale_gate"] == "unavailable" and mk["mark_staleness_s"] > 120
    # 真实接缝：G2 asof.mark_bar_at 读 silver 1m bars；bar 早于 t_a 超过 120s → MARK_STALE；未闭合 bar 不可见
    bars = marks.bars(BTC, T0 - timedelta(minutes=10), T0 + timedelta(minutes=5))
    fm = FrameMarks(bars, manifest="test-bars")
    ok = fm.mark_at(BTC, T0 + timedelta(seconds=30))
    assert ok.price is not None
    assert ok.close_time <= T0 + timedelta(seconds=30)
    stale = fm.mark_at(BTC, T0 + timedelta(minutes=15))
    assert stale.price is None and stale.reason == "MARK_STALE"
    # 未闭合：at 恰在 bar 内部，只能看到上一根
    mid = T0.replace(second=0) + timedelta(seconds=20)
    m2 = fm.mark_at(BTC, mid)
    assert m2.close_time <= mid
    # S04：已闭合但当时不可知（available_at 在未来 / 最终修订晚到）的 bar 不得被读到
    late = bars.with_columns(pl.when(pl.col("close_time") == T0.replace(second=0)).then(pl.lit(T0 + timedelta(days=1))).otherwise(pl.col("available_at")).alias("available_at"))
    fm2 = FrameMarks(late, manifest="late-bars")
    m3 = fm2.mark_at(BTC, T0 + timedelta(seconds=30))
    assert m3.close_time < T0.replace(second=0)  # 跳过晚到那根，取更早已知的
    single = pl.DataFrame({"instrument_id": [BTC], "interval": ["1m"], "open_time": [T0 - timedelta(minutes=2)], "close_time": [T0 - timedelta(minutes=1)], "open": [123.0], "high": [123.0], "low": [123.0], "close": [123.0], "volume": [0.0],
                           "event_time": [T0 - timedelta(minutes=1)], "available_at": [T0 + timedelta(days=1)], "ingested_at": [T0]}).with_columns(pl.col("^(.*time.*|available_at|ingested_at)$").cast(pl.Datetime("us", "UTC")))
    assert FrameMarks(single).mark_at(BTC, T0).price is None
    assert FrameMarks(single.with_columns(pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("available_at"))).mark_at(BTC, T0).price is None  # 可知时刻未知=不可知
    nocol = FrameMarks(bars.drop("available_at")).mark_at(BTC, T0 + timedelta(seconds=30))
    assert nocol.price is None and nocol.reason == "MARK_STALE"  # 无可知时刻列的行情表整体不可用
    # 合成锚点是阶梯函数：不用未来锚点插值
    sm = SyntheticMarks({BTC: [(T0 - timedelta(days=1), 100.0), (T0 + timedelta(days=1), 200.0)]})
    assert float(sm.mark_at(BTC, T0).price) == 100.0


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    layout = Layout.flat(tmp_path_factory.mktemp("validate"))
    ts = datetime(2026, 9, 11, tzinfo=UTC)
    normalize.run(FIX, layout, ingested_at=ts)
    dedup.run(layout, ingested_at=ts)
    extract.run(layout, llm_fixture=LLM_FIX, ingested_at=ts)
    with pytest.raises(ValueError):
        validate.run(layout, ingested_at=ts)  # 非夹具模式必须显式给 marks/registry（S04）
    summary = validate.run(layout, ingested_at=ts, synthetic=True)
    return layout, summary


@pytest.fixture(scope="module")
def cp(lake):
    return pl.read_parquet(lake[0].canonical_plan)


def _p(cp, peer, mid):
    d = cp.filter((pl.col("channel_id") == PEER[peer]) & (pl.col("message_id") == mid) & (pl.col("extractor_name") == "parser"))
    assert d.height == 1
    return d.row(0, named=True)


def test_pipeline_canonical_plans(cp, lake):
    assert cp.height >= 20 and cp["plan_id"].n_unique() == cp.height
    r = _p(cp, "A", A["CE1_edited_sl"])
    assert r["instrument_id"] == BTC and r["time_grade"] == "H1" and "EDIT_ORIGINAL_UNAVAILABLE" in r["reason_codes"]
    assert json.loads(r["eligibility_by_estimand"])["original_entry"] is False and r["t_a"] == datetime(2025, 3, 1, tzinfo=UTC)
    r = _p(cp, "A", A["CE2_unfilled_tp"])
    assert r["entry_ref"] == 2902.5 and r["mark_price"] > 3000 and r["scale_gate"] == "ok" and r["plausibility_status"] == "insufficient"
    r = _p(cp, "B", A["B_album_inferred"])  # ZRO 上市前 → 品种当时身份无效
    assert r["instrument_status"] == "invalid_time" and "SYMBOL_TIME_INVALID" in r["reason_codes"] and json.loads(r["eligibility_by_estimand"])["execution"] is False
    r = _p(cp, "A", A["A_btc_short_wan"])  # 万单位：6.48-6.53万 → 64800-65300 vs mark 65200
    assert r["entry"]["lo"] == 64800.0 and r["scale_gate"] == "ok"
    assert lake[1]["scale_gate"].get("ok", 0) >= 10


def test_direction_conflict_and_inherited_reasons_block_execution(cp):
    """S07：方向冲突 / 继承的隔离原因 → execution=false。"""
    bad = cp.filter(~pl.col("direction_ok"))
    for r in bad.iter_rows(named=True):
        assert json.loads(r["eligibility_by_estimand"])["execution"] is False
    inherited = cp.filter(pl.col("reason_codes").list.contains("TIME_UNIT_INVALID") | pl.col("reason_codes").list.contains("TEXT_IMAGE_CONFLICT"))
    for r in inherited.iter_rows(named=True):
        assert json.loads(r["eligibility_by_estimand"])["execution"] is False


def test_pipeline_quarantine_loss_idempotent(lake):
    layout, s1 = lake
    q = read_quarantine(layout.quarantine_path).filter(pl.col("object_kind") == "canonical_plan")
    assert q.height >= 2 and {"SYMBOL_TIME_INVALID", "EDIT_ORIGINAL_UNAVAILABLE"} <= set(q["all_reason_codes"].explode())
    assert q.filter(pl.col("reason_code") == "SYMBOL_TIME_INVALID")["severity"][0] == "fatal"
    # S07：混合一般+致命 → severity=fatal（主原因致命优先）
    mixed = q.filter(pl.col("all_reason_codes").list.contains("UNIT_SCALE_CONFLICT") & (pl.col("all_reason_codes").list.len() > 1))
    assert mixed.height == 0 or (mixed["severity"] == "fatal").all()
    loss = pl.read_parquet(layout.loss(s1["batch_id"]))
    assert set(loss["layer"]) == {1, 2, 3, 4, 5}
    l4, l5 = loss.filter(pl.col("layer") == 4), loss.filter(pl.col("layer") == 5)
    assert l4["input_n"].sum() == pl.read_parquet(layout.extracted_event).height and l5["output_n"].sum() >= 10
    assert (loss["input_n"] == loss["n_ok"] + loss["n_review"] + loss["n_quarantine"] + loss["n_dup_ref"]).all()
    # 累计排除按 stratum 逐层不下降
    for st, g in loss.group_by("stratum"):
        cum = g.sort("layer")["cum_excluded_ids"].to_list()
        assert all(b >= a for a, b in zip(cum, cum[1:])), (st, cum)
    before = pl.read_parquet(layout.canonical_plan)
    s2 = validate.run(layout, ingested_at=datetime(2026, 9, 12, tzinfo=UTC), synthetic=True)
    after = pl.read_parquet(layout.canonical_plan)
    assert sorted(after["plan_id"]) == sorted(before["plan_id"]) and s2["scale_gate"] == s1["scale_gate"]
    assert read_quarantine(layout.quarantine_path).filter(pl.col("object_kind") == "canonical_plan").height == q.height


def test_a10_scale_basis_recorded(lake):
    layout, _ = lake
    cp = pl.read_parquet(layout.canonical_plan)
    conf = cp.filter(pl.col("scale_gate") == "conflict")
    assert conf.height >= 1
    for r in conf.iter_rows(named=True):
        assert json.loads(r["checks"]).get("scale_basis") in ("far_end_only", "near_end_only", "both_ends") or json.loads(r["checks"]).get("scale_conflict_field") in ("stop", "tps")
    q = read_quarantine(layout.quarantine_path).filter(pl.col("all_reason_codes").list.contains("UNIT_SCALE_CONFLICT"))
    assert q.height >= 1 and all(json.loads(v).get("basis") in (None, "far_end_only", "near_end_only", "both_ends") for v in q["observed_value_ref"])
    loss5 = pl.read_parquet(layout.loss(_["batch_id"] if isinstance(_, dict) else lake[1]["batch_id"])).filter(pl.col("layer") == 5)
    keys = set()
    for d in loss5["primary_reason_dist"]:
        keys |= set(json.loads(d))
    assert any(k.startswith("UNIT_SCALE_CONFLICT") for k in keys)
    # 远端触门用例：basis=far_end_only
    reg, marks = fixture_registry(), fixture_marks()
    wide, _, _ = canonicalize_row(_ex_row(entry={"lo": 60000.0, "hi": 600000.0, "kind": "zone"}, stop=59000.0, tps=[]), registry=reg)
    mk, reasons, checks = market_check_row(wide, t_a=T0, marks=marks, t_plaus=None, channel_id=1)
    assert checks["scale_basis"] == "far_end_only" and checks["scale_conflict_end"] == "entries[0].price_hi"
