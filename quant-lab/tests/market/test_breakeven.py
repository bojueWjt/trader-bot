"""首次实际止盈后把止损改到当前均价；false 不进政策内容，旧夹具经济结果保持不变。"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from quant_lab.market import contract as c, execution
from quant_lab.market.kernel_a import KERNEL_VERSION, kernel_build_id, simulate_a
from quant_lab.market.nautilus_adapter import KernelBUnavailable

T0 = dt.datetime(2024, 1, 1, 1, tzinfo=dt.UTC)
OLD_BUILD = "kernel-a-v0.3+cf792aed4704"
# 改源码前冻结：events sha256、net_R、当时的 trace_hash、request_canonical sha256。
BASELINE = {
    "E01": ("c9b6eb9c666da4d862694fdfb8c704d7bedea1ed7c9fb0de86109017a8f4965e", "-2.000000000000", "5665c27e4d4995781ccbaf795651529f6e6a78d8852bdfa2b873efad7cf51dcf", "472846c1f71e7eed0f5ef12496af0b63cf914acc40fe6549b9764dca1d21b216"),
    "E02": ("c3f789f4c1992cfd5f5525d356ca6d8ebd0bb264565a20100814ea1fe4621ce4", "-0.400000000000", "a5c1338ec235039e59775fe2afcdaa73c261a83fe8a418b02a766a7291ba2e54", "7b2b171d0966232f6855dfd36897ae5b59d03e0a880f7f300dfb2cba381ca057"),
    "E03": ("80fc8fbfd285467a6708c3168db42b85f08348e3356dfd0a49c1d268d59f4f90", "1.000000000000", "6244772bf00e5a47b8dcab4c67537e2b632d1aa9e6d8d07124c2a80446a0f75c", "d38df14dcd1c26b4e5427567e44ea0c028f02e22712e5afd0ef93ceed57ae728"),
    "E04a": ("c4d964b9dea8a471a082996ca6c31c3a01595455c5b9d1c5c3fa0a058099c1b6", "2.000000000000", "e054ca3a48798e13e2edfb8017509e93c82195c4ec271fbdef4fb3ef2a96bd4b", "cc1b27773e1df000f56fd778d9b68370889f4808c3093f34cf5fdf504ab4ce5d"),
    "E04b": ("8bba1f5bdb18e7ff017436372623038f5b32cba9b678ad8f3a4a6c727ee0268e", "-2.000000000000", "27f3b22b52f571624d831e967c665acb55f0531579c78ef12126cafa7619992c", "a2a214a8f1567a50c2aefca50cbb9984abc2e64da8930b268e8ffbe0a1040909"),
    "E04c": ("44f7829e5cd9bdc445e3ecebfa3356713a91306aef9b134b8fd2c114267c6120", "-2.000000000000", "ae2f730351de27a70eb8defef42d48dad895fadf7df13fe2a6df49063fbb42f6", "bb2af7911e822404d28fb562d9b32930b923ad22b9dd5a04453e4750f35b8612"),
    "E05": ("d285feda1b39135f3bd1edb9defa46da568bd578acf8fe99057f3bd821ff8eee", "0.980000000000", "90b04dba9e5dfcdb2640286d19063b8c8ad8688ad17ca38f1439fa3448f4eeee", "a7ac38a7f918a4e0e71750b55d2a06675deb66536128a3a336603d65c196b9ec"),
    "E06": ("712f121d8f2a75a09eb504ee1079a9d125de3baa343658497bbfff5947e05201", "1.000000000000", "8c8b0a5d152994a55f71ef210c7a11c364fb24c3fc30b522b24cac4c358dfae3", "a155b45ae05fe5ddfaee2f3e3d8d3ab0a3ccdc28b20ec3f03aa6eed63a7999db"),
    "E07": ("2d081f7695805b85b9a2ed5a7e7130793874bd776694e635376cba6e9ad16322", "0.180000000000", "210f5586a60e709e9cb1f3aceacd7260101e3f390b04dc78e6cb5bdc2e986631", "00c4896e27e939e86f8b3afc8d8d8cea1840a9a668df5f245e5f6bd22010f76d"),
    "E08": ("584b13062e24136d112ffebece390b7d69b1cd4847f7ab69746927af0649a1d6", "0.500000000000", "3123300be08ce741eebf362dbd703108d0677d57bd8b8644a428339883b95e31", "95edae60044f40c23ef4c461e91a1bafe0d38d137deef70ca1fb6d6e42263eee"),
    "E09": ("8ce23363f8bd1ffc82d4ef6c46a4332b45ca15d3c443337c155b6ddfe5e4b799", "0.000000000000", "26f5a4549ff701d9d795d4931a8900c4017fd1597da8327efa0cdc4b8042d331", "03745102788b87ba4f87529c92cf27efb21bb30540a025ae6615c0410ff1bb09"),
    "E10": ("2a4475f5524c9134c6ba5a0a24ad6efaa675a42f63c40ec11e7d28c652621d10", "1.000000000000", "965eceab58647ef50cd61a56edc5fef978924c4357d8ad7c23e80980347aacb3", "6f3f5f01f50c0b493ac4b4a334f65aebebe8e5b2e32b3c23b9591ab02b759c8d"),
    "E11": ("4aadeca953b93b86f02b13f55b82b94b9b5c709dcc5331c26af35028d064f3b3", "1.200000000000", "f2d92c1ecebd9a945d85e115ed5ce57595772fd24225bedc558a70689fd1e897", "b32a5dc255d2b635ec49e3f4fb6aed5ca217bbd20f1b502429cdbc15b07faebf"),
    "E12": ("11fd88a12a25b05a8084566d9f711ea9a6524e45739d90b0f4f33726086fd6fd", "0.700000000000", "7df3c41a97bf59206044a6bd29cc0185cdc68a43046e063b751fa4c64ef61019", "a1a3dfa632fa3b69728de47815e7323c975439a59cde66c090cf4bf3465bba7b"),
    "E14a": ("1d1b0743df013b2599b04b9b53e72954a042433ed0b22dbe681a19900b532903", "0.000000000000", "2cd680f347947363f3e72d4848919fc80c2612d301d9d894f644c31098300002", "c71a7b359e7cc551811b920110ef32120f2f1364d86e686408113cf46d65e378"),
    "E14b": ("5d486f6b5e2fff4064b00841f4eb12225f4263ff64aacf194391d67dbbf4de70", "0.000000000000", "189f565453b9062ec25aa55dc3e6dff50ef07d2ac529a26f7fb82173b97ca8cf", "270dcc0bed5bb158d246a468c8767d3b20cc69032efa3d0da1b57f0e3f8d8465"),
    "E14c": ("c0952ff4edc2f2caaca7ed4646fff9386bebfa4a81aba859168b604a94b9ddd8", "0.000000000000", "a3f2fd177aad715e33f4172610897b7051f6a445d62f5f42b74910d00bb6342f", "e2c001fdbf974525aa444cefa232fb45ae78821c2e21913aef4bc8a0ce15812e"),
    "E15a": ("53e0ca8b7235c6e4263ba43988ca939156f274850d5d53ea293d745e779173f7", None, "07e4b99395e4a80909bf41d52d9ca75d3930d05987b2441ed1c2d9d3c4561146", "4370d78bc38e3e0c36075ac3b3b49a5121ada9398ee8c0b95075bb6a1e379964"),
    "E15b": ("53e0ca8b7235c6e4263ba43988ca939156f274850d5d53ea293d745e779173f7", None, "868b8c0f9b8c5425877337d3102e785e9cb96acbdc2a9fed05f7a8411393059d", "cbb1dcf5832a48a4ec3b0bad72be265894befa380f5dc8dbfa79659b8a6ecef4"),
    "E16": ("3e43c7c9af114e8ac26174b5c687b25d39a9d94d4d61190a820ce506e344d766", None, "e51a2250b79bb079b56e248a591d9baa546fd6a8aa66248e44174c8beab3ac9f", "7b45a6602741c371d1354d314507c50a75839bd9d134bdf0fb9286452ff953d1"),
    "E17": ("928fe5c3c651e7c178fba52dd021b0e078c83faf88c15d570b8861711c3105e0", "1.579000000000", "255b01bfa986f930f8b448ebb78bae6028922e04c0e9a5b4a52100e3f0a5930d", "276f44625e46711e4f52770ad2b780956834264084fab05c737b1b82095c4ae6"),
    "E18": ("6177a6d55c533e7e15afbd76f47bb8e0f38e4916d2af203d6a80b757a5266172", "0.000000000000", "1a0c96f3813a19bcfea56aced1acc9130e633b9b0b5c357898b09bee7ba83acc", "5b453cf165221da210446424b9ed4ffa89c17aca974600ee89706be2382d2373"),
}
OLD_HASHES = {
    "fixture-zero-v1": "12ca10ebb10ef27d10873584ee42a224c44efdf99e9b55c7656e8a838c58cd1d",
    "fixture-tick-v1": "79a818107c42ffc1aec9af8ca4beb672231adfaf4b514c8eb0b63664fbb0238f",
    "fixture-halftp-v1": "b5b9db9aaec5d88e1574d028a0807cdd34cff3d5141d93f2390edd69ccf338c9",
    "fixture-wallet99-v1": "982b510678f507dfec7269216e68ba24479f161624cdfebf48bc0b28d417f41f",
    "base-v1": "12f83216444dff123f7cc6e9b4f4752449b8a678555079f033cbb9980f07d5db",
    "base-v1-timeexit": "0e2a374b36d286f418d350f9159e34d73e4e56bf3280251df3531273450cf424",
    "base-v1-timeexit-w14": "a36932b0e2050366342a69d47e9fd73d84aa08474f95fa2cfc51bc202e898b53",
}


def bar(second: int, o, h, l, close, volume="1000") -> c.Bar:
    return c.Bar(open_time=T0 + dt.timedelta(seconds=second), o=D(o), h=D(h), l=D(l), c=D(close), volume=D(volume))


def request_market(plan: c.OrderPlan, lasts, marks, *, policy: str, risk: D = D(40), horizon: int = 300,
                   points: bool = False, origin: dt.datetime = T0, tick: str = "1", path: str = "primary"):
    pol = c.resolve_policy(policy)
    req = c.ExecutionRequest(
        episode_id="breakeven", graph_version="g", decision_snapshot_hash="d", t_dec=origin, order_plan=plan,
        policy_version=pol.version, policy_hash=pol.content_hash, risk_budget=risk, market_manifest="synthetic",
        horizon_end=origin + dt.timedelta(seconds=horizon), cost_scenario="base", path_scenario=path, seed=0,
        horizon_source="caller",
    )
    rules = c.Rules(tick_size=D(tick), step_size=D(1))
    if points:
        market = c.MarketView(manifest_id="synthetic", last=lasts, mark=marks, rules=rules)
    else:
        market = c.MarketView(manifest_id="synthetic", bars_last=lasts, bars_mark=marks, rules=rules)
    return req, market


def fee_bars(side: str):
    if side == "long":
        lasts = [bar(0, 100, 100, 100, 100), bar(60, 105, 110, 105, 108), bar(120, 100, 100, 100, 100), bar(180, 80, 80, 80, 80)]
        marks = [bar(0, 100, 100, 100, 100), bar(60, 105, 105, 105, 105), bar(120, 100, 100, 100, 100), bar(180, 80, 80, 80, 80)]
        stop, tp = D(80), D(110)
    else:
        lasts = [bar(0, 100, 100, 100, 100), bar(60, 95, 95, 90, 92), bar(120, 100, 100, 100, 100), bar(180, 120, 120, 120, 120)]
        marks = [bar(0, 100, 100, 100, 100), bar(60, 95, 95, 95, 95), bar(120, 100, 100, 100, 100), bar(180, 120, 120, 120, 120)]
        stop, tp = D(120), D(90)
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side=side,
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))],
        stop=c.Stop(price=stop), tps=[c.TakeProfit(level=tp, fraction=D("0.25"))],
        sizing=c.Sizing(mode="fixed_qty", qty=D(4)), expiry=c.Expiry(entry_ttl_s=3600),
    )
    return plan, lasts, marks


def sl_amends(res):
    return [(e.price, e.qty) for e in res.canonical_events if e.kind == "amended" and e.order_id == "sl-0"]


def test_false_omitted_from_policy_content_hash_and_request_canonical():
    off, on = c.resolve_policy("base-v1"), c.resolve_policy("base-v1-timeexit-be1")
    assert off.breakeven_after_first_tp is False and "breakeven_after_first_tp" not in off.model_dump()
    assert on.breakeven_after_first_tp is True and on.model_dump()["breakeven_after_first_tp"] is True
    plan, lasts, marks = fee_bars("long")
    req, market = request_market(plan, lasts, marks, policy="base-v1")
    rc = c.request_canonical(req, off, t_start=req.resolved_t_start(off), market_manifest_hash=market.manifest_hash)
    assert "breakeven_after_first_tp" not in rc["policy"]["content"]
    req_on, _ = request_market(plan, lasts, marks, policy="base-v1-timeexit-be1")
    rc_on = c.request_canonical(req_on, on, t_start=req_on.resolved_t_start(on), market_manifest_hash=market.manifest_hash)
    assert rc_on["policy"]["content"]["breakeven_after_first_tp"] is True
    assert off.content_hash == OLD_HASHES["base-v1"]


def test_registered_breakeven_policies_keep_seven_old_hashes():
    be, w14 = c.resolve_policy("base-v1-timeexit-be1"), c.resolve_policy("base-v1-timeexit-w14-be1")
    assert be.content_hash == "c608338ed32dc1738adb7cc08bd3e9b82f9809f59d672b50fb9033588c79f0d2"
    assert w14.content_hash == "8fcc23b1ebcb2a3a0f4fe65b37aad11390096e44adc823aadefb1f6c68e906cd"
    assert be.time_exit_at_horizon and be.breakeven_after_first_tp and be.research_horizon_s == 5 * 86400
    assert w14.time_exit_at_horizon and w14.breakeven_after_first_tp and w14.research_horizon_s == 13 * 86400
    reg = c.load_policy_registry()
    assert {k: reg[k] for k in OLD_HASHES} == OLD_HASHES
    assert reg["base-v1-timeexit-be1"] == be.content_hash
    assert reg["base-v1-timeexit-w14-be1"] == w14.content_hash
    assert {k: p.content_hash for k, p in c.POLICIES.items()} == reg


@pytest.mark.parametrize("side,be_net,be_r,off_net,off_r", [
    ("long", D("6.6295"), D("0.165737500000"), D("-53.3405"), D("-1.333512500000")),
    ("short", D("6.6305"), D("0.165762500000"), D("-53.3995"), D("-1.334987500000")),
])
def test_fee_adjusted_quarter_tp_then_breakeven_versus_original_stop(side, be_net, be_r, off_net, off_r):
    plan, lasts, marks = fee_bars(side)
    be_req, market = request_market(plan, lasts, marks, policy="base-v1-timeexit-be1")
    off_req, _ = request_market(plan, lasts, marks, policy="base-v1-timeexit")
    be, off = simulate_a(be_req, market), simulate_a(off_req, market)
    assert be_req.order_plan.stop.price == off_req.order_plan.stop.price == plan.stop.price
    assert be.outcome_kind == off.outcome_kind == "stopped"
    assert be.gross_pnl == D(7) and be.fees == (D("0.3705") if side == "long" else D("0.3695"))
    assert be.net_pnl == be_net and be.net_R == be_r == c.quantize_ratio(be.net_pnl / be_req.risk_budget)
    assert be.net_R != c.quantize_ratio(be.net_pnl / (abs(D(100) - plan.stop.price) * D(4)))
    assert off.net_pnl == off_net and off.net_R == off_r == c.quantize_ratio(off.net_pnl / off_req.risk_budget)
    assert sl_amends(be)[-1][0] == D(100)
    assert all(price == plan.stop.price for price, _ in sl_amends(off))
    be_stop = next(e for e in be.canonical_events if e.kind == "stop_triggered")
    off_stop = next(e for e in off.canonical_events if e.kind == "stop_triggered")
    assert be_stop.trigger_basis == "mark" and be_stop.price == D(100)
    assert off_stop.trigger_basis == "mark" and off_stop.price == plan.stop.price
    c.check_invariants(be_req, be, multiplier=market.rules.multiplier)
    c.check_invariants(off_req, off, multiplier=market.rules.multiplier)


@pytest.mark.parametrize("side,be_net,be_r,off_net,off_r", [
    ("long", D("6.6295"), D("0.082868750000"), D("-53.3405"), D("-0.666756250000")),
    ("short", D("6.6305"), D("0.082881250000"), D("-53.3995"), D("-0.667493750000")),
])
def test_quarter_tp_r_uses_initial_stop_risk(side, be_net, be_r, off_net, off_r):
    plan, lasts, marks = fee_bars(side)
    be_req, market = request_market(plan, lasts, marks, policy="base-v1-timeexit-be1", risk=D(80))
    off_req, _ = request_market(plan, lasts, marks, policy="base-v1-timeexit", risk=D(80))
    be, off = simulate_a(be_req, market), simulate_a(off_req, market)
    distance_risk = abs(D(100) - plan.stop.price) * D(4)
    assert be_req.risk_budget == off_req.risk_budget == distance_risk == D(80)
    assert be.gross_pnl == D(7) and c.quantize_ratio(be.gross_pnl / D(80)) == D("0.087500000000")
    assert off.gross_pnl == D(-53)
    assert c.quantize_ratio(off.gross_pnl / D(80)) == D("-0.662500000000") == D("0.125") - D("0.7875")
    assert be.fees == (D("0.3705") if side == "long" else D("0.3695"))
    assert be.net_pnl == be_net and be.net_R == be_r == c.quantize_ratio(be.net_pnl / be_req.risk_budget)
    assert off.net_pnl == off_net and off.net_R == off_r == c.quantize_ratio(off.net_pnl / off_req.risk_budget)
    c.check_invariants(be_req, be, multiplier=market.rules.multiplier)
    c.check_invariants(off_req, off, multiplier=market.rules.multiplier)


def test_partial_tp_fill_arms_breakeven_once():
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))], stop=c.Stop(price=D(80)),
        tps=[c.TakeProfit(level=D(110), fraction=D("0.5"))], sizing=c.Sizing(mode="fixed_qty", qty=D(4)),
        expiry=c.Expiry(entry_ttl_s=3600),
    )
    lasts = [bar(0, 100, 100, 100, 100, "1000"), bar(60, 105, 110, 105, 108, "40"), bar(120, 100, 100, 100, 100, "1000")]
    marks = [bar(0, 100, 100, 100, 100), bar(60, 105, 105, 105, 105), bar(120, 100, 100, 100, 100)]
    req, market = request_market(plan, lasts, marks, policy="base-v1-timeexit-be1")
    res = simulate_a(req, market)
    c.check_invariants(req, res, multiplier=market.rules.multiplier)
    partial = [e for e in res.canonical_events if e.kind == "partial_fill" and e.leg == "tp"]
    assert partial == [partial[0]] and partial[0].qty == D(1)
    assert (D(100), D(3)) in sl_amends(res)
    assert [e.price for e in res.canonical_events if e.kind == "filled" and e.leg == "sl"] == [D(99)]
    assert res.net_R == c.quantize_ratio(res.net_pnl / D(40)) and plan.stop.price == D(80)


def test_tp_touch_without_fill_keeps_original_stop():
    plan, lasts, marks = fee_bars("long")
    lasts[1] = bar(60, 105, 110, 105, 108, "0")
    res = simulate_a(*request_market(plan, lasts, marks, policy="base-v1-timeexit-be1"))
    assert any(e.kind == "tp_triggered" for e in res.canonical_events)
    assert not [e for e in res.canonical_events if e.leg == "tp" and e.kind in c.FILL_KINDS]
    assert all(price == D(80) for price, _ in sl_amends(res))
    assert [e.price for e in res.canonical_events if e.kind == "filled" and e.leg == "sl"] == [D(79)]


def test_same_bar_stop_precedes_tp_and_p7_keeps_initial_stop():
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))], stop=c.Stop(price=D(95)),
        tps=[c.TakeProfit(level=D(110), fraction=D("0.25"))], sizing=c.Sizing(mode="fixed_qty", qty=D(4)),
        expiry=c.Expiry(entry_ttl_s=3600),
    )
    lasts = [bar(0, 100, 100, 100, 100), bar(60, 100, 120, 90, 100)]
    marks = [bar(0, 100, 100, 100, 100), bar(60, 100, 120, 90, 100)]
    be = simulate_a(*request_market(plan, lasts, marks, policy="base-v1-timeexit-be1", horizon=180))
    off = simulate_a(*request_market(plan, lasts, marks, policy="base-v1-timeexit", horizon=180))

    def sig(res):
        return [(e.kind, e.order_id, e.price, e.qty, e.trigger_basis) for e in res.canonical_events]

    assert sig(be) == sig(off)
    assert not [e for e in be.canonical_events if e.leg == "tp" and e.kind in c.FILL_KINDS]
    assert [e.price for e in be.canonical_events if e.kind == "filled" and e.leg == "sl"] == [D(94)]
    assert plan.stop.price == D(95)

    def pt(second, price):
        return c.PricePoint(ts=T0 + dt.timedelta(seconds=second), price=D(price))

    # 入场当时的 mark 位于初始止损与均价之间：P7 仍用初始止损，不能提前按保本价砍仓。
    between = simulate_a(*request_market(
        plan, [pt(0, 100), pt(30, 110)], [pt(0, 97), pt(30, 105)], policy="base-v1-timeexit-be1", points=True))
    assert not [e for e in between.canonical_events if e.kind == "stop_triggered" and e.ts == T0]
    assert any(e.kind in c.FILL_KINDS and e.leg == "tp" for e in between.canonical_events)

    # 同一时刻 mark 已穿过初始止损、last 已穿过止盈：先止损，止盈不成交，保本不武装。
    collided = simulate_a(*request_market(
        plan, [pt(0, 100), pt(30, 110)], [pt(0, 100), pt(30, 90)], policy="base-v1-timeexit-be1", points=True))
    assert not [e for e in collided.canonical_events if e.leg == "tp" and e.kind in c.FILL_KINDS]
    stop = next(e for e in collided.canonical_events if e.kind == "stop_triggered")
    assert stop.trigger_basis == "mark" and stop.price == D(90) and stop.ts == T0 + dt.timedelta(seconds=30)
    assert all(price == D(95) for price, _ in sl_amends(collided))


def test_close_trigger_switches_to_mark_after_first_tp():
    origin = dt.datetime(2024, 1, 1, 1, tzinfo=dt.UTC)
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))],
        stop=c.Stop(price=D(90), trigger="close", timeframe="15m"),
        tps=[c.TakeProfit(level=D(110), fraction=D("0.25"))],
        sizing=c.Sizing(mode="fixed_qty", qty=D(4)), expiry=c.Expiry(entry_ttl_s=3600),
    )

    def minute(n, o, h, l, close):
        return c.Bar(open_time=origin + dt.timedelta(minutes=n), o=D(o), h=D(h), l=D(l), c=D(close), volume=D(1000))

    lasts, marks = [], []
    for n in range(16):
        if n == 0:
            lasts.append(minute(0, 100, 100, 80, 100)); marks.append(minute(0, 100, 100, 100, 100))
        elif n == 1:
            lasts.append(minute(1, 105, 110, 105, 108)); marks.append(minute(1, 105, 105, 105, 105))
        elif n == 14:
            lasts.append(minute(14, 80, 80, 80, 80)); marks.append(minute(14, 100, 100, 100, 100))
        else:
            lasts.append(minute(n, 100, 100, 100, 100)); marks.append(minute(n, 100, 100, 100, 100))
    be_req, be_market = request_market(plan, lasts, marks, policy="base-v1-timeexit-be1", horizon=15 * 60 + 30, origin=origin)
    off_req, off_market = request_market(plan, lasts, marks, policy="base-v1-timeexit", horizon=15 * 60 + 30, origin=origin)
    be, off = simulate_a(be_req, be_market), simulate_a(off_req, off_market)
    c.check_invariants(be_req, be, multiplier=be_market.rules.multiplier)
    c.check_invariants(off_req, off, multiplier=off_market.rules.multiplier)
    be_stop = next(e for e in be.canonical_events if e.kind == "stop_triggered")
    off_stop = next(e for e in off.canonical_events if e.kind == "stop_triggered")
    assert be_stop.trigger_basis == "mark" and be_stop.price == D(100)
    assert be_stop.ts == origin + dt.timedelta(minutes=2)
    assert off_stop.trigger_basis == "close" and off_stop.price == D(80)
    assert off_stop.ts == origin + dt.timedelta(minutes=15) - dt.timedelta(microseconds=1)
    assert plan.stop.trigger == "close" and plan.stop.price == D(90)
    assert be.net_R == c.quantize_ratio(be.net_pnl / D(40))


def test_breakeven_price_moves_once():
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))], stop=c.Stop(price=D(80)),
        tps=[c.TakeProfit(level=D(110), fraction=D("0.25")), c.TakeProfit(level=D(120), fraction=D("0.25"))],
        sizing=c.Sizing(mode="fixed_qty", qty=D(4)), expiry=c.Expiry(entry_ttl_s=3600),
    )
    lasts = [bar(0, 100, 100, 100, 100), bar(60, 105, 110, 105, 108), bar(120, 115, 120, 115, 118)]
    marks = [bar(0, 100, 100, 100, 100), bar(60, 105, 105, 105, 105), bar(120, 115, 115, 115, 115)]
    req, market = request_market(plan, lasts, marks, policy="base-v1-timeexit-be1", horizon=200)
    res = simulate_a(req, market)
    c.check_invariants(req, res, multiplier=market.rules.multiplier)
    assert sl_amends(res) == [(D(80), D(3)), (D(100), D(3)), (D(100), D(2))]


def test_full_tp_does_not_move_a_flat_position():
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))], stop=c.Stop(price=D(80)),
        tps=[c.TakeProfit(level=D(110), fraction=D(1))], sizing=c.Sizing(mode="fixed_qty", qty=D(4)),
        expiry=c.Expiry(entry_ttl_s=3600),
    )
    lasts = [bar(0, 100, 100, 100, 100), bar(60, 105, 110, 105, 108)]
    marks = [bar(0, 100, 100, 100, 100), bar(60, 105, 105, 105, 105)]
    res = simulate_a(*request_market(plan, lasts, marks, policy="base-v1-timeexit-be1", horizon=180))
    assert res.outcome_kind == "tp_hit" and not sl_amends(res)
    assert [(e.order_id, e.reason) for e in res.canonical_events if e.kind == "cancelled"] == [("sl-0", "position_closed")]


def test_partial_entry_uses_filled_average_and_cancels_the_rest():
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100), fraction=D("0.5")),
                 c.Entry(kind="limit", price_lo=D(102), price_hi=D(102), fraction=D("0.5"))],
        stop=c.Stop(price=D(80)), tps=[c.TakeProfit(level=D(110), fraction=D("0.5"))],
        sizing=c.Sizing(mode="fixed_qty", qty=D(4)), expiry=c.Expiry(entry_ttl_s=3600),
    )
    avg = c.quantize_ratio(D(304) / D(3))

    def pt(second, price, cap=None):
        return c.PricePoint(ts=T0 + dt.timedelta(seconds=second), price=D(price) if not isinstance(price, D) else price,
                            capacity=None if cap is None else D(cap))

    lasts = [pt(0, 102, 2), pt(10, 100, 1), pt(20, 110, 10), pt(30, avg, 10)]
    marks = [pt(0, 102), pt(10, 100), pt(20, 105), pt(30, avg)]
    req, market = request_market(plan, lasts, marks, policy="base-v1-timeexit-be1", points=True, tick="0.000000000001")
    res = simulate_a(req, market)
    assert res.entry_avg_price == avg
    assert [e for e in res.canonical_events if e.kind == "amended" and e.order_id == "sl-0" and e.price == avg]
    tp = next(e for e in res.canonical_events if e.leg == "tp" and e.kind in c.FILL_KINDS)
    cancelled = [e for e in res.canonical_events if e.kind == "cancelled" and e.leg == "entry"]
    assert [(e.order_id, e.reason, e.ts) for e in cancelled] == [("entry-0", "exit_latch", tp.ts)]
    stop = next(e for e in res.canonical_events if e.kind == "stop_triggered")
    assert cancelled[0].seq < stop.seq
    assert stop.trigger_basis == "mark" and stop.price == avg
    assert req.order_plan.stop.price == D(80)
    assert res.net_R == c.quantize_ratio(res.net_pnl / req.risk_budget)
    c.check_invariants(req, res, multiplier=market.rules.multiplier)


def test_partial_entry_bars_use_filled_average_and_cancel_before_price_returns():
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100), fraction=D("0.5")),
                 c.Entry(kind="limit", price_lo=D(102), price_hi=D(102), fraction=D("0.5"))],
        stop=c.Stop(price=D(80)), tps=[c.TakeProfit(level=D(110), fraction=D("0.5"))],
        sizing=c.Sizing(mode="fixed_qty", qty=D(4)), expiry=c.Expiry(entry_ttl_s=3600),
    )
    avg = c.quantize_ratio(D(304) / D(3))
    # 80 → 每点容量 2，平 bar 102 只吃到 102 那一档共 2。40 → 每点容量 1，仅 L=100 成交 1。
    # 第四根 last 回到 100；若剩余 entry 没有在止盈成交当时撤销，这一根会再成交。
    lasts = [bar(0, 102, 102, 102, 102, "80"), bar(60, 102, 102, 100, 102, "40"),
             bar(120, 110, 110, 110, 110, "40"), bar(180, 100, 100, 100, 100, "40")]
    marks = [bar(0, 102, 102, 102, 102), bar(60, 102, 102, 102, 102), bar(120, 110, 110, 110, 110),
             bar(180, 105, 105, avg, 105)]
    req, market = request_market(plan, lasts, marks, policy="base-v1-timeexit-be1", horizon=240, tick="0.000000000001")
    res = execution.simulate(req, kernel="A", market=market)
    assert res.entry_avg_price == avg
    assert [(e.price, e.qty) for e in res.canonical_events if e.leg == "entry" and e.kind in c.FILL_KINDS] == [(D(102), D(2)), (D(100), D(1))]
    amended = [e for e in res.canonical_events if e.kind == "amended" and e.order_id == "sl-0" and e.price == avg]
    assert amended and amended[-1].trigger_basis == "none"
    tp = next(e for e in res.canonical_events if e.leg == "tp" and e.kind in c.FILL_KINDS)
    cancelled = [e for e in res.canonical_events if e.kind == "cancelled" and e.leg == "entry"]
    assert [(e.order_id, e.reason, e.ts) for e in cancelled] == [("entry-0", "exit_latch", tp.ts)]
    assert not [e for e in res.canonical_events if e.leg == "entry" and e.kind in c.FILL_KINDS and e.ts > tp.ts]
    stop = next(e for e in res.canonical_events if e.kind == "stop_triggered")
    assert stop.ts == T0 + dt.timedelta(seconds=220) and cancelled[0].seq < stop.seq
    assert stop.trigger_basis == "mark" and stop.price == avg
    assert stop.price != D(80) and stop.price != D(101)
    assert req.order_plan.stop.price == D(80) and res.outcome_kind == "stopped"
    assert res.net_R == c.quantize_ratio(res.net_pnl / req.risk_budget)


@pytest.mark.parametrize("lower", [100, 80])
def test_same_bar_favorable_path_takes_profit_then_breakeven_stop(lower):
    plan = c.OrderPlan(
        instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
        entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))], stop=c.Stop(price=D(80)),
        tps=[c.TakeProfit(level=D(110), fraction=D("0.25"))], sizing=c.Sizing(mode="fixed_qty", qty=D(4)),
        expiry=c.Expiry(entry_ttl_s=3600),
    )
    if lower == 100:
        # primary 会先走 L：|104-100| < |110-104|。favorable 先走 H，止盈后的下一价点才是 L。
        assert abs(D(104) - D(lower)) < abs(D(110) - D(104))
    lasts = [bar(0, 100, 100, 100, 100, "160"), bar(60, 104, 110, lower, 104, "80")]
    marks = [bar(0, 100, 100, 100, 100), bar(60, 104, 110, lower, 104)]
    req, market = request_market(plan, lasts, marks, policy="base-v1-timeexit-be1", horizon=120, path="favorable")
    res = execution.simulate(req, kernel="A", market=market)
    tp = next(e for e in res.canonical_events if e.leg == "tp" and e.kind in c.FILL_KINDS)
    stop = next(e for e in res.canonical_events if e.kind == "stop_triggered")
    sl_fills = [e for e in res.canonical_events if e.leg == "sl" and e.kind in c.FILL_KINDS]
    assert tp.path_step == "H" and tp.ts == T0 + dt.timedelta(seconds=80)
    assert stop.ts == tp.ts + dt.timedelta(seconds=20) and stop.path_step == "L"
    assert stop.trigger_basis == "mark" and stop.price == D(lower)
    amended = next(e for e in res.canonical_events if e.kind == "amended" and e.order_id == "sl-0" and e.price == D(100))
    assert amended.ts == tp.ts and amended.trigger_basis == "none" and amended.seq < stop.seq < sl_fills[0].seq
    assert req.order_plan.stop.price == D(80) and res.outcome_kind == "stopped"
    assert res.entry_avg_price == D(100)
    assert res.net_R == c.quantize_ratio(res.net_pnl / req.risk_budget)


def test_kernel_b_rejects_breakeven_before_importing_nautilus():
    root = Path(__file__).resolve().parents[2]
    script = r"""
