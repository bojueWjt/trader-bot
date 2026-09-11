"""D-05 层 3 抽取：解析器分类/字段/span、单位、bench 报告、LLM 录制回放与证据拒收、落盘与损耗。"""
from __future__ import annotations

import json
from decimal import Decimal  # r4 §8 V01: exact decimal expectations.
import pathlib
from datetime import UTC, datetime

import polars as pl
import pytest

from quant_lab.data import dedup, extract, normalize
from quant_lab.data.extract import parse_message, parse_number_token, run_bench
from quant_lab.data.lake import Layout, read_quarantine
from quant_lab.data.llm import NoNetworkClient, RecordedClient, RecordedOcr, TransportError, call_with_retry, gate, validate_evidence
from quant_lab.data.extract import apply_ocr

FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample"
LLM_FIX = pathlib.Path(__file__).parent / "fixtures" / "llm_recorded" / "extract_v1.json"
OCR_FIX = pathlib.Path(__file__).parent / "fixtures" / "llm_recorded" / "ocr_v1.json"
ANCH = json.loads((FIX / "ANCHORS.json").read_text())
PEER, A = ANCH["peers"], ANCH["anchors"]


def _spans_ok(text: str, res) -> None:
    for s in res.spans:
        assert 0 <= s["start"] < s["end"] <= len(text), s
        if s["field"] in ("stop", "entry.lo", "entry.hi", "tps", "entries"):
            assert parse_number_token(text[s["start"]:s["end"]]) is not None, (s, text[s["start"]:s["end"]])


def test_shuqin_zone_template():
    t = "ETH\n方向：做多\n入场：2295-2315附近\n信心度：中\n倍数：10倍\n仓位：10%\n止盈：点位1：2360附近（求稳） 点位2：2430附近  点位3：2530\n止损：小幅跌破2268一点。\n理由：支撑。"
    r = parse_message(t)
    assert r.kind == "entry_proposal" and r.symbol_raw == "ETH" and r.side == "long"
    assert r.entry == {"lo": 2295.0, "hi": 2315.0, "kind": "zone"} and r.stop == 2268.0
    assert [x["level"] for x in r.tps] == [2360.0, 2430.0, 2530.0]
    assert r.size_hint["leverage"] == 10 and r.size_hint["fraction"] == Decimal("0.1")
    assert r.confidence_bucket == "high"
    _spans_ok(t, r)
    stop_span = next(s for s in r.spans if s["field"] == "stop")
    assert t[stop_span["start"]:stop_span["end"]] == "2268"


def test_wan_unit_and_propagation():
    t = "比特币\n方向：做多\n入场：7.36-7.42万附近\n止盈：点位1：7.56附近（求稳） 点位2：7.7附近  点位3：7.91\n止损：小幅跌破7.28万一点。"
    r = parse_message(t)
    assert r.symbol_raw == "BTC" and r.entry == {"lo": 73600.0, "hi": 74200.0, "kind": "zone"}
    assert r.stop == 72800.0
    assert [x["level"] for x in r.tps] == [75600.0, 77000.0, 79100.0]
    assert any(s["field"] == "unit_anchor" for s in r.spans) and any(n.startswith("unit_propagated") for n in r.notes)


def test_shorthand_not_guessed():
    """'747' 无单位缩写不猜倍数（留给层 5 数量级门）。"""
    r = parse_message("大饼今天如果测试747 小仓位试一个多单 防守727")
    assert r.kind == "entry_proposal" and r.symbol_raw == "BTC" and r.side == "long"
    assert r.stop == 727.0 and r.entry and r.entry["lo"] == 747.0


