"""v8 F2/F6 plan_merge on invented plans only (方案 F2 仿写测试 1–24；数字自拟，没有真实消息原文)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from quant_lab.data import plan_merge as pm
from quant_lab.data.plan_merge import Item, merge

T0 = datetime(2025, 3, 3, 8, tzinfo=UTC)
BTC, ETH, SOL, SUI, PENDLE = "BTCUSDT-PERP", "ETHUSDT-PERP", "SOLUSDT-PERP", "SUIUSDT-PERP", "PENDLEUSDT-PERP"


def it(pid, mid, dt_s=0, *, inst=BTC, side="short", legs=(), stop=None, market=False, mark=None, author="a", reply=None, channel=1,
       text="", seq=None, branch=0, tps=(), media=False, title=None, card=None, supplement_of=None, conditional=False, entry_kind=None,
       executable=True, relation=None, relation_target=None, t_post=None, quote=None):
    legs = tuple(D(str(x)) for x in legs)
    stop = None if stop is None else D(str(stop))
    if title is None:
        title = not legs and stop is None and len(text.replace(" ", "")) <= 40
    return Item(pid=pid, svid="sv-" + pid, channel=channel, message_id=mid, t_vis=T0 + timedelta(seconds=dt_s),
                t_post=None if t_post is None else T0 + timedelta(seconds=t_post), author=author, reply_to=reply, sequence=seq, inst=inst,
                side=side, branch_index=branch, legs=legs, market_ref=market, mark=None if mark is None else D(str(mark)), quote=None if quote is None else D(str(quote)),
                entry_kind=entry_kind or ("market_ref" if market and not legs else "limit"), stop=stop, tps=tuple(D(str(x)) for x in tps),
                text=text, segment=text, has_media=media, is_title=title, card=pm.is_card(text) if card is None else card,
                venue_words=bool(pm.VENUE_WORDS.search(text)), reentry_words=bool(pm.REENTRY_WORDS.search(text)),
                supplement_of=supplement_of, conditional=conditional, executable=executable, relation=relation, relation_target=relation_target)


def test_1_title_then_body_body_kept():
    title = it("t", 1, 0, market=True, mark=62050, text="做空比特币了")
    body = it("b", 2, 8, legs=[62000], stop=62200, text="BTC 空 入场 62000 止损 62200")
    links, _ = merge([title, body])
    assert links["b"].kind == "root" and links["b"].dup_of is None and links["b"].stop == D(62200)
    assert links["t"].dup_of == "b" and links["t"].kind == "body_after_title"
    assert set(links["b"].providers) == {"b"}  # t_dec = t(body) + 1 s: the title supplies no field


def test_2_body_then_title_is_restatement():
    body = it("b", 1, 0, legs=[62000], stop=62200, text="BTC 空 入场 62000 止损 62200")
    title = it("t", 2, 5, market=True, mark=62050, text="做空比特币了")
    links, _ = merge([body, title])
    assert links["t"].dup_of == "b" and links["t"].kind == "restatement"


def test_3_image_then_text_text_kept_stop_from_image():
    image = it("img", 1, 0, inst=PENDLE, side="long", market=True, mark="3.22", stop="3.05", media=True, text="多 PENDLE")
    text = it("txt", 2, 60, inst=PENDLE, side="long", legs=["3.20", "3.25"], text="PENDLE 入场 3.20-3.25")
    links, _ = merge([image, text])
    assert links["txt"].kept == "txt" and links["txt"].dup_of is None
    assert links["txt"].stop == D("3.05") and links["txt"].stop_pid == "img"
    assert links["img"].dup_of == "txt" and links["img"].kind == "image_text_pair"
    assert set(links["txt"].providers) == {"txt", "img"}


def test_4_bilingual_double_post_is_companion():
    zh = it("zh", 1, 0, legs=[62000], stop=62200, text="BTC 空 62000 止损 62200")
    en = it("en", 2, 2, legs=[62000], stop=62200, reply=1, text="BTC short 62000 SL 62200")
    links, _ = merge([zh, en])
    assert links["en"].dup_of == "zh" and links["en"].kind == "companion"


def commentary(dt=0, mid=1):
    return it("c", mid, dt, inst=ETH, legs=[3180, 3200], text="ETH 空 3180-3200 看回落")


def formal(dt, mid=2, edit=None):
    return it("f", mid, dt, inst=ETH, legs=[3170, 3190], stop="3229.66", text="ETH 3170-3190 空，小幅涨破3220一点止损")


def test_5_commentary_then_formal_within_window_merges():
    links, _ = merge([commentary(), formal(600)])
    assert links["f"].dup_of is None and links["f"].stop == D("3229.66")
    assert links["c"].dup_of == "f" and links["c"].kind == "commentary_formal"
    assert set(links["f"].providers) == {"f"}


def test_6_formal_three_hours_later_is_late_stop():
    links, counts = merge([commentary(), formal(3 * 3600)])
    assert links["c"].dup_of is None and links["c"].kind == "root"
    assert links["f"].kind == "late_stop" and links["f"].dup_of == "c"
    assert links["f"].synthetic == [{"action": "move_stop", "target": "c", "at": T0 + timedelta(hours=3), "stop": D("3229.66")}]
    assert counts["late_entry_dropped"] == 1


def test_7_commentary_after_formal_is_repost_then_independent_after_a_day():
    f = it("f", 1, 0, inst=ETH, legs=[3170, 3190], stop="3229.66", text="ETH 空 3170-3190 止损 3229.66")
    late = it("c", 2, 5 * 3600, inst=ETH, legs=[3183], text="ETH 3183 空")
    links, _ = merge([f, late])
    assert links["c"].kind == "repost" and links["c"].repost_of == "f" and links["c"].family == links["f"].family
    much_later = it("c", 2, 26 * 3600, inst=ETH, legs=[3183], text="ETH 3183 空")
    links, _ = merge([f, much_later])
    assert links["c"].kind == "root" and links["c"].repost_of is None


def test_8_same_message_long_term_leg_stays_independent():
    long_term = it("a", 1, 0, legs=[98000], text="98000 空，长线 1 倍不设止损", branch=0)
    stopped = it("b", 1, 0, legs=[94000, 94400], stop=95200, text="94000-94400 空，止损 95200", branch=1)
    links, _ = merge([long_term, stopped])
    assert links["a"].dup_of is None and links["b"].dup_of is None
    assert links["a"].kind == links["b"].kind == "root"


def test_9_reentry_is_independent_without_parent_stop():
    t = it("t", 1, 0, inst=ETH, legs=[2803], stop=2865, text="2803 空 止损 2865")
    c = it("c", 2, 3600, inst=ETH, legs=[2770], reply=1, text="再到 2770 附近可二次进场")
    links, _ = merge([t, c])
    assert links["c"].kind == "reentry" and links["c"].reentry_of == "t" and links["c"].stop is None
    assert links["c"].reentry_parent_stop == D(2865)


def test_10_reply_at_market_past_the_limit_is_amend():
    """The spec's own numbers: limit 2270, market 2285 (0.66% away) — the limit can no longer fill, so C amends T."""
    t = it("t", 1, 0, inst=ETH, side="long", legs=[2270], stop=2205, text="挂单 2270 做多 止损 2205")
    c = it("c", 2, 58 * 60, inst=ETH, side="long", market=True, mark=2285, quote=2285, reply=1, text="现价 2285 入场")
    links, _ = merge([t, c])
    assert links["c"].kind == "amend" and links["c"].amend_of == "t"
    assert links["c"].stop == D(2205) and links["c"].stop_pid == "t"
    assert links["c"].synthetic == [{"action": "cancel_pending", "target": "t", "at": T0 + timedelta(minutes=58), "stop": None}]
    # Only the quote is known (no as-of mark): still compared, still past the limit.
    quoted = it("c", 2, 58 * 60, inst=ETH, side="long", market=True, quote=2285, reply=1, text="现价 2285 入场")
    assert merge([t, quoted])[0]["c"].kind == "amend"


