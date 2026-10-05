"""v8 看板口径：按定量方式分块、v8 新列、变体图、单笔复算带止损消息原文。全部仿写数据。"""
from datetime import timedelta
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

import polars as pl
import pytest

import quant_lab.viz.data as data_module
from quant_lab.market.l0_replay import replay, summarize
from quant_lab.viz.data import Dashboard, dup_members, net_U, statistics_for
from tests.data.l0_fixtures import CHANNEL, T0

MAIN = "base-v1-timeexit-w60-live-follow-ns300"
W1 = "base-v1-timeexit-w1-live-follow-ns300"
REPORT_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "v8_report.py"


def load_v8_report():
    """The frozen judgment script as its own module (it does not import quant_lab)."""
    if "v8_report" not in sys.modules:
        spec = importlib.util.spec_from_file_location("v8_report", REPORT_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules["v8_report"] = module            # dataclass 需要能按模块名找到自己
        spec.loader.exec_module(module)
    return sys.modules["v8_report"]


def row(eid, net, day=0, **changes):
    return {"episode_id": eid, "t_dec": T0 + timedelta(days=day), "net_R": Decimal(net), "fill_status": "filled",
            "censor_reason": None, "mark_ok": True, "funding_ok": True, "rules_ok": True, "bars_ok": True,
            "risk_budget": Decimal("180"), **changes}


def run(synthetic, policy, directory):
    previous = os.environ.get("QUANT_LAB_DATA_ROOT")
    os.environ["QUANT_LAB_DATA_ROOT"] = str(synthetic["root"])
    try:
        return replay(graph_version="viz-test", channel=CHANNEL, out=synthetic["reports"] / directory / "demo",
                      market_lake=synthetic["lake"], policy_version=policy, risk_budget=Decimal("180"),
                      followup_actions=synthetic["follow_path"])
    finally:
        if previous is None:
            os.environ.pop("QUANT_LAB_DATA_ROOT", None)
        else:
            os.environ["QUANT_LAB_DATA_ROOT"] = previous


@pytest.fixture(scope="module")
def v8(synthetic, tmp_path_factory):
    reports = synthetic["reports"]
    run(synthetic, MAIN, "l0-v8-w60lf-ns300")
    run(synthetic, MAIN, "l0-v8e")
    run(synthetic, W1, "l0-v8-w1-ns300")
    # 把 w1 档的第一笔改成无止损定量的行，再用 L0 自己的 summarize 重算 summary（overall 只算按风险定量的行）。
    folder = reports / "l0-v8-w1-ns300" / "demo"
    table = pl.read_parquet(folder / "trades.parquet")
    flipped = table["episode_id"][0]
    table = table.with_columns(pl.when(pl.col("episode_id") == flipped).then(pl.lit("nostop"))
                               .otherwise(pl.col("sizing_basis")).alias("sizing_basis"))
    table.write_parquet(folder / "trades.parquet")
    summary = json.loads((folder / "summary.json").read_text())
    summary.update(json.loads(json.dumps(summarize(table), default=str)))
    (folder / "summary.json").write_text(json.dumps(summary))
    config = {"reports": str(reports), "tag": "v8", "market_lake": str(synthetic["lake"]), "channels": [
        {"key": "demo", "name": "合成老师", "channel_id": CHANNEL, "data_root": str(synthetic["root"]), "graph_version": "viz-test"}]}
    path = tmp_path_factory.mktemp("v8-config") / "dashboard.json"
    path.write_text(json.dumps(config))
    return {"config": path, "flipped": flipped, "folder": folder, "config_doc": config}


def test_statistics_split_by_sizing_basis_and_total_is_money_only():
    rows = [row("a", "1"), row("b", "-0.5", 1), row("n1", "0.5", sizing_basis="nostop"),
            row("n2", "-0.25", 1, sizing_basis="nostop"), row("n3", "9", sizing_basis="nostop", censor_reason="LABEL_RIGHT_CENSORED")]
    stats = statistics_for(rows)
    assert stats["n"] == 2 and stats["mean_R"] == 0.25 and stats["sum_R"] == 0.5 and stats["win_rate"] == 0.5
    assert [p["episode_id"] for p in stats["cumulative_R"]] == ["a", "b"]           # 无止损单不进累计 R
    nostop = stats["blocks"]["nostop"]
    assert nostop["n"] == 2 and nostop["mean_U"] == pytest.approx(22.5) and nostop["sum_U"] == 45
    assert stats["blocks"]["total"]["sum_net_U"] == pytest.approx(0.5 * 180 + 45)
    assert set(stats["blocks"]["total"]) == {"sum_net_U", "note"}                  # 合计没有均值和胜率
    assert "仅" in stats["blocks"]["note"] and "内核 A" in stats["blocks"]["note"]
    # 旧报告（没有 sizing_basis）输出不变：没有 blocks。
    assert "blocks" not in statistics_for([row("a", "1"), row("b", "2", 1)])
    # 没有 risk_budget 不能换成金额：整块为空，不当成 0。
    unknown = statistics_for([row("a", "1"), row("n", "1", sizing_basis="nostop", risk_budget=None)])
    assert unknown["blocks"]["total"]["sum_net_U"] is None and unknown["blocks"]["nostop"]["status"] == "risk_budget 缺失"
    assert net_U(row("x", "0.5")) == Decimal("90") and net_U(row("x", "1", risk_budget=None)) is None


def test_v8_variants_discovered_and_summary_with_blocks_accepted(v8):
    dashboard = Dashboard(v8["config"])
    variants = dashboard.variants()
    assert list(variants)[0] == "w60lf-ns300"                         # 主口径在第一位（频道页默认）
    assert {"w1-ns300", "5d-ns300", "w14-ns300", "v8e", "v8w", "v8nw"} <= set(variants)
    assert variants["v8e"]["suffix"] == "e" and "base" not in variants   # v8 不带 v7 的内置口径
    stats = dashboard.overview()["channels"][0]["statistics"]
    assert stats["w60lf-ns300"]["status"] == "已出" and stats["v8e"]["status"] == "已出"
    assert stats["v8w"]["status"] == "未出" and stats["5d-ns300"]["status"] == "未出"
    # overall 只算按风险定量的行：parquet 行数 = overall + blocks.nostop，看板据此认为是一对完整产物。
    w1 = stats["w1-ns300"]
    assert w1["status"] == "已出" and "blocks" in w1
    rows = pl.read_parquet(v8["folder"] / "trades.parquet").to_dicts()
    flipped = next(r for r in rows if r["episode_id"] == v8["flipped"])
    assert w1["blocks"]["nostop"]["n"] == int(data_module.evaluable(flipped))
    assert w1["n"] == sum(data_module.evaluable(r) for r in rows if r["episode_id"] != v8["flipped"])
    summary_path = v8["folder"] / "summary.json"
    original = summary_path.read_text()
    try:
        summary = json.loads(original)
        summary["blocks"]["nostop"]["n_trades"] += 1
        summary_path.write_text(json.dumps(summary))
        assert dashboard.report("demo", "w1-ns300")[0] is None
    finally:
        summary_path.write_text(original)
    assert dashboard.report("demo", "w1-ns300")[0] is not None


def test_channel_list_carries_v8_columns(v8):
    dashboard = Dashboard(v8["config"])
    data = dashboard.trades("demo")
    trade = next(t for t in data["trades"] if t["episode_id"] == v8["flipped"])
    record = trade["variants"]["w1-ns300"]
    assert record["sizing_basis"] == "nostop"
    assert record["net_U"] == (None if record["net_R"] is None else record["net_R"] * Decimal("180"))
    risk = trade["variants"]["w60lf-ns300"]
    assert risk["sizing_basis"] == "risk" and "legs_n" in risk
    # 报告里没有的列不写成空字段；图没有 dup_of 不写合并数，而不是写 0。
    # 集成（B 组合入后）：合成图由 G1 建，带 v8 列与 dup_of；断言随报告/图的实际列走，两个方向都查。
    columns = set(pl.read_parquet(v8["config_doc"]["reports"] + "/l0-v8-w60lf-ns300/demo/trades.parquet").columns)
    for field in ("plan_link_kind", "triage_verdict"):
        assert (field in risk) == (field in columns)
    graph_counts = data_module.dup_counts(dashboard.channel("demo")["layout"], "viz-test", CHANNEL)
    assert ("n_dup_members" in risk) == (graph_counts is not None)
    assert "n_dup_members" not in trade and "内核 A" in data["nostop_note"]


def test_trade_records_carry_only_the_reports_own_v8_columns(dashboard, tmp_path):
    # M9：频道页的每个口径记录只带该报告真有的 v8 列；旧报告（v8 之前的 L0，没有这些列）不带一串空字段。
    data = dashboard.trades("demo")
    for variant in data["variants"]:
        _, rows = dashboard.report("demo", variant)
        columns = set(rows[0]) if rows else set()
        for trade in data["trades"]:
            record = trade["variants"].get(variant)
            if record is not None:
                assert set(record) & set(data_module.V8_TRADE_FIELDS) <= columns
    # 仿一份 v8 之前的报告：去掉 v8 列后，记录里没有任何 v8 字段，也没有 sizing_basis/net_U/n_dup_members。
    source = dashboard.reports / "l0-v7" / "demo"
    reports = tmp_path / "reports"
    folder = reports / "l0-v7" / "demo"
    folder.mkdir(parents=True)
    frame = pl.read_parquet(source / "trades.parquet")
    frame.drop([c for c in frame.columns if c in data_module.V8_TRADE_FIELDS]).write_parquet(folder / "trades.parquet")
    summary = json.loads((source / "summary.json").read_text())
    summary.pop("blocks", None)
    (folder / "summary.json").write_text(json.dumps(summary, default=str))
    doc = json.loads(dashboard.config_path.read_text())
    doc["reports"] = str(reports)
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(doc))
    old = Dashboard(path)
    records = [r for t in old.trades("demo")["trades"] for r in t["variants"].values()]
    assert records
    # 合并数取自图的 dup_of，不取自报告：集成后合成图由 G1 v8 建、带 dup_of，此时合并数照写；图没有 dup_of 时不写。
    graph_has_dup = data_module.dup_counts(old.channel("demo")["layout"], "viz-test", CHANNEL) is not None
    for record in records:
        assert not set(data_module.V8_TRADE_FIELDS) & set(record)
        assert not {"sizing_basis", "net_U"} & set(record)
        assert ("n_dup_members" in record) == graph_has_dup