def test_titan_ladder_and_gauls():
    r = parse_message("我的 #ZRO 止损买入区域\n\n👉 首次入场价：> (0.7102)\n👉 第二次入场价：> (0.688)\n\n🚨 我的止损价：(0.6666) 美元 区域。")
    assert r.kind == "entry_proposal" and r.symbol_raw == "ZRO" and r.side == "long"
    assert r.entry["kind"] == "ladder" and r.entries == [Decimal("0.7102"), Decimal("0.688")] and r.stop == Decimal("0.6666")
    r2 = parse_message("$ETHFI 购买策略\n\n入场价：当前价格和 3612\n目标价：4542\n止损价：3446")
    assert r2.symbol_raw == "ETHFI" and r2.side == "long" and r2.entries == [3612.0] and r2.stop == 3446.0 and [x["level"] for x in r2.tps] == [4542.0]


def test_management_kinds_symbol_nearest_stop():
    r = parse_message("🔥 #LONG（做多）交易计划更新 🔥\nBTC 和 ETH 都实现了 2.5% 的上涨。\n现在把 ETH 的止损上移到1875保本，平仓20%。")
    assert r.kind == "stop_move" and r.stop == 1875.0 and r.symbol_raw == "ETH"
    assert parse_message("$TIA 交易更新：\n👉 在止损位附近平仓，尽量减少损失。\n👉 亏损 -3%，止损比例为 0.5 倍。").kind == "close_claimed"
    r3 = parse_message("我的 $BTC 交易更新 🔥\n我之前在 64,570-64,900 区间的 $BTC 做空操作。\n👉 现在锁定 10% 的利润，并将我的止损位调整到盈亏平衡点（入场价）或略高于 65,000 美元。")
    assert r3.kind == "stop_move" and r3.stop == 65000.0 and r3.side == "short"
    assert parse_message("美光这里进个空单\n止损在1011\n止盈看781").kind == "entry_proposal"
    r5 = parse_message("刚起 大饼被套200点 空个sol吧一直不喜欢sol 108止损")
    assert r5.kind == "entry_proposal" and r5.symbol_raw == "SOL" and r5.side == "short" and r5.stop == 108.0


def test_negatives_and_expiry():
    assert parse_message("发300u 在dc 爱你们").kind == "chatter"
    assert parse_message("MIGRATION TEST 2026-08-17. This is a non-trading connectivity check. Do not call trading tools").kind in ("chatter", "analysis")
    assert parse_message("高倍合约的话，再观察下，等临近支撑位我就会发单了，目前还没到，不要急。").kind in ("analysis", "chatter")
    r = parse_message("#JTO 交易更新\n\n目标 1 已正式达成。价格精准地支撑在 0.5872 附近。")
    assert r.kind == "result_post"
    r2 = parse_message("")
    assert r2.kind == "undecidable" and "OCR_UNREADABLE" in r2.reason_codes
    r3 = parse_message("SOL\n方向：做空\n入场：150-152附近\n止损：小幅涨破156一点\n本单 24 小时内有效，未成交自动作废。")
    assert r3.kind == "entry_proposal" and r3.expires_after_s == 86400
    r4 = parse_message("BTC 做多也行，做空也行，看突破方向。")
    assert "INTENT_AMBIGUOUS" in r4.reason_codes


def test_bench_report():
    with pytest.raises(FileNotFoundError):
        run_bench("../eval/hermes/v3-trader-signal-bench")  # 旧路径不存在：显式报错，不静默回退
    rep = run_bench(pathlib.Path(__file__).resolve().parents[3] / "eval" / "v3_trader_signal_bench")
    assert rep["n_items"] >= 30 and rep["parser_recall"] > 0
    assert rep["parser_recall"] >= 0.6 and rep["skip_recall"] >= 0.8
    co = rep["co_error_with_recorded_llm"]["configs"]
    assert co and all("action" in v for v in co.values())