def test_10c_stop_after_an_amend_never_moves_the_replaced_plan_and_lands_on_the_amend():
    """Review fix: a stopless limit T, amended at +300 s by a market reply C, then a stop supplement for T at +600 s
    (inside W_sup). T's group is frozen by the amend: the stop is not absorbed (T keeps t_dec at its own post, so the
    cancel_pending at t_vis(C) stays after it), and it becomes a move_stop on C, the plan that replaced T."""
    t = it("t", 1, 0, inst=ETH, side="long", legs=[2270], text="挂单 2270 做多 ETH")
    c = it("c", 2, 300, inst=ETH, side="long", market=True, mark=2286, quote=2285, reply=1, text="现价 2285 入场")
    s = it("s", 3, 600, inst=ETH, side="long", stop=2205, supplement_of="t", reply=1, text="止损:2205")
    links, counts = merge([t, c], [s])
    assert links["c"].kind == "amend" and links["c"].stop is None
    assert links["c"].synthetic == [{"action": "cancel_pending", "target": "t", "at": T0 + timedelta(seconds=300), "stop": None}]
    assert links["t"].stop is None and set(links["t"].providers) == {"t"}
    assert links["s"].kind == "late_stop" and links["s"].gap_s == 600
    assert links["s"].synthetic == [{"action": "move_stop", "target": "c", "at": T0 + timedelta(seconds=600), "stop": D(2205)}]
    assert counts["frozen_stop_as_move"] == 1
    # The same stop as a full plan message replying to T (adds a stop to T) is not merged into the frozen T either.
    # (Another author, so T is its only link target; the same author within 1800 s of C would also link C → ambiguous.)
    d = it("d", 3, 600, inst=ETH, side="long", legs=[2270], stop=2205, reply=1, author="b", text="ETH 2270 多 止损 2205")
    links, _ = merge([t, c, d])
    assert links["t"].stop is None and set(links["t"].providers) == {"t"}
    assert links["d"].kind == "late_stop" and links["d"].dup_of == "t"
    assert links["d"].synthetic == [{"action": "move_stop", "target": "c", "at": T0 + timedelta(seconds=600), "stop": D(2205)}]
    # Without the amend the supplement still merges into T (unchanged F6 behaviour).
    links, _ = merge([t], [s])
    assert links["s"].kind == "supplement" and links["t"].stop == D(2205)


