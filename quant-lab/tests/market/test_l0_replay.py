"""G1→真实湖接口→kernel A 的小样、统计口径与可执行突变。"""
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import subprocess

import polars as pl
import pytest

from quant_lab.market import l0_replay as l0
from quant_lab.market.contract import resolve_policy
from quant_lab.market.partition_check import rules_path
from quant_lab.market.vision import instrument_id
from tests.data.l0_fixtures import CHANNEL, SECRET, T0, account, market_lake, mutant


def build_graph():
    """G1/G2 虚拟环境分离；真实 CLI 链路由 G1 构建，G2 消费已发布图。"""
    project = Path(__file__).resolve().parents[2]
    subprocess.run([str(project / ".venv-g1" / "bin" / "python"), "-m", "quant_lab.data.api",
                    "--build", "--market", "real", "--graph-version", "l0-test"],
                   cwd=project, check=True, capture_output=True, text=True)


@pytest.fixture
def built(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path))
    account(tmp_path)
    lake = market_lake(tmp_path)
    build_graph()
    return tmp_path, lake


def assert_sample(report, table):
    assert report["claim_status"] == "descriptive_only"
    assert report["policy_version"] == "base-v1"
    assert report["policy_hash"] == resolve_policy("base-v1").content_hash
    assert table.height == 2
    assert set(table["outcome_kind"]) == {"tp_hit", "unfilled_expired"}
    assert set(table["fill_status"]) == {"filled", "none"}
    assert table["censor_reason"].null_count() == 2
    overall = report["overall"]
    assert overall["n_trades"] == 2 and overall["n_filled"] == 1
    assert overall["fill_rate"] == 0.5 and overall["win_rate"] == 1.0
    # 限价买 100、卖 110，风险预算 100 / (100-90)=10 张；marketable entry taker 0.05%、静止 TP maker 0.02%。
    assert float(table["net_R"].sum()) == pytest.approx(0.9928)
    assert overall["mean_net_R"] == pytest.approx(0.4964)
    assert report["cumulative_R"][-1]["cumulative_R"] == pytest.approx(0.9928)
    assert report["by"]["year"]["2024"] == overall
    assert report["by"]["side"]["long"] == overall
    assert report["by"]["instrument"][instrument_id("BTCUSDT")] == overall


def test_l0_cli_end_to_end_and_channel_mutant(built, monkeypatch):
    root, _ = built
    out = root / "report"
    assert l0.main(["--graph-version", "l0-test", "--channel", str(CHANNEL), "--out", str(out)]) == 0
    report = json.loads((out / "summary.json").read_text())
    table = pl.read_parquet(out / "trades.parquet")
    assert_sample(report, table)
    assert {"episode_id", "instrument", "side", "t_dec", "entries", "stop", "targets", "fill_status", "net_R",
            "outcome_kind", "censor_reason", "policy_hash", "trace_hash"} <= set(table.columns)
    assert (out / "summary.md").read_text().splitlines()[0] == "claim_status: descriptive_only"
    for content in ((out / "summary.json").read_text(), (out / "summary.md").read_text(), str(table.to_dicts())):
        assert SECRET not in content and "BTC 做多" not in content and "order_plan" not in content
    broken = mutant(l0.replay,
        'load_episodes(graph_version, decision_graph=True).filter(pl.col("channel_id") == channel)',
        'load_episodes(graph_version, decision_graph=True)')
    result = broken(graph_version="l0-test", channel=CHANNEL, out=root / "mutant")
    with pytest.raises(AssertionError):
        assert_sample(result, pl.read_parquet(root / "mutant" / "trades.parquet"))


def test_l0_missing_rules_censored_and_mutant(built, monkeypatch):
    root, lake = built
    rules_path(lake, instrument_id("BTCUSDT")).unlink()
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "missing")
    assert report["overall"]["n_evaluable"] == 0
    assert report["overall"]["mean_net_R"] is None
    assert report["overall"]["n_coverage_excluded"] == 2
    assert report["censor_counts"] == {"RULE_HISTORY_MISSING": 2}
    original = l0.load_market_from_lake

    def fabricate_rules(request, **kwargs):
        return original(request, **kwargs).model_copy(update={"rules_known": True})

    monkeypatch.setattr(l0, "load_market_from_lake", fabricate_rules)
    changed = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "mutant")
    with pytest.raises(AssertionError):
        assert changed["censor_counts"] == {"RULE_HISTORY_MISSING": 2}


def arithmetic_rows():
    rows = []
    cases = [("long", "BTC", 2024, "2", 1, None, True), ("short", "ETH", 2025, "-1", 1, None, True),
             ("long", "BTC", 2024, "0", 0, None, True), ("short", "ETH", 2025, None, 1, "LABEL_RIGHT_CENSORED", True),
             ("long", "BTC", 2024, None, 0, "BAR_GAP", False)]
    for i, (side, inst, year, net, qty, censor, bars) in enumerate(cases):
        rows.append({"episode_id": str(i), "t_dec": T0.replace(year=year) + timedelta(minutes=i),
                     "side": side, "instrument": inst, "net_R": Decimal(net) if net is not None else None,
                     "filled_qty": Decimal(qty), "censor_reason": censor,
                     "mark_ok": True, "funding_ok": True, "rules_ok": True, "bars_ok": bars})
    return rows


