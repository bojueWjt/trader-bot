"""v8 G1 gold end to end on invented messages: plan merge (F2/F6), clocks (F8), triage gating (F3), promotion (F11),
plan_link and followup synthesis (F13), variant switches. No real channel text."""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import polars as pl
import pytest

from quant_lab.data import lifecycle
from v8_lake import CHANNEL, OTHER, T0, Msg, build, episode, llm_row, open_action, stop_action, triage_frame


def test_stop_supplement_merges_moves_t_dec_and_records_its_source(tmp_path):
    msgs = [Msg(1, "仿写 SOL 216.4 空", 0, [open_action("SOL", entry="216.4")]),
            Msg(2, "止损:223", 6, [stop_action(223)])]
    r = build(tmp_path, msgs)
    ep = episode(r, 1)
    assert ep["order_plan"]["stop"]["price"] == D(223)
    assert ep["t_dec"] == T0 + timedelta(seconds=7)
    assert ep["stop_source_version_id"] == f"sv{CHANNEL % 10}-2" and ep["stop_rule"] == "supplement"
    assert ep["signal_age_s"] == 6 and ep["signal_anchor"] == "root" and ep["dec_eligibility"]["entry_decision"]
    links = {row["plan_link_kind"] for row in r["summary"]["plan_link_rows"]}
    assert "supplement" in links


def test_supplement_beyond_window_becomes_synthetic_move_stop(tmp_path):
    msgs = [Msg(1, "仿写 SOL 216.4 空", 0, [open_action("SOL", entry="216.4")]),
            Msg(2, "止损:223", 1801, [stop_action(223)], reply=1)]
    r = build(tmp_path, msgs)
    ep = episode(r, 1)
    assert ep["order_plan"]["stop"] is None and ep["t_dec"] == T0 + timedelta(seconds=1)
    late = [row for row in r["summary"]["plan_link_rows"] if row["plan_link_kind"] == "late_stop"]
    assert len(late) == 1 and late[0]["synthetic_action"] == "move_stop" and late[0]["synthetic_stop_price"] == D(223)
    assert late[0]["synthetic_at"] == T0 + timedelta(seconds=1801) and late[0]["kept_episode_id"] == ep["episode_id"]
    nw = build(tmp_path / "nw", [Msg(1, "仿写 SOL 216.4 空", 0, [open_action("SOL", entry="216.4")]),
                                 Msg(2, "止损:223", 6, [stop_action(223)])], supplement_window_s=0)
    assert episode(nw, 1)["order_plan"]["stop"] is None
    assert [row["plan_link_kind"] for row in nw["summary"]["plan_link_rows"] if row["synthetic_action"] == "move_stop"] == ["late_stop"]


def test_title_then_card_keeps_the_card_and_title_is_dup(tmp_path):
    msgs = [Msg(1, "仿写 做空比特币了", 0, [open_action("BTC")]),
            Msg(2, "仿写 BTC 空 入场 62000 止损 62200", 8, [open_action("BTC", entry=62000, stop=62200)])]
    r = build(tmp_path, msgs)
    title, card = episode(r, 1), episode(r, 2)
    assert card["dec_eligibility"]["entry_decision"] and card["plan_link_kind"] == "root"
    assert title["dup_of"] == card["episode_id"] and title["plan_link_kind"] == "body_after_title"
    assert "SAME_PLAN_COMPANION" in title["reason_codes"] and not title["dec_eligibility"]["entry_decision"]
    assert not title["eligibility_by_estimand"]["entry_decision"]
    assert card["t_dec"] == T0 + timedelta(seconds=9) and card["family_id"] == card["plan_group_id"]