def test_10d_an_amend_with_its_own_stop_leaves_a_later_stop_for_t_to_followup():
    t = it("t", 1, 0, inst=ETH, side="long", legs=[2270], text="挂单 2270 做多 ETH")
    c = it("c", 2, 300, inst=ETH, side="long", market=True, mark=2286, quote=2285, stop=2210, reply=1,
           relation="amends", relation_target=1, text="现价 2285 入场 止损 2210")
    s = it("s", 3, 600, inst=ETH, side="long", stop=2205, supplement_of="t", reply=1)
    links, _ = merge([t, c], [s], stage=2)
    assert links["c"].kind == "amend" and links["c"].stop == D(2210)
    assert links["s"].kind == "stop_move" and not links["s"].synthetic and links["t"].stop is None


def test_10e_a_reposted_group_is_frozen_too():
    """An identical repost R freezes T: a stop supplement for T after R is a move_stop on T, never a t_dec change."""
    t = it("t", 1, 0, inst=ETH, side="long", legs=[2270], text="ETH 2270 多")
    r = it("r", 2, 120, inst=ETH, side="long", legs=[2270], author=None, text="ETH 2270 多")
    s = it("s", 3, 600, inst=ETH, side="long", stop=2205, supplement_of="t", reply=1)
    links, _ = merge([t, r], [s])
    assert links["r"].kind == "repost" and links["r"].repost_of == "t"
    assert links["t"].stop is None and set(links["t"].providers) == {"t"}
    assert links["s"].kind == "late_stop"
    assert links["s"].synthetic == [{"action": "move_stop", "target": "t", "at": T0 + timedelta(seconds=600), "stop": D(2205)}]


