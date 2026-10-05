"""Synthetic unit cases for every live rule, including conservative wording scope."""
from copy import deepcopy
from decimal import Decimal as D

import pytest

from quant_lab.market.contract import ContractError, OrderPlan, canonical_json
from quant_lab.market.live_profile import apply_live_profile, wording_flags


def plan(side="long", *, zone=False, pair=False):
    lo, hi = ("100", "110") if side == "long" else ("90", "100")
    entries = [{"kind": "ladder" if zone else "limit", "price_lo": D(lo),
                "price_hi": D(hi if zone else lo), "fraction": None, "tif": "GTC", "post_only": False}]
    if pair:
        entries.append(dict(entries[0], price_lo=D(hi), price_hi=D(hi)))
    return {"instrument_id": "BTCUSDT-PERP.BINANCE-UM", "side": side, "entries": entries,
            "stop": {"price": D("80" if side == "long" else "120"), "trigger": "mark", "timeframe": None},
            "tps": [{"level": D("130" if side == "long" else "70"), "fraction": None}],
            "sizing": {"mode": "risk_budget", "qty": None},
            "expiry": {"entry_ttl_s": None, "max_holding_s": None}, "reduce_only_exit": True}


@pytest.mark.parametrize("side,entry,expected", [("long", "100", "100.100"), ("short", "90", "89.910")])
@pytest.mark.parametrize("word", ["{}附近", "{}左右", "大约{}", "约{}", "大概在{}", "{}上下"])
def test_entry_concession_only_for_fuzzy_wording(side, entry, expected, word):
    source = plan(side)
    saved = canonical_json(source)
    out, audit = apply_live_profile(source, f"入场{word.format(entry)}\n止损{source['stop']['price']}", D("0.001"))
    assert out["entries"][0]["price_lo"] == D(expected)
    assert audit["rule_counts"]["entry_concession"] == 1
    exact, audit = apply_live_profile(source, f"入场{entry}\n止损{source['stop']['price']}", D("0.001"))
    assert exact["entries"][0]["price_lo"] == D(entry)
    assert audit["rule_counts"]["entry_concession"] == 0
    assert canonical_json(source) == saved


@pytest.mark.parametrize("side,base,near,breakout", [("long", "80", "79.920", "79.680240"),
                                                     ("short", "120", "120.120", "120.480360")])
@pytest.mark.parametrize("word", ["略破{}", "小幅突破{}", "稍微超过{}", "小幅跌破{}", "小幅涨破{}", "跌破{}一点"])
def test_stop_breakout_then_widening(side, base, near, breakout, word):
    source = plan(side)
    out, audit = apply_live_profile(source, f"止损{word.format(base)}", D("0.000001"))
    assert out["stop"]["price"] == D(breakout)
    assert audit["rule_counts"]["stop_breakout"] == 1 and audit["rule_counts"]["stop_widening"] == 1
    approx, audit = apply_live_profile(source, f"止损{base}附近", D("0.000001"))
    assert approx["stop"]["price"] == D(near)
    assert audit["rule_counts"]["stop_breakout"] == 0 and audit["rule_counts"]["stop_widening"] == 1
    # 让点只在措辞模糊时生效：精确止损原值执行。
    precise, audit = apply_live_profile(source, f"止损{base}", D("0.000001"))
    assert precise["stop"]["price"] == D(base)
    assert audit["rule_counts"]["stop_breakout"] == 0 and audit["rule_counts"]["stop_widening"] == 0


@pytest.mark.parametrize("side,expected", [("long", "129.870"), ("short", "70.070")])
def test_take_profit_concession(side, expected):
    source = plan(side)
    level = source["tps"][0]["level"]
    out, audit = apply_live_profile(source, f"目标{level}附近", D("0.001"))
    assert out["tps"][0]["level"] == D(expected)
    assert audit["rule_counts"]["take_profit_concession"] == 1
    entry = source["entries"][0]["price_lo"]
    for text in (None, f"目标{level}", f"入场{entry}附近\n目标{level}"):
        exact, audit = apply_live_profile(source, text, D("0.001"))
        assert exact["tps"][0]["level"] == level and audit["rule_counts"]["take_profit_concession"] == 0
    empty = plan(side)
    empty["tps"] = []
    out, audit = apply_live_profile(empty, None, D("0.001"))
    assert out["tps"] == [] and audit["rule_counts"]["take_profit_concession"] == 0


