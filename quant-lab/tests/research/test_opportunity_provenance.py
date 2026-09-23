"""OR-05 I05：冻结机会集必须绑定每个机会的来源身份（图版本、决策快照、t_dec），执行两臂逐行对照它。

原先只核两臂**彼此**一致：两臂同时取自错误的图版本或决策快照，仍然 status=ok（审查复现：foreign_graph θ 与
基线相同、n_evaluated 相同）。这里每条拒收都配一个突变：去掉对照冻结身份这一步，同样的伪造就会被放行。
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import polars as pl
import pytest

from quant_lab.research.evaluator import (EvalProtocolError, OpportunitySet, evaluate, freeze_opportunity_set,
                                          pair_arms)
from quant_lab.research.ledger import MemoryLedger

from tests.research.test_evaluator import AST, H, T0, episodes, execution, features, take_all

ROWS = [("a", "c1", False), ("b", "c1", False), ("c", "c2", False), ("d", "c3", False)]
R = [0.5, -0.2, 0.3, 0.1]


def _setup():
    ep = episodes(ROWS)
    opp = freeze_opportunity_set(ep)
    return ep, opp, execution(ep, R), features(opp.episode_ids, [1.0] * len(opp.episode_ids))


def _eval(opp, ex, feats, **kw):
    return evaluate(AST, opp, features=feats, rule=take_all, execution=ex, fold_id="f0", attempt_id=kw.pop("attempt_id", "a0"), **kw)


def test_legal_input_passes_and_replays_identically():
    _, opp, ex, feats = _setup()
    r1, r2 = _eval(opp, ex, feats), _eval(opp, ex, feats)
    assert r1.status == "ok" and r1.n_evaluated == 4
    assert (r1.theta, r1.se, r1.n_evaluated) == (r2.theta, r2.se, r2.n_evaluated)


@pytest.mark.parametrize("key,value", [("graph_version", "wrong-graph"), ("decision_snapshot_hash", "wrong-snapshot"),
                                       ("t_dec", T0 + dt.timedelta(days=9))])
def test_single_identity_mismatch_is_refused(key, value):
    _, opp, ex, feats = _setup()
    bad = ex.with_columns(pl.lit(value).cast(ex.schema[key]).alias(key))
    with pytest.raises(EvalProtocolError, match=key):
        _eval(opp, bad, feats)


def test_the_or05_reproduction_both_identity_columns_foreign():
    """审查原样复现：两列同时换成外来身份。"""
    _, opp, ex, feats = _setup()
    bad = ex.with_columns(pl.lit("wrong-graph").alias("graph_version"), pl.lit("wrong-snapshot").alias("decision_snapshot_hash"))
    with pytest.raises(EvalProtocolError, match="冻结机会集"):
        _eval(opp, bad, feats)


def test_two_arms_consistently_wrong_are_refused():
    """两个 policy 臂**彼此一致**地取自错误图版本：臂间比较放行，冻结身份比较拒收。"""
    ep, opp, _, feats = _setup()
    both = pl.concat([execution(ep, R, policy="base"), execution(ep, R, policy="cand")], how="vertical_relaxed")
    both = both.with_columns(pl.lit("wrong-graph").alias("graph_version"))
    with pytest.raises(EvalProtocolError, match="graph_version"):
        _eval(opp, both, feats, baseline_policy="base", candidate_policy="cand")
    # 突变：只做臂间比较（不给冻结身份）时，同一份伪造照样配对成功
    paired = pair_arms(both, opp.episode_ids, baseline_policy="base", candidate_policy="cand")
    assert paired.height == 4


def test_mutation_without_provenance_check_the_forgery_is_paired():
    _, opp, ex, _ = _setup()
    bad = ex.with_columns(pl.lit("wrong-graph").alias("graph_version"))
    with pytest.raises(EvalProtocolError, match="graph_version"):
        pair_arms(bad, opp.episode_ids, provenance=opp.provenance)
    assert pair_arms(bad, opp.episode_ids).height == 4


def test_unresolved_alias_does_not_match_the_frozen_version():
    """alias 必须先解析成不可变版本：执行结果写 latest、机会集冻结的是具体版本，按不等拒收。"""
    _, opp, ex, feats = _setup()
    with pytest.raises(EvalProtocolError, match="graph_version"):
        _eval(opp, ex.with_columns(pl.lit("latest").alias("graph_version")), feats)


def test_features_carrying_foreign_identity_are_refused():
    _, opp, ex, feats = _setup()
    f = feats.with_columns(pl.lit("wrong-graph").alias("graph_version"))
    with pytest.raises(EvalProtocolError, match="features"):
        _eval(opp, ex, f)


def test_provenance_is_part_of_the_frozen_digest():
    _, opp, ex, feats = _setup()
    swapped = dataclasses.replace(opp, provenance=opp.provenance.with_columns(pl.lit("wrong-graph").alias("graph_version")))
    with pytest.raises(EvalProtocolError, match="改动"):
        swapped.verify()
    dropped = dataclasses.replace(opp, provenance=None)
    with pytest.raises(EvalProtocolError, match="改动"):
        dropped.verify()


def test_hand_built_set_without_provenance_is_refused():
    _, opp, ex, feats = _setup()
    bare = OpportunitySet(opp.episode_ids, opp.eligibility, opp.weights)
    with pytest.raises(EvalProtocolError, match="来源身份"):
        _eval(bare, ex, feats)


def test_freeze_requires_identity_columns_and_non_null_values():
    ep = episodes(ROWS)
    with pytest.raises(EvalProtocolError, match="decision_snapshot_hash"):
        freeze_opportunity_set(ep.drop("decision_snapshot_hash"))
    with pytest.raises(EvalProtocolError, match="graph_version"):
        freeze_opportunity_set(ep.with_columns(pl.lit(None, dtype=pl.Utf8).alias("graph_version")))


def _reserved(led):
    return led.reserve(origin="enumeration", canonical_hash=H, params={"w": 1}, fold_id="f0", visible_cutoff=T0,
                       objective="theta", data_manifest="dm", seed=1)


@pytest.mark.parametrize("damage", ["foreign_execution", "tampered_opportunity_set"])
def test_refusal_leaves_a_failed_terminal_state_in_the_ledger(damage):
    """账本已预留时，拒收必须留下 failed 终态，不能停在 running（被改动的机会集原先在 try 之外被拒）。"""
    _, opp, ex, feats = _setup()
    led = MemoryLedger()
    aid = _reserved(led)
    if damage == "foreign_execution":
        ex = ex.with_columns(pl.lit("wrong-graph").alias("graph_version"))
    else:
        opp = OpportunitySet(opp.episode_ids + ["zzz"], opp.eligibility, opp.weights, opp.digest, provenance=opp.provenance)
    with pytest.raises(EvalProtocolError):
        _eval(opp, ex, feats, attempt_id=aid, ledger=led)
    assert led.status_of(aid) == "failed"


# ---------------------------------------------------------------- 二审残留：公开的 feature_snapshot 路径

def _snapshot(opp, *, graph="gv", snapshot="h", with_identity=True):
    from quant_lab.research.features import feature_snapshot
    n = len(opp.episode_ids)
    anchors = opp.provenance.with_columns(pl.lit("BTCUSDT-PERP.BINANCE-UM").alias("instrument_id"),
                                          pl.lit(graph).alias("graph_version"), pl.lit(snapshot).alias("decision_snapshot_hash"))
    if not with_identity:
        anchors = anchors.drop("graph_version", "decision_snapshot_hash")
    bars = pl.DataFrame({"instrument_id": ["BTCUSDT-PERP.BINANCE-UM"] * n, "interval": ["1m"] * n,
                         "close_time": opp.provenance["t_dec"], "close": [1.0] * n})
    return feature_snapshot([AST], anchors, bars=bars)


def test_public_snapshot_carries_the_anchor_identity_and_matching_features_pass():
    _, opp, ex, _ = _setup()
    f = _snapshot(opp)
    assert {"graph_version", "decision_snapshot_hash", "t_dec"} <= set(f.columns)
    assert _eval(opp, ex, f).status == "ok"


def test_public_snapshot_from_a_foreign_graph_is_refused():
    """二审复现：另一张图的 anchors 经正常 feature_snapshot 算出的特征，以前只剩 t_dec、照样被接受。"""
    _, opp, ex, _ = _setup()
    with pytest.raises(EvalProtocolError, match="features"):
        _eval(opp, ex, _snapshot(opp, graph="foreign-graph", snapshot="foreign-snapshot"))


def test_features_without_identity_columns_are_refused():
    """来源身份不可省略：anchors 不带身份时快照也不带，评估拒收，而不是按「没有就不查」放行。"""
    _, opp, ex, _ = _setup()
    with pytest.raises(EvalProtocolError, match="缺来源身份列"):
        _eval(opp, ex, _snapshot(opp, with_identity=False))


def test_mutation_optional_feature_identity_would_accept_the_foreign_snapshot(monkeypatch):
    """突变：把特征侧核对退回「列在才查」，丢掉身份列的外来快照就被接受——拒收来自这道门。"""
    import quant_lab.research.evaluator as E
    _, opp, ex, _ = _setup()
    foreign = _snapshot(opp, graph="foreign-graph", snapshot="foreign-snapshot").drop("graph_version", "decision_snapshot_hash")
    real = E._check_provenance
    monkeypatch.setattr(E, "PROVENANCE_KEYS", ("t_dec",))
    monkeypatch.setattr(E, "_check_provenance", lambda frame, prov, label, keys=("t_dec",): real(frame, prov, label, keys=("t_dec",)))
    assert _eval(opp, ex, foreign).status == "ok"