@pytest.mark.parametrize("side,legs,stop,price,kind", [
    ("long", [2270], 2205, 2265, "restatement"),   # below a long limit: T can still fill, not an amend
    ("short", [2270], 2330, 2275, "restatement"),  # above a short limit: T can still fill
    ("short", [2270], 2330, 2255, "amend"),        # below a short limit: past it
])
def test_10b_amend_needs_the_side_the_limit_cannot_fill(side, legs, stop, price, kind):
    t = it("t", 1, 0, inst=ETH, side=side, legs=legs, stop=stop)
    c = it("c", 2, 20 * 60, inst=ETH, side=side, market=True, mark=price, quote=price, reply=1, text=f"现价 {price} 入场")
    assert merge([t, c])[0]["c"].kind == kind
    # quote and mark must agree: one on each side is not an amend
    split = it("c", 2, 20 * 60, inst=ETH, side=side, market=True, mark=2270, quote=price, reply=1)
    assert merge([t, split])[0]["c"].kind != "amend"


def test_11_identical_repost_two_days_later():
    t = it("t", 1, 0, side="long", legs=[66933], stop=66000)
    c = it("c", 2, 2 * 86400, side="long", legs=[66933], stop=66000, author="b")
    links, _ = merge([t, c])
    assert links["c"].kind == "repost" and links["c"].repost_of == "t"


def test_12_param_free_confirmation_reply_is_permanent_and_unreplied_later_is_repost():
    t = it("t", 1, 0, side="long", legs=[66000], stop=65000)
    ack = it("ack", 2, 2 * 86400 - 1, side="long", market=True, mark=70000, reply=1, text="上车", title=True)
    links, _ = merge([t, ack])
    assert links["ack"].kind == "restatement" and links["ack"].dup_of == "t"
    ack3h = it("ack", 2, 3 * 3600, side="long", market=True, text="上车", title=True)
    links, _ = merge([t, ack3h])
    assert links["ack"].kind == "repost" and links["ack"].repost_of == "t"


def test_13_stop_supplement_member_merges_and_moves_t_dec():
    root = it("r", 1, 0, inst=SOL, legs=["216.4"], text="SOL 216.4 空")
    supp = it("s", 2, 6, inst=SOL, stop=223, supplement_of="r", text="止损:223")
    links, _ = merge([root], [supp])
    assert links["s"].kind == "supplement" and links["r"].stop == D(223) and links["r"].stop_pid == "s"
    assert "s" in links["r"].providers


@pytest.mark.parametrize("gap,window,kind", [(1800, 1800, "supplement"), (1801, 1800, "late_stop"), (1800, 0, "late_stop"), (1801, 0, "late_stop")])
def test_14_supplement_window_edges(gap, window, kind):
    root = it("r", 1, 0, inst=SOL, legs=["216.4"])
    supp = it("s", 2, gap, inst=SOL, stop=223, supplement_of="r")
    links, _ = merge([root], [supp], supplement_window_s=window)
    assert links["s"].kind == kind
    if kind == "late_stop":
        assert links["r"].stop is None and links["s"].synthetic[0]["action"] == "move_stop" and links["s"].synthetic[0]["stop"] == D(223)
    else:
        assert links["r"].stop == D(223)


def test_14c_stop_linked_to_a_restatement_is_timed_from_the_original_post():
    """T posted at t0, a restatement R at +1500 s (dup_of T), a stop linked by the linker to R at +3000 s: Δ is 3000 s
    from T's post, so it is a late stop (move_stop), and T keeps its own t_dec (no provider added)."""
    t = it("t", 1, 0, inst=SOL, legs=["216.4"], text="SOL 216.4 空")
    r = it("r", 2, 1500, inst=SOL, legs=["216.4"], text="SOL 216.4 空")
    s = it("s", 3, 3000, inst=SOL, stop=223, supplement_of="r", reply=2, text="止损:223")
    links, _ = merge([t, r], [s])
    assert links["r"].dup_of == "t" and links["r"].kind == "restatement"
    assert links["s"].kind == "late_stop" and links["s"].gap_s == 3000 and links["s"].target_message_id == 1
    assert links["t"].stop is None and set(links["t"].providers) == {"t"}
    assert links["s"].synthetic == [{"action": "move_stop", "target": "t", "at": T0 + timedelta(seconds=3000), "stop": D(223)}]
    within = it("s", 3, 1700, inst=SOL, stop=223, supplement_of="r", reply=2)
    links, _ = merge([t, r], [within])
    assert links["s"].kind == "supplement" and links["s"].gap_s == 1700 and links["t"].stop == D(223)