def test_llm_evidence_validation():
    text = "ETH 做多 入场 2295-2315 止损 2268"
    ok = {"kind": "entry_proposal", "entry": {"lo": 2295, "hi": 2315}, "stop": 2268,
          "spans": [{"field": "entry.lo", "start": 10, "end": 14}, {"field": "entry.hi", "start": 15, "end": 19}, {"field": "stop", "start": 23, "end": 27}]}
    assert validate_evidence(ok, text) == []
    bad = {"kind": "entry_proposal", "stop": 2268, "spans": [{"field": "stop", "start": 10, "end": 14}]}
    assert any(e.startswith("span_mismatch") for e in validate_evidence(bad, text))
    none = {"kind": "entry_proposal", "stop": 2268, "spans": []}
    assert any(e.startswith("missing_span") for e in validate_evidence(none, text))
    # 非法类型不抛异常，整条拒收（S13）
    assert any(e.startswith("bad_type") for e in validate_evidence({"kind": "entry_proposal", "stop": "abc", "spans": []}, text))
    assert any(e.startswith("bad_type") for e in validate_evidence({"kind": "entry_proposal", "entry": "2295", "spans": []}, text))
    assert validate_evidence("not a dict", text) == ["payload_not_object"]
    assert any(e.startswith("bad_type") for e in validate_evidence({"kind": "x", "size_hint": {"leverage": "ten"}, "spans": []}, text))


def test_gate_and_retry(monkeypatch):
    class Real:
        name, version = "openai-real", "x"

        def complete_json(self, **kw):
            raise AssertionError("must not be called")

    monkeypatch.delenv("QUANT_LAB_ALLOW_LLM", raising=False)
    with pytest.raises(PermissionError):
        gate(Real())
    assert gate(None).name == "no_network"
    from quant_lab.data.llm import SCHEMA_NAME_EXTRACT, build_extract_prompt, record_key
    system, user = build_extract_prompt("x", channel_name="c", message_date=None)
    key = record_key(system, user, SCHEMA_NAME_EXTRACT)
    rc = RecordedClient({key: {"transport_error": {"times": 3}, "response": {"kind": "chatter", "spans": []}}})
    out, attempts = call_with_retry(rc, system=system, user=user, schema_name=SCHEMA_NAME_EXTRACT)
    assert out.__class__.__name__ == "Abstention" and attempts == 3  # 两次重试后放弃
    rc2 = RecordedClient({key: {"transport_error": {"times": 1}, "response": {"kind": "chatter", "spans": []}}})
    out2, attempts2 = call_with_retry(rc2, system=system, user=user, schema_name=SCHEMA_NAME_EXTRACT)
    assert attempts2 == 2 and out2.payload["kind"] == "chatter"


def test_ocr_pure_image_and_text_image_conflict():
    ocr = RecordedOcr.from_file(OCR_FIX)
    import hashlib
    fixdoc = json.loads((FIX / "BetaTrades" / "result.json").read_text())
    def h(mid):
        m = next(x for x in fixdoc["messages"] if x["id"] == mid)
        return hashlib.sha256((FIX / "BetaTrades" / m["photo"]).read_bytes()).hexdigest()
    pure = apply_ocr(parse_message(""), "", [h(A["B_pure_image"])], ocr)
    assert pure.kind == "entry_proposal" and pure.entry == {"lo": 64000.0, "hi": 64500.0, "kind": "zone"} and pure.stop == 63000.0
    assert all(sp["source"] == "ocr" for sp in pure.spans) and {b["field"] for b in pure.bboxes} >= {"entry.lo", "entry.hi", "stop"}
    assert "OCR_UNREADABLE" not in pure.reason_codes
    # 未录制图片 → 拒答保留
    unread = apply_ocr(parse_message(""), "", ["deadbeef" * 8], ocr)
    assert unread.kind == "undecidable" and "OCR_UNREADABLE" in unread.reason_codes
    # 图文冲突：文字 SL Decimal("5.998") vs 图 5.5
    text = next(x for x in fixdoc["messages"] if x["id"] == A["B_album_explicit"])["text"]
    r = apply_ocr(parse_message(text), text, [h(A["B_album_explicit"])], ocr)
    assert "TEXT_IMAGE_CONFLICT" in r.reason_codes and r.stop == Decimal("5.998")  # 只标不改
    # 图文一致：无冲突且数字带 bbox
    text2 = next(x for x in fixdoc["messages"] if x["id"] == A["B_edited_photo_signal"])["text"]
    r2 = apply_ocr(parse_message(text2), text2, [h(A["B_edited_photo_signal"])], ocr)
    assert "TEXT_IMAGE_CONFLICT" not in r2.reason_codes and any(b["field"] == "stop" for b in r2.bboxes)
    # 未接 OCR：not_run（不等于一致）
    from quant_lab.data.llm import NoOcr
    r3 = apply_ocr(parse_message(""), "", [h(A["B_pure_image"])], NoOcr())
    assert r3.kind == "undecidable" and "ocr_not_run" in r3.notes