def test_commentary_then_formal_merges_with_fuzzy_stop_from_formal(tmp_path):
    msgs = [Msg(1, "仿写 ETH 空 3180-3200 看回落", 0, [open_action("ETH", lo=3180, hi=3200)]),
            Msg(2, "仿写 ETH 3170-3190 空，小幅涨破3220一点止损", 600, [open_action("ETH", lo=3170, hi=3190, stop=3220)])]
    r = build(tmp_path, msgs)
    formal, comment = episode(r, 2), episode(r, 1)
    assert comment["dup_of"] == formal["episode_id"] and comment["plan_link_kind"] == "commentary_formal"
    assert formal["order_plan"]["stop"]["price"] == D("3229.66")
    assert formal["stop_rule"] == "r9_fuzzy_break" and formal["stop_base"] == D(3220)
    assert formal["t_dec"] == T0 + timedelta(seconds=601)


def test_formal_three_hours_later_is_late_stop_and_commentary_executes(tmp_path):
    msgs = [Msg(1, "仿写 ETH 空 3180-3200 看回落", 0, [open_action("ETH", lo=3180, hi=3200)]),
            Msg(2, "仿写 ETH 3170-3190 空，小幅涨破3220一点止损", 3 * 3600, [open_action("ETH", lo=3170, hi=3190, stop=3220)])]
    r = build(tmp_path, msgs)
    comment, formal = episode(r, 1), episode(r, 2)
    assert comment["dec_eligibility"]["entry_decision"] and comment["order_plan"]["stop"] is None
    assert comment["nostop_kind"] == "contract_no_stop" and comment["stop_rule"] is None
    assert formal["dup_of"] == comment["episode_id"] and formal["plan_link_kind"] == "late_stop"
    assert "SAME_PLAN_RESTATEMENT" in formal["reason_codes"]
    late = [row for row in r["summary"]["plan_link_rows"] if row["synthetic_action"] == "move_stop"]
    assert late and late[0]["synthetic_stop_price"] == D("3229.66") and late[0]["kept_episode_id"] == comment["episode_id"]


def test_repost_and_family_columns(tmp_path):
    msgs = [Msg(1, "仿写 BTC 多 66933 止损 66000", 0, [open_action("BTC", "long", entry=66933, stop=66000)]),
            Msg(2, "仿写 BTC 多 66933 止损 66000", 2 * 86400, [open_action("BTC", "long", entry=66933, stop=66000)])]
    r = build(tmp_path, msgs, marks={"BTC": 66900})
    first, second = episode(r, 1), episode(r, 2)
    assert second["repost_of"] == first["episode_id"] and second["plan_link_kind"] == "repost"
    assert second["family_id"] == first["family_id"] and second["dec_eligibility"]["entry_decision"]


def test_signal_age_counts_from_original_post_and_stale_signal_is_excluded(tmp_path):
    # A text card whose album photo arrives 1801 s later: t_dec moves past the 1800 s staleness rule.
    msgs = [Msg(1, "仿写 BTC 空 入场 62000 止损 62200", 0, [open_action("BTC", entry=62000, stop=62200)])]
    r = build(tmp_path, msgs)
    assert episode(r, 1)["signal_age_s"] == 0 and "STALE_SIGNAL_30M" not in episode(r, 1)["reason_codes"]
    msgs = [Msg(1, "仿写 SOL 216.4 空", 0, [open_action("SOL", entry="216.4")]),
            Msg(2, "止损:223", 1800, [stop_action(223)])]
    r = build(tmp_path / "edge", msgs)
    ep = episode(r, 1)
    assert ep["signal_age_s"] == 1800 and "STALE_SIGNAL_30M" not in ep["reason_codes"] and ep["dec_eligibility"]["entry_decision"]