def test_14d_supplement_without_a_reference_price_is_rejected():
    root = it("r", 1, 0, inst=SOL, market=True, mark=None, text="SOL 现价空")
    links, counts = merge([root], [it("s", 2, 6, inst=SOL, stop=100, supplement_of="r")])
    assert links["s"].supplement_rejected == "no_reference" and links["r"].stop is None and counts["supplement_rejected"] == 1


@pytest.mark.parametrize("stop,reason", [(210, "wrong_side"), (400, "too_far")])
def test_14b_supplement_gates(stop, reason):
    root = it("r", 1, 0, inst=SOL, legs=["216.4"])
    supp = it("s", 2, 6, inst=SOL, stop=stop, supplement_of="r")
    links, counts = merge([root], [supp])
    assert links["s"].supplement_rejected == reason and links["r"].stop is None and counts["supplement_rejected"] == 1
    links, _ = merge([root], [supp], rejected_supplements={"s": "scale_not_unique"})
    assert links["s"].supplement_rejected == "scale_not_unique"


def test_16_same_message_split_without_stop():
    main = it("m", 1, 0, side="long", legs=["92109.7"], stop="91479.1", branch=0, text="92109.7 多 止损 91479.1")
    branch = it("x", 1, 0, side="long", market=True, mark=92300, branch=1, text="现在在 92300 附近")
    links, _ = merge([main, branch])
    assert links["x"].kind == "same_message_split" and links["x"].dup_of == "m"


def test_17b_venue_sibling_of_a_merged_branch_never_reranks_an_older_plan():
    """Branch s of message 2 is a restatement of the older spot post T; its contract-worded sibling C follows s into T's
    group without re-ranking T's plan (keep, providers and stop source stay T's)."""
    t = it("t", 1, 0, inst=SUI, side="long", legs=["3.90"], stop="3.70", text="SUI 现货 3.90 止损 3.70")
    s = it("s", 2, 60, inst=SUI, side="long", legs=["3.90"], stop="3.70", branch=0, text="SUI 现货 3.90 止损 3.70")
    c = it("c", 2, 60, inst=SUI, side="long", legs=["3.90"], stop="3.70", branch=1, text="SUI U本位 3.90 止损 3.70")
    links, _ = merge([t, s, c])
    assert links["t"].dup_of is None and links["t"].kept == "t" and links["t"].stop_pid == "t" and set(links["t"].providers) == {"t"}
    assert links["s"].dup_of == "t" and links["c"].dup_of == "t" and links["c"].kind == links["s"].kind == "restatement"


def test_17_venue_branches_keep_the_contract_one():
    spot = it("s", 1, 0, inst=SUI, side="long", legs=["3.90"], stop="3.70", branch=0, text="SUI 现货 3.90 止损 3.70")
    um = it("u", 1, 0, inst=SUI, side="long", legs=["3.90"], stop="3.70", branch=1, text="SUI U本位 3.90 止损 3.70")
    cm = it("c", 1, 0, inst=SUI, side="long", legs=["3.90"], stop="3.70", branch=2, text="SUI 币本位 3.90 止损 3.70")
    links, _ = merge([spot, um, cm])
    executed = [pid for pid, link in links.items() if link.dup_of is None]
    assert executed == ["u"]
    assert links["s"].kind == links["c"].kind == "same_message_venue"


def test_18_conditional_confirmation_within_a_day():
    parent = it("p", 1, 0, inst=ETH, side="long", legs=[3000], stop=2950, conditional=True, text="回踩 3000 确认就进多，止损 2950")
    confirm = it("c", 2, 20 * 60, inst=None, side=None, reply=1, market=True, text="进了", title=True)
    links, _ = merge([confirm], conditionals=[parent])
    assert links["c"].kind == "conditional_confirmed" and links["c"].conditional_parent == "p"
    late = it("c", 2, 25 * 3600, inst=None, side=None, reply=1, market=True, text="进了", title=True)
    links, _ = merge([late], conditionals=[parent])
    assert links["c"].kind == "root" and links["c"].conditional_parent is None


