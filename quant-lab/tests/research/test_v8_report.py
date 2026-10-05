"""§10.4 盈利判定脚本（scripts/v8_report.py）：全部用仿写的 L0 产物，不读任何真实频道数据。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
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
          "censor_at": pl.Datetime("us", "UTC"), "edit_may_contain_outcome": pl.Boolean, "kernel_version": pl.String,
          "mtm_U_at_censor": DEC, "triage_verdict": pl.String, "time_ref_promoted": pl.String, "l0_build_id": pl.String}
KERNEL = "A-test-0.7"
L0_BUILD = "l0-replay-v8+test"


def trade(eid, t, net_R, *, basis="risk", budget="180", fill="filled", censor=None, notional=None,
          open_at=None, close_at=None, edit=None, cover=True, mtm=None, **extra):
    filled = fill in ("filled", "partial")
    # 主口径里无止损行一定经过分诊判 new_entry（D6）；有止损、非升级行是 n/a。
    extra.setdefault("triage_verdict", "new_entry" if basis == "nostop" else "n/a")
    extra.setdefault("l0_build_id", L0_BUILD)
    return {"episode_id": eid, "mtm_U_at_censor": None if mtm is None else Decimal(str(mtm)), "time_ref_promoted": None, "t_dec": t, "fill_status": fill, "censor_reason": censor,
            "net_R": None if net_R is None else Decimal(str(net_R)), "risk_budget": Decimal(budget),
            "mark_ok": cover, "funding_ok": True, "rules_ok": True, "bars_ok": True, "sizing_basis": basis,
            "filled_qty": Decimal("1") if filled else Decimal("0"),
            "entry_notional_U": None if notional is None else Decimal(str(notional)),
            "position_open_at": (open_at or t) if filled else None,
            "position_close_at": (close_at or (t + dt.timedelta(hours=6))) if filled and censor is None else None,
            "censor_at": None, "edit_may_contain_outcome": edit, "kernel_version": KERNEL, **extra}


MAIN_GRAPH, EDIT_GRAPH = "ch-v8@abc", "ch-v8e@def"


def follow_path(channel):
    return f"/followup/{channel}/followup_action-v8.parquet"


def publish(reports: Path, directory: str, channel: str, rows: list[dict], *, policy=rep.MAIN_POLICY, budget="180",
            graph=MAIN_GRAPH, policy_hash="ph", follow="main"):
    folder = reports / directory / channel
    folder.mkdir(parents=True, exist_ok=True)
    full = [dict(r, graph_version=graph, policy_hash=policy_hash) for r in rows]
    pl.DataFrame(full, schema=SCHEMA).write_parquet(folder / "trades.parquet")
    n_nostop = sum(r["sizing_basis"] == "nostop" for r in rows)
    summary = {"channel": -100, "graph_version": graph, "policy_version": policy, "policy_hash": policy_hash,
               "risk_budget": budget, "overall": {"n_trades": len(rows) - n_nostop},
               "blocks": {"nostop": {"n_trades": n_nostop}},
               "l0_build_id": next((r.get("l0_build_id") for r in rows), L0_BUILD)}     # L0 写进 summary 与每一行
    if follow is not None:
        summary["follow_teacher"] = {"path": follow_path(channel) if follow == "main" else follow}
    (folder / "summary.json").write_text(json.dumps(summary))
    return folder


def rewrite(folder: Path, *, summary=None, frame=None):
    """Change a published run the way L0 publishes: parquet first, then summary (so summary is not older)."""
    doc = json.loads((folder / "summary.json").read_text())
    if frame is not None:
        frame(pl.read_parquet(folder / "trades.parquet")).write_parquet(folder / "trades.parquet")
    if summary is not None:
        summary(doc)
    (folder / "summary.json").write_text(json.dumps(doc))


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


def test_boundary_week_is_a_whole_week_in_both_periods():
    """M1：2026-06-29（周一）那一周在判定期只含 06-29/30，在校准期只含 07-01–07-05，两边都按整周计入；
    首周从数据起点所在 ISO 周算起，同样按整周。每单只按 t_dec 落进一个区间。"""
    boundary = dt.datetime(2026, 6, 29, tzinfo=UTC)
    start = dt.datetime(2026, 5, 6, 15, tzinfo=UTC)                     # 周三
    judgment = rep.span_for("judgment", start)
    calibration = rep.span_for("calibration", None)
    assert judgment[0] == dt.datetime(2026, 5, 4, tzinfo=UTC) and judgment[-1] == boundary
    assert calibration[0] == boundary and calibration[-1] == dt.datetime(2026, 9, 28, tzinfo=UTC)
    june30, july1 = dt.datetime(2026, 6, 30, 23, tzinfo=UTC), dt.datetime(2026, 7, 1, 1, tzinfo=UTC)
    rows = [trade("j", june30, "1"), trade("c", july1, "-1")]
    tables = {p: rep.three_tables(rows, rows, p, start, judged=False)["total"] for p in ("judgment", "calibration")}
    assert (tables["judgment"]["n"], tables["judgment"]["sum"]) == (1, 180.0)
    assert (tables["calibration"]["n"], tables["calibration"]["sum"]) == (1, -180.0)
    assert tables["judgment"]["n_span_weeks"] == len(judgment) == 9          # 首周、末周都算一整周
    assert tables["calibration"]["n_span_weeks"] == len(calibration) == 14
    assert tables["judgment"]["mean"] == pytest.approx(180 / 9)
    assert "06-29" in rep.frozen_rules()["partial_weeks"]


def test_weeks_counted_as_zero_without_evaluable_trades_are_visible():
    """M4：合计表把「有行但没有可评估单」的周也按 0 计入（口径不变），周数与不可评估的成交单另列。"""
    span = rep.weeks_between(MON, MON + dt.timedelta(weeks=3))
    rows = [trade("a", MON, "1"),
            trade("cens", MON + dt.timedelta(weeks=1), "2", censor="LABEL_RIGHT_CENSORED"),     # 有成交，不能评估
            trade("cov", MON + dt.timedelta(weeks=1, days=1), "1", cover=False),               # 有成交，覆盖失败
            trade("none", MON + dt.timedelta(weeks=2), "0", fill="none"),                     # 没成交：真的 0
            trade("cov2", MON, "1", cover=False)]                                               # 与可评估单同周
    table = rep.total_table(rows, [], "judgment", span, judged=False)
    assert table["n"] == 1 and table["n_span_weeks"] == 4 and table["mean"] == pytest.approx(180 / 4)
    assert table["n_span_weeks_with_rows_but_no_evaluable"] == 2
    assert table["n_span_weeks_filled_not_evaluable"] == 1
    assert table["n_filled_not_evaluable"] == 3
    per_trade = rep.with_stop_table(rows, "judgment", MON, judged=False)
    assert per_trade["n"] == 1 and per_trade["n_filled_not_evaluable"] == 3


def test_censored_stopless_trades_count_at_their_censor_mtm_and_v1_drops_them():
    """Review fix (v8-report-2): a filled stopless trade censored by SYMBOL_TIME_INVALID with mtm −900 is part of the
    stopless sample at −900; v1 (re-reported) drops it as before."""
    rows = weekly_series("n", 40, ["0.3", "0.1"], basis="nostop", notional=600)
    lost = trade("gone", MON + dt.timedelta(weeks=3, days=2), None, basis="nostop", censor="SYMBOL_TIME_INVALID", mtm=-900)
    v1 = rep.nostop_table(rows + [lost], "judgment", MON, judged=True, censored_as_mtm=False)
    v2 = rep.nostop_table(rows + [lost], "judgment", MON, judged=True)
    base = rep.nostop_table(rows, "judgment", MON, judged=True)
    assert v1["n"] == base["n"] == 40 and v1["mean"] == base["mean"] and v1["ci95"] == base["ci95"]
    assert v1["n_filled_not_evaluable"] == 1
    assert v2["n"] == 41 and v2["sum"] == pytest.approx(base["sum"] - 900)
    assert v2["censoring"]["n_censored_filled"] == 1 and v2["censoring"]["mtm_sum_U"] == -900
    assert rep.CENSOR_LOSS in v2["flags"] and rep.CENSOR_LOSS not in v1["flags"]
    span = rep.weeks_between(MON, MON + dt.timedelta(weeks=40))
    total_v1 = rep.total_table([], rows + [lost], "judgment", span, judged=False, censored_as_mtm=False)
    total_v2 = rep.total_table([], rows + [lost], "judgment", span, judged=False)
    assert total_v2["sum"] == pytest.approx(total_v1["sum"] - 900) and total_v2["n"] == total_v1["n"] + 1


def test_heavy_or_unvalued_censoring_downgrades_a_profit(monkeypatch):
    monkeypatch.setattr(rep, "bootstrap_interval", lambda sums, counts, **_: [0.1, 0.2])
    rows = weekly_series("n", 40, ["0.3"], basis="nostop", notional=600)
    # 3 censored of 43 known (< 10%), valued and positive: profit stays, no flag.
    few = [trade(f"c{i}", MON + dt.timedelta(weeks=i, days=1), None, basis="nostop", censor="BAR_GAP", mtm=5) for i in range(3)]
    table = rep.nostop_table(rows + few, "judgment", MON, judged=True)
    assert table["verdict"] == "盈利" and table["flags"] == []
    # 5 of 45 (> 10%): downgraded and flagged; v1 keeps the profit.
    many = [trade(f"c{i}", MON + dt.timedelta(weeks=i, days=1), None, basis="nostop", censor="BAR_GAP", mtm=5) for i in range(5)]
    table = rep.nostop_table(rows + many, "judgment", MON, judged=True)
    assert table["verdict"] == table["verdict_28d"] == "无显著优势"
    assert table["flags"] == [rep.CENSOR_SHARE_HIGH, rep.CENSOR_DOWNGRADED]
    assert table["verdict_before_censor_guard"] == ["盈利", "盈利"]
    assert rep.nostop_table(rows + many, "judgment", MON, judged=True, censored_as_mtm=False)["verdict"] == "盈利"
    # One censored row without mtm cannot be valued: not read as 0, the profit is downgraded.
    unknown = [trade("cx", MON + dt.timedelta(days=1), None, basis="nostop", censor="FUNDING_SCHEDULE_GAP")]
    table = rep.nostop_table(rows + unknown, "judgment", MON, judged=True)
    assert table["n"] == 40 and table["censoring"]["n_unvalued"] == 1
    assert table["verdict"] == "无显著优势" and rep.CENSOR_UNVALUED in table["flags"]
    # The total table follows the same rule.
    span = rep.weeks_between(MON, MON + dt.timedelta(weeks=40))
    total = rep.total_table([], rows + many, "judgment", span, judged=True)
    assert total["verdict"] == "无显著优势" and rep.CENSOR_DOWNGRADED in total["flags"]


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
    curve = judgment["realized_by_exit"]
    assert (curve["n"], curve["final_U"], curve["max_drawdown_U"]) == (4, pytest.approx(-90), pytest.approx(180))
    # 曲线本身（§10.4「按出场日期的累计已实现 U 曲线」）：按出场时刻排序的 (时刻, 单, 累计 U)。
    assert [(p[1], p[2]) for p in curve["points"]] == [(":b", -180), (":a", 0), (":n2", -180), (":n1", -90)]
    assert [p[0] for p in curve["points"]] == [jan + dt.timedelta(days=2), dt.datetime(2026, 1, 20, tzinfo=UTC),
                                              dt.datetime(2026, 2, 12, tzinfo=UTC), dt.datetime(2026, 3, 2, tzinfo=UTC)]
    # 最大回撤从起点（0，峰值时刻记 None）跌到 b 平仓时刻；与之后同深度的第二次回撤并列时取先到的。
    assert curve["drawdown_peak_at"] is None and curve["drawdown_trough_at"] == jan + dt.timedelta(days=2)
    assert curve["by_exit_month"] == {"2026-01": 0, "2026-02": -180, "2026-03": -90}
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
        # v8e：主口径策略、B=180、v8e 变体图、它自己的跟单表。
        publish(reports, "l0-v8e", ch, stop[:10], policy=rep.MAIN_POLICY, graph=EDIT_GRAPH,
                follow=f"/followup/{ch}/followup_action-v8e.parquet")
        publish(reports, "l0-v8-w60lf-ns100", ch, stop, policy="base-v1-timeexit-w60-live-follow-ns100")
        # C 批次（与 v7 对照）：B=100，目录名在 l0-v8 之后没有连字符。
        publish(reports, "l0-v8cmp-w60lf", ch, stop, policy="base-v1-timeexit-w60-live-follow", budget="100",
                policy_hash="ph-cmp")


def test_end_to_end_report(tmp_path):
    reports = tmp_path / "reports"
    build_reports(reports)
    out = tmp_path / "out" / "final-report-v8"
    assert rep.main(["--reports", str(reports), "--out", str(out)]) == 0
    doc = json.loads(Path(f"{out}.json").read_text())
    assert doc["channels"] == ["alpha", "beta"]
    assert doc["script_sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    # M6：区间依赖 numpy 的随机流与分位算法，版本随报告记下。
    assert doc["runtime"] == {"python": platform.python_version(), "numpy": np.__version__, "polars": pl.__version__}
    assert doc["frozen_rules"]["bootstrap"]["quantile_method"] == "linear"
    # S1：同代输入写进 inputs。
    generation = doc["inputs"]["generation"]
    assert generation["kernel_version"] == KERNEL and generation["l0_build_id"] == L0_BUILD
    assert doc["judgment_version"] == "v8-report-2" and [c["version"] for c in doc["changelog"]] == ["v8-report-1", "v8-report-2"]
    assert generation["policy_hashes"][rep.MAIN_POLICY] == "ph"
    assert generation["follow_teacher_paths"] == {"alpha": follow_path("alpha"), "beta": follow_path("beta")}
    assert doc["inputs"]["hold:w1"]["alpha"]["follow_teacher_path"] == follow_path("alpha")
    judgment = doc["judgment"]
    alpha = judgment["with_stop"]["alpha"]
    assert alpha["n"] == 44 + 1 and alpha["verdict"] == "盈利"           # 带 edit 标记的单仍在主口径里
    assert judgment["with_stop"]["ALL"]["n"] == 90
    # 无止损：alpha 四档一致；beta 的 w1 档为负 → 依持有期而定。
    assert judgment["nostop"]["alpha"]["verdict"] == "盈利"
    assert judgment["nostop"]["beta"]["verdict"] == "依持有期而定"
    assert judgment["nostop"]["beta"]["holds"]["w1"]["verdict"] == "亏损"
    assert judgment["total"]["alpha"]["verdict"] == "盈利"
    # v1（删失丢弃）并列重报；没有删失单时两版相同。
    assert judgment["nostop"]["beta"]["v1"]["verdict"] == "依持有期而定"
    assert judgment["total"]["alpha"]["v1"]["holds"]["w60"]["mean"] == judgment["total"]["alpha"]["holds"]["w60"]["mean"]
    assert doc["headline"]["alpha"]["main"]["verdict_v1"] == "盈利"
    total = judgment["total"]["alpha"]["holds"]["w60"]
    assert total["unit"] == "U/周" and total["n"] == 44 + 1 + 44
    assert not {"win_rate", "mean_R"} & set(total)
    # M4：未成交单所在的周是真的 0，不算「有行无可评估」里有成交的那一类。
    assert total["n_span_weeks_with_rows_but_no_evaluable"] == 0 and total["n_filled_not_evaluable"] == 0
    # 校准期单独出表、不给判定。
    calib = doc["calibration"]
    assert calib["with_stop"]["alpha"]["n"] == 3 and calib["with_stop"]["alpha"]["verdict"] is None
    assert "verdict" not in calib["total"]["alpha"]
    # 敏感性只有区间：v8e 进头条，ns100 与 C 批次（l0-v8cmp*）自动发现，并标出各自的策略与 B。
    assert set(doc["sensitivities"]) == {"l0-v8-w60lf-ns100", "l0-v8e", "l0-v8cmp-w60lf"}
    for data in doc["sensitivities"].values():
        assert data["tables"]["alpha"]["judgment"]["total"]["verdict"] is None
    cmp_batch = doc["sensitivities"]["l0-v8cmp-w60lf"]
    assert cmp_batch["risk_budgets"] == ["100"] and cmp_batch["policy_versions"] == ["base-v1-timeexit-w60-live-follow"]
    assert doc["sensitivities"]["l0-v8e"]["graph_versions"] == {"alpha": EDIT_GRAPH, "beta": EDIT_GRAPH}
    assert doc["headline"]["alpha"]["main"]["verdict"] == "盈利"
    assert doc["headline"]["alpha"]["v8e"]["n"] == 10 and doc["headline"]["alpha"]["v8e"]["verdict"] is None
    # S2：v8e 与主口径同一个周跨度（v8e 只到第 10 周，跨度仍从主口径的数据起点数到 06-30）。
    assert doc["headline"]["alpha"]["v8e"]["n_span_weeks"] == total["n_span_weeks"]
    assert doc["headline"]["alpha"]["span"]["n_span_weeks"] == total["n_span_weeks"]
    assert "w60_on_common_span" not in doc["headline"]["alpha"]["main"]
    assert doc["edit_excluded"]["alpha"]["n_excluded"] == 1
    assert doc["edit_excluded"]["alpha"]["judgment"]["with_stop"]["n"] == 44
    assert doc["realizability"]["ALL"]["n_skipped"] == 0
    months = doc["monthly"]["alpha"]["months"]
    assert months[0]["month"] == "2025-09" and months[-1]["month"] == "2026-07"
    assert doc["monthly"]["alpha"]["periods"]["judgment"]["realized_by_exit"]["points"]
    assert doc["frozen_rules"]["bootstrap"]["seed"] == 20261006
    assert "06-29" in doc["frozen_rules"]["partial_weeks"]
    text = Path(f"{out}.txt").read_text()
    assert "判定期" in text and "校准期" in text and "依持有期而定" in text
    assert "合计胜率" not in text.replace("不出合计均值 R 与合计胜率", "")
    assert "l0-v8cmp-w60lf [策略 base-v1-timeexit-w60-live-follow · B=100]" in text
    assert f"v8e [策略 {rep.MAIN_POLICY} · B=180]" in text
    assert f"numpy {np.__version__}" in text and f"kernel_version {KERNEL}" in text


def test_missing_hold_gives_no_stopless_verdict(tmp_path):
    reports = tmp_path / "reports"
    build_reports(reports, channels=("alpha",))
    for name in ("summary.json", "trades.parquet"):
        (reports / rep.HOLD_DIRS["5d"] / "alpha" / name).unlink()
    doc = rep.build_report(reports, ["alpha"])
    assert doc["judgment"]["nostop"]["alpha"]["verdict"] == "持有期档缺失"
    assert doc["judgment"]["total"]["alpha"]["missing"] == ["5d"]
    assert doc["judgment"]["with_stop"]["alpha"]["verdict"] == "盈利"


def _break(reports: Path, break_it: str):
    main = reports / rep.MAIN_DIR / "alpha"
    hold = reports / rep.HOLD_DIRS["w14"] / "alpha"
    edit = reports / rep.EDIT_DIR / "alpha"
    if break_it == "policy":
        rewrite(main, summary=lambda s: s.update(policy_version="base-v1-timeexit-w60-live-follow"))
    elif break_it == "budget":
        rewrite(main, summary=lambda s: s.update(risk_budget="100"))
    elif break_it == "count":
        rewrite(main, summary=lambda s: s["overall"].update(n_trades=s["overall"]["n_trades"] + 1))
    elif break_it == "graph":
        rewrite(hold, summary=lambda s: s.update(graph_version="ch-v8w@zzz"),
                frame=lambda f: f.with_columns(pl.lit("ch-v8w@zzz").alias("graph_version")))
    elif break_it == "row":
        frame = pl.read_parquet(main / "trades.parquet")
        frame.with_columns(pl.when(pl.col("episode_id") == "alpha-s0").then(pl.lit("other")).otherwise(pl.col("policy_hash"))
                           .alias("policy_hash")).write_parquet(main / "trades.parquet")
        stat = (main / "summary.json").stat()
        os.utime(main / "trades.parquet", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif break_it == "newer":
        stat = (main / "summary.json").stat()
        os.utime(main / "trades.parquet", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    elif break_it == "missing_main":
        for name in ("summary.json", "trades.parquet"):
            (main / name).unlink()
    elif break_it == "half":
        (main / "summary.json").unlink()
    elif break_it == "truncated":                       # L0 写 summary.json 不是原子的：写到一半
        text = (main / "summary.json").read_text()
        (main / "summary.json").write_text(text[: len(text) // 2])
    elif break_it == "hash":                            # 审查探针：beta 主口径同名不同哈希（代码同步后部分重跑）
        rewrite(reports / rep.MAIN_DIR / "beta", summary=lambda s: s.update(policy_hash="ph-new"),
                frame=lambda f: f.with_columns(pl.lit("ph-new").alias("policy_hash")))
    elif break_it == "kernel":
        rewrite(reports / rep.HOLD_DIRS["w1"] / "alpha",
                frame=lambda f: f.with_columns(pl.lit("A-test-0.6").alias("kernel_version")))
    elif break_it == "follow":
        rewrite(hold, summary=lambda s: s.update(follow_teacher={"path": "/followup/alpha/followup_action-v7.parquet"}))
    elif break_it == "v8e_policy":
        rewrite(edit, summary=lambda s: s.update(policy_version="base-v1-timeexit-w60-live-follow"))
    elif break_it == "v8e_budget":
        rewrite(edit, summary=lambda s: s.update(risk_budget="100"))
    elif break_it == "v8e_graph":
        rewrite(edit, summary=lambda s: s.update(graph_version=MAIN_GRAPH),
                frame=lambda f: f.with_columns(pl.lit(MAIN_GRAPH).alias("graph_version")))
    elif break_it == "l0_build":
        rewrite(reports / rep.HOLD_DIRS["5d"] / "alpha", summary=lambda s: s.update(l0_build_id="l0-replay-v8+other"))
    elif break_it == "l0_build_rows":
        rewrite(reports / rep.HOLD_DIRS["w14"] / "beta",
                frame=lambda f: f.with_columns(pl.lit(None, dtype=pl.String).alias("l0_build_id")))
    elif break_it in ("untriaged", "untriaged_hold", "untriaged_v8e", "promoted_untriaged"):
        folder = {"untriaged": main, "untriaged_hold": hold, "untriaged_v8e": edit, "promoted_untriaged": main}[break_it]
        if break_it == "promoted_untriaged":
            change = (pl.when(pl.col("episode_id") == "alpha-s3").then(pl.lit("setup_card")).otherwise(pl.col("time_ref_promoted"))
                      .alias("time_ref_promoted"))
        elif break_it == "untriaged_v8e":
            change = pl.when(pl.col("episode_id") == "alpha-s0").then(pl.lit("nostop")).otherwise(pl.col("sizing_basis")).alias("sizing_basis")
        else:
            change = (pl.when(pl.col("episode_id") == "alpha-n5").then(pl.lit(None, dtype=pl.String))
                      .otherwise(pl.col("triage_verdict")).alias("triage_verdict"))

        def frame(f, change=change, break_it=break_it):
            f = f.with_columns(change)
            if break_it == "untriaged_v8e":    # 一张无止损行（分诊为 n/a，不是 new_entry），summary 计数随之调整
                f = f.with_columns(pl.when(pl.col("episode_id") == "alpha-s0").then(pl.lit("n/a"))
                                   .otherwise(pl.col("triage_verdict")).alias("triage_verdict"))
            return f

        def summary(s, break_it=break_it):
            if break_it == "untriaged_v8e":
                s["overall"]["n_trades"] -= 1
                s["blocks"]["nostop"]["n_trades"] += 1
        rewrite(folder, frame=frame, summary=summary)
    elif break_it == "v8e_hash":
        rewrite(edit, summary=lambda s: s.update(policy_hash="ph-other"),
                frame=lambda f: f.with_columns(pl.lit("ph-other").alias("policy_hash")))
    else:
        raise AssertionError(break_it)


@pytest.mark.parametrize("break_it,message", [
    ("policy", "is not the frozen"), ("budget", "risk_budget"), ("count", "summary counts"),
    ("newer", "newer than summary"), ("graph", "differs from main"), ("row", "differs from summary"),
    ("missing_main", "main 口径 missing"), ("half", "must both exist"), ("truncated", "unreadable L0 output"),
    ("hash", "policy_hash differs across runs"), ("kernel", "kernel_version differs"),
    ("follow", "follow_teacher.path"), ("v8e_policy", "is not the frozen"), ("v8e_budget", "risk_budget"),
    ("v8e_graph", "is the main graph"), ("v8e_hash", "policy_hash differs across runs"),
    ("l0_build", "l0_build_id differs"), ("l0_build_rows", "l0_build_id differs"),
    ("untriaged", "stopless row alpha-n5 has triage_verdict None"), ("untriaged_hold", "stopless row alpha-n5"),
    ("untriaged_v8e", "stopless row alpha-s0 has triage_verdict 'n/a'"), ("promoted_untriaged", "promoted row alpha-s3"),
])
def test_inconsistent_inputs_are_refused(tmp_path, break_it, message, capsys):
    reports = tmp_path / "reports"
    build_reports(reports)
    _break(reports, break_it)
    code = rep.main(["--reports", str(reports), "--out", str(tmp_path / "out"), "--channels", "alpha,beta"])
    assert code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


def test_unfinished_sensitivity_does_not_block_the_judgment(tmp_path):
    reports = tmp_path / "reports"
    build_reports(reports, channels=("alpha",))
    folder = reports / "l0-v8w" / "alpha"
    folder.mkdir(parents=True)
    (folder / "trades.parquet").write_bytes(b"in progress")
    # M3：写到一半的 summary.json、坏 parquet、不是 L0 产物的目录都只让该批次标「未出」。
    ns100 = reports / "l0-v8-w60lf-ns100" / "alpha" / "summary.json"
    ns100.write_text(ns100.read_text()[:40])
    junk = reports / "l0-v8-junk" / "alpha"
    junk.mkdir(parents=True)
    pl.DataFrame({"x": [1]}).write_parquet(junk / "trades.parquet")
    (junk / "summary.json").write_text(json.dumps({"hello": 1}))
    nocount = reports / "l0-v8-nocount" / "alpha"
    nocount.mkdir(parents=True)
    pl.DataFrame({"x": [1]}).write_parquet(nocount / "trades.parquet")
    (nocount / "summary.json").write_text(json.dumps({"graph_version": "g", "policy_version": "p", "policy_hash": "h",
                                                      "risk_budget": "180"}))
    broken = reports / "l0-v8-broken" / "alpha"
    broken.mkdir(parents=True)
    (broken / "trades.parquet").write_bytes(b"PAR1 not really")
    (broken / "summary.json").write_text(json.dumps({"graph_version": "g", "policy_version": "p", "policy_hash": "h",
                                                     "risk_budget": "180", "overall": {"n_trades": 1}}))
    # 不同代的敏感性批次（内核版本不同、L0 代码身份不同）只标未出并写原因。
    publish(reports, "l0-v8-oldkernel", "alpha", [trade("o1", MON, "1", kernel_version="A-test-0.6")],
            policy="base-v1-timeexit-w60-live-ns300")
    publish(reports, "l0-v8-oldl0", "alpha", [trade("o2", MON, "1", l0_build_id="l0-replay-v8+old")],
            policy="base-v1-timeexit-w60-live-ns300")
    # 宽口径（v8w 一类）执行存疑分诊：敏感性批次不查分诊门，照出区间。
    publish(reports, "l0-v8-wide", "alpha", [trade("w1", MON, "0.2", basis="nostop", triage_verdict="uncertain")],
            policy=rep.MAIN_POLICY)
    # v8e 写到一半：头条的 v8e 标未出（不拒绝），主口径判定照出。
    edit = reports / rep.EDIT_DIR / "alpha" / "summary.json"
    edit.write_text(edit.read_text()[:10])
    doc = rep.build_report(reports, ["alpha"])
    tables = {name: data["tables"]["alpha"] for name, data in doc["sensitivities"].items()}
    assert tables["l0-v8w"]["status"] == "未出" and "must both exist" in tables["l0-v8w"]["refused"]
    assert "unreadable L0 output" in tables["l0-v8-w60lf-ns100"]["refused"]
    assert "not an L0 summary" in tables["l0-v8-junk"]["refused"]
    assert "lacks overall/blocks" in tables["l0-v8-nocount"]["refused"]
    assert "unreadable L0 output" in tables["l0-v8-broken"]["refused"]
    assert "kernel_version" in tables["l0-v8-oldkernel"]["refused"]
    assert "l0_build_id" in tables["l0-v8-oldl0"]["refused"]
    assert "judgment" in tables["l0-v8-wide"]
    assert "judgment" in tables["l0-v8cmp-w60lf"]                   # 完整的批次照出区间
    assert doc["headline"]["alpha"]["v8e"]["status"] == "未出" and "unreadable" in doc["headline"]["alpha"]["v8e"]["refused"]
    assert doc["judgment"]["with_stop"]["alpha"]["verdict"] == "盈利"
    assert "未出" in rep.render_text(doc)


def test_v8e_starting_before_main_is_aligned_on_a_common_span(tmp_path):
    reports = tmp_path / "reports"
    build_reports(reports, channels=("alpha",))
    early = [trade("alpha-early", MON - dt.timedelta(weeks=3), "1")]
    stop = weekly_series("alpha-s", 10, ["1", "0.5", "-0.2", "0.8"])
    publish(reports, "l0-v8e", "alpha", early + stop, graph=EDIT_GRAPH,
            follow="/followup/alpha/followup_action-v8e.parquet")
    doc = rep.build_report(reports, ["alpha"])
    headline = doc["headline"]["alpha"]
    main_weeks = doc["judgment"]["total"]["alpha"]["holds"]["w60"]["n_span_weeks"]
    assert headline["v8e"]["n_span_weeks"] == main_weeks + 3 == headline["span"]["n_span_weeks"]
    common = headline["main"]["w60_on_common_span"]
    assert common["n_span_weeks"] == main_weeks + 3 and common["verdict"] is None
    # 主口径的判定不因 v8e 改变。
    assert headline["main"]["verdict"] == doc["judgment"]["total"]["alpha"]["verdict"]
    assert headline["main"]["w60"]["n_span_weeks"] == main_weeks


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
