from datetime import timedelta
from decimal import Decimal
from http.client import HTTPConnection, HTTPResponse
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urlencode

import polars as pl
import pytest

from quant_lab.data.api import load_episodes, load_message_texts
from quant_lab.data.lake import Layout
from quant_lab.viz.data import automatic_interval, evaluable, mixed_timeline, read_bars, statistics_for
from quant_lab.viz.server import STATIC, make_server
from tests.data.l0_fixtures import T0
from .conftest import HTML_TEXT


def row(eid, net, day=0, **changes):
    return {"episode_id": eid, "t_dec": T0 + timedelta(days=day), "net_R": Decimal(net), "fill_status": "filled",
            "censor_reason": None, "mark_ok": True, "funding_ok": True, "rules_ok": True, "bars_ok": True, **changes}


def test_evaluable_statistics_day_clusters():
    # Unequal day sizes: per-trade point estimate 1.75, equal-day CI center 2.5.
    rows = [row("a", "1"), row("b", "1"), row("c", "1"), row("d", "4", day=1, fill_status="partial"),
            row("unfilled", "0", fill_status="none"), row("censored", "8", censor_reason="BAR_GAP"),
            row("uncovered", "7", mark_ok=False), row("null", "0", net_R=None)]
    stats = statistics_for(rows)
    assert stats["n"] == 4 and stats["n_days"] == 2
    assert stats["mean_R"] == 1.75 and stats["sum_R"] == 7 and stats["win_rate"] == 1
    assert stats["ci95"] == pytest.approx([-0.44, 5.44])
    assert stats["conclusion"] == "≈0"
    assert [r["cumulative_R"] for r in stats["cumulative_R"]] == [1, 2, 3, 7]
    assert statistics_for([row("a","2"), row("b","2",1)])["conclusion"] == "正期望"
    assert statistics_for([row("a","-2"), row("b","-2",1)])["conclusion"] == "负期望"
    assert statistics_for([row("a","2")])["ci95"] is None
    assert statistics_for([])["mean_R"] is None
    assert evaluable(row("a","0",censor_reason=""))
    for flag in ("bars_ok", "funding_ok", "rules_ok", "mark_ok"):
        assert not evaluable(row("x", "3", **{flag: None}))


def test_missing_running_and_extra_variant(dashboard, synthetic):
    reports = synthetic["reports"]
    running = reports / "l0-v7-be1" / "demo"
    extra = reports / "l0-v7-experimental" / "demo"
    running.mkdir(parents=True, exist_ok=True)
    extra.mkdir(parents=True, exist_ok=True)
    (running / "summary.json").write_text('{"incomplete":')
    (extra / "trades.parquet").write_bytes(b"in progress")
    data = dashboard.overview()
    stats = data["channels"][0]["statistics"]
    assert stats["base"]["status"] == "已出"
    assert stats["be1"]["status"] == "未出" and stats["w14"]["status"] == "未出"
    assert stats["experimental"]["status"] == "未出"
    assert data["variants"]["experimental"]["name"] == "-experimental"
    rows = pl.read_parquet(reports / "l0-v7" / "demo" / "trades.parquet").to_dicts()
    expected = sum(evaluable(r) for r in rows)
    assert stats["base"]["n"] == expected
    # Complete parquet with an old/incomplete summary is not a published pair.
    pl.DataFrame(rows).write_parquet(running / "trades.parquet")
    summary = json.loads((reports / "l0-v7" / "demo" / "summary.json").read_text())
    summary["overall"]["n_trades"] += 1
    (running / "summary.json").write_text(json.dumps(summary))
    assert dashboard.overview()["channels"][0]["statistics"]["be1"]["status"] == "未出"
    summary["overall"]["n_trades"] -= 1
    (running / "summary.json").write_text(json.dumps(summary))
    assert dashboard.report("demo", "be1")[0] is not None
    (running / "trades.parquet").touch()
    assert dashboard.report("demo", "be1")[0] is None


@pytest.mark.parametrize("variant", ["base", "live", "follow"])
def test_real_single_episode_trace_and_cache(dashboard, synthetic, variant):
    eid = synthetic["episode"]["episode_id"]
    detail = dashboard.detail("demo", variant, eid)
    assert detail["consistency"]["ok"] is True
    assert detail["consistency"]["actual"] == detail["trade"]["trace_hash"]
    assert detail["events"] and any(event["kind"] == "filled" for event in detail["events"])
    assert any(item.get("text") == HTML_TEXT for item in detail["timeline"])
    assert detail["bars"]["start"] == detail["trade"]["t_dec"] - timedelta(days=1)
    assert detail["bars"]["end"] == detail["horizon_end"] + timedelta(days=1)
    cached = dashboard.detail("demo", variant, eid)
    assert cached["consistency"] == detail["consistency"]
    assert cached["events"][0]["ts"] == detail["events"][0]["ts"]
    if variant == "follow":
        assert any(event["kind"] == "management" for event in detail["events"])
        assert any(segment["price"] == Decimal("95") or segment["price"] == "95.000000000000" for segment in detail["stop_segments"])