def assert_arithmetic(report):
    overall = report["overall"]
    assert overall["n_trades"] == 5 and overall["n_filled"] == 3
    assert overall["fill_rate"] == 3 / 5
    assert overall["n_evaluable"] == 3 and overall["n_evaluable_filled"] == 2
    assert overall["mean_net_R"] == pytest.approx(1 / 3) and overall["win_rate"] == 0.5
    assert overall["n_censored"] == 2 and overall["n_coverage_excluded"] == 1
    assert [r["cumulative_R"] for r in report["cumulative_R"]] == [2, 2, 1]
    assert report["by"]["side"]["short"]["mean_net_R"] == -1
    assert report["by"]["instrument"]["BTC"]["mean_net_R"] == 1
    assert report["by"]["year"]["2025"]["win_rate"] == 0


@pytest.mark.parametrize("before,after", [
    ('len(filled) / len(rows)', 'len(filled) / len(evaluated)'),
    ('float(total / len(evaluated))', 'float(total / len(rows))'),
    ('/ len(closed_filled) if closed_filled', '/ len(evaluated) if closed_filled'),
    ('sum(r["censor_reason"] is not None for r in rows)', '0'),
    ('sum(not _covered(r) or r["censor_reason"] in EVIDENCE_CENSORS for r in rows)', '0'),
])
def test_summary_arithmetic_and_mutants(monkeypatch, before, after):
    table = pl.DataFrame(arithmetic_rows())
    assert_arithmetic(l0.summarize(table))
    monkeypatch.setattr(l0, "metrics", mutant(l0.metrics, before, after))
    with pytest.raises(AssertionError):
        assert_arithmetic(l0.summarize(table))


def test_cumulative_and_grouping_mutants(monkeypatch):
    table = pl.DataFrame(arithmetic_rows())
    for before, after in [('value += row["net_R"]', 'value = row["net_R"]'),
                          ('str(r["t_dec"].year)', '"2024"')]:
        broken = mutant(l0.summarize, before, after)
        with pytest.raises((AssertionError, KeyError)):
            assert_arithmetic(broken(table))


def test_l0_empty_channel(built):
    root, _ = built
    report = l0.replay(graph_version="l0-test", channel=-1009999999999, out=root / "empty")
    assert report["overall"]["n_trades"] == 0
    assert report["overall"]["fill_rate"] is None
    assert report["cumulative_R"] == []
    assert pl.read_parquet(root / "empty" / "trades.parquet").height == 0


def test_g1_coverage_exclusions_and_decision_view_mutant(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path))
    source = account(tmp_path) / "account" / "result.json"
    doc = json.loads(source.read_text())
    message = doc["chats"]["list"][0]["messages"][0]
    earlier = T0 - timedelta(days=5)
    message.update(date=earlier.isoformat(), date_unixtime=str(int(earlier.timestamp())))
    source.write_text(json.dumps(doc))
    market_lake(tmp_path)
    build_graph()

    def assert_excluded(report):
        assert report["overall"]["n_trades"] == 1
        assert report["g1_exclusions"] == {"n_entry_episodes": 2, "n_excluded": 1,
            "n_coverage_excluded": 1, "reason_counts": {"SYMBOL_TIME_INVALID": 1}}

    assert_excluded(l0.replay(graph_version="l0-test", channel=CHANNEL, out=tmp_path / "out"))
    broken = mutant(l0.replay, 'decision_graph=False', 'decision_graph=True')
    with pytest.raises(AssertionError):
        assert_excluded(broken(graph_version="l0-test", channel=CHANNEL, out=tmp_path / "mutant"))


# ---------------------------------------------------------------- 参考价入场（真实数据：峰哥 472 笔里 418 笔是「现价附近」无价格）

from quant_lab.market.asof import MarkAt
from quant_lab.market.contract import ContractError


class _Marks:
    """t_dec 及以前是 100，之后是 999：用来识别有没有偷看决策时刻之后的价格。"""

    def __init__(self, missing=False):
        self.missing, self.calls = missing, []

    def mark_at(self, instrument_id, at, **_):
        self.calls.append(at)
        if self.missing:
            return MarkAt(None, "MARK_STALE", None, None)
        return MarkAt(Decimal("100") if at <= T0 else Decimal("999"), None, at, 0.0)


def _row(entries):
    return {"episode_id": "e1", "instrument_id": instrument_id("BTCUSDT"), "t_dec": T0,
            "order_plan": {"entries": entries, "side": "short"}}


MREF = {"kind": "market_ref", "price_lo": None, "price_hi": None, "fraction": None, "tif": "IOC", "post_only": False}


def test_market_ref_gets_the_mark_known_at_decision_time():
    marks = _Marks()
    fixed, why = l0.resolve_market_refs(_row([MREF]), marks)
    assert why is None and marks.calls == [T0]
    e = fixed["order_plan"]["entries"][0]
    assert e["price_lo"] == e["price_hi"] == Decimal("100") and e["kind"] == "market_ref"