import sys
import datetime as dt
from decimal import Decimal as D
from quant_lab.market import contract as c
from quant_lab.market.nautilus_adapter import KernelBUnavailable, simulate_b
T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
pol = c.resolve_policy("base-v1-timeexit-be1")
plan = c.OrderPlan(
    instrument_id="BTCUSDT-PERP.BINANCE-UM", side="long",
    entries=[c.Entry(kind="limit", price_lo=D(100), price_hi=D(100))],
    stop=c.Stop(price=D(80)), tps=[c.TakeProfit(level=D(110), fraction=D("0.25"))],
    sizing=c.Sizing(mode="fixed_qty", qty=D(1)), expiry=c.Expiry(entry_ttl_s=60),
)
req = c.ExecutionRequest(
    episode_id="b-reject", graph_version="g", decision_snapshot_hash="d", t_dec=T0, order_plan=plan,
    policy_version=pol.version, policy_hash=pol.content_hash, risk_budget=D(10), market_manifest="synthetic",
    horizon_end=T0 + dt.timedelta(minutes=2), cost_scenario="base", path_scenario="primary", seed=0,
    horizon_source="caller",
)
market = c.MarketView(manifest_id="synthetic", last=[c.PricePoint(ts=T0, price=D(100))],
                      mark=[c.PricePoint(ts=T0, price=D(100))], rules=c.Rules(tick_size=D(1), step_size=D(1)))
