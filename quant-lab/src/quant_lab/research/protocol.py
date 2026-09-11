"""研究协议切分（契约 feature-snapshot §5；ADR-G3 §7；合并稿 D.5/E.1）。

walk_forward(anchors, *, scheme ∈ {expanding, rolling}, min_train_clusters, embargo, label_maturity, ...) → 折列表。
- anchors 列：episode_id, cluster_id, t_dec, t1（标签信息终点，来自执行/随访证据；null = 未知 → 不入监督训练，记损耗）。
- 日历：UTC 固定起点 origin（默认最早 t_dec 所在日 00:00），先 min_train_span 训练跨度，然后连续 test_span 测试窗。
- 训练集：t_dec < test_start 且 t1 + label_maturity <= test_start（标签成熟）且 [t_dec, t1] 与任一测试机会区间闭区间不交
  且不与测试机会同簇；rolling 另限 t_dec >= test_start − train_span。embargo：t_dec ∈ (max_test_t1, max_test_t1 + embargo] 的候选
  purge（walk-forward 只有过去训练时通常为空操作）。
- 测试集：t_dec ∈ [test_start, test_end)；同簇跨外测试窗 → 整簇归最早触及窗，其余成员记 CLUSTER_BOUNDARY 排除；每机会只进一次测试。
- 折不足 min_train_clusters 不删除，标 insufficient（整体降档）。
PurgedKFold(t1)：O(n²) 枚举 oracle（ADR §7.1），只作稳定性对照，不作上线后表现声明。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import polars as pl

UTC_US = pl.Datetime("us", "UTC")


@dataclass(frozen=True)
class Fold:
    fold_id: str
    train_ids: tuple[str, ...]
    test_ids: tuple[str, ...]
    train_start: dt.datetime | None
    cutoff: dt.datetime            # 训练可见上限 = test_start
    test_start: dt.datetime
    test_end: dt.datetime
    purged: dict[str, str]         # episode_id → reason（SAME_CLUSTER / INTERVAL_OVERLAP / EMBARGO / IMMATURE / UNKNOWN_T1 / INVALID_INTERVAL / OUT_OF_TRAIN_SPAN）
    embargo_interval: tuple[dt.datetime, dt.datetime] | None
    n_train_clusters: int
    n_test_clusters: int
    insufficient: bool
    loss: dict = field(default_factory=dict)


def _check(anchors: pl.DataFrame) -> pl.DataFrame:
    for c in ("episode_id", "cluster_id", "t_dec"):
        if c not in anchors.columns:
            raise ValueError(f"anchors 缺列 {c}")
    a = anchors
    if "t1" not in a.columns:
        a = a.with_columns(pl.lit(None, dtype=UTC_US).alias("t1"))
    if "duplicate_group_id" not in a.columns:
        a = a.with_columns(pl.col("episode_id").alias("duplicate_group_id"))
    for c in ("t_dec", "t1"):
        d = a.schema[c]
        if not isinstance(d, pl.Datetime) or d.time_zone != "UTC":
            raise ValueError(f"{c} 必须为 Datetime(_, 'UTC')")
    if a["episode_id"].n_unique() != a.height:
        raise ValueError("episode_id 重复")
    return a.select("episode_id", "cluster_id", "duplicate_group_id", "t_dec", "t1").sort("t_dec", "episode_id")


def purge_train(cands: list[dict], tests: list[dict], *, embargo: dt.timedelta, label_maturity: dt.timedelta,
                fit_time: dt.datetime | None) -> tuple[list[str], dict[str, str]]:
    """枚举 oracle（ADR §7.1）：逐候选 i × 逐测试 j 判 purge，返回 (保留 id 列表, purge 原因)。"""
    keep, why = [], {}
    test_clusters = {t["cluster_id"] for t in tests}
    test_groups = {t["duplicate_group_id"] for t in tests}
    max_test_t1 = max((t["t1"] for t in tests if t["t1"] is not None), default=None)
    for c in cands:
        t0, t1 = c["t_dec"], c["t1"]
        if t1 is None:
            why[c["episode_id"]] = "UNKNOWN_T1"
            continue
        if t0 is None or t1 < t0:
            why[c["episode_id"]] = "INVALID_INTERVAL"
            continue
        if fit_time is not None and t1 + label_maturity > fit_time:
            why[c["episode_id"]] = "IMMATURE"
            continue
        if c["cluster_id"] in test_clusters or c["duplicate_group_id"] in test_groups:
            why[c["episode_id"]] = "SAME_CLUSTER"
            continue
        if any(t["t1"] is not None and t0 <= t["t1"] and t["t_dec"] <= t1 for t in tests) or \
           any(t["t1"] is None and t["t_dec"] <= t1 and t0 <= t["t_dec"] for t in tests):
            why[c["episode_id"]] = "INTERVAL_OVERLAP"
            continue
        if max_test_t1 is not None and embargo > dt.timedelta(0) and max_test_t1 < t0 <= max_test_t1 + embargo:
            why[c["episode_id"]] = "EMBARGO"
            continue
        keep.append(c["episode_id"])
    return keep, why


def walk_forward(
    anchors: pl.DataFrame, *, scheme: str = "expanding", min_train_clusters: int = 20,
    embargo: dt.timedelta = dt.timedelta(0), label_maturity: dt.timedelta = dt.timedelta(0),
    test_span: dt.timedelta = dt.timedelta(days=30), min_train_span: dt.timedelta = dt.timedelta(days=90),
    train_span: dt.timedelta | None = None, origin: dt.datetime | None = None, max_folds: int | None = None,
) -> list[Fold]:
    if scheme not in ("expanding", "rolling"):
        raise ValueError("scheme ∈ {expanding, rolling}")
    if scheme == "rolling" and train_span is None:
        train_span = min_train_span
    a = _check(anchors)
    rows = a.to_dicts()
    if not rows:
        return []
    first = min(r["t_dec"] for r in rows)
    origin = origin or first.replace(hour=0, minute=0, second=0, microsecond=0)
    last = max(r["t_dec"] for r in rows)
    folds: list[Fold] = []
    assigned: dict[str, str] = {}       # cluster_id → fold_id（整簇归最早触及窗）
    test_start = origin + min_train_span
    k = 0
    carry: list[dict] = []              # 空测试窗的边界排除，并入下一个非空折的 purged（不丢损耗记录）
    while test_start <= last and (max_folds is None or k < max_folds):
        test_end = test_start + test_span
        fid = f"wf{k:03d}"
        in_win = [r for r in rows if test_start <= r["t_dec"] < test_end]
        tests, boundary = [], list(carry)
        for r in in_win:
            owner = assigned.setdefault(r["cluster_id"], fid)
            (tests if owner == fid else boundary).append(r)
        if not tests:
            # 空测试窗保留为折（不删除）：记录边界损耗；不进入训练/测试统计
            why0 = {r["episode_id"]: "CLUSTER_BOUNDARY" for r in boundary}
            folds.append(Fold(fold_id=fid, train_ids=(), test_ids=(), train_start=None, cutoff=test_start, test_start=test_start, test_end=test_end,
                              purged=why0, embargo_interval=None, n_train_clusters=0, n_test_clusters=0, insufficient=True,
                              loss={"n_candidates": 0, "n_train": 0, "n_test": 0, "n_boundary_excluded": len(boundary), "purge_reasons": _count(why0), "empty_window": True}))
            carry = []
            test_start = test_end
            k += 1
            continue
        carry = []
        cands = [r for r in rows if r["t_dec"] < test_start]
        span_excl = {}
        if scheme == "rolling":
            lo = test_start - train_span
            span_excl = {r["episode_id"]: "OUT_OF_TRAIN_SPAN" for r in cands if r["t_dec"] < lo}
            cands = [r for r in cands if r["t_dec"] >= lo]
        keep, why = purge_train(cands, tests, embargo=embargo, label_maturity=label_maturity, fit_time=test_start)
        why.update(span_excl)
        why.update({r["episode_id"]: "CLUSTER_BOUNDARY" for r in boundary})
        keep_rows = [r for r in cands if r["episode_id"] in set(keep)]
        n_tr = len({r["cluster_id"] for r in keep_rows})
        max_t1 = max((t["t1"] for t in tests if t["t1"] is not None), default=None)
        emb = (max_t1, max_t1 + embargo) if (max_t1 is not None and embargo > dt.timedelta(0)) else None
        folds.append(Fold(
            fold_id=fid, train_ids=tuple(r["episode_id"] for r in keep_rows), test_ids=tuple(r["episode_id"] for r in tests),
            train_start=min((r["t_dec"] for r in keep_rows), default=None), cutoff=test_start, test_start=test_start, test_end=test_end,
            purged=why, embargo_interval=emb, n_train_clusters=n_tr, n_test_clusters=len({t["cluster_id"] for t in tests}),
            insufficient=n_tr < min_train_clusters,
            loss={"n_candidates": len(cands) + len(span_excl), "n_train": len(keep_rows), "n_test": len(tests),
                  "n_boundary_excluded": len(boundary), "purge_reasons": _count(why)},
        ))
        test_start = test_end
        k += 1
    return folds


def _count(why: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in why.values():
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


class PurgedKFold:
    """PurgedKFold(t1) 参考实现：按 t_dec 顺序切 n_splits 个连续测试块，训练 = 其余经 purge/embargo（枚举 oracle）。
    只作稳定性对照（可用未来数据训练），不作上线后表现声明。"""

    def __init__(self, t1: pl.DataFrame, *, n_splits: int = 5, embargo: dt.timedelta = dt.timedelta(0), label_maturity: dt.timedelta = dt.timedelta(0)):
        self.anchors = _check(t1)
        self.n_splits, self.embargo, self.label_maturity = n_splits, embargo, label_maturity

    def split(self) -> list[Fold]:
        rows = self.anchors.to_dicts()
        n = len(rows)
        folds = []
        bounds = [round(i * n / self.n_splits) for i in range(self.n_splits + 1)]
        for k in range(self.n_splits):
            tests = rows[bounds[k]:bounds[k + 1]]
            if not tests:
                continue
            cands = rows[:bounds[k]] + rows[bounds[k + 1]:]
            keep, why = purge_train(cands, tests, embargo=self.embargo, label_maturity=self.label_maturity, fit_time=None)
            keep_rows = [r for r in cands if r["episode_id"] in set(keep)]
            max_t1 = max((t["t1"] for t in tests if t["t1"] is not None), default=None)
            folds.append(Fold(
                fold_id=f"pk{k:02d}", train_ids=tuple(r["episode_id"] for r in keep_rows), test_ids=tuple(t["episode_id"] for t in tests),
                train_start=min((r["t_dec"] for r in keep_rows), default=None), cutoff=tests[0]["t_dec"], test_start=tests[0]["t_dec"],
                test_end=tests[-1]["t_dec"], purged=why,
                embargo_interval=(max_t1, max_t1 + self.embargo) if (max_t1 is not None and self.embargo > dt.timedelta(0)) else None,
                n_train_clusters=len({r["cluster_id"] for r in keep_rows}), n_test_clusters=len({t["cluster_id"] for t in tests}),
                insufficient=False, loss={"purge_reasons": _count(why)},
            ))
        return folds


__all__ = ["Fold", "PurgedKFold", "purge_train", "walk_forward"]