def test_trace_mismatch_is_explicit_and_events_hidden(dashboard, synthetic):
    path = synthetic["reports"] / "l0-v7" / "demo" / "trades.parquet"
    original = path.read_bytes()
    summary_path = path.with_name("summary.json")
    eid = synthetic["episode"]["episode_id"]
    try:
        frame = pl.read_parquet(path).with_columns(pl.when(pl.col("episode_id") == eid).then(pl.lit("0"*64)).otherwise(pl.col("trace_hash")).alias("trace_hash"))
        frame.write_parquet(path)
        summary_path.touch()
        detail = dashboard.detail("demo", "base", eid)
        assert detail["consistency"]["ok"] is False and detail["consistency"]["label"] == "复算不一致"
        assert detail["events"] == [] and all(item["type"] == "message" for item in detail["timeline"])
        assert detail["consistency"]["actual"] != detail["consistency"]["expected"]
    finally:
        path.write_bytes(original)
        summary_path.touch()


@pytest.mark.parametrize("same_result", [True, False])
def test_older_kernel_with_identical_result_is_accepted_but_labelled(dashboard, synthetic, same_result):
    path = synthetic["reports"] / "l0-v7" / "demo" / "trades.parquet"
    original = path.read_bytes()
    summary_path = path.with_name("summary.json")
    eid = synthetic["episode"]["episode_id"]
    try:
        hit = pl.col("episode_id") == eid
        frame = pl.read_parquet(path).with_columns(
            pl.when(hit).then(pl.lit("0" * 64)).otherwise(pl.col("trace_hash")).alias("trace_hash"),
            pl.when(hit).then(pl.lit("kernel-a-v0.5+synthetic")).otherwise(pl.col("kernel_version")).alias("kernel_version"))
        if not same_result:
            frame = frame.with_columns(pl.when(hit).then(pl.lit("unfilled")).otherwise(pl.col("outcome_kind")).alias("outcome_kind"))
        frame.write_parquet(path)
        summary_path.touch()
        detail = dashboard.detail("demo", "base", eid)
        assert detail["consistency"]["ok"] is same_result
        assert detail["consistency"]["label"] == ("结果一致（内核版本不同）" if same_result else "复算不一致")
        assert detail["consistency"]["expected_kernel"] == "kernel-a-v0.5+synthetic"
        assert bool(detail["events"]) is same_result
    finally:
        path.write_bytes(original)
        summary_path.touch()


def test_timeline_order_and_unused_instruction_data(dashboard, synthetic):
    detail = dashboard.detail("demo", "follow", synthetic["episode"]["episode_id"])
    dated = [item["time"] for item in detail["timeline"] if item["time"] is not None]
    assert dated == sorted(dated)
    instructions = [item for message in detail["timeline"] if message["type"] == "message" for item in message["instructions"]]
    assert {item["discard_reason"] for item in instructions} == {None, "uncertain", "episode_ambiguity"}
    rejected = [r for r in instructions if r["discard_reason"]]
    assert all(r["adoption"] == "未采用" and not r["adopted"] for r in rejected)
    assert any(r["episode_ambiguity"] == "ambiguous_root_episode" for r in rejected)
    assert any(r["message_id"] == 14 and r["discard_reason"] == "uncertain" for r in rejected)
    assert any(item.get("text") == 'BTC 合成窗口评论' for item in detail["timeline"])
    timeline = mixed_timeline([{"time":T0,"source_version_id":"m"},{"time":None,"source_version_id":"u"}],
                              [{"ts":T0,"seq":2},{"ts":T0,"seq":1}])
    assert [r["type"] for r in timeline] == ["message","backtest","backtest","message"]
    assert [r["seq"] for r in timeline if r["type"] == "backtest"] == [1,2]


def test_explicit_layout_does_not_use_environment(dashboard, synthetic, monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path / "nonexistent"))
    layout = Layout.from_root(synthetic["root"])
    eid = synthetic["episode"]["episode_id"]
    assert eid in load_episodes("viz-test", layout=layout)["episode_id"]
    texts = load_message_texts([synthetic["episode"]["root_source_version_id"]], layout=layout)
    assert HTML_TEXT in texts.values()
    assert dashboard.detail("demo","base",eid)["consistency"]["ok"]
    assert __import__('os').environ["QUANT_LAB_DATA_ROOT"] == str(tmp_path / "nonexistent")


def test_server_aggregates_1m_without_filling_missing_minutes(synthetic):
    result = read_bars(synthetic["lake"], synthetic["episode"]["instrument_id"], T0, T0+timedelta(minutes=20), "5m")
    bars = result["series"]["klines"]
    assert len(bars) == 5 and bars[0]["minutes"] == 5
    assert bars[0]["open"] == 100 and bars[0]["high"] == 110 and bars[0]["low"] == 99
    assert bars[-1]["partial"] and bars[-1]["minutes"] == 1
    assert automatic_interval(T0,T0+timedelta(days=3)) == "5m"
    assert automatic_interval(T0,T0+timedelta(days=60)) == "1h"