def test_stale_signal_from_late_dependency(tmp_path, monkeypatch):
    msgs = [Msg(1, "仿写 BTC 空 入场 62000 止损 62200", 0, [open_action("BTC", entry=62000, stop=62200)])]
    real = lifecycle.plan_dependencies

    def late(root, *a, **k):
        at, refs = real(root, *a, **k)
        return (None if at is None else at + timedelta(seconds=1801)), refs
    monkeypatch.setattr(lifecycle, "plan_dependencies", late)
    r = build(tmp_path, msgs)
    ep = episode(r, 1)
    assert ep["signal_age_s"] == 1801 and "STALE_SIGNAL_30M" in ep["reason_codes"]
    assert not ep["dec_eligibility"]["entry_decision"] and not ep["eligibility_by_estimand"]["execution"]


def test_triage_sidecar_gates_stopless_roots_by_scope(tmp_path):
    msgs = [Msg(1, "仿写 BTC 空 62000", 0, [open_action("BTC", entry=62000)]),
            Msg(2, "仿写 ETH 多 3180", 7200, [open_action("ETH", "long", entry=3180)]),
            Msg(3, "仿写 SOL 多 216", 14400, [open_action("SOL", "long", entry=216)]),
            Msg(4, "仿写 BTC 多 61000 止损 60000", 21600, [open_action("BTC", "long", entry=61000, stop=60000)], author="other")]
    sv = lambda mid: f"sv{CHANNEL % 10}-{mid}"
    tri = triage_frame([dict(source_version_id=sv(1), verdict="new_entry", reason="other", venue_hint="spot"),
                        dict(source_version_id=sv(2), verdict="uncertain", reason="other")])
    main = build(tmp_path / "main", msgs, triage=tri)
    assert episode(main, 1)["dec_eligibility"]["entry_decision"] and episode(main, 1)["triage_verdict"] == "new_entry"
    assert episode(main, 1)["venue_hint"] == "spot" and episode(main, 1)["nostop_kind"] == "spot"
    assert "TRIAGE_UNCERTAIN" in episode(main, 2)["reason_codes"] and not episode(main, 2)["dec_eligibility"]["entry_decision"]
    assert "TRIAGE_MISSING" in episode(main, 3)["reason_codes"] and episode(main, 3)["triage_verdict"] == "missing"
    assert episode(main, 4)["triage_verdict"] == "n/a" and episode(main, 4)["dec_eligibility"]["entry_decision"]
    wide = build(tmp_path / "wide", msgs, triage=tri, wide=True)
    assert all(episode(wide, mid)["dec_eligibility"]["entry_decision"] for mid in (1, 2, 3, 4))
    assert "TRIAGE_MISSING" in episode(wide, 3)["reason_codes"]  # recorded, not blocking
    stage1 = build(tmp_path / "stage1", msgs)
    assert all(episode(stage1, mid)["dec_eligibility"]["entry_decision"] for mid in (1, 2, 3, 4))
    assert episode(stage1, 1)["triage_verdict"] is None


def test_triage_not_entry_never_executes_and_invalid_rows_count_separately(tmp_path):
    msgs = [Msg(1, "仿写 BTC 空 62000", 0, [open_action("BTC", entry=62000)]),
            Msg(2, "仿写 ETH 多 3180", 7200, [open_action("ETH", "long", entry=3180)])]
    sv = lambda mid: f"sv{CHANNEL % 10}-{mid}"
    tri = triage_frame([dict(source_version_id=sv(1), verdict="not_entry", reason="result_post"),
                        dict(source_version_id=sv(2), verdict="maybe", reason="other")])
    for wide in (False, True):
        r = build(tmp_path / str(wide), msgs, triage=tri, wide=wide)
        assert "TRIAGE_NOT_ENTRY" in episode(r, 1)["reason_codes"] and not episode(r, 1)["dec_eligibility"]["entry_decision"]
        assert episode(r, 1)["triage_reason"] == "result_post"
        assert "TRIAGE_INVALID" in episode(r, 2)["reason_codes"]
        assert episode(r, 2)["dec_eligibility"]["entry_decision"] is wide
    with pytest.raises(ValueError):
        lifecycle.triage_index(tri.drop("verdict"))
    with pytest.raises(ValueError):
        lifecycle.triage_index(pl.concat([tri, tri]))


