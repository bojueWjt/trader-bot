"""封顶小语法枚举生成器（契约 feature-snapshot §6；ADR-G3 §12）。DEAP 未接入（G-LICENSE-SEARCH 未批），只做封顶枚举。

enumerate_grammar(depth=2, *, ops, windows, fields, cap) → 合法 JSON AST 列表（≤ cap，确定性顺序）：
- ops / windows / fields 先排序去重并校验（未知项拒绝；depth ∈ [1, 4]；windows 正整数；cap/visited 非负）；
- 逐深度**流式**生成：itertools.product 惰性迭代，逐候选先做类型/单位剪枝（lint）→ canonical_hash 去重 → 预算检查，
  不先物化笛卡尔积；visited（尝试的候选节点数）达上限或收集满 cap 即停；
- 可交换算子只生成 canonical_hash 序 i ≤ j 的对；不读收益；输入重排不改变集合与顺序。
返回 list；enumerate_grammar_meta 另返回 {"n": 池大小, "visited": 访问数, "stop_reason": cap|visited|exhausted}。
"""
from __future__ import annotations

import itertools

from quant_lab.research.ast import MAX_DEPTH, ASTRejected, canonical_hash, lint
from quant_lab.research.ops import FIELD_UNITS, REGISTRY

MAX_VISITED = 200_000


def enumerate_grammar_meta(depth: int = 2, *, ops: list[str], windows: list[int], fields: list[str], cap: int,
                           max_visited: int = MAX_VISITED) -> tuple[list[dict], dict]:
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1 or depth > MAX_DEPTH:
        raise ValueError(f"depth 须为 1..{MAX_DEPTH} 的整数")
    if cap < 0 or max_visited < 0:
        raise ValueError("cap / max_visited 须非负")
    ops = sorted(set(ops)); fields = sorted(set(fields))
    ws = []
    for w in windows:
        if isinstance(w, bool) or not isinstance(w, int) or w <= 0:
            raise ValueError(f"windows 必须为正整数，得到 {w!r}")
        ws.append(w)
    windows = sorted(set(ws))
    bad = [o for o in ops if o not in REGISTRY] + [f for f in fields if f not in FIELD_UNITS]
    if bad:
        raise ValueError(f"未注册的 op/field: {bad}")
    out: list[dict] = []
    seen: set[str] = set()
    visited = 0
    meta = {"stop_reason": "exhausted"}
    if cap == 0:
        return [], {"n": 0, "visited": 0, "stop_reason": "cap"}
    levels: list[list[tuple[str, dict]]] = [[(canonical_hash(n), n) for n in ({"field": f} for f in fields)]]

    def try_add(node: dict) -> tuple[bool, str | None]:
        nonlocal visited
        visited += 1
        try:
            lint(node)
            h = canonical_hash(node)
        except ASTRejected:
            return False, None
        if h in seen:
            return False, None
        seen.add(h)
        out.append(node)
        return True, h

    for d in range(2, depth + 1):
        new: list[tuple[str, dict]] = []
        prev_all = [n for lv in levels for n in lv]
        prev_top = levels[-1]
        top_hashes = {h for h, _ in prev_top}
        for op in ops:
            spec = REGISTRY[op]
            variants: list[tuple[dict | None, dict | None]] = [(None, None)]
            if "lag" in spec.params:
                variants = [(None, {"lag": w}) for w in windows if w >= spec.params["lag"]["min"]]
            elif spec.windowed:
                variants = [({"unit": "rows", "count": w}, None) for w in windows if w >= spec.min_samples]
            if spec.arity == 1:
                combos = ((a,) for a in prev_top)
            else:
                combos = ((a, b) for a, b in itertools.product(prev_all, prev_all)
                          if (a[0] in top_hashes or b[0] in top_hashes) and (not spec.commutative or a[0] <= b[0]))
            for args in combos:                       # 惰性：逐候选剪枝/去重/预算
                for win, params in variants:
                    if visited >= max_visited:
                        meta["stop_reason"] = "visited"
                        return out[:cap], {"n": len(out[:cap]), "visited": visited, **meta}
                    node = {"op": op, "args": [a[1] for a in args]}
                    if win is not None:
                        node["window"] = win
                    if params is not None:
                        node["params"] = params
                    ok, h = try_add(node)
                    if ok:
                        new.append((h, node))
                    if len(out) >= cap:
                        meta["stop_reason"] = "cap"
                        return out[:cap], {"n": len(out[:cap]), "visited": visited, **meta}
        levels.append(new)
    return out[:cap], {"n": len(out[:cap]), "visited": visited, **meta}


def enumerate_grammar(depth: int = 2, *, ops: list[str], windows: list[int], fields: list[str], cap: int,
                      max_visited: int = MAX_VISITED) -> list[dict]:
    return enumerate_grammar_meta(depth, ops=ops, windows=windows, fields=fields, cap=cap, max_visited=max_visited)[0]


__all__ = ["MAX_VISITED", "enumerate_grammar", "enumerate_grammar_meta"]