def test_dup_members_from_g1_dup_of():
    frame = pl.DataFrame({"episode_id": ["k", "d1", "d2", "x"], "root_message_id": [1, 2, 3, 4],
                          "plan_link_kind": ["root", "companion", "restatement", "root"], "dup_of": [None, "k", "k", ""],
                          "t_dec": [T0, None, T0, T0]})
    members = dup_members(frame)
    assert list(members) == ["k"] and [m["episode_id"] for m in members["k"]] == ["d2", "d1"]   # 时刻未知置末
    assert {m["plan_link_kind"] for m in members["k"]} == {"companion", "restatement"}
    assert dup_members(frame.drop("dup_of")) == {} and dup_members(None) == {}


def g1_columns(frame, eid, stop_source, other, *, merged=True):
    """仿 B 组 gold 新列（eager 或 lazy 都行）：本单的止损来自另一条消息；`merged` 时另几张单被并入本单。"""
    into = pl.col("episode_id").is_in(other) if merged else pl.lit(False)
    return frame.with_columns(
        pl.when(pl.col("episode_id") == eid).then(pl.lit(stop_source)).otherwise(None).alias("stop_source_version_id"),
        pl.when(pl.col("episode_id") == eid).then(pl.lit("root")).otherwise(pl.lit("companion")).alias("plan_link_kind"),
        pl.when(into).then(pl.lit(eid)).otherwise(None).alias("dup_of"))