def test_no_network_default_gated(monkeypatch):
    monkeypatch.delenv("QUANT_LAB_ALLOW_LLM", raising=False)
    out = NoNetworkClient().complete_json(system="s", user="u", schema_name="x")
    assert out.__class__.__name__ == "Abstention"
    monkeypatch.setenv("QUANT_LAB_ALLOW_LLM", "1")
    with pytest.raises(NotImplementedError):
        NoNetworkClient().complete_json(system="s", user="u", schema_name="x")


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    layout = Layout.flat(tmp_path_factory.mktemp("extract"))
    ts = datetime(2026, 9, 11, tzinfo=UTC)
    normalize.run(FIX, layout, ingested_at=ts)
    dedup.run(layout, ingested_at=ts)
    summary = extract.run(layout, llm_fixture=LLM_FIX, ocr_fixture=OCR_FIX, ingested_at=ts)
    return layout, summary


@pytest.fixture(scope="module")
def ex(lake) -> pl.DataFrame:
    return pl.read_parquet(lake[0].extracted_event)


def _parser_row(ex, peer, mid):
    d = ex.filter((pl.col("channel_id") == PEER[peer]) & (pl.col("message_id") == mid) & (pl.col("extractor").struct.field("name") == "parser"))
    assert d.height == 1
    return d.row(0, named=True)


def test_pipeline_rows_and_anchors(ex, lake):
    assert ex.height > 150
    assert ex["extract_id"].n_unique() == ex.height
    for c in ("event_time", "available_at", "ingested_at"):
        assert ex.schema[c] == pl.Datetime("us", "UTC")
    r = _parser_row(ex, "A", A["CE1_edited_sl"])
    assert r["kind"] == "entry_proposal" and r["stop"] == 60800.0 and r["side"] == "long" and r["symbol_raw"] == "BTC"
    assert r["version_no"] == 1 and r["available_at"] == datetime(2025, 3, 1, tzinfo=UTC)
    r = _parser_row(ex, "A", A["CE5_orphan_mgmt"])
    assert r["kind"] == "stop_move" and r["stop"] == 65000.0 and r["symbol_raw"] == "BTC"
    assert _parser_row(ex, "A", A["CE2_unfilled_tp"] + 1)["kind"] == "result_post"  # "TP1 到了"
    assert _parser_row(ex, "A", A["CE2_unfilled_tp"] + 2)["kind"] == "reduce"
    assert _parser_row(ex, "A", A["CE2_unfilled_tp"])["entry_mode"] == "price"
    # 层 1 隔离原因跨层携带（S07）
    r = _parser_row(ex, "C", A["C_bad_time"])
    assert "TIME_UNIT_INVALID" in r["reason_codes"]
    # 纯图经录制 OCR 解析成计划，span 来源 ocr
    r = _parser_row(ex, "B", A["B_pure_image"])
    assert r["kind"] == "entry_proposal" and all(sp["source"] == "ocr" for sp in r["spans"]) and len(r["bboxes"]) >= 3
    r = _parser_row(ex, "A", A["CE3_timeout"])
    assert r["expires_after_s"] == 86400 and r["kind"] == "entry_proposal"
    kinds = lake[1]["kind_dist"]
    assert kinds.get("entry_proposal", 0) >= 12 and kinds.get("chatter", 0) >= 10