def test_18b_reply_to_an_ordinary_plan_is_not_a_confirmation_of_another_conditional():
    """C replies to plan T; the same author's conditional P is within 1800 s. C is a restatement of T, never P's confirm."""
    parent = it("p", 1, 0, inst=ETH, side="long", legs=[3000], stop=2950, conditional=True, text="回踩 3000 确认就进多，止损 2950")
    t = it("t", 2, 60, inst=ETH, side="long", legs=[3050], stop=2990, text="ETH 3050 多 止损 2990")
    c = it("c", 3, 600, inst=ETH, side="long", reply=2, market=True, text="上车", title=True)
    links, _ = merge([t, c], conditionals=[parent])
    assert links["c"].kind == "restatement" and links["c"].dup_of == "t" and links["c"].conditional_parent is None
    # No reply at all: the same-author fallback still confirms P.
    free = it("c", 3, 600, inst=None, side=None, market=True, text="进了", title=True)
    links, _ = merge([free], conditionals=[parent])
    assert links["c"].kind == "conditional_confirmed" and links["c"].conditional_parent == "p"


def test_19_equal_time_without_sequence_never_merges():
    a = it("a", 1, 0, legs=[62000], text="BTC 空 62000")
    b = it("b", 2, 0, legs=[62000], stop=62200, text="BTC 空 62000 止损 62200")
    links, _ = merge([a, b])
    assert links["a"].dup_of is None and links["b"].dup_of is None
    a.sequence, b.sequence = 1, 2
    links, _ = merge([a, b])
    assert links["a"].dup_of == "b"


def test_20_never_across_channels():
    a = it("a", 1, 0, legs=[62000], channel=1)
    b = it("b", 2, 10, legs=[62000], stop=62200, channel=2)
    links, _ = merge([a, b])
    assert links["a"].dup_of is None and links["b"].dup_of is None and links["b"].repost_of is None


def test_21_late_explicit_entry_on_market_plan_is_repost():
    t = it("t", 1, 0, side="long", market=True, mark=66000, stop=65000)
    c = it("c", 2, 20 * 60, side="long", legs=[66100])
    links, _ = merge([t, c])
    assert links["t"].kind == "root" and links["t"].dup_of is None
    assert links["c"].kind == "repost" and links["c"].repost_of == "t" and not links["t"].synthetic


def test_22_late_entry_supplement_without_stop_never_moves_a_stop_to_none():
    limit = it("t", 1, 0, side="long", legs=[66000])
    c = it("c", 2, 40 * 60, side="long", legs=[66050])
    links, _ = merge([limit, c])
    assert links["c"].kind == "repost" and not links["c"].synthetic
    market = it("t", 1, 0, side="long", market=True, mark=66000)
    links, _ = merge([market, it("c", 2, 40 * 60, side="long", legs=[66050])])
    assert links["c"].kind == "repost" and not any(s["action"] == "move_stop" for link in links.values() for s in link.synthetic)


# Spec test 24 (v8e removes the long-edited H1 formal card from the pool before merging) is a lifecycle behaviour:
# it is covered end to end by tests/data/test_v8_gold.py::test_v8e_moves_long_edit_out_of_the_pool.


def test_unrelated_author_far_apart_stays_independent_and_ambiguity_is_counted():
    a = it("a", 1, 0, legs=[62000], stop=62300, author="x")
    b = it("b", 2, 60, legs=[62010], stop=62310, author="x")
    c = it("c", 3, 120, legs=[61000], author="x")  # 1.6% away from both: a different plan
    links, _ = merge([a, b, c])
    assert links["c"].dup_of is None and links["c"].kind == "root"
    d = it("d", 4, 180, market=True, author="x", text="上车", title=True)
    two = [it("a", 1, 0, legs=[62000], stop=62300, author="x"), it("e", 5, 30, legs=[61000], stop=61400, author="x")]
    links, counts = merge(two + [d])
    assert links["d"].kind == "root" and counts["ambiguous_targets"] == 1