def synthetic_graph(synthetic, eid):
    layout = data_module.Layout.from_root(synthetic["root"])
    graph = data_module.load_episodes("viz-test", decision_graph=False, layout=layout).filter(pl.col("channel_id") == CHANNEL)
    other = [e for e in graph["episode_id"].to_list() if e != eid]
    assert other                                   # 合成频道里还有别的单可以并入
    return layout, other


def test_detail_passes_stop_message_text_and_shows_plan_link(v8, synthetic, monkeypatch):
    eid = synthetic["episode"]["episode_id"]
    layout, other = synthetic_graph(synthetic, eid)
    versions = pl.read_parquet(layout.message_version).to_dicts()
    stop_source = next(r["source_version_id"] for r in versions
                       if r["channel_id"] == CHANNEL and r["source_id"]["message_id"] == 10)
    original_load, original_scan = data_module.load_episodes, data_module.scan_episodes
    original_prepare = data_module.prepare_episode_request

    def load(graph_version, *, decision_graph=True, layout=None):
        return g1_columns(original_load(graph_version, decision_graph=decision_graph, layout=layout), eid, stop_source, other)

    def scan(layout, graph_version):
        frame = original_scan(layout, graph_version)
        return None if frame is None else g1_columns(frame, eid, stop_source, other)

    seen = {}

    def prepare(row, **kwargs):
        seen.update(stop_text=kwargs.get("stop_text"), text=kwargs.get("text"))
        return original_prepare(row, **kwargs)

    monkeypatch.setattr(data_module, "load_episodes", load)
    monkeypatch.setattr(data_module, "scan_episodes", scan)
    monkeypatch.setattr(data_module, "prepare_episode_request", prepare)
    dashboard = Dashboard(v8["config"])
    detail = dashboard.detail("demo", "w60lf-ns300", eid)
    assert seen["stop_text"] == "BTC 合成管理消息"
    assert detail["plan_link"]["stop_source_version_id"] == stop_source and detail["plan_link"]["plan_link_kind"] == "root"
    assert {m["episode_id"] for m in detail["dup_members"]} == set(other)
    assert detail["sizing"]["sizing_basis"] == "risk" and "note" not in detail["sizing"]
    assert detail["consistency"]["ok"] is True
    listed = {t["episode_id"]: t["variants"]["w60lf-ns300"]["n_dup_members"] for t in dashboard.trades("demo")["trades"]}
    assert listed[eid] == len(other) and all(listed[e] == 0 for e in listed if e != eid)


