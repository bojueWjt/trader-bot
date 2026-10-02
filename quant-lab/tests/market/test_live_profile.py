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
@pytest.mark.parametrize("word", ["附近", "左右", "大约", "约"])
def test_entry_concession_only_for_fuzzy_wording(side, entry, expected, word):
    source = plan(side)
    saved = canonical_json(source)
    out, audit = apply_live_profile(source, f"入场{entry}{word}\n止损{source['stop']['price']}", D("0.001"))
    assert out["entries"][0]["price_lo"] == D(expected)
    assert audit["rule_counts"]["entry_concession"] == 1
    exact, audit = apply_live_profile(source, f"入场{entry}\n止损{source['stop']['price']}", D("0.001"))
    assert exact["entries"][0]["price_lo"] == D(entry)
    assert audit["rule_counts"]["entry_concession"] == 0
    assert canonical_json(source) == saved


@pytest.mark.parametrize("side,base,exact,fuzzy", [("long", "80", "79.920", "79.680240"),
                                                    ("short", "120", "120.120", "120.480360")])
@pytest.mark.parametrize("word", ["略破", "小幅突破", "稍微超过", "小幅跌破", "小幅涨破", "一点"])
def test_stop_breakout_then_widening(side, base, exact, fuzzy, word):
    source = plan(side)
    out, audit = apply_live_profile(source, f"止损{word}{base}", D("0.000001"))
    assert out["stop"]["price"] == D(fuzzy)
    assert audit["rule_counts"]["stop_breakout"] == 1
    precise, audit = apply_live_profile(source, f"止损{base}", D("0.000001"))
    assert precise["stop"]["price"] == D(exact)
    assert audit["rule_counts"]["stop_breakout"] == 0
    assert audit["rule_counts"]["stop_widening"] == 1


@pytest.mark.parametrize("side,expected", [("long", "129.870"), ("short", "70.070")])
def test_take_profit_concession(side, expected):
    out, audit = apply_live_profile(plan(side), None, D("0.001"))
    assert out["tps"][0]["level"] == D(expected)
    assert audit["rule_counts"]["take_profit_concession"] == 1
    empty = plan(side)
    empty["tps"] = []
    out, audit = apply_live_profile(empty, None, D("0.001"))
    assert out["tps"] == [] and audit["rule_counts"]["take_profit_concession"] == 0


@pytest.mark.parametrize("side,entry,stop,tp", [("long", "100.25", "79.75", "129.75"),
                                               ("short", "89.75", "120.25", "70.25")])
def test_tick_rounding_toward_fill_and_later_stop(side, entry, stop, tp):
    source = plan(side)
    out, audit = apply_live_profile(source, f"入场{source['entries'][0]['price_lo']}附近", D("0.25"))
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


@pytest.mark.parametrize("text", [None, "100附近，80略破", "入场101附近\n止损81略破",
    "入场100\n目标130附近\n备注大约明天开盘", "入场100\n止损80\n评论略破80",
    "入场100\n止损80\n合约交易规则约定", "入场100附近；入场100", "入场100\n止损80略破；止损80"])
def test_unresolved_or_unrelated_wording_is_exact(text):
    flags = wording_flags(plan(), text)
    assert flags["entry_fuzzy"] is False and flags["stop_fuzzy"] is False


def test_one_line_roles_do_not_leak_and_line_numbers_are_audited():
    text = "私密哨兵\nBTC 做多 入场100 止损略破80 目标130附近"
    flags = wording_flags(plan(), text)
    assert flags == {"entry_fuzzy": False, "stop_fuzzy": True, "entry_line": 2, "stop_line": 2}
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