@pytest.mark.parametrize("side,entry,stop,tp", [("long", "100.25", "79.75", "129.75"),
                                               ("short", "89.75", "120.25", "70.25")])
def test_tick_rounding_toward_fill_and_later_stop(side, entry, stop, tp):
    source = plan(side)
    text = (f"入场{source['entries'][0]['price_lo']}附近 止损{source['stop']['price']}附近 "
            f"目标{source['tps'][0]['level']}附近")
    out, audit = apply_live_profile(source, text, D("0.25"))
    assert out["entries"][0]["price_lo"] == D(entry)
    assert out["stop"]["price"] == D(stop)
    assert out["tps"][0]["level"] == D(tp)
    assert audit["rule_counts"]["tick_rounding"] == 3
    assert all(price % D("0.25") == 0 for price in (D(entry), D(stop), D(tp)))
    precise, audit = apply_live_profile(source, None, D("0.01"))
    assert precise["entries"][0]["price_lo"] == source["entries"][0]["price_lo"]
    assert audit["rule_counts"]["tick_rounding"] == 0


@pytest.mark.parametrize("side,prices", [("long", ["110", "105", "101.5"]), ("short", ["90", "95", "98.5"])])
@pytest.mark.parametrize("fuzzy", [False, True])
def test_zone_depth_order_and_risk_shares(side, prices, fuzzy):
    source = plan(side, zone=True)
    e = source["entries"][0]
    text = f"入场{e['price_lo']}-{e['price_hi']}" + ("附近" if fuzzy else "")
    out, audit = apply_live_profile(source, text, D("0.0001"))
    factor = D("1.001") if side == "long" else D("0.999")
    expected = [D(p) * (factor if fuzzy else 1) for p in prices]
    assert [e["price_lo"] for e in out["entries"]] == expected
    assert [a["tranche"] for a in audit["zone_allocation"]] == ["t1_near", "t2_mid", "t3_deep"]
    assert [a["risk_share"] for a in audit["zone_allocation"]] == [D("0.55"), D("0.30"), D("0.15")]
    fractions = [e["fraction"] for e in out["entries"]]
    assert sum(fractions) == 1
    # Independent production sizing oracle: q_i = R*w_i / abs(p_i - SL).
    oracle = [D(100) * w / abs(p - out["stop"]["price"]) for p, w in zip(expected, [D("0.55"), D("0.30"), D("0.15")])]
    for fraction, qty in zip(fractions, oracle):
        assert abs(fraction - qty / sum(oracle)) < D("0.000000000003")
    assert [e["kind"] for e in out["entries"]] == ["limit"] * 3
    assert audit["rule_counts"]["entry_concession"] == (3 if fuzzy else 0)
    assert audit["rule_counts"]["zone_ladder"] == 1
    OrderPlan.model_validate(out)


@pytest.mark.parametrize("side", ["long", "short"])
def test_explicit_pair_equal_notional(side):
    source = plan(side, pair=True)
    source["entries"][0]["fraction"] = D("0.9")
    source["entries"][1]["fraction"] = D("0.1")
    out, audit = apply_live_profile(source, None, D("0.01"))
    notionals = [e["fraction"] * e["price_lo"] for e in out["entries"]]
    assert abs(notionals[0] - notionals[1]) < D("0.000000001")
    assert audit["rule_counts"]["equal_notional_pair"] == 1
    assert audit["rule_counts"]["zone_ladder"] == 0
    OrderPlan.model_validate(out)


