"""M-08：候选 B spike——B 结果满足不变量、MATCH 集合明确、非 MATCH 全部有解释码（UNEXPLAINED=0）、重放一致、路径合成独立实现一致。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from quant_lab.market import contract as c
from quant_lab.market import nautilus_adapter as nb
from quant_lab.market.kernel_a import KernelA, simulate_a

EP = Path(__file__).parent / "fixtures" / "episodes"
FIX = c.load_fixtures(EP)
EXPECT_MATCH = {"E01", "E03", "E04b", "E04c", "E05", "E07", "E14a", "E14b", "E14c", "E15a", "E15b", "E16"}


@pytest.fixture(scope="module")
def b_results():
    return {f.id: nb.simulate_b(f.request, f.market) for f in FIX}


def test_b_results_satisfy_invariants(b_results):
    for f in FIX:
        c.check_invariants(f.request, b_results[f.id])
        assert b_results[f.id].kernel == "B" and b_results[f.id].kernel_version.startswith(nb.KERNEL_VERSION + "+")


def test_b_match_set_and_all_diffs_explained(b_results):
    match, codes = set(), {}
    for f in FIX:
        d = c.diff_result(f.expected, b_results[f.id], ignore_reason=False, all_diffs=True)   # classify 的合同输入：完整差异
        code = nb.classify(f.id, d, simulate_a(f.request, f.market), b_results[f.id])
        codes[f.id] = code
        if not d:
            match.add(f.id)
    assert match == EXPECT_MATCH, (match - EXPECT_MATCH, EXPECT_MATCH - match)
    assert all(v != "UNEXPLAINED" for v in codes.values()), codes
    assert all(v in nb.EXPLANATION for v in codes.values())


def test_b_replay_consistent(b_results):
    for f in FIX[:6]:
        assert nb.simulate_b(f.request, f.market).trace_hash == b_results[f.id].trace_hash
    assert b_results["E01"].trace_hash != simulate_a(FIX[0].request, FIX[0].market).trace_hash   # 内核身份进 hash


def test_b_funding_via_adjust_account_matches_gold(b_results):
    for eid, fund in (("E05", "-0.1"), ("E07", "-0.1")):
        r = b_results[eid]
        assert r.funding == Decimal(fund) and c.diff_result(next(f for f in FIX if f.id == eid).expected, r) == []
    assert b_results["E06"].funding == 0 and b_results["E06"].net_R == 1      # 序列差异仅 accepted 时钟（B_COMMAND_LATENCY）


def test_independent_path_expansion_agrees_with_a():
    b = c.Bar(open_time=dt.datetime(2024, 1, 1, tzinfo=dt.UTC), o=Decimal(100), h=Decimal(120), l=Decimal(95), c=Decimal(100))
    for sc in ("primary", "adverse", "favorable"):
        for side in ("long", "short"):
            pa = [(p.ts, p.price, p.path_step) for p in KernelA.expand_bar(b, sc, side, None, Decimal(1))]
            pb = [(p.ts, p.price, p.path_step) for p in nb.expand_bar_b(b, sc, side)]
            assert pa == pb


def test_known_semantic_differences_are_the_documented_ones(b_results):
    # GTD 等号：B 先撮合后过期 → 成交并右删失；A 过期无成交
    assert b_results["E09"].fill_status == "filled" and b_results["E09"].censor_reason == "LABEL_RIGHT_CENSORED"
    # 跳空限价：B 按限价 105 成交
    assert b_results["E04a"].exit_avg_price == Decimal(105) and b_results["E04a"].net_R == Decimal(1)
    # 同刻竞合：B 的 resting TP 先成交，SL market 被 reduce-only 拒绝
    ev = b_results["E17"].canonical_events
    assert any(e.kind == "rejected" and e.order_id == "sl-0" for e in ev) and b_results["E17"].slippage == 0