def copy_report(source_reports, target_reports, directory, graph_version):
    """A copy of a published L0 run moved onto another graph version (rows and summary), parquet before summary."""
    folder = target_reports / directory / "demo"
    folder.mkdir(parents=True)
    pl.read_parquet(source_reports / directory / "demo" / "trades.parquet").with_columns(
        pl.lit(graph_version).alias("graph_version")).write_parquet(folder / "trades.parquet")
    summary = json.loads((source_reports / directory / "demo" / "summary.json").read_text())
    summary["graph_version"] = graph_version
    (folder / "summary.json").write_text(json.dumps(summary))


def test_dup_count_follows_each_variants_own_graph(v8, synthetic, monkeypatch, tmp_path):
    """M8：v8e/v8w/v8nw 的合并与主图不同；频道列表的合并数按该口径报告自己的图版本。"""
    eid = synthetic["episode"]["episode_id"]
    _, other = synthetic_graph(synthetic, eid)
    reports = tmp_path / "reports"
    copy_report(synthetic["reports"], reports, "l0-v8-w60lf-ns300", "viz-test")
    copy_report(synthetic["reports"], reports, "l0-v8e", "viz-test-v8e")
    original_scan = data_module.scan_episodes
    asked = []

    def scan(layout, graph_version):
        asked.append(graph_version)
        frame = original_scan(layout, "viz-test")           # 变体图与主图同一份合成数据，只是合并不同
        return g1_columns(frame, eid, None, other, merged=graph_version == "viz-test")

    monkeypatch.setattr(data_module, "scan_episodes", scan)
    doc = json.loads(json.dumps(v8["config_doc"]))
    doc["reports"] = str(reports)
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(doc))
    trades = {t["episode_id"]: t for t in Dashboard(path).trades("demo")["trades"]}
    assert trades[eid]["variants"]["w60lf-ns300"]["n_dup_members"] == len(other)
    assert trades[eid]["variants"]["v8e"]["n_dup_members"] == 0          # v8e 图上没有并入本单
    assert sorted(set(asked)) == ["viz-test", "viz-test-v8e"]


def test_variant_graph_needs_explicit_alias(v8, synthetic, tmp_path):
    eid = synthetic["episode"]["episode_id"]
    doc = json.loads(json.dumps(v8["config_doc"]))
    doc["channels"][0]["graph_version"] = "viz-test-main-not-published"
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="配置图版本与回测报告不一致"):
        Dashboard(path).detail("demo", "v8e", eid)
    # 列表形式按「-<口径>」后缀归属：viz-test 不是 v8e 的图名，不接受。
    doc["channels"][0]["graph_aliases"] = ["viz-test"]
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="配置图版本与回测报告不一致"):
        Dashboard(path).detail("demo", "v8e", eid)
    doc["channels"][0]["graph_aliases"] = {"v8e": "viz-test"}
    path.write_text(json.dumps(doc))
    detail = Dashboard(path).detail("demo", "v8e", eid)
    assert detail["consistency"]["ok"] is True and detail["variant"] == "v8e"
    # M11：别名只给它自己的变体口径；主口径目录里出现变体图的报告（放错目录）仍拒绝。
    with pytest.raises(ValueError, match="配置图版本与回测报告不一致"):
        Dashboard(path).detail("demo", "w60lf-ns300", eid)
    with pytest.raises(ValueError, match="配置图版本与回测报告不一致"):
        Dashboard(path).detail("demo", "w1-ns300", eid)