@pytest.mark.parametrize("text", [None, "入场101附近\n止损81略破",
    "入场100\n目标130附近\n备注大约明天开盘", "入场100\n止损80\n评论略破80",
    "入场100\n止损80\n合约交易规则约定", "入场100附近；入场100", "入场100\n止损80略破；止损80"])
def test_unresolved_or_unrelated_wording_is_exact(text):
    flags = wording_flags(plan(), text)
    assert flags["entry_fuzzy"] is False and flags["stop_fuzzy"] is False


def test_one_line_roles_do_not_leak_and_line_numbers_are_audited():
    text = "私密哨兵\nBTC 做多 入场100 止损略破80 目标130附近"
    flags = wording_flags(plan(), text)
    assert flags == {"entry_fuzzy": False, "entry_legs_fuzzy": [False], "stop_fuzzy": True, "stop_breakout": True,
                     "tp_fuzzy": [True], "entry_line": 2, "stop_line": 2}
    _, audit = apply_live_profile(plan(), text, D("0.01"))
    assert "私密哨兵" not in canonical_json(audit) and "附近" not in canonical_json(audit)


def test_opposite_opening_does_not_enable_entry_concession():
    assert wording_flags(plan(), "开空100附近")["entry_fuzzy"] is False
    assert wording_flags(plan(), "ETH 做多 入场100附近\nETH 止损略破80")["stop_fuzzy"] is False
    assert wording_flags(plan(), "ETH 做多 入场100附近")["entry_fuzzy"] is False
    flags = wording_flags(plan(), "BTC 做多 入场100\nETH 做多 入场100附近")
    assert flags["entry_fuzzy"] is False and flags["entry_line"] == 1


def test_market_reference_and_disabled_are_unchanged():
    source = plan()
    source["entries"][0]["kind"] = "market_ref"
    out, audit = apply_live_profile(source, "入场100附近", D("0.01"))
    assert out["entries"] == source["entries"] and audit["rule_counts"]["entry_concession"] == 0
    before = deepcopy(source)
    out, audit = apply_live_profile(source, "入场100附近 止损略破80", D("NaN"), enabled=False)
    assert out is source and source == before and audit is None
    assert canonical_json(out) == canonical_json(before)


@pytest.mark.parametrize("tick", ["0", "-0.1", "NaN", "Infinity"])
def test_invalid_tick_is_not_fabricated(tick):
    with pytest.raises(ContractError, match="tick"):
        apply_live_profile(plan(), None, D(tick))


def test_mixed_zones_are_not_silently_reallocated():
    source = plan(zone=True)
    source["entries"].append(dict(source["entries"][0], kind="limit", price_lo=D(100), price_hi=D(100)))
    with pytest.raises(ContractError, match="one zone"):
        apply_live_profile(source, None, D("0.01"))


def test_rounding_to_zero_is_a_contract_exclusion():
    with pytest.raises(ContractError, match="nonpositive"):
        apply_live_profile(plan("short"), None, D(1000))


def test_stop_trigger_and_explicit_tp_sizing_are_preserved():
    source = plan()
    source["stop"].update(trigger="close", timeframe="4h")
    source["tps"][0]["fraction"] = D("0.4")
    out, _ = apply_live_profile(source, "入场100附近\n止损4h收盘略破80", D("0.001"))
    assert out["stop"] == {"price": D("79.680"), "trigger": "close", "timeframe": "4h"}
    assert out["tps"][0]["fraction"] == D("0.4")
    assert out["sizing"] == source["sizing"] and out["expiry"] == source["expiry"]


@pytest.mark.parametrize("side,expected", [("long", ["100.1", "110.11"]), ("short", ["89.91", "99.9"])])
def test_fuzzy_pair_moves_both_entry_legs(side, expected):
    source = plan(side, pair=True)
    prices = [e["price_lo"] for e in source["entries"]]
    out, audit = apply_live_profile(source, f"入场{prices[0]}和{prices[1]}附近", D("0.01"))
    assert [e["price_lo"] for e in out["entries"]] == [D(p) for p in expected]
    assert audit["rule_counts"]["entry_concession"] == 2