def test_mutant_reading_after_decision_time_takes_a_future_price():
    """突变：参考价取 t_dec 之后一分钟，拿到的是未来价格 999——正常实现必须是 100。"""
    broken = mutant(l0.resolve_market_refs, 'marks.mark_at(row["instrument_id"], row["t_dec"])',
                    'marks.mark_at(row["instrument_id"], row["t_dec"] + __import__("datetime").timedelta(minutes=1))')
    fixed, _ = broken(_row([MREF]), _Marks())
    assert fixed["order_plan"]["entries"][0]["price_lo"] == Decimal("999")


def test_market_ref_without_a_mark_is_excluded_with_a_reason():
    fixed, why = l0.resolve_market_refs(_row([MREF]), _Marks(missing=True))
    assert fixed is None and why == "MARKET_REF_UNRESOLVED:MARK_STALE"


def test_priced_entries_are_left_alone():
    row = _row([{"kind": "limit", "price_lo": Decimal("101"), "price_hi": Decimal("101"), "fraction": None, "tif": "GTC", "post_only": False}])
    marks = _Marks()
    fixed, why = l0.resolve_market_refs(row, marks)
    assert fixed is row and why is None and marks.calls == []


def test_one_bad_plan_is_excluded_and_the_run_continues(built, monkeypatch):
    root, _ = built
    real, state = l0.build_request, {"n": 0}

    def flaky(row, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise ContractError("synthetic bad plan")
        return real(row, **kw)

    monkeypatch.setattr(l0, "build_request", flaky)
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "flaky")
    assert report["replay_exclusions"]["reason_counts"] == {"PLAN_CONTRACT_INVALID:ContractError": 1}
    assert report["replay_exclusions"]["n_replayed"] == report["replay_exclusions"]["n_decision_episodes"] - 1
    assert pl.read_parquet(root / "flaky" / "trades.parquet").height == report["replay_exclusions"]["n_replayed"]


def test_plan_without_stop_is_counted_as_no_stop(built, monkeypatch):
    root, _ = built
    real, state = l0.resolve_market_refs, {"n": 0}

    def drop_first_stop(row, marks):
        fixed, why = real(row, marks)
        state["n"] += 1
        if fixed is not None and state["n"] == 1:
            fixed = dict(fixed, order_plan=dict(fixed["order_plan"], stop=None))
        return fixed, why

    monkeypatch.setattr(l0, "resolve_market_refs", drop_first_stop)
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "nostop")
    # Without a stop there is no R; it is its own reason, not a contract error.
    assert report["replay_exclusions"]["reason_counts"] == {"PLAN_NO_STOP": 1}
    assert report["replay_exclusions"]["n_replayed"] == report["replay_exclusions"]["n_decision_episodes"] - 1


def test_missing_plan_is_not_counted_as_missing_stop(built, monkeypatch):
    root, _ = built
    real, state = l0.resolve_market_refs, {"n": 0}

    def drop_first_plan(row, marks):
        fixed, why = real(row, marks)
        state["n"] += 1
        if fixed is not None and state["n"] == 1:
            fixed = dict(fixed, order_plan=None)
        return fixed, why

    monkeypatch.setattr(l0, "resolve_market_refs", drop_first_plan)
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "noplan")
    assert report["replay_exclusions"]["reason_counts"] == {"PLAN_NOT_EXECUTABLE": 1}


@pytest.mark.parametrize("reason",["PLAN_STALE","PLAN_STALE_QUOTE"])
def test_follower_invalid_plan_counts_in_replay_exclusions_not_r(built,monkeypatch,reason):
    root,_=built
    original=l0.resolve_market_refs
    calls={"n":0}

    def controlled(row,marks):
        calls["n"]+=1
        if calls["n"]!=1:
            return original(row,marks)
        stop=Decimal(str(row["order_plan"]["stop"]["price"]))
        if reason=="PLAN_STALE":
            mark=stop
        else:
            mark=stop+Decimal("10")
            row=dict(row,order_plan=dict(row["order_plan"],entries=[dict(row["order_plan"]["entries"][0],kind="market_ref",price_lo=stop+Decimal("5"),price_hi=stop+Decimal("5"),tif="IOC")]))
        class Known:
            def mark_at(self,*args):
                return MarkAt(mark,None,row["t_dec"],0)
        return original(row,Known())

    monkeypatch.setattr(l0,"resolve_market_refs",controlled)
    report=l0.replay(graph_version="l0-test",channel=CHANNEL,out=root/reason)
    table=pl.read_parquet(root/reason/"trades.parquet")
    assert report["replay_exclusions"]["reason_counts"]=={reason:1}
    assert table.height==1 and report["overall"]["n_trades"]==1
    evaluated=table.filter(pl.col("evaluable"))
    assert evaluated.height==report["overall"]["n_evaluable"]
    assert float(evaluated["net_R"].sum())==report["overall"]["sum_net_R"]