def test_llm_rows_recorded_and_rejected(ex, lake):
    llm = ex.filter(pl.col("extractor").struct.field("name") == "llm")
    assert lake[1]["llm"]["rows"] >= 4 and lake[1]["llm"]["rejected"] >= 2 and lake[1]["llm"]["abstain"] >= 3
    good = llm.filter((pl.col("channel_id") == PEER["A"]) & (pl.col("message_id") == A["A_eth_1"])).row(0, named=True)
    assert good["kind"] == "entry_proposal" and good["stop"] == 3268.0 and good["extractor"]["model"] == "synthetic-recorded"
    bad = llm.filter((pl.col("channel_id") == PEER["B"]) & (pl.col("message_id") == A["B_pure_image"])).row(0, named=True)
    assert bad["kind"] == "undecidable"  # 伪造数字无 span → 拒收
    bad2 = llm.filter((pl.col("channel_id") == PEER["B"]) & (pl.col("message_id") == A["B_album_inferred"])).row(0, named=True)
    assert bad2["kind"] == "undecidable"  # span 错位 → 拒收
    # parser 与 LLM 各自成行，不投票
    both = ex.filter((pl.col("channel_id") == PEER["A"]) & (pl.col("message_id") == A["A_eth_1"]))
    assert both.height == 2 and set(both["extractor"].struct.field("name")) == {"parser", "llm"}
    assert lake[1]["co_error"]["n_pairs"] >= 4


def test_loss_layer3_and_quarantine(lake):
    layout, summary = lake
    loss = pl.read_parquet(layout.loss(summary["batch_id"])).filter(pl.col("layer") == 3)
    assert loss.height >= 4 and loss["input_n"].sum() == summary["n_canonical"]
    assert loss["output_n"].sum() >= 12 and loss["n_review"].sum() >= 1
    assert (loss["input_n"] == loss["n_ok"] + loss["n_review"] + loss["n_quarantine"] + loss["n_dup_ref"]).all()
    m = pl.read_parquet(layout.mapping(summary["batch_id"], 3))
    assert m.filter(pl.col("relation") == "split").height == summary["llm"]["rows"] + summary["llm"]["abstain"] + pl.read_parquet(layout.extracted_event).filter((pl.col("extractor").struct.field("name") == "parser") & (pl.col("branch_index") > 0)).height  # LLM 一入多出走映射账
    q = read_quarantine(layout.quarantine_path).filter(pl.col("object_kind") == "extracted_event")
    assert q.height >= 1 and set(q["reason_code"]) <= {"INTENT_AMBIGUOUS", "OCR_UNREADABLE", "TEXT_IMAGE_CONFLICT", "TIME_UNIT_INVALID", "MEDIA_MISSING", "SCHEMA_DRIFT"}


def test_idempotent(lake):
    layout, s1 = lake
    before = pl.read_parquet(layout.extracted_event)
    s2 = extract.run(layout, llm_fixture=LLM_FIX, ocr_fixture=OCR_FIX, ingested_at=datetime(2026, 9, 12, tzinfo=UTC))
    after = pl.read_parquet(layout.extracted_event)
    assert after.height == before.height and sorted(after["extract_id"]) == sorted(before["extract_id"])
    assert set(after["ingested_at"]) == {datetime(2026, 9, 11, tzinfo=UTC)}  # 重跑保留首次入湖时刻
    assert s2["kind_dist"] == s1["kind_dist"]