def test_wan_written_prices_still_match_their_clause():
    # Invented prices: a BTC plan written in 万 ("7.31-7.36万附近", "小幅跌破7.25一点") carries 73100/73600/72500.
    p = plan(zone=True)
    p["entries"][0].update(price_lo=D("73100"), price_hi=D("73600"))
    p["stop"]["price"] = D("72500")
    flags = wording_flags(p, "仿写\n方向：做多\n入场：7.31-7.36万附近\n止损：小幅跌破7.25一点。")
    assert flags["entry_fuzzy"] is True and flags["stop_fuzzy"] is True


def test_each_target_uses_its_own_wording():
    source = plan()
    source["tps"] = [{"level": D("130"), "fraction": None}, {"level": D("140"), "fraction": None}]
    for text in ("入场100\n止损80\nTP1 130 TP2 140附近", "入场100\n止损80\n目标130\n目标140左右"):
        out, audit = apply_live_profile(source, text, D("0.001"))
        assert [tp["level"] for tp in out["tps"]] == [D("130"), D("139.860")]
        assert audit["rule_counts"]["take_profit_concession"] == 1
        assert out["stop"]["price"] == D("80") and out["entries"][0]["price_lo"] == D("100")


def custom(side, entries, stop, tps, symbol="BTCUSDT"):
    out = plan(side)
    out["instrument_id"] = f"{symbol}-PERP.BINANCE-UM"
    lo, hi = entries
    out["entries"][0].update(kind="ladder" if lo != hi else "limit", price_lo=D(lo), price_hi=D(hi))
    out["stop"]["price"] = D(stop)
    out["tps"] = [{"level": D(level), "fraction": None} for level in tps]
    return out


# 仿写的无标签写法（数字均为虚构）：模糊词直接挂在价格上即算，不需要「入场/止损/目标」标签。
@pytest.mark.parametrize("side,entries,stop,tps,text,expected", [
    ("short", ("41250", "41250"), "42300", ["39800"], "比特币做空参考\n现价:41250附近\n止损:42300\n止盈:39800",
     (True, False, [False])),
    ("long", ("52340", "52340"), "51200", ["54000"], "大饼等52340附近多（止损51200）目标54000左右",
     (True, False, [True])),
    ("short", ("1830", "1850"), "1890", [], "以太看1830-1850附近回抽加空（止损1890上下）", (True, True, [])),
    ("long", ("80500", "81150"), "79900", ["84000"], "购买区域：$81,150 - $80,500\n止损：79,900\n目标：$84,000",
     (False, False, [False])),
    ("long", ("71250", "71250"), "70400", [], "在约$71,250区域做多，止损$70,400", (True, False, [])),
    ("long", ("100", "100"), "80", [], "100附近，80略破", (True, False, [])),
    ("long", ("100", "100"), "80", ["130"], "仓位0.3-0.5左右\n入场100\n止损80\n目标130", (False, False, [False])),
    ("long", ("100", "100"), "80", ["130"], "入场100\n止损80\n目标130\n浮盈5%左右先看", (False, False, [False])),
])
def test_wording_attached_to_the_price_needs_no_label(side, entries, stop, tps, text, expected):
    flags = wording_flags(custom(side, entries, stop, tps), text)
    assert (flags["entry_fuzzy"], flags["stop_fuzzy"], flags["tp_fuzzy"]) == expected
    assert flags["stop_breakout"] is False


def test_attached_wording_respects_symbol_and_side():
    assert wording_flags(custom("long", ("100", "100"), "80", []), "ETH 100附近多")["entry_fuzzy"] is False
    assert wording_flags(custom("long", ("100", "100"), "80", []), "100附近空")["entry_fuzzy"] is False
    # 没有标签时任一处把模糊词挂在这个价格上即算（「在100附近买入，直到价格达到100」）。
    assert wording_flags(custom("long", ("100", "100"), "80", []), "100附近多，回到100再补")["entry_fuzzy"] is True
    assert wording_flags(custom("long", ("100", "110"), "80", []), "100附近和110之间")["entry_fuzzy"] is False
    flags = wording_flags(custom("long", ("100", "100"), "80", []), "BTC\n100附近多")
    assert flags["entry_fuzzy"] is True and flags["entry_line"] == 2