@pytest.mark.parametrize("text,code", [("仿写 BTC 空 62000，合约先等等，现货布局", "CONTRACT_DISCOURAGED"),
                                       ("仿写 BTC 空 62000 不建议跟", "CONTRACT_DISCOURAGED"),
                                       ("仿写 BTC 关注区域 62000 附近，跌破 61000 失效", "CASH_WATCH_POST")])
def test_deterministic_not_entry_rules(tmp_path, text, code):
    r = build(tmp_path, [Msg(1, text, 0, [open_action("BTC", entry=62000)])])
    ep = episode(r, 1)
    assert code in ep["reason_codes"] and not ep["dec_eligibility"]["entry_decision"]


@pytest.mark.parametrize("text", ["仿写 BTC 空 62000，别跟大饼走反了", "仿写 BTC 空 62000，不要跟在后面追", "仿写 BTC 空 62000，只做现货"])
def test_dissuasion_false_friends_execute(tmp_path, text):
    ep = episode(build(tmp_path, [Msg(1, text, 0, [open_action("BTC", entry=62000)])]), 1)
    assert "CONTRACT_DISCOURAGED" not in ep["reason_codes"] and ep["dec_eligibility"]["entry_decision"]
    if "现货" in text:
        assert ep["venue_hint"] == "spot"


def test_promoted_wide_only_and_main_scope_promotion(tmp_path):
    past = open_action("ETH", "long", entry=3115, stop=3010, time_ref="past")
    cond = open_action("ETH", "long", entry=3115, stop=3010, time_ref="conditional")
    text = "仿写 交易策略：入场 3115，止损 3010，回踩确认再进"
    r = build(tmp_path, [Msg(1, text, 0, [past]), Msg(2, text, 7200, [cond], author="b")], marks={"ETH": 3120})
    wide_only, main = episode(r, 1), episode(r, 2)
    assert wide_only["time_ref_promoted"] == "setup_card" and wide_only["promotion_scope"] == "wide"
    assert "PROMOTED_WIDE_ONLY" in wide_only["reason_codes"] and not wide_only["dec_eligibility"]["entry_decision"]
    assert main["promotion_scope"] == "main" and main["dec_eligibility"]["entry_decision"]
    w = build(tmp_path / "w", [Msg(1, text, 0, [past])], marks={"ETH": 3120}, wide=True)
    assert episode(w, 1)["dec_eligibility"]["entry_decision"]


def test_conditional_confirmation_executes_at_the_confirm(tmp_path):
    cond = open_action("ETH", "long", entry=3000, stop=2950, time_ref="conditional")
    msgs = [Msg(1, "仿写 回踩 3000 确认就进多，止损 2950", 0, [cond]),
            Msg(2, "进了", 20 * 60, [open_action(None, None)], reply=1)]
    r = build(tmp_path, msgs, marks={"ETH": 3010})
    ep = episode(r, 2)
    assert ep["plan_link_kind"] == "conditional_confirmed" and ep["signal_anchor"] == "confirm"
    assert ep["instrument_id"].startswith("ETHUSDT") and ep["side"] == "long"
    assert ep["order_plan"]["entries"][0]["price_lo"] == D(3000) and ep["order_plan"]["stop"]["price"] == D(2950)
    assert ep["t_dec"] == T0 + timedelta(minutes=20, seconds=1) and ep["signal_age_s"] == 0
    late = build(tmp_path / "late", [msgs[0], Msg(2, "进了", 25 * 3600, [open_action(None, None)], reply=1)], marks={"ETH": 3010})
    assert episode(late, 2)["plan_link_kind"] == "root" and episode(late, 2)["t_dec"] is None