def test_unit_propagation_only_from_price_fields():
    """非价格数字的单位（成交量 6.5万）不得放大入场/止损（S06，换行版与同句分号版都要过）。"""
    r = parse_message("BTC 历史成交量 6.5万，方向：做多，入场：6附近，止损：5")
    assert r.entry["lo"] == 6.0 and r.stop == 5.0 and not any(n.startswith("unit_propagated") for n in r.notes)
    r = parse_message("BTC 做多 入场 6 止损 5 止盈 7；历史成交量 6.5万")
    assert r.entry["lo"] == 6.0 and r.stop == 5.0 and [t["level"] for t in r.tps] == [7.0]
    r = parse_message("BTC 做多 入场 6 止损 5 止盈 7\n历史成交量 6.5万")
    assert r.entry["lo"] == 6.0 and r.stop == 5.0 and [t["level"] for t in r.tps] == [7.0]


def test_market_ref_needs_evidence_and_no_default_fraction():
    """S06：无入场数字且无市价证据 → entry_mode=unknown（不猜市价）；有"这里进"证据 → market_ref。"""
    assert parse_message("BTC 做多 止损 90").entry_mode == "unknown"
    assert parse_message("美光这里进个空单\n止损在1011").entry_mode == "market_ref"
    r = parse_message("ETH\n方向：做多\n入场：2295-2315附近\n止盈：点位1：2360附近 点位2：2430附近\n止损：2268")
    assert all(t["fraction"] is None for t in r.tps)
    r2 = parse_message("BTC 方向：做多 入场：6.4-6.5万附近 止损：6.2 止盈：点位1：6.8")
    assert r2.entry == {"lo": 64000.0, "hi": 65000.0, "kind": "zone"} and r2.stop == 62000.0 and [t["level"] for t in r2.tps] == [68000.0]


def test_t03_ocr_bbox_validation():
    from quant_lab.data.llm import RecordedOcr, valid_ocr_number
    assert valid_ocr_number({"value": 1, "bbox": [0, 0, 2, 2]}, [8, 8])
    for bad in ({"value": 1, "bbox": [-1, 0, 2, 1]}, {"value": 1, "bbox": [0]}, {"value": 1, "bbox": [2, 2, 1, 1]}, {"value": 1, "bbox": [0, 0, 9, 1]}, {"value": "x", "bbox": [0, 0, 1, 1]}, {"value": float("nan")}):
        assert not valid_ocr_number(bad, [8, 8]), bad
    ocr = RecordedOcr({"h": {"size": [8, 8], "text": "BTC 做多 入场 5 止损 4", "numbers": [{"value": 5, "bbox": [0]}, {"value": 4, "bbox": [-1, 0, 2, 1]}]}})
    r = apply_ocr(parse_message(""), "", ["h"], ocr)  # 非法 bbox 不中断：仍能解析文本，只是没有 bbox
    assert r.kind == "undecidable" and r.bboxes == [] and r.entry is None and r.stop is None and r.reason_codes  # r4 §6/§7: no bbox, no numeric evidence.


def test_u01_ocr_size_validation_and_s06_cross_symbol_anchor():
    from quant_lab.data.llm import RecordedOcr, valid_size
    for bad in ([8], ["bad", 8], [8, float("nan")], [], [0, 8], [-1, 8], [8, float("inf")], "8x8"):
        assert not valid_size(bad), bad
        r = RecordedOcr({"h": {"size": bad, "text": "x", "numbers": [{"value": 100, "bbox": [0, 0, 1, 999]}]}}).read("h")
        assert r.status == "unreadable" and r.numbers == []
    assert valid_size([8, 8]) and RecordedOcr({"h": {"size": [8, 8], "text": "x", "numbers": [{"value": 1, "bbox": [0, 0, 1, 1]}]}}).read("h").numbers
    r = parse_message("FIL 做多 入场 6 止损 5 止盈 7 BTC 阻力 6.5万")
    assert r.entry["lo"] == 6.0 and r.stop == 5.0 and [t["level"] for t in r.tps] == [7.0] and r.symbol_raw == "FIL"