@pytest.mark.parametrize("side,entries,stop,tps,text,legs,stop_fuzzy,tp_fuzzy", [
    # 简写：875 写的是 87500，885 是 88500（虚构数字）。
    ("short", [("87500", "87500")], "88500", [], "大饼875附近继续空吧。防守885", [True], False, []),
    ("long", [("62000", "62000"), ("63000", "63000")], "61000", [], "在6.2和6.3万支撑附近做多，6.1止损", [True, True], False, []),
    ("long", [("0.3228", "0.3228")], "0.3093", ["0.4125"], "买入：在 0.3228 附近买入，直到价格达到 0.3228。\n目标价：0.4125\n止损价：0.3093",
     [True], False, [False]),
    # 两档入场只有第一档带模糊词：只让第一档。
    ("short", [("3680", "3680"), ("3740", "3740")], "3810", ["3340", "3000"],
     "以太坊现价3680附近长线做空\n止盈:3340-3000\n补仓:3740\n止损:3810", [True, False], False, [False, False]),
    ("long", [("100", "100")], "80", [], "入场100\n止损：80（约1%）", [False], False, []),
    ("long", [("100", "100")], "80", ["120", "130"], "入场100\n止盈：点位1：120附近 点位2：130\n止损：小幅跌破80", [False], True, [True, False]),
])
def test_level_wording_cases(side, entries, stop, tps, text, legs, stop_fuzzy, tp_fuzzy):
    p = custom(side, entries[0], stop, tps)
    p["entries"] = [dict(p["entries"][0], kind="limit", price_lo=D(lo), price_hi=D(hi)) for lo, hi in entries]
    flags = wording_flags(p, text)
    assert (flags["entry_legs_fuzzy"], flags["stop_fuzzy"], flags["tp_fuzzy"]) == (legs, stop_fuzzy, tp_fuzzy)


def test_only_the_fuzzy_leg_gets_the_entry_concession():
    p = custom("short", ("3680", "3680"), "3810", [])
    p["entries"].append(dict(p["entries"][0], price_lo=D("3740"), price_hi=D("3740")))
    out, audit = apply_live_profile(p, "以太坊现价3680附近做空\n补仓:3740\n止损:3810", D("0.01"))
    assert [e["price_lo"] for e in out["entries"]] == [D("3676.32"), D("3740")]
    assert audit["rule_counts"]["entry_concession"] == 1 and out["stop"]["price"] == D("3810")


def test_run_on_clause_wording_stays_with_its_own_price():
    # 无标点长句：「附近」属于后面的加仓价，不属于止损价（虚构数字）。
    p = custom("short", ("2555", "2555"), "2577", [])
    flags = wording_flags(p, "以太空单 整体的大止损在2577 后面如果拉在2555附近可以加仓")
    assert flags["stop_fuzzy"] is False and flags["entry_legs_fuzzy"] == [True]
    assert wording_flags(custom("long", ("100", "100"), "80", []), "入场100\n止损80，亏一点没事")["stop_fuzzy"] is False
    assert wording_flags(custom("long", ("116630", "116630"), "115700", []), "如果BTC在116630$左右提供机会")["entry_fuzzy"] is True
    assert wording_flags(custom("long", ("240", "240"), "221", []), "入场点：在240点附近买入\n止损点：221")["entry_fuzzy"] is True


# ---------------------------------------------------------------------------
# live v4 (v8 F4): v8 rows carry the silver stop_rule; all texts below are invented.
def v8_plan(side, entry, stop, *, trigger="mark", timeframe=None, symbol="BTCUSDT"):
    out = custom(side, (entry, entry), stop, [], symbol=symbol)
    out["stop"].update(trigger=trigger, timeframe=timeframe)
    return out


