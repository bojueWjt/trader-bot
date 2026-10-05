"""§10.4 盈利判定脚本（scripts/v8_report.py）：全部用仿写的 L0 产物，不读任何真实频道数据。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "v8_report.py"
spec = importlib.util.spec_from_file_location("v8_report", SCRIPT)
rep = importlib.util.module_from_spec(spec)
sys.modules.setdefault("v8_report", rep)        # dataclass 需要能按模块名找到自己
spec.loader.exec_module(rep)

UTC = dt.timezone.utc
MON = dt.datetime(2025, 9, 1, 12, tzinfo=UTC)        # 2025-09-01 是周一
DEC = pl.Decimal(38, 12)
SCHEMA = {"episode_id": pl.String, "graph_version": pl.String, "policy_hash": pl.String, "t_dec": pl.Datetime("us", "UTC"),
          "fill_status": pl.String, "censor_reason": pl.String, "net_R": DEC, "risk_budget": DEC,
          "mark_ok": pl.Boolean, "funding_ok": pl.Boolean, "rules_ok": pl.Boolean, "bars_ok": pl.Boolean,
          "sizing_basis": pl.String, "filled_qty": DEC, "entry_notional_U": DEC,
          "position_open_at": pl.Datetime("us", "UTC"), "position_close_at": pl.Datetime("us", "UTC"),
          "censor_at": pl.Datetime("us", "UTC"), "edit_may_contain_outcome": pl.Boolean}


def trade(eid, t, net_R, *, basis="risk", budget="180", fill="filled", censor=None, notional=None,
          open_at=None, close_at=None, edit=None, cover=True, **extra):
    filled = fill in ("filled", "partial")
    return {"episode_id": eid, "t_dec": t, "fill_status": fill, "censor_reason": censor,
            "net_R": None if net_R is None else Decimal(str(net_R)), "risk_budget": Decimal(budget),
            "mark_ok": cover, "funding_ok": True, "rules_ok": True, "bars_ok": True, "sizing_basis": basis,
            "filled_qty": Decimal("1") if filled else Decimal("0"),
            "entry_notional_U": None if notional is None else Decimal(str(notional)),
            "position_open_at": (open_at or t) if filled else None,
            "position_close_at": (close_at or (t + dt.timedelta(hours=6))) if filled and censor is None else None,
            "censor_at": None, "edit_may_contain_outcome": edit, **extra}


def publish(reports: Path, directory: str, channel: str, rows: list[dict], *, policy=rep.MAIN_POLICY, budget="180",
            graph="ch-v8@abc", policy_hash="ph"):
    folder = reports / directory / channel
    folder.mkdir(parents=True, exist_ok=True)
    full = [dict(r, graph_version=graph, policy_hash=policy_hash) for r in rows]
    pl.DataFrame(full, schema=SCHEMA).write_parquet(folder / "trades.parquet")
    n_nostop = sum(r["sizing_basis"] == "nostop" for r in rows)
    summary = {"channel": -100, "graph_version": graph, "policy_version": policy, "policy_hash": policy_hash,
               "risk_budget": budget, "overall": {"n_trades": len(rows) - n_nostop},
               "blocks": {"nostop": {"n_trades": n_nostop}}}
    (folder / "summary.json").write_text(json.dumps(summary))
    return folder


def weekly_series(prefix, weeks, values, *, basis="risk", start=MON, notional=None):
    return [trade(f"{prefix}{i}", start + dt.timedelta(weeks=i), values[i % len(values)], basis=basis, notional=notional)
            for i in range(weeks)]


# ---------------------------------------------------------------------------
# 样本、时钟与单位
# ---------------------------------------------------------------------------

def test_sample_is_evaluable_filled_only():
    t = MON
    assert rep.evaluable(trade("a", t, 1))
    assert rep.evaluable(trade("p", t, 1, fill="partial"))
    assert rep.evaluable(trade("e", t, 1, censor=""))
    assert not rep.evaluable(trade("u", t, 0, fill="none"))
    assert not rep.evaluable(trade("c", t, 1, censor="LABEL_RIGHT_CENSORED"))
    assert not rep.evaluable(trade("n", t, None))
    assert not rep.evaluable(trade("m", t, 1, cover=None))       # 覆盖必须严格为 True
    assert not rep.evaluable(trade("m", t, 1, cover=False))


def test_units_with_stop_R_nostop_U_and_total_money_only():
    stop = trade("s", MON, "0.5")
    nostop = trade("n", MON, "0.25", basis="nostop")
    assert rep.net_U(stop) == Decimal("90")       # net_R × 180
    assert rep.net_U(nostop) == Decimal("45")     # 无止损的 net_R 是 net_U / B
    assert rep.sizing_of({"sizing_basis": None}) == "risk"     # v8 之前的行按风险定量
    with pytest.raises(rep.ReportError):
        rep.net_U(trade("x", MON, 1) | {"risk_budget": None})
    table = rep.three_tables([stop], [nostop], "judgment", MON, judged=True)
    assert table["with_stop"]["mean"] == 0.5 and table["with_stop"]["unit"] == "R/单"
    assert table["nostop"]["mean"] == 45 and table["nostop"]["unit"] == "U/单"
    assert table["total"]["sum"] == 135 and table["total"]["unit"] == "U/周"
    # 合计表只有金额：没有胜率、没有均值 R。
    assert not {"win_rate", "mean_R", "mean_net_R"} & set(table["total"])


def test_periods_split_at_july_first_utc_and_drop_after_september():
    assert rep.period_of(dt.datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)) == "judgment"
    assert rep.period_of(dt.datetime(2026, 7, 1, tzinfo=UTC)) == "calibration"
    assert rep.period_of(dt.datetime(2026, 9, 30, 23, 59, tzinfo=UTC)) == "calibration"
    assert rep.period_of(dt.datetime(2026, 10, 1, tzinfo=UTC)) is None


def test_iso_week_and_28_day_blocks():
    sunday_night = dt.datetime(2025, 9, 7, 23, 59, tzinfo=UTC)
    assert rep.week_start(sunday_night) == dt.datetime(2025, 9, 1, tzinfo=UTC)
    assert rep.week_start(dt.datetime(2025, 9, 8, tzinfo=UTC)) == dt.datetime(2025, 9, 8, tzinfo=UTC)
    # 东八区周一早上仍属 UTC 的上一周。
    assert rep.week_start(dt.datetime(2025, 9, 8, 7, tzinfo=dt.timezone(dt.timedelta(hours=8)))) == dt.datetime(2025, 9, 1, tzinfo=UTC)
    origin = dt.datetime(2025, 9, 1, tzinfo=UTC)
    assert rep.block_of(dt.datetime(2025, 9, 28, tzinfo=UTC), origin, 28) == origin
    assert rep.block_of(dt.datetime(2025, 9, 29, tzinfo=UTC), origin, 28) == origin + dt.timedelta(days=28)
    assert len(rep.weeks_between(origin, dt.datetime(2025, 9, 15, tzinfo=UTC))) == 3


# ---------------------------------------------------------------------------
# 区间与判定
# ---------------------------------------------------------------------------

def test_bootstrap_is_frozen_seeded_ratio_over_blocks():
    assert (rep.N_BOOT, rep.SEED, rep.MIN_TRADES, rep.MIN_WEEKS) == (10_000, 20261006, 30, 20)
    sums, counts = [3.0, -1.0, 2.0, 0.5], [2, 1, 3, 1]
    got = rep.bootstrap_interval(sums, counts)
    rng = np.random.default_rng(20261006)
    idx = rng.integers(0, 4, size=(10_000, 4))
    stats = np.asarray(sums)[idx].sum(axis=1) / np.asarray(counts, float)[idx].sum(axis=1)
    assert got == pytest.approx(list(np.quantile(stats, [0.025, 0.975])))
    assert rep.bootstrap_interval(sums, counts) == got          # 可复现
    assert rep.bootstrap_interval([1.0], [1]) is None
    with pytest.raises(ValueError):
        rep.bootstrap_interval([1.0, 2.0], [1, 0])


@pytest.mark.parametrize("n,weeks,ci,expected", [
    (29, 40, [0.1, 0.2], "样本不足"), (30, 19, [0.1, 0.2], "样本不足"), (30, 20, None, "样本不足"),
    (30, 20, [0.01, 0.2], "盈利"), (30, 20, [-0.3, -0.01], "亏损"), (30, 20, [-0.1, 0.2], "无显著优势"),
    (30, 20, [0.0, 0.2], "无显著优势"),
])
def test_verdict_rules(n, weeks, ci, expected):
    assert rep.verdict(n, weeks, ci) == expected


def test_total_table_counts_empty_weeks_as_zero_and_resamples_weeks_jointly():
    span = rep.weeks_between(MON, MON + dt.timedelta(weeks=3))           # 4 周，第 2、4 周无单
    samples = [(MON, 100.0), (MON + dt.timedelta(days=1), -40.0), (MON + dt.timedelta(weeks=2), 20.0)]
    table = rep.weekly_total_table(samples, span, judged=True)
    assert table["n_span_weeks"] == 4 and table["n_weeks"] == 2 and table["n"] == 3
    assert table["mean"] == pytest.approx(80 / 4)
    # 7 天块：同一周的有止损与无止损先合成一个周和（60），再抽周。
    assert table["ci95"]["7d"] == rep.bootstrap_interval([60.0, 0.0, 20.0, 0.0], [1, 1, 1, 1])
    assert table["ci95"]["28d"] is None          # 只有一个 28 天块
    assert table["verdict"] == "样本不足"
    with pytest.raises(rep.ReportError):
        rep.weekly_total_table([(MON - dt.timedelta(weeks=1), 1.0)], span, judged=True)


def test_per_trade_table_resamples_only_weeks_with_trades():
    samples = [(MON, 1.0), (MON + dt.timedelta(days=2), 3.0), (MON + dt.timedelta(weeks=5), -1.0)]
    table = rep.per_trade_table(samples, MON, judged=False, unit="R/单")
    assert table["n"] == 3 and table["n_weeks"] == 2 and table["mean"] == pytest.approx(1.0)
    assert table["ci95"]["7d"] == rep.bootstrap_interval([4.0, -1.0], [2, 1])
    assert table["ci95"]["28d"] == rep.bootstrap_interval([4.0, -1.0], [2, 1])     # 第 0 周与第 5 周分属两个 28 天块
    assert table["verdict"] is None and table["flags"] == []                         # 不判定的表不给结论


def test_block_length_disagreement_is_flagged(monkeypatch):
    monkeypatch.setattr(rep, "bootstrap_interval", lambda sums, counts, **_: [0.1, 0.2] if len(sums) > 15 else [-0.1, 0.2])
    samples = [(MON + dt.timedelta(weeks=i), 1.0) for i in range(40)]
    table = rep.per_trade_table(samples, MON, judged=True, unit="R/单")
    assert (table["verdict"], table["verdict_28d"]) == ("盈利", "无显著优势")
    assert table["flags"] == ["对块长敏感"]


def test_hold_consensus():
    def t(v, v28=None):
        return {"verdict": v, "verdict_28d": v28 or v}
    assert rep.consensus({h: t("盈利") for h in rep.HOLDS}) == {"verdict": "盈利", "verdict_28d": "盈利", "flags": []}
    mixed = {**{h: t("盈利") for h in rep.HOLDS}, "w1": t("无显著优势")}
    assert rep.consensus(mixed)["verdict"] == "依持有期而定"
    assert rep.consensus({**{h: t("盈利") for h in rep.HOLDS}, "5d": None}) == {
        "verdict": "持有期档缺失", "missing": ["5d"], "flags": []}
    sensitive = {**{h: t("盈利") for h in rep.HOLDS}, "w14": t("盈利", "无显著优势")}
    assert rep.consensus(sensitive)["flags"] == ["对块长敏感"]


# ---------------------------------------------------------------------------
# 逐月、回撤、在场名义、可实现性
# ---------------------------------------------------------------------------

def test_monthly_table_drawdown_by_exit_date_and_concurrency_peak():
    jan = dt.datetime(2026, 1, 5, tzinfo=UTC)
    rows = [
        trade("a", jan, "1"),                                                         # +180，1 月 20 日才平
        trade("b", jan + dt.timedelta(days=1), "-1", close_at=jan + dt.timedelta(days=2)),   # −180，先平
        trade("n1", jan + dt.timedelta(days=3), "0.5", basis="nostop", notional=6000,
              open_at=jan + dt.timedelta(days=3), close_at=dt.datetime(2026, 3, 2, tzinfo=UTC)),
        trade("n2", dt.datetime(2026, 2, 10, tzinfo=UTC), "-1", basis="nostop", notional=3000,
              close_at=dt.datetime(2026, 2, 12, tzinfo=UTC)),
        trade("late", dt.datetime(2026, 7, 2, tzinfo=UTC), "-0.5"),
        trade("after", dt.datetime(2026, 10, 2, tzinfo=UTC), "9"),                    # 数据截止之后，不进任何表
    ]
    rows[0]["position_close_at"] = dt.datetime(2026, 1, 20, tzinfo=UTC)
    data = rep.monthly(rows)
    months = {m["month"]: m for m in data["months"]}
    assert list(months) == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]
    jan_row = months["2026-01"]
    assert (jan_row["n_with_stop"], jan_row["n_nostop"]) == (2, 1)
    assert (jan_row["with_stop_U"], jan_row["nostop_U"], jan_row["total_U"]) == (0.0, 90.0, 90.0)
    assert months["2026-02"]["cumulative_total_U"] == pytest.approx(90 - 180)
    assert months["2026-07"]["period"] == "calibration"
    # 在场名义：1 月 6000，2 月 6000+3000，3 月起 n1 在 3 月 2 日平仓之前仍在场。
    assert months["2026-01"]["peak_nostop_notional_U"] == 6000
    assert months["2026-02"]["peak_over_account"] == pytest.approx(1.0)
    assert months["2026-03"]["peak_nostop_notional_U"] == 6000
    assert months["2026-04"]["peak_nostop_notional_U"] == 0
    judgment = data["periods"]["judgment"]
    # 出场顺序：b(−180) → a(+180) → n2(−180) → n1(+90)：峰值 0 → −180 → 0 → −180 → −90，最大回撤 180。
    assert judgment["realized_by_exit"] == {"n": 4, "final_U": pytest.approx(-90), "max_drawdown_U": pytest.approx(180)}
    assert judgment["n_active_months"] == 2 and judgment["loss_month_share"] == 0.5
    assert data["periods"]["calibration"]["loss_month_share"] == 1.0


def test_realizability_skips_new_stopless_trades_over_the_account_cap():
    t = dt.datetime(2026, 2, 2, tzinfo=UTC)
    rows = [trade("x1", t, "0.1", basis="nostop", notional=4000, close_at=t + dt.timedelta(days=5)),
            trade("x2", t + dt.timedelta(hours=1), "0.1", basis="nostop", notional=4000, close_at=t + dt.timedelta(days=2)),
            trade("x3", t + dt.timedelta(hours=2), "0.1", basis="nostop", notional=4000),          # 12000 > 9000 → 跳过
            trade("x4", t + dt.timedelta(days=2), "0.1", basis="nostop", notional=4000),           # x2 同刻平仓，先平后开
            trade("s1", t + dt.timedelta(hours=3), "1"),                                          # 有止损单不受影响
            trade("x5", t + dt.timedelta(hours=4), "0.1", basis="nostop", notional=None)]          # 名义未知：保留并单列
    kept, info = rep.realizable([dict(r, _channel="c") for r in rows])
    assert info["skipped"] == ["c:x3"] and info["n_skipped"] == 1 and info["n_notional_unknown"] == 1
    assert {r["episode_id"] for r in kept} == {"x1", "x2", "x4", "s1", "x5"}


# ---------------------------------------------------------------------------
# 端到端：读 L0 目录、拒绝不一致输入、写 JSON 与文本
# ---------------------------------------------------------------------------

def build_reports(reports: Path, channels=("alpha", "beta")):
    for k, ch in enumerate(channels):
        stop = weekly_series(f"{ch}-s", 44, ["1", "0.5", "-0.2", "0.8"])
        nostop = weekly_series(f"{ch}-n", 44, ["0.3", "0.1"], basis="nostop", start=MON + dt.timedelta(days=1), notional=600)
        calib = [trade(f"{ch}-c{i}", dt.datetime(2026, 7, 6, tzinfo=UTC) + dt.timedelta(weeks=i), "-1") for i in range(3)]
        flagged = [trade(f"{ch}-e", MON + dt.timedelta(days=2), "5", edit=True)]
        unfilled = [trade(f"{ch}-u", MON, "0", fill="none")]
        publish(reports, rep.MAIN_DIR, ch, stop + nostop + calib + flagged + unfilled)
        for hold, directory in rep.HOLD_DIRS.items():
            sign = "-" if (hold == "w1" and ch == "beta") else ""
            hold_nostop = weekly_series(f"{ch}-n", 44, [f"{sign}0.3", f"{sign}0.1"], basis="nostop",
                                        start=MON + dt.timedelta(days=1), notional=600)
            publish(reports, directory, ch, stop + hold_nostop, policy=rep.HOLD_POLICIES[hold])
        publish(reports, "l0-v8e", ch, stop[:10], policy=rep.MAIN_POLICY)
        publish(reports, "l0-v8-w60lf-ns100", ch, stop, policy="base-v1-timeexit-w60-live-follow-ns100")


def test_end_to_end_report(tmp_path):
    reports = tmp_path / "reports"
    build_reports(reports)
    out = tmp_path / "out" / "final-report-v8"
    assert rep.main(["--reports", str(reports), "--out", str(out)]) == 0
    doc = json.loads(Path(f"{out}.json").read_text())
    assert doc["channels"] == ["alpha", "beta"]
    assert doc["script_sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    judgment = doc["judgment"]
    alpha = judgment["with_stop"]["alpha"]
    assert alpha["n"] == 44 + 1 and alpha["verdict"] == "盈利"           # 带 edit 标记的单仍在主口径里
    assert judgment["with_stop"]["ALL"]["n"] == 90
    # 无止损：alpha 四档一致；beta 的 w1 档为负 → 依持有期而定。
    assert judgment["nostop"]["alpha"]["verdict"] == "盈利"
    assert judgment["nostop"]["beta"]["verdict"] == "依持有期而定"
    assert judgment["nostop"]["beta"]["holds"]["w1"]["verdict"] == "亏损"
    assert judgment["total"]["alpha"]["verdict"] == "盈利"
    total = judgment["total"]["alpha"]["holds"]["w60"]
    assert total["unit"] == "U/周" and total["n"] == 44 + 1 + 44
    assert not {"win_rate", "mean_R"} & set(total)
    # 校准期单独出表、不给判定。
    calib = doc["calibration"]
    assert calib["with_stop"]["alpha"]["n"] == 3 and calib["with_stop"]["alpha"]["verdict"] is None
    assert "verdict" not in calib["total"]["alpha"]
    # 敏感性只有区间：v8e 进头条，ns100 自动发现。
    assert set(doc["sensitivities"]) == {"l0-v8-w60lf-ns100", "l0-v8e"}
    for data in doc["sensitivities"].values():
        assert data["tables"]["alpha"]["judgment"]["total"]["verdict"] is None
    assert doc["headline"]["alpha"]["main"]["verdict"] == "盈利"
    assert doc["headline"]["alpha"]["v8e"]["n"] == 10 and doc["headline"]["alpha"]["v8e"]["verdict"] is None
    assert doc["edit_excluded"]["alpha"]["n_excluded"] == 1
    assert doc["edit_excluded"]["alpha"]["judgment"]["with_stop"]["n"] == 44
    assert doc["realizability"]["ALL"]["n_skipped"] == 0
    months = doc["monthly"]["alpha"]["months"]
    assert months[0]["month"] == "2025-09" and months[-1]["month"] == "2026-07"
    assert doc["frozen_rules"]["bootstrap"]["seed"] == 20261006
    text = Path(f"{out}.txt").read_text()
    assert "判定期" in text and "校准期" in text and "依持有期而定" in text
    assert "合计胜率" not in text.replace("不出合计均值 R 与合计胜率", "")


def test_missing_hold_gives_no_stopless_verdict(tmp_path):
    reports = tmp_path / "reports"
    build_reports(reports, channels=("alpha",))
    for name in ("summary.json", "trades.parquet"):
        (reports / rep.HOLD_DIRS["5d"] / "alpha" / name).unlink()
    doc = rep.build_report(reports, ["alpha"])
    assert doc["judgment"]["nostop"]["alpha"]["verdict"] == "持有期档缺失"
    assert doc["judgment"]["total"]["alpha"]["missing"] == ["5d"]
    assert doc["judgment"]["with_stop"]["alpha"]["verdict"] == "盈利"


@pytest.mark.parametrize("break_it,message", [
    ("policy", "is not the frozen"), ("budget", "risk_budget"), ("count", "summary counts"),
    ("newer", "newer than summary"), ("graph", "differs from main"), ("row", "differs from summary"),
    ("missing_main", "main 口径 missing"), ("half", "must both exist"),
])
def test_inconsistent_inputs_are_refused(tmp_path, break_it, message, capsys):
    reports = tmp_path / "reports"
    build_reports(reports, channels=("alpha",))
    main = reports / rep.MAIN_DIR / "alpha"
    summary = json.loads((main / "summary.json").read_text())
    if break_it == "policy":
        summary["policy_version"] = "base-v1-timeexit-w60-live-follow"
    elif break_it == "budget":
        summary["risk_budget"] = "100"
    elif break_it == "count":
        summary["overall"]["n_trades"] += 1
    elif break_it == "graph":
        hold = reports / rep.HOLD_DIRS["w14"] / "alpha"
        hs = json.loads((hold / "summary.json").read_text())
        frame = pl.read_parquet(hold / "trades.parquet").with_columns(pl.lit("ch-v8w@zzz").alias("graph_version"))
        frame.write_parquet(hold / "trades.parquet")
        hs["graph_version"] = "ch-v8w@zzz"
        (hold / "summary.json").write_text(json.dumps(hs))
    elif break_it == "row":
        frame = pl.read_parquet(main / "trades.parquet")
        frame.with_columns(pl.when(pl.col("episode_id") == "alpha-s0").then(pl.lit("other")).otherwise(pl.col("policy_hash"))
                           .alias("policy_hash")).write_parquet(main / "trades.parquet")
    if break_it not in ("newer", "missing_main", "half", "row"):
        (main / "summary.json").write_text(json.dumps(summary))
    if break_it in ("newer", "row"):
        import os
        stat = (main / "summary.json").stat()
        os.utime(main / "trades.parquet", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9) if break_it == "newer"
                 else (stat.st_atime_ns, stat.st_mtime_ns))
    if break_it == "missing_main":
        for name in ("summary.json", "trades.parquet"):
            (main / name).unlink()
    if break_it == "half":
        (main / "summary.json").unlink()
    code = rep.main(["--reports", str(reports), "--out", str(tmp_path / "out"), "--channels", "alpha"])
    assert code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


def test_unfinished_sensitivity_does_not_block_the_judgment(tmp_path):
    reports = tmp_path / "reports"
    build_reports(reports, channels=("alpha",))
    folder = reports / "l0-v8w" / "alpha"
    folder.mkdir(parents=True)
    (folder / "trades.parquet").write_bytes(b"in progress")
    doc = rep.build_report(reports, ["alpha"])
    assert doc["sensitivities"]["l0-v8w"]["tables"]["alpha"]["status"] == "未出"
    assert "refused" in doc["sensitivities"]["l0-v8w"]["tables"]["alpha"]
    assert doc["judgment"]["with_stop"]["alpha"]["verdict"] == "盈利"


def test_too_few_trades_or_weeks_is_insufficient(tmp_path):
    reports = tmp_path / "reports"
    rows = weekly_series("s", 19, ["1"]) + weekly_series("t", 19, ["1"], start=MON + dt.timedelta(days=1))
    publish(reports, rep.MAIN_DIR, "solo", rows)
    doc = rep.build_report(reports, ["solo"])
    table = doc["judgment"]["with_stop"]["solo"]
    assert table["n"] == 38 and table["n_weeks"] == 19 and table["verdict"] == "样本不足"
    # 合计表跨度从数据起点一直数到 06-30，空周按 0 计入。
    total = doc["judgment"]["total"]["solo"]["holds"]["w60"]
    assert total["n_span_weeks"] == len(rep.weeks_between(MON, dt.datetime(2026, 6, 30, tzinfo=UTC)))
    assert doc["judgment"]["total"]["solo"]["verdict"] == "持有期档缺失"