try:
    simulate_b(req, market, pol)
except KernelBUnavailable as exc:
    if "breakeven" not in str(exc):
        raise SystemExit(f"wrong rejection: {exc}")
else:
    raise SystemExit("missing rejection")
if any(name == "nautilus_trader" or name.startswith("nautilus_trader.") for name in sys.modules):
    raise SystemExit("nautilus imported before rejection")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    proc = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_public_simulate_rejects_both_breakeven_policies_before_time_exit():
    plan, lasts, marks = fee_bars("long")
    for policy in ("base-v1-timeexit-be1", "base-v1-timeexit-w14-be1"):
        req, market = request_market(plan, lasts, marks, policy=policy)
        with pytest.raises(KernelBUnavailable, match="breakeven"):
            execution.simulate(req, kernel="B", market=market)
    req, market = request_market(plan, lasts, marks, policy="base-v1-timeexit")
    with pytest.raises(c.ContractError, match="内核 B 尚未实现到期平仓"):
        execution.simulate(req, kernel="B", market=market)


def test_follower_fixtures_match_new_gold_and_invalidate_old_traces():
    assert KERNEL_VERSION == "kernel-a-v0.6"
    assert kernel_build_id(False) == kernel_build_id(True)
    fixtures = c.load_fixtures(Path(__file__).resolve().parent / "fixtures" / "episodes")
    assert {f.id for f in fixtures} == set(BASELINE)
    changed = {"E01", "E02", "E04a", "E04b", "E04c", "E10", "E12", "E14a", "E17", "E18"}
    for fixture in fixtures:
        events_sha, net_r, old_trace, rc_sha = BASELINE[fixture.id]
        res = simulate_a(fixture.request, fixture.market)
        assert c.diff_result(fixture.expected, res) == []
        actual_sha = hashlib.sha256(c.canonical_json([e.model_dump() for e in res.canonical_events]).encode()).hexdigest()
        if fixture.id not in changed:
            assert actual_sha == events_sha
            assert (None if res.net_R is None else format(res.net_R, "f")) == net_r
        assert res.trace_hash != old_trace
        assert res.kernel_version.startswith("kernel-a-v0.6+")