def r9(base):
    return {"stop_rule": "r9_fuzzy_break", "stop_base": D(base)}


@pytest.mark.parametrize("text", ["BTC 6万6附近接多，小幅跌破就止损", "BTC 66000附近多\n止损：小幅跌破65802", "BTC 66000多\n止损65802"])
def test_v4_r9_widens_once_and_never_reapplies_breakout(text):
    # Silver already moved the stop 0.3% below the base (66000 × 0.997 = 65802); live only adds 0.1% and rounds.
    out, audit = apply_live_profile(v8_plan("long", "66000", "65802"), text, D("0.1"), stop_meta=r9("66000"))
    assert out["stop"]["price"] == D("65736.1")                      # 65802 × 0.999 = 65736.198 → floor to tick
    assert audit["rule_counts"]["stop_breakout"] == 0 and audit["rule_counts"]["stop_widening"] == 1
    assert audit["stop_v4"] == {"stop_rule": "r9_fuzzy_break", "decision": "r9_forced", "stop_text_resolved": False}
    short, _ = apply_live_profile(v8_plan("short", "3180", "3229.66"), "ETH 3170-3190 空，小幅涨破3220一点止损", D("0.01"),
                                  stop_meta=r9("3220"))
    assert short["stop"]["price"] == D("3232.89")                    # 3229.66 × 1.001 = 3232.88966 → ceil to tick


@pytest.mark.parametrize("rule", [None, "", "plain_break", "chart", "relative", "supplement", "inherited_amend",
                                  "close_like_kept_price"])
def test_v4_close_and_wording_rules(rule):
    meta = {"stop_rule": rule, "stop_base": None}
    exact, audit = apply_live_profile(v8_plan("long", "150", "145", symbol="SOLUSDT"), "SOL 150 附近多，跌破145就止损", D("0.001"), stop_meta=meta)
    assert exact["stop"]["price"] == D("145") and audit["rule_counts"]["stop_widening"] == 0
    near, audit = apply_live_profile(v8_plan("long", "150", "145", symbol="SOLUSDT"), "SOL 150 附近多，跌破145附近就走", D("0.001"), stop_meta=meta)
    assert near["stop"]["price"] == D("144.855") and audit["stop_v4"]["decision"] == "near_wording"
    # Breakout wording is not a fuzzy word in v4: only r9 (decided in silver) moves a stop for it.
    broke, audit = apply_live_profile(v8_plan("long", "100", "80"), "入场100\n止损略破80", D("0.001"), stop_meta=meta)
    assert broke["stop"]["price"] == D("80") and audit["rule_counts"]["stop_breakout"] == 0
    v3, _ = apply_live_profile(v8_plan("long", "100", "80"), "入场100\n止损略破80", D("0.001"))
    assert v3["stop"]["price"] == D("79.680")
    close, audit = apply_live_profile(v8_plan("long", "70000", "68000", trigger="close", timeframe="1d"),
                                      "BTC 7万多，日线收盘跌破68000附近止损", D("0.1"),
                                      stop_meta={"stop_rule": "close_from_clause", "stop_base": D(68000)})
    assert close["stop"] == {"price": D("68000"), "trigger": "close", "timeframe": "1d"}
    assert audit["stop_v4"]["decision"] == "close_trigger" and audit["rule_counts"]["stop_widening"] == 0