def test_v8e_moves_long_edit_out_of_the_pool(tmp_path):
    msgs = [Msg(1, "仿写 ETH 空 3180-3200 看回落", 0, [open_action("ETH", lo=3180, hi=3200)]),
            Msg(2, "仿写 ETH 3170-3190 空，小幅涨破3220一点止损", 600, [open_action("ETH", lo=3170, hi=3190, stop=3220)],
                grade="H1", edit_delay_s=int(26.5 * 3600))]
    main = build(tmp_path / "main", msgs)
    assert episode(main, 1)["dup_of"] == episode(main, 2)["episode_id"]
    assert episode(main, 2)["edit_delay_s"] == int(26.5 * 3600) and episode(main, 2)["edit_original_unavailable"]
    e = build(tmp_path / "e", msgs, max_edit_delay_s=1800)
    comment, formal = episode(e, 1), episode(e, 2)
    assert comment["dup_of"] is None and comment["dec_eligibility"]["entry_decision"]
    assert "STALE_EDIT_30M" in formal["reason_codes"] and not formal["dec_eligibility"]["entry_decision"]
    assert e["summary"]["rule_versions"]["variant"] == "max_edit_delay_s=1800"
    keep = build(tmp_path / "keep", [msgs[0], Msg(2, msgs[1].text, 600, msgs[1].actions, grade="H1", edit_delay_s=1800)], max_edit_delay_s=1800)
    assert episode(keep, 1)["dup_of"] == episode(keep, 2)["episode_id"]


def test_edit_may_contain_outcome_flag(tmp_path):
    msgs = [Msg(1, "仿写 BTC 空 入场 62000 止损 62200 ✅ TP1 已达", 0, [open_action("BTC", entry=62000, stop=62200)],
                grade="H1", edit_delay_s=600)]
    ep = episode(build(tmp_path, msgs), 1)
    assert ep["edit_may_contain_outcome"] and ep["t_dec"] == T0 + timedelta(seconds=1)


def test_cross_channel_never_merges(tmp_path):
    msgs = [Msg(1, "仿写 BTC 空 62000", 0, [open_action("BTC", entry=62000)]),
            Msg(2, "仿写 BTC 空 62000 止损 62200", 10, [open_action("BTC", entry=62000, stop=62200)], channel=OTHER)]
    r = build(tmp_path, msgs)
    assert episode(r, 1)["dup_of"] is None and episode(r, 2, OTHER)["dup_of"] is None


# ---------------------------------------------------------------- validate (F11-D3/D4, F9, F4 mark, F12)
def test_d4_symbol_inheritance_from_same_author_and_unit_rescale(tmp_path):
    msgs = [Msg(1, "仿写 BTC 78300 多", 0, [open_action("BTC", "long", entry=78300)]),
            Msg(2, "防守782", 6, [stop_action(782)])]
    r = build(tmp_path, msgs, marks={"BTC": 78300})
    stop_row = llm_row(r, 2)
    checks = json.loads(stop_row["checks"])
    assert stop_row["symbol_raw"] == "BTC" and checks["symbol_inherited"]["basis"] == "same_author_recent"
    assert stop_row["stop"] == D(78200) and checks["unit_rescaled"]["factor"] == "100"
    assert any(dep["ref"] == f"sv{CHANNEL % 10}-1" for dep in checks["dependencies"])
    assert episode(r, 1)["order_plan"]["stop"]["price"] == D(78200)