def test_stage2_flip_when_kept_is_not_executable():
    t = it("t", 1, 0, legs=[62000], stop=62200, executable=False)
    ack = it("ack", 2, 60, market=True, reply=1, text="上车", title=True)
    links, _ = merge([t, ack], stage=1)
    assert links["ack"].dup_of == "t"
    links, _ = merge([t, ack], stage=2)
    assert links["ack"].kind == "root" and links["ack"].dup_of is None


def test_stage2_relation_amends_and_reenters():
    t = it("t", 1, 0, inst=ETH, side="long", legs=[2270], stop=2205)
    c = it("c", 2, 600, inst=ETH, side="long", legs=[2272], relation="amends", relation_target=1)
    assert merge([t, c], stage=1)[0]["c"].kind == "restatement"
    links, _ = merge([t, c], stage=2)
    assert links["c"].kind == "amend" and links["c"].amend_of == "t"
    c2 = it("c", 2, 600, inst=ETH, side="long", legs=[2272], relation="reenters", relation_target=1)
    assert merge([t, c2], stage=2)[0]["c"].kind == "reentry"


def test_repost_family_prefers_latest_member():
    t = it("t", 1, 0, side="long", legs=[66000], stop=65000)
    r1 = it("r1", 2, 3 * 3600, side="long", market=True, text="上车", title=True)
    r2 = it("r2", 3, 5 * 3600, side="long", market=True, text="上车", title=True)
    links, _ = merge([t, r1, r2])
    assert links["r1"].repost_of == "t" and links["r2"].kind == "repost"
    assert links["r1"].family == links["r2"].family == links["t"].family


def test_compatible_and_identical_thresholds():
    t = it("t", 1, 0, legs=[100], stop=110)
    assert pm.compatible(it("c", 2, 1, legs=["100.9"], stop="110.9"), t)
    assert not pm.compatible(it("c", 2, 1, legs=["101.1"]), t)
    assert pm.identical(it("c", 2, 1, legs=["100.05"], stop="110.05"), t)
    assert not pm.identical(it("c", 2, 1, legs=["100.2"], stop=110), t)
    assert not pm.compatible(it("c", 2, 1, legs=[100], side="long"), t)


@pytest.mark.parametrize("inst,reason", [(None, "symbol_unknown"), (ETH, "other_symbol")])
def test_supplement_needs_the_root_coin(inst, reason):
    root = it("r", 1, 0, inst=SOL, legs=["216.4"])
    supp = it("s", 2, 6, inst=inst, stop=223, supplement_of="r")
    links, _ = merge([root], [supp])
    assert links["s"].supplement_rejected == reason and links["r"].stop is None


def test_supplement_to_a_root_with_its_own_stop_is_a_move_for_followup():
    root = it("r", 1, 0, inst=SOL, legs=["216.4"], stop=225)
    links, _ = merge([root], [it("s", 2, 6, inst=SOL, stop=223, supplement_of="r")])
    assert links["s"].kind == "stop_move" and links["r"].stop == D(225) and links["s"].supplement_rejected is None


def test_gap_is_reported_rounded_up_and_boundary_is_exact():
    root = it("r", 1, 0, inst=SOL, legs=["216.4"])
    supp = it("s", 2, 1800.5, inst=SOL, stop=223, supplement_of="r")
    links, _ = merge([root], [supp])
    assert links["s"].kind == "late_stop" and links["s"].gap_s == 1801


def test_merge_scales_with_many_plans():
    import time
    many = [it(f"p{i}", i, i * 600, inst=(BTC, ETH, SOL)[i % 3], side=("long", "short")[i % 2], legs=[100 + i], stop=90 + i if i % 2 == 0 else 110 + i,
               author=f"a{i % 7}") for i in range(3000)]
    start = time.perf_counter()
    links, _ = merge(many)
    assert len(links) == 3000 and time.perf_counter() - start < 20