def test_v4_anchors_wording_on_stop_base_in_stop_text():
    meta = {"stop_rule": "supplement", "stop_base": D("223")}
    out, audit = apply_live_profile(v8_plan("short", "216.4", "223", symbol="SOLUSDT"), "SOL 216.4 空", D("0.01"), stop_meta=meta,
                                    stop_text="止损:223附近")
    assert out["stop"]["price"] == D("223.23") and audit["stop_v4"]["stop_text_resolved"] is True
    plain, _ = apply_live_profile(v8_plan("short", "216.4", "223", symbol="SOLUSDT"), "SOL 216.4 空 止损223附近", D("0.01"), stop_meta=meta,
                                  stop_text="止损:223")
    assert plain["stop"]["price"] == D("223")                        # the stop message decides, not the root
    fallback, _ = apply_live_profile(v8_plan("short", "216.4", "223", symbol="SOLUSDT"), "SOL 216.4 空 止损223附近", D("0.01"), stop_meta=meta)
    assert fallback["stop"]["price"] == D("223.23")                  # no stop message → root text
    # Wording must sit on the base: a relative stop's text has no such price, so it stays exact.
    relative, _ = apply_live_profile(v8_plan("long", "100000", "99000"), "BTC 现价多，带1000点防守，100000附近", D("0.1"),
                                     stop_meta={"stop_rule": "relative", "stop_base": None})
    assert relative["stop"]["price"] == D("99000")
    based, _ = apply_live_profile(v8_plan("long", "100", "79.5"), "入场100\n止损80附近", D("0.001"),
                                  stop_meta={"stop_rule": "plain_break", "stop_base": D(80)})
    assert based["stop"]["price"] == D("79.420")                     # base 80 carries 附近 → the stop 79.5 widens


def test_stopless_plan_skips_the_stop_and_keeps_entry_and_target_wording():
    source = plan()
    source["stop"] = None
    for meta in (None, {"stop_rule": None, "stop_base": None}):
        out, audit = apply_live_profile(source, "入场100附近\n目标130附近", D("0.001"), stop_meta=meta)
        assert out["stop"] is None and audit["rule_counts"]["stop_widening"] == 0
        assert out["entries"][0]["price_lo"] == D("100.100") and out["tps"][0]["level"] == D("129.870")
    flags = wording_flags(source, "止损略破80")
    assert flags["stop_fuzzy"] is False and flags["stop_breakout"] is False


# 30 invented stop sentences: the silver breakout reading must match live v3 sentence for sentence (spec F4 test 18).
BREAKOUT_CORPUS = [
    ("long", "80", "止损略破80"), ("long", "80", "止损小幅跌破80"), ("long", "80", "止损：80"), ("long", "80", "止损80附近"),
    ("long", "80", "止损跌破80一点"), ("long", "80", "止损稍微跌破80"), ("long", "80", "跌破80止损"), ("long", "80", "80略破就走"),
    ("long", "80", "止损80，略破再说"), ("long", "80", "SL 80"), ("long", "80", "止损 80（小幅跌破）"), ("long", "80", "止损:80一点"),
    ("long", "80", "防守80"), ("long", "80", "止损小幅跌破 80"), ("long", "80", "止损80\n目标略破120"),
    ("short", "120", "止损略破120"), ("short", "120", "止损小幅涨破120"), ("short", "120", "止损小幅突破120"),
    ("short", "120", "止损稍微超过120"), ("short", "120", "止损稍微涨破120"), ("short", "120", "止损：120"),
    ("short", "120", "止损涨破120一点"), ("short", "120", "止损120附近"), ("short", "120", "突破120止损"),
    ("short", "120", "止损120\n入场100附近"), ("short", "120", "SL 120 小幅突破"), ("short", "120", "120略破就走"),
    ("short", "120", "止损:小幅涨破120"), ("short", "120", "止损 120 一点"), ("short", "120", "止损120，略破离场"),
]


def test_silver_stop_rules_breakout_matches_live_v3():
    import re
    from quant_lab.market import live_profile
    stop_rules = pytest.importorskip("quant_lab.data.stop_rules")   # A 组模块；未合入时跳过
    assert len(BREAKOUT_CORPUS) == 30
    for side, price, text in BREAKOUT_CORPUS:
        p = custom(side, ("100", "100"), price, [])
        assert bool(stop_rules.breakout_on(D(price), text)) == wording_flags(p, text)["stop_breakout"], (side, price, text)
    for word in live_profile._BREAKOUT.pattern.split("|"):          # 数据层词表 ⊇ live 词表
        assert re.search(stop_rules._BREAKOUT, word), word