def test_d4_two_coins_in_window_inherit_nothing(tmp_path):
    msgs = [Msg(1, "仿写 BTC 78300 多", 0, [open_action("BTC", "long", entry=78300)]),
            Msg(2, "仿写 ETH 3100 多", 600, [open_action("ETH", "long", entry=3100)]),
            Msg(3, "防守3000", 1200, [stop_action(3000)])]
    r = build(tmp_path, msgs, marks={"BTC": 78300, "ETH": 3100})
    row = llm_row(r, 3)
    assert row["symbol_raw"] is None and "symbol_inherited" not in json.loads(row["checks"])
    late = [Msg(1, "仿写 BTC 78300 多", 0, [open_action("BTC", "long", entry=78300)]), Msg(3, "防守782", 1801, [stop_action(782)])]
    row = llm_row(build(tmp_path / "late", late, marks={"BTC": 78300}), 3)
    assert row["symbol_raw"] is None
    replied = [Msg(1, "仿写 BTC 78300 多", 0, [open_action("BTC", "long", entry=78300)]), Msg(3, "防守782", 7200, [stop_action(782)], reply=1)]
    row = llm_row(build(tmp_path / "reply", replied, marks={"BTC": 78300}), 3)
    assert row["symbol_raw"] == "BTC" and json.loads(row["checks"])["symbol_inherited"]["basis"] == "reply_parent"


def test_d3_promoted_open_without_side_takes_side_from_stop(tmp_path):
    card = open_action("ETH", None, entry=3115, stop=3010, time_ref="conditional")
    r = build(tmp_path, [Msg(1, "仿写 交易策略：入场 3115，止损 3010", 0, [card])], marks={"ETH": 3120})
    row = llm_row(r, 1)
    assert row["side"] == "long" and json.loads(row["checks"])["side_inferred_promoted"] == {"side": "long", "basis": "entry"}
    assert episode(r, 1)["dec_eligibility"]["entry_decision"] and episode(r, 1)["side"] == "long"


def test_f9_relative_points_resolved_against_mark(tmp_path):
    r = build(tmp_path, [Msg(1, "仿写 BTC 现价多，带1000点防守", 0, [open_action("BTC", "long")])], marks={"BTC": 100000})
    row = llm_row(r, 1)
    assert row["stop"] == D(99000) and json.loads(row["checks"])["stop_relative_resolved"]["ref"] == "mark"
    ep = episode(r, 1)
    assert ep["order_plan"]["stop"]["price"] == D(99000) and ep["stop_rule"] == "relative"


def test_f4_fuzzy_break_on_market_entry_uses_the_mark(tmp_path):
    r = build(tmp_path, [Msg(1, "仿写 BTC 现价多，小幅跌破就止损", 0, [open_action("BTC", "long")])], marks={"BTC": 66000})
    row = llm_row(r, 1)
    assert row["stop"] == D(66000) * D("0.997") and json.loads(row["checks"])["stop_from_mark"]["mark"] == "66000"
    ep = episode(r, 1)
    assert ep["stop_rule"] == "r9_fuzzy_break" and ep["stop_base"] == D(66000)


def test_f4_close_clause_keeps_a_close_trigger(tmp_path):
    action = open_action("BTC", "long", entry=70000, stop=68000)
    action["entry"]["price"] = dict(value="70000", quote="7万")
    action["stop"]["price"] = dict(value="68000", quote="6.8万")
    r = build(tmp_path, [Msg(1, "仿写 BTC 7万多，日线收盘跌破6.8万止损", 0, [action])], marks={"BTC": 70000})
    ep = episode(r, 1)
    assert ep["stop_rule"] == "close_from_clause"
    assert ep["order_plan"]["stop"] == {"price": D(68000), "trigger": "close", "timeframe": "1d"}


def test_f4_explained_missing_condition_stop_is_a_nostop_plan(tmp_path):
    action = open_action("BTC", "short", entry=70000, stop_condition="突破就止损")
    action["entry"]["price"] = dict(value="70000", quote="7万")
    r = build(tmp_path, [Msg(1, "仿写 BTC 7万空，突破就止损", 0, [action])], marks={"BTC": 70000})
    row = llm_row(r, 1)
    checks = json.loads(row["checks"])
    assert checks["nostop_hint"] == "zero_distance_break" and checks["stop_condition_dropped"] == "zero_distance_break"
    assert "mapping_issues" not in checks and json.loads(row["eligibility_by_estimand"])["execution"]
    ep = episode(r, 1)
    assert ep["order_plan"]["stop"] is None and ep["nostop_kind"] == "zero_distance_break" and ep["dec_eligibility"]["entry_decision"]