def test_variant_graph_names_and_cmp_batch_discovery(v8, tmp_path):
    channel = {"graph_aliases": ["demo-v8e", "demo-v8w", "demo-v8nw"]}
    assert data_module.variant_graph_names(channel, "v8e") == ["demo-v8e"]
    assert data_module.variant_graph_names(channel, "v8nw") == ["demo-v8nw"]
    assert data_module.variant_graph_names({"graph_aliases": {"v8w": ["a", "b"]}}, "v8w") == ["a", "b"]
    assert data_module.variant_graph_names({}, "v8e") == []
    # M10：C 批次 l0-v8cmp*（目录名在 l0-v8 之后没有连字符）按前缀发现，用主图；其它无连字符目录不发现。
    reports = tmp_path / "reports"
    for name in ("l0-v8cmp-w60lf", "l0-v8cmpbase", "l0-v8xyz"):
        (reports / name).mkdir(parents=True)
    doc = json.loads(json.dumps(v8["config_doc"]))
    doc["reports"] = str(reports)
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(doc))
    variants = Dashboard(path).variants()
    assert variants["v8cmp-w60lf"] == {"suffix": "cmp-w60lf", "name": "cmp-w60lf", "graph": "main"}
    assert "v8cmpbase" in variants and "v8xyz" not in variants
    assert variants["v8e"]["graph"] == "variant" and variants["w60lf-ns300"]["graph"] == "main"


def test_v8_report_reads_real_l0_output(v8, synthetic, tmp_path):
    """M5：冻结判定脚本直接读 l0_replay.replay()/summarize() 的真实产物（键名、Decimal(38,12) 列、
    kernel_version、follow_teacher.path），而不是只读手写的仿 summary。"""
    report = load_v8_report()
    reports = tmp_path / "reports"
    for directory in ("l0-v8-w60lf-ns300", "l0-v8-w1-ns300", "l0-v8e"):
        shutil.copytree(synthetic["reports"] / directory, reports / directory)      # copy2 保留 mtime 顺序
    # 合成夹具的 v8e 跑在主图上：S2 要求 v8e 是变体图，拒绝出报告。
    with pytest.raises(report.ReportError, match="is the main graph"):
        report.build_report(reports, ["demo"])
    shutil.rmtree(reports / "l0-v8e")
    doc = report.build_report(reports, ["demo"])
    main = pl.read_parquet(reports / "l0-v8-w60lf-ns300" / "demo" / "trades.parquet")
    w1 = pl.read_parquet(reports / "l0-v8-w1-ns300" / "demo" / "trades.parquet")
    assert main.schema["net_R"] == pl.Decimal(38, 12) and main["kernel_version"].n_unique() == 1
    generation = doc["inputs"]["generation"]
    assert generation["kernel_version"] == main["kernel_version"][0]
    assert generation["follow_teacher_paths"] == {"demo": str(synthetic["follow_path"])}
    assert set(generation["policy_hashes"]) == {MAIN, W1}

    def evaluable_n(frame, basis):
        # 独立按 §10.4 的样本定义数（不调用脚本里的函数）。
        return frame.filter(pl.col("fill_status").is_in(["filled", "partial"]) & pl.col("censor_reason").is_null()
                            & pl.col("net_R").is_not_null() & pl.col("mark_ok") & pl.col("funding_ok") & pl.col("rules_ok")
                            & pl.col("bars_ok") & (pl.col("sizing_basis").fill_null("risk") == basis)
                            & (pl.col("t_dec") < pl.datetime(2026, 7, 1, time_zone="UTC"))).height

    judgment = doc["judgment"]
    assert judgment["with_stop"]["demo"]["n"] == evaluable_n(main, "risk")
    assert judgment["nostop"]["demo"]["holds"]["w1"]["n"] == evaluable_n(w1, "nostop")
    assert judgment["nostop"]["demo"]["holds"]["w60"]["n"] == evaluable_n(main, "nostop")
    assert judgment["with_stop"]["demo"]["verdict"] == "样本不足"
    assert judgment["nostop"]["demo"]["verdict"] == "持有期档缺失"
    total = judgment["total"]["demo"]["holds"]["w60"]
    expected = sum(float(r["net_R"]) * float(r["risk_budget"]) for r in main.to_dicts()
                   if r["fill_status"] in ("filled", "partial") and r["censor_reason"] is None and r["net_R"] is not None
                   and all(r[k] for k in ("mark_ok", "funding_ok", "rules_ok", "bars_ok")))
    assert total["sum"] == pytest.approx(expected)
    assert doc["headline"]["demo"]["v8e"]["status"] == "未出"
    assert doc["inputs"]["main"]["demo"]["n_rows"] == main.height
    assert "判定期" in report.render_text(json.loads(json.dumps(doc, default=report._json_default)))
def test_v7_dashboard_unchanged_by_v8_reports(dashboard, v8):
    # v8 目录与 v7 并存：v7 口径集合不变，没有 v8 口径混入。
    variants = dashboard.variants()
    assert "base" in variants and not any(key.startswith("v8") or "ns300" in key for key in variants)
