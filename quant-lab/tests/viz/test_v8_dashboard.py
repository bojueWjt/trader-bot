"""v8 看板口径：按定量方式分块、v8 新列、变体图、单笔复算带止损消息原文。全部仿写数据。"""
from datetime import timedelta
from decimal import Decimal
import json
import os

import polars as pl
import pytest

import quant_lab.viz.data as data_module
from quant_lab.market.l0_replay import replay, summarize
from quant_lab.viz.data import Dashboard, dup_members, net_U, statistics_for
from tests.data.l0_fixtures import CHANNEL, T0

MAIN = "base-v1-timeexit-w60-live-follow-ns300"
W1 = "base-v1-timeexit-w1-live-follow-ns300"


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
    assert risk["sizing_basis"] == "risk" and "legs_n" in risk and "plan_link_kind" in risk
    assert trade["n_dup_members"] == 0 and "内核 A" in data["nostop_note"]


def test_dup_members_from_g1_dup_of():
    frame = pl.DataFrame({"episode_id": ["k", "d1", "d2", "x"], "root_message_id": [1, 2, 3, 4],
                          "plan_link_kind": ["root", "companion", "restatement", "root"], "dup_of": [None, "k", "k", ""],
                          "t_dec": [T0, None, T0, T0]})
    members = dup_members(frame)
    assert list(members) == ["k"] and [m["episode_id"] for m in members["k"]] == ["d2", "d1"]   # 时刻未知置末
    assert {m["plan_link_kind"] for m in members["k"]} == {"companion", "restatement"}
    assert dup_members(frame.drop("dup_of")) == {} and dup_members(None) == {}


def test_detail_passes_stop_message_text_and_shows_plan_link(v8, synthetic, monkeypatch):
    eid = synthetic["episode"]["episode_id"]
    layout = data_module.Layout.from_root(synthetic["root"])
    versions = pl.read_parquet(layout.message_version).to_dicts()
    stop_source = next(r["source_version_id"] for r in versions
                       if r["channel_id"] == CHANNEL and r["source_id"]["message_id"] == 10)
    original_load, original_prepare = data_module.load_episodes, data_module.prepare_episode_request
    graph = original_load("viz-test", decision_graph=False, layout=layout).filter(pl.col("channel_id") == CHANNEL)
    other = [e for e in graph["episode_id"].to_list() if e != eid]
    assert other                                   # 合成频道里还有别的单可以并入

    def load(graph_version, *, decision_graph=True, layout=None):
        frame = original_load(graph_version, decision_graph=decision_graph, layout=layout)
        # 仿 B 组 gold 新列：本单的止损来自另一条消息；另一张单被并入本单。
        frame = frame.with_columns(
            pl.when(pl.col("episode_id") == eid).then(pl.lit(stop_source)).otherwise(None).alias("stop_source_version_id"),
            pl.when(pl.col("episode_id") == eid).then(pl.lit("root")).otherwise(pl.lit("companion")).alias("plan_link_kind"),
            pl.when(pl.col("episode_id").is_in(other)).then(pl.lit(eid)).otherwise(None).alias("dup_of"))
        return frame

    seen = {}

    def prepare(row, **kwargs):
        seen.update(stop_text=kwargs.get("stop_text"), text=kwargs.get("text"))
        return original_prepare(row, **kwargs)

    monkeypatch.setattr(data_module, "load_episodes", load)
    monkeypatch.setattr(data_module, "prepare_episode_request", prepare)
    dashboard = Dashboard(v8["config"])
    detail = dashboard.detail("demo", "w60lf-ns300", eid)
    assert seen["stop_text"] == "BTC 合成管理消息"
    assert detail["plan_link"]["stop_source_version_id"] == stop_source and detail["plan_link"]["plan_link_kind"] == "root"
    assert {m["episode_id"] for m in detail["dup_members"]} == set(other)
    assert detail["sizing"]["sizing_basis"] == "risk" and "note" not in detail["sizing"]
    assert detail["consistency"]["ok"] is True
    listed = {t["episode_id"]: t["n_dup_members"] for t in dashboard.trades("demo")["trades"]}
    assert listed[eid] == len(other)


def test_variant_graph_needs_explicit_alias(v8, synthetic, tmp_path):
    eid = synthetic["episode"]["episode_id"]
    doc = json.loads(json.dumps(v8["config_doc"]))
    doc["channels"][0]["graph_version"] = "viz-test-main-not-published"
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="配置图版本与回测报告不一致"):
        Dashboard(path).detail("demo", "v8e", eid)
    doc["channels"][0]["graph_aliases"] = ["viz-test"]
    path.write_text(json.dumps(doc))
    detail = Dashboard(path).detail("demo", "v8e", eid)
    assert detail["consistency"]["ok"] is True and detail["variant"] == "v8e"


def test_v7_dashboard_unchanged_by_v8_reports(dashboard, v8):
    # v8 目录与 v7 并存：v7 口径集合不变，没有 v8 口径混入。
    variants = dashboard.variants()
    assert "base" in variants and not any(key.startswith("v8") or "ns300" in key for key in variants)