@pytest.mark.parametrize("text,kept", [("仿写 SOL 现货长线 多 150，目标 600", True), ("仿写 SOL 多 150，目标 600", False)])
def test_f12_spot_long_term_targets_survive_the_scale_gate(tmp_path, text, kept):
    r = build(tmp_path, [Msg(1, text, 0, [open_action("SOL", "long", entry=150, stop=140, tps=[600])])], marks={"SOL": 150})
    row = llm_row(r, 1)
    assert [t["level"] for t in row["tps"]] == ([D(600)] if kept else [])
    assert ("tp_gate" in json.loads(row["checks"])) is kept


def test_promoted_time_ref_is_current_at_every_consumer():
    """F11/E1: plan_source.descriptive_only, validate execution and followup's two filters all read effective_time_ref."""
    from quant_lab.data import followup, plan_source
    promoted = {"schema_version": 2, "op": "open", "time_ref": "conditional",
                "time_ref_promoted": {"from": "conditional", "rule": "setup_card", "scope": "main"}}
    plain = dict(promoted, time_ref_promoted=None)
    assert not plan_source.descriptive_only({"checks": json.dumps(promoted)})
    assert plan_source.descriptive_only({"checks": json.dumps(plain)})
    mgmt = dict(promoted, op="stop_move")
    assert followup.qualifying_source_ids([{"checks": mgmt, "source_version_id": "a"}, {"checks": dict(mgmt, time_ref_promoted=None), "source_version_id": "b"}]) == {"a"}


# ---------------------------------------------------------------- run / CLI / variants / followup (F13)
def test_api_edit_clocks_are_mutually_exclusive(tmp_path):
    from quant_lab.data import api
    with pytest.raises(SystemExit):
        api.main(["--build", "--fixture", str(tmp_path), "--out", str(tmp_path / "o"), "--edit-visible-at-post", "--edit-visible-at-last-edit"])
    with pytest.raises(ValueError):
        api.build(tmp_path, lifecycle.Layout.flat(tmp_path / "o"), graph_version="g", edit_visible_at_post=True, edit_visible_at_last_edit=True)



def _published(tmp_path, msgs, **kw):
    r = build(tmp_path, msgs, **kw)
    return r, r["layout"]


def _cli(layout, *args):
    return lifecycle.main(["--out", str(layout.canonical_plan.parent), *args])


def test_cli_variants_like_signature_and_side_tables(tmp_path, capsys):
    msgs = [Msg(1, "仿写 SOL 216.4 空", 0, [open_action("SOL", entry="216.4")]), Msg(2, "止损:223", 6, [stop_action(223)])]
    _, layout = _published(tmp_path, msgs)
    with pytest.raises(SystemExit):
        _cli(layout, "--graph-version", "g")  # no --like: plan source and registry must be explicit
    _cli(layout, "--graph-version", "g", "--plan-source", "llm", "--registry-version", "registry-synthetic-v1", "--alias")
    from quant_lab.data.graph import read_manifest, resolve_alias
    main = resolve_alias(layout, "g")
    doc = read_manifest(layout, main)
    assert doc["assumptions"]["plan_source"] == "llm" and doc["assumptions"]["silver_signature"]
    loss_before = sorted((p.name, p.stat().st_mtime_ns) for p in layout.loss_dir.glob("*"))
    _cli(layout, "--graph-version", "g-nw", "--like", "g", "--supplement-window-s", "0", "--alias")
    _cli(layout, "--graph-version", "g-w", "--like", "g", "--wide", "--alias")
    assert sorted((p.name, p.stat().st_mtime_ns) for p in layout.loss_dir.glob("*")) == loss_before  # variants never write layer 6
    hashes = {read_manifest(layout, resolve_alias(layout, gv))["input_hash"] for gv in ("g", "g-nw", "g-w")}
    assert len(hashes) == 3
    nw = pl.read_parquet(layout.episode(resolve_alias(layout, "g-nw")))
    assert nw.filter(pl.col("root_message_id") == 1)["order_plan"][0]["stop"] is None
    links = pl.read_parquet(lifecycle.plan_link_path(layout, resolve_alias(layout, "g-nw")))
    assert links.filter(pl.col("plan_link_kind") == "late_stop").height == 1
    for col in ("event_time", "available_at", "ingested_at"):
        assert links.schema[col] == pl.Datetime("us", "UTC")
    # A changed silver input breaks --like.
    cp = pl.read_parquet(layout.canonical_plan)
    cp.with_columns(pl.lit("x").alias("symbol_raw")).write_parquet(layout.canonical_plan)
    with pytest.raises(RuntimeError, match="silver"):
        _cli(layout, "--graph-version", "g-e", "--like", "g", "--max-edit-delay-s", "1800")
    cp.write_parquet(layout.canonical_plan)