@pytest.fixture
def http(dashboard):
    try:
        server = make_server(dashboard, port=0)
    except PermissionError:
        # Managed Codex sandboxes may forbid all listening sockets. Exercise the
        # same HTTP parser/handler in memory, without dropping API assertions.
        server = make_server(dashboard, port=0, bind_and_activate=False)
        thread = None
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    class MemorySocket:
        def __init__(self, incoming):
            self.incoming = BytesIO(incoming)
            self.outgoing = BytesIO()

        def makefile(self, *args):
            return self.incoming

        def sendall(self, body):
            self.outgoing.write(body)

    def request(path, args=None, method="GET"):
        target = path + ("?" + urlencode(args) if args else "")
        if thread is None:
            socket = MemorySocket(f"{method} {target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
            server.RequestHandlerClass(socket, ("127.0.0.1",0), server)
            response = HTTPResponse(MemorySocket(socket.outgoing.getvalue()), method=method)
            response.begin()
            connection = None
        else:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=30)
            connection.request(method, target)
            response = connection.getresponse()
        body = response.read().decode()
        status = response.status
        response.close()
        if connection is not None:
            connection.close()
        return status, body

    yield request
    if thread is not None:
        server.shutdown()
        thread.join()
    server.server_close()


def test_get_endpoints_html_and_script_text(http, synthetic):
    eid = synthetic["episode"]["episode_id"]
    for path, args in (("/api/health",{}), ("/api/overview",{}), ("/api/channel",{"channel":"demo"}),
                       ("/api/detail",{"channel":"demo","episode":eid,"variant":"base","interval":"15m"})):
        status, body = http(path,args)
        assert status == 200 and isinstance(json.loads(body),dict)
        if path == "/api/detail":
            assert HTML_TEXT in [row.get("text") for row in json.loads(body)["timeline"]]
    for path,args in (("/",{}), ("/channel",{"channel":"demo"}), ("/trade",{"channel":"demo","episode":eid,"variant":"base"})):
        status, body = http(path,args)
        assert status == 200 and 'id="timeline"' in body and '/static/app.js' in body
    for path in ("/static/app.js","/static/style.css"):
        assert http(path)[0] == 200
    script = (STATIC / "app.js").read_text()
    assert "textContent" in script and "innerHTML" not in script and "insertAdjacentHTML" not in script


@pytest.mark.parametrize("path,args", [
    ("/api/channel",{"channel":"unknown"}), ("/api/channel",{"channel":"../demo"}),
    ("/api/detail",{"channel":"demo","episode":"../../secret","variant":"base"}),
    ("/api/detail",{"channel":"demo","episode":"unknown","variant":"base"}),
    ("/api/detail",{"channel":"demo","episode":"unknown","variant":"../base"}),
    ("/api/detail",{"channel":"demo","episode":"unknown","variant":"unknown"}),
    ("/api/overview",{"path":"/etc/passwd"}), ("/static/../data.py",{}),
    ("/api/channel?channel=demo&channel=other",{}), ("/%2e%2e/secret",{}),
])
def test_unknown_ids_paths_and_unexpected_parameters_rejected(http,path,args):
    assert http(path,args)[0] == 404


@pytest.mark.parametrize("method",["POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
def test_all_writes_rejected(http,method):
    assert http('/api/overview',method=method)[0] == 405


def test_static_ids_and_javascript_syntax():
    html = (STATIC / "index.html").read_text()
    ids = set(re.findall(r'id="([^\"]+)"', html))
    script = (STATIC / "app.js").read_text()
    assert set(re.findall(r"\$\('([^']+)'\)",script)).issubset(ids)
    assert "lightweight-charts@4.2.3" in html
    subprocess.run(["nice","-n","19","node","--check",str(STATIC / "app.js")],check=True,capture_output=True)


def test_cache_invalidated_when_inputs_change(dashboard, synthetic, monkeypatch):
    import quant_lab.viz.data as data_module
    eid = synthetic["episode"]["episode_id"]
    dashboard.detail("demo","base",eid)
    original = data_module.simulate_batch
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(data_module,"simulate_batch",counted)
    dashboard.detail("demo","base",eid)
    assert calls == []
    path = synthetic["follow_path"]
    path.touch()
    dashboard.detail("demo","base",eid)
    assert calls == [1]


def test_health_identity_is_running_config_snapshot(synthetic, tmp_path):
    from quant_lab.viz.data import Dashboard
    config = tmp_path / "config.json"
    config.write_bytes(synthetic["config"].read_bytes())
    dashboard = Dashboard(config)
    identity = dashboard.config_id
    config.write_text(config.read_text() + "\n")
    assert dashboard.config_id == identity
    assert Dashboard(config).config_id != identity
