"""R-07：walk-forward / PurgedKFold(t1) 性质测试 + 尝试账本。"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quant_lab.research.ledger import Ledger, LedgerBudgetExhausted, LedgerError, TERMINAL
from quant_lab.research.protocol import PurgedKFold, purge_train, walk_forward

UTC = pl.Datetime("us", "UTC")
T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
D = dt.timedelta(days=1)
H = dt.timedelta(hours=1)
US = dt.timedelta(microseconds=1)


def anchors(rows):
    """rows: (id, cluster, t_dec_days, t1_days|None)"""
    return pl.DataFrame({
        "episode_id": [r[0] for r in rows], "cluster_id": [r[1] for r in rows],
        "t_dec": [T0 + r[2] * D for r in rows], "t1": [None if r[3] is None else T0 + r[3] * D for r in rows],
    }, schema_overrides={"t_dec": UTC, "t1": UTC})


def row(id_, cl, t0, t1):
    return {"episode_id": id_, "cluster_id": cl, "duplicate_group_id": id_, "t_dec": t0, "t1": t1}


# ---------------------------------------------------------------- purge oracle 性质
def test_closed_interval_endpoints_equal_purged_1us_apart_not():
    test = [row("t", "ct", T0 + 10 * D, T0 + 12 * D)]
    keep, why = purge_train([row("a", "ca", T0 + 5 * D, T0 + 10 * D)], test, embargo=dt.timedelta(0), label_maturity=dt.timedelta(0), fit_time=None)
    assert why == {"a": "INTERVAL_OVERLAP"}                                    # 端点相等 → purge
    keep, why = purge_train([row("a", "ca", T0 + 5 * D, T0 + 10 * D - US)], test, embargo=dt.timedelta(0), label_maturity=dt.timedelta(0), fit_time=None)
    assert keep == ["a"] and not why                                            # 差 1us 不重叠
    keep, why = purge_train([row("b", "cb", T0 + 12 * D, T0 + 13 * D)], test, embargo=dt.timedelta(0), label_maturity=dt.timedelta(0), fit_time=None)
    assert why == {"b": "INTERVAL_OVERLAP"}


def test_same_cluster_unknown_t1_invalid_interval_immature():
    test = [row("t", "c1", T0 + 10 * D, T0 + 11 * D)]
    cands = [row("a", "c1", T0 + 1 * D, T0 + 2 * D), row("b", "c9", T0 + 1 * D, None), row("c", "c9", T0 + 3 * D, T0 + 2 * D),
             row("d", "c9", T0 + 1 * D, T0 + 9 * D), row("e", "c9", T0 + 1 * D, T0 + 2 * D)]
    keep, why = purge_train(cands, test, embargo=dt.timedelta(0), label_maturity=2 * D, fit_time=T0 + 10 * D)
    assert why == {"a": "SAME_CLUSTER", "b": "UNKNOWN_T1", "c": "INVALID_INTERVAL", "d": "IMMATURE"} and keep == ["e"]


def test_embargo_monotone_and_test_expansion_monotone():
    test = [row("t", "ct", T0 + 10 * D, T0 + 12 * D)]
    cands = [row(f"a{i}", f"c{i}", T0 + (12 + i / 4) * D, T0 + (13 + i / 4) * D) for i in range(1, 9)]
    sizes = [len(purge_train(cands, test, embargo=e * D, label_maturity=dt.timedelta(0), fit_time=None)[0]) for e in (0, 0.5, 1, 2, 3)]
    assert sizes == sorted(sizes, reverse=True) and sizes[0] > sizes[-1]        # 增大 embargo 不增加 train
    keep1, _ = purge_train(cands, test, embargo=dt.timedelta(0), label_maturity=dt.timedelta(0), fit_time=None)
    keep2, _ = purge_train(cands, [row("t", "ct", T0 + 10 * D, T0 + 13 * D)], embargo=dt.timedelta(0), label_maturity=dt.timedelta(0), fit_time=None)
    assert set(keep2) <= set(keep1)                                             # 扩大测试区间不增加 train


def test_row_order_invariance_and_purged_kfold_oof_once():
    import random
    rows = [(f"e{i}", f"c{i // 3}", i * 2.0, i * 2.0 + 1.5) for i in range(40)]
    a = anchors(rows)
    f1 = PurgedKFold(a, n_splits=4, embargo=1 * D).split()
    rnd = rows[:]
    random.Random(3).shuffle(rnd)
    f2 = PurgedKFold(anchors(rnd), n_splits=4, embargo=1 * D).split()
    assert [set(f.train_ids) for f in f1] == [set(f.train_ids) for f in f2]
    assert [set(f.test_ids) for f in f1] == [set(f.test_ids) for f in f2]
    tested = [i for f in f1 for i in f.test_ids]
    assert sorted(tested) == sorted(a["episode_id"].to_list()) and len(tested) == len(set(tested))   # 每机会恰一次折外
    for f in f1:
        assert not (set(f.train_ids) & set(f.test_ids))
        assert f.embargo_interval is not None
    assert sum("SAME_CLUSTER" in f.loss["purge_reasons"] for f in f1) >= 3          # 跨块簇被 purge（最后一块之后无候选可 purge）
    assert any("EMBARGO" in f.loss["purge_reasons"] for f in f1)


# ---------------------------------------------------------------- walk-forward
def _wf_anchors(n=120, seed=0):
    import random
    r = random.Random(seed)
    rows = []
    for i in range(n):
        t0 = i * 1.5 + r.random()
        rows.append((f"e{i}", f"c{i // 2}", t0, t0 + r.choice([0.5, 1.0, 3.0])))
    return anchors(rows)


def test_walk_forward_shapes_and_causality():
    a = _wf_anchors()
    folds = walk_forward(a, scheme="expanding", min_train_clusters=5, embargo=1 * D, label_maturity=1 * D,
                         test_span=30 * D, min_train_span=60 * D)
    assert len(folds) >= 3
    ids = dict(zip(a["episode_id"], zip(a["t_dec"], a["t1"])))
    prev_cut = None
    tested = []
    for f in folds:
        assert prev_cut is None or f.cutoff > prev_cut
        prev_cut = f.cutoff
        for i in f.train_ids:
            t0, t1 = ids[i]
            assert t0 < f.cutoff and t1 + 1 * D <= f.cutoff                 # 训练只用 cutoff 前已成熟标签
        for i in f.test_ids:
            assert f.test_start <= ids[i][0] < f.test_end
        assert not (set(f.train_ids) & set(f.test_ids))
        assert f.n_train_clusters == len({a.filter(pl.col("episode_id") == i)["cluster_id"][0] for i in f.train_ids})
        tested += f.test_ids
    assert len(tested) == len(set(tested))                                   # 每机会至多一次折外
    assert any(f.insufficient for f in folds[:1]) or folds[0].n_train_clusters >= 5


def test_walk_forward_rolling_subset_of_expanding_and_cluster_boundary():
    a = _wf_anchors()
    ex = walk_forward(a, min_train_clusters=1, test_span=30 * D, min_train_span=60 * D)
    ro = walk_forward(a, scheme="rolling", min_train_clusters=1, test_span=30 * D, min_train_span=60 * D, train_span=45 * D)
    assert len(ex) == len(ro)
    for fe, fr in zip(ex, ro):
        assert set(fr.train_ids) <= set(fe.train_ids) and fr.test_ids == fe.test_ids
        assert "OUT_OF_TRAIN_SPAN" in fr.loss["purge_reasons"] or fr.train_ids == fe.train_ids
    # 跨窗簇：簇 c 的两机会分别落在相邻两个测试窗 → 整簇归最早窗，后者记 CLUSTER_BOUNDARY
    b = anchors([("x0", "cx", 89.5, 90.0), ("x1", "cx", 90.5, 91.0), ("w", "cw", 90.2, 90.4), ("y", "cy", 20.0, 21.0), ("z", "cz", 100.0, 101.0)])
    fs = walk_forward(b, min_train_clusters=1, test_span=1 * D, min_train_span=89 * D, origin=T0)
    f0, f1 = fs[0], fs[1]
    assert f0.test_ids == ("x0",) and f1.test_ids == ("w",) and f1.purged.get("x1") == "CLUSTER_BOUNDARY"
    assert f1.loss["n_boundary_excluded"] == 1
    assert "x1" not in [i for f in fs for i in f.test_ids]                    # x1 不进任何测试窗（整簇归最早窗）
    # 空测试窗的边界排除并入下一个非空折
    c = anchors([("x0", "cx", 89.5, 90.0), ("x1", "cx", 90.5, 91.0), ("y", "cy", 20.0, 21.0), ("z", "cz", 100.0, 101.0)])
    fs2 = walk_forward(c, min_train_clusters=1, test_span=1 * D, min_train_span=89 * D, origin=T0)
    nonempty = [f for f in fs2 if f.test_ids]
    assert [f.test_ids for f in nonempty] == [("x0",), ("z",)]
    empties = [f for f in fs2 if not f.test_ids]
    assert empties and empties[0].loss.get("empty_window") and empties[0].purged.get("x1") == "CLUSTER_BOUNDARY" and empties[0].insufficient   # 空窗保留为折并记边界损耗


def test_future_label_changes_do_not_alter_earlier_folds():
    a = _wf_anchors()
    f1 = walk_forward(a, min_train_clusters=1, test_span=30 * D, min_train_span=60 * D)
    late = a["t_dec"].max() - 20 * D
    a2 = a.with_columns(pl.when(pl.col("t_dec") > late).then(pl.col("t1") + 10 * D).otherwise(pl.col("t1")).alias("t1"))
    f2 = walk_forward(a2, min_train_clusters=1, test_span=30 * D, min_train_span=60 * D)
    for x, y in zip(f1[:-1], f2[:-1]):
        assert x.train_ids == y.train_ids and x.test_ids == y.test_ids


def test_walk_forward_preconditions():
    with pytest.raises(ValueError, match="缺列"):
        walk_forward(pl.DataFrame({"episode_id": ["a"], "t_dec": [T0]}, schema_overrides={"t_dec": UTC}))
    with pytest.raises(ValueError, match="scheme"):
        walk_forward(_wf_anchors(10), scheme="random")


# ---------------------------------------------------------------- 账本
def test_ledger_reserve_before_eval_terminal_states_and_budget(lake_root):
    led = Ledger(budget_configs=2)
    assert led.root.is_relative_to(lake_root)                                  # 路径经 QUANT_LAB_DATA_ROOT
    kw = dict(origin="enumeration", params={"w": 5}, fold_id="wf000", visible_cutoff=T0, objective="theta", data_manifest="dm1", seed=1)
    a1 = led.reserve(canonical_hash="h1", **kw)
    df = led.read()
    assert df.height == 1 and df["status"][0] == "reserved" and (led.root / "ledger.parquet").exists()
    assert set(df.columns) >= {"attempt_id", "origin", "parent_id", "canonical_hash", "params", "fold_id", "visible_cutoff",
                               "code_version", "model_version", "data_manifest", "seed", "cost", "objective", "status"}
    led.mark(a1, "running")
    led.mark(a1, "completed", objective=0.12, cost={"cpu_s": 1.5})
    assert led.read()["status"][0] == "completed" and led.read()["objective_value"][0] == 0.12
    with pytest.raises(LedgerError, match="终态"):
        led.mark(a1, "failed")                                                 # 终态不可改写
    a2 = led.reserve(canonical_hash="h1", **kw)                                # 同配置同折同 cutoff → duplicate 留终态
    r2 = led.read().filter(pl.col("attempt_id") == a2)
    assert r2["status"][0] == "duplicate" and r2["duplicate_of"][0] == a1
    a3 = led.reserve(canonical_hash="h2", **kw)
    led.mark(a3, "failed", reason="ValueError: boom")                          # 失败留终态
    with pytest.raises(LedgerBudgetExhausted):
        led.reserve(canonical_hash="h3", **kw)                                 # 第 3 个唯一配置超预算：记账且不派发
    st = dict(zip(led.read()["canonical_hash"], led.read()["status"]))
    assert st["h3"] == "budget_exhausted" and st["h2"] == "failed"
    a4 = led.reserve(canonical_hash="h2", **{**kw, "fold_id": "wf001"})        # 已用配置在新折不耗额度
    assert led.read().filter(pl.col("attempt_id") == a4)["status"][0] == "reserved"
    # 崩溃恢复：未闭合 → interrupted；事件文件只追加
    n_ev = len(list((led.root / "ledger-events").glob("*.json")))
    led2 = Ledger(budget_configs=2)
    assert led2.recover() == 0                                                     # 未指定 run 且未超龄：不中断活跃 run
    assert led2.recover(run_id=led.run_id) == 1 and led2.read().filter(pl.col("attempt_id") == a4)["status"][0] == "interrupted"
    assert len(list((led.root / "ledger-events").glob("*.json"))) == n_ev + 1
    # 投影损坏只重建不丢事件
    (led.root / "ledger.parquet").write_bytes(b"garbage")
    assert Ledger().read().height == 5
    led.invalidate(a1, by="gv-0002")
    r = led.read().filter(pl.col("attempt_id") == a1)
    assert r["status"][0] == "completed" and r["invalidated_by"][0] == "gv-0002"
    assert all(s in TERMINAL for s in led.read()["status"].to_list())


def test_ledger_rejects_bad_origin(lake_root):
    with pytest.raises(LedgerError):
        Ledger().reserve(origin="robot", canonical_hash="h", params={}, fold_id="f", visible_cutoff=T0, objective="theta", data_manifest="d", seed=0)