def test_triage_sidecar_in_silver_enters_input_hash(tmp_path):
    msgs = [Msg(1, "仿写 BTC 空 62000", 0, [open_action("BTC", entry=62000)])]
    _, layout = _published(tmp_path, msgs)
    before = lifecycle.input_hash_of(layout, plan_source="llm")
    sv = f"sv{CHANNEL % 10}-1"
    triage_frame([dict(source_version_id=sv, verdict="new_entry")]).write_parquet(lifecycle.triage_path(layout))
    first = lifecycle.input_hash_of(layout, plan_source="llm")
    triage_frame([dict(source_version_id=sv, verdict="not_entry")]).write_parquet(lifecycle.triage_path(layout))
    second = lifecycle.input_hash_of(layout, plan_source="llm")
    assert len({before, first, second}) == 3
    out = lifecycle.run(layout, graph_version="t", plan_source="llm")
    assert out["rule_versions"]["triage"].startswith("sidecar:") and out["v8"]["reason_codes"]["TRIAGE_NOT_ENTRY"] == 1


def test_followup_synthetic_rows_and_dup_redirect(tmp_path):
    from quant_lab.data import followup
    msgs = [Msg(1, "仿写 ETH 空 3180-3200 看回落", 0, [open_action("ETH", lo=3180, hi=3200)]),
            Msg(2, "仿写 ETH 3170-3190 空，小幅涨破3220一点止损", 3 * 3600, [open_action("ETH", lo=3170, hi=3190, stop=3220)])]
    r, layout = _published(tmp_path, msgs)
    lifecycle.run(layout, graph_version="f", plan_source="llm")
    episodes = pl.read_parquet(layout.episode("f")).to_dicts()
    rows, counts = followup.synthetic_rows(layout, "f", episodes)
    comment = next(e for e in episodes if e["root_message_id"] == 1)
    assert counts == {"move_stop": 1} and rows[0]["episode_id"] == comment["episode_id"]
    assert rows[0]["stop_price"] == D("3229.66") and rows[0]["uncertain"] is False and rows[0]["source"] == "plan_merge"
    assert rows[0]["available_at"] == T0 + timedelta(hours=3) and rows[0]["available_at"] > comment["t_dec"]
    # An instruction aimed at the merged (dup_of) message is managed through the kept plan.
    formal = next(e for e in episodes if e["root_message_id"] == 2)
    assert formal["dup_of"] == comment["episode_id"]
    episode_id, ambiguity, redirected = followup._map_episode_redirect(CHANNEL, 2, episodes)
    assert (episode_id, ambiguity, redirected) == (comment["episode_id"], None, formal["episode_id"])
    frame = followup._write_table(rows, tmp_path / "fu.parquet")
    assert frame.height == 1 and frame["mapping_version"][0] == followup.MAPPING_VERSION
