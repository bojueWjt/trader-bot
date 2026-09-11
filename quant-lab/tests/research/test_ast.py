"""R-03：AST 硬门拒收用例 + 规范化/哈希性质（ADR-G3 §2：只消表示差异，根深度 1）。"""
from __future__ import annotations

import copy
import json

import pytest

from quant_lab.research import ast as A
from quant_lab.research import ops
from quant_lab.research.ast import ASTRejected, canonical_hash, canonicalize, lint, parse_json_text

F = lambda n: {"field": n}                                    # noqa: E731
C = lambda v: {"const": v}                                    # noqa: E731
W = lambda m: {"unit": "wallclock", "minutes": m}             # noqa: E731
R = lambda n: {"unit": "rows", "count": n}                    # noqa: E731


def op(name, *args, window=None, params=None):
    d = {"op": name, "args": list(args)}
    if window is not None:
        d["window"] = window
    if params is not None:
        d["params"] = params
    return d


GOOD = op("SafeDiv", op("Mean", F("close"), window=W(300)), F("close"))   # 契约 §1 示例（Div → SafeDiv，ADR C01）


def test_contract_example_passes():
    rep = lint(GOOD)
    assert rep.depth == 3 and rep.n_nodes == 4 and rep.unit == "ratio"
    assert rep.fields == ("close",) and rep.ops == ("Mean", "SafeDiv") and rep.windows == (("wallclock", 300),)
    assert rep.lookback_minutes == 300 and rep.lookback_rows == 0
    assert rep.canonicalization_version == "g3-json-c14n-v1" and rep.registry_digest == ops.registry_digest()
    assert len(canonical_hash(GOOD)) == 64


# ---------------------------------------------------------------- 拒收用例（每条硬门至少一例）
REJECT = [
    ("UNKNOWN_OP", op("Div", F("close"), F("open"))),                 # 契约示例的 Div 未注册：拒收，不自动别名
    ("UNKNOWN_OP", op("Rank", F("close"), window=R(5))),
    ("CROSS_ASSET", op("CSRank", F("close"))),
    ("UNKNOWN_FIELD", op("Abs", F("funding_rate_next"))),
    ("UNKNOWN_FIELD", op("Abs", F("net_R"))),
    ("UNKNOWN_FIELD", {"field": 3}),
    ("UNKNOWN_KEY", {"op": "Abs", "args": [F("close")], "extra": 1}),
    ("UNKNOWN_KEY", {"op": "Abs", "args": [F("close")], "metadata": {"x": 1}}),
    ("UNKNOWN_KEY", {"field": "close", "op": "Abs"}),
    ("UNKNOWN_KEY", {"const": 1, "window": W(5)}),
    ("UNKNOWN_KEY", {"args": [F("close")]}),
    ("UNKNOWN_KEY", op("Mean", F("close"), window={"unit": "wallclock", "minutes": 5, "min_samples": 1})),
    ("UNKNOWN_KEY", op("Mean", F("close"), window={"unit": "rows", "count": 5, "minutes": 5})),
    ("UNKNOWN_KEY", op("Ref", F("close"), params={"lag": 1, "fill": 0})),
    ("NON_FINITE_CONST", op("Add", F("close"), C(float("inf")))),
    ("NON_FINITE_CONST", op("Add", F("close"), C(float("nan")))),
    ("CONST_TYPE", op("Add", F("close"), C("1"))),
    ("CONST_TYPE", op("Add", F("close"), C(True))),
    ("EMPTY", C(1)),
    ("EMPTY", {}),
    ("NOT_A_DICT", op("Abs", "close")),
    ("NOT_A_DICT", op("Abs", ["close"])),
    ("ARITY", op("Add", F("close"))),
    ("ARITY", op("Abs", F("close"), F("open"))),
    ("ARITY", {"op": "Abs", "args": []}),
    ("ARITY", {"op": "Abs", "args": F("close")}),
    ("NEGATIVE_REF", op("Ref", F("close"), params={"lag": -1})),
    ("PARAM", op("Ref", F("close"))),                                   # 缺 lag
    ("UNKNOWN_KEY", op("Ref", F("close"), params={"n": 1})),            # 旧参数名
    ("PARAM", op("Ref", F("close"), params={"lag": 1.5})),
    ("PARAM", op("Ref", F("close"), params={"lag": True})),
    ("PARAM", op("Delta", F("close"), params={"lag": 0})),              # Delta lag ≥ 1
    ("WINDOW_MISSING", op("Mean", F("close"))),
    ("WINDOW_FORBIDDEN", op("Abs", F("close"), window=W(5))),
    ("WINDOW_FORBIDDEN", op("Ref", F("close"), window=R(5), params={"lag": 1})),
    ("WINDOW_CENTER", op("Mean", F("close"), window={"unit": "rows", "count": 5, "center": True})),
    ("WINDOW_CENTER", op("Mean", F("close"), window={"unit": "rows", "count": 5, "center": False})),
    ("WINDOW_UNIT", op("Mean", F("close"), window={"unit": "bars", "count": 5})),
    ("WINDOW_UNIT", op("Mean", F("close"), window=[5])),
    ("WINDOW_UNIT", op("EMA", F("close"), window=W(60))),               # EMA 只接受 rows
    ("WINDOW_SIZE", op("Mean", F("close"), window=R(0))),
    ("WINDOW_SIZE", op("Mean", F("close"), window={"unit": "wallclock", "minutes": -5})),
    ("WINDOW_SIZE", op("Mean", F("close"), window={"unit": "wallclock", "minutes": 300.0})),   # 校验先于规范化
    ("WINDOW_SIZE", op("Mean", F("close"), window={"unit": "rows", "count": 2.5})),
    ("WINDOW_SIZE", op("Std", F("close"), window=R(1))),                # Std min_samples=2
    ("UNIT_MISMATCH", op("Add", F("close"), F("volume"))),
    ("UNIT_MISMATCH", op("Gt", F("close"), F("trades"))),
    ("UNIT_MISMATCH", op("And", F("close"), op("Gt", F("close"), F("open")))),
    ("UNIT_MISMATCH", op("Mean", op("Gt", F("close"), F("open")), window=R(5))),
    ("UNIT_MISMATCH", op("Ref", op("Gt", F("close"), F("open")), params={"lag": 1})),
    ("UNIT_MISMATCH", op("Sub", op("LogPositive", F("close")), F("close"))),
]


@pytest.mark.parametrize("code,tree", REJECT, ids=[f"{c}-{i}" for i, (c, _) in enumerate(REJECT)])
def test_rejects(code, tree):
    with pytest.raises(ASTRejected) as ei:
        lint(tree)
    assert ei.value.code == code, str(ei.value)
    with pytest.raises(ASTRejected):
        canonical_hash(tree)


def _chain(n):  # n 层一元算子链；深度 = n + 1（叶为 1）
    t = F("close")
    for _ in range(n):
        t = op("Abs", t)
    return t


def test_depth_gate_root_is_one_leaf_counts():
    assert lint(F("close")).depth == 1
    assert lint(_chain(3)).depth == 4
    with pytest.raises(ASTRejected) as ei:
        lint(_chain(4))
    assert ei.value.code == "DEPTH" and ei.value.path == "$"


def test_width_gate_counts_repeated_subtrees():
    leaves = [F("close")] * 8
    lvl = leaves
    while len(lvl) > 1:
        lvl = [op("Add", lvl[i], lvl[i + 1]) for i in range(0, len(lvl), 2)]
    assert lint(lvl[0]).n_nodes == 15 and lint(lvl[0]).depth == 4
    with pytest.raises(ASTRejected) as ei:
        lint(op("Mul", lvl[0], C(2)))     # 17 节点，重复子树按出现次数计
    assert ei.value.code == "WIDTH"


def test_window_registry_gate():
    lint(GOOD, allowed_windows=[("wallclock", 300)])
    with pytest.raises(ASTRejected) as ei:
        lint(GOOD, allowed_windows=[("wallclock", 60), ("rows", 20)])
    assert ei.value.code == "WINDOW_NOT_REGISTERED"


def test_quarantined_op_rejected():
    ops.quarantine("Corr", "test")
    try:
        with pytest.raises(ASTRejected) as ei:
            lint(op("Corr", F("close"), F("volume"), window=R(5)))
        assert ei.value.code == "OP_QUARANTINED"
    finally:
        ops.release("Corr")
    lint(op("Corr", F("close"), F("volume"), window=R(5)))


# ---------------------------------------------------------------- 文本传输层
def test_parse_json_text_gates():
    assert parse_json_text('{"op":"Abs","args":[{"field":"close"}]}') == op("Abs", F("close"))
    for bad, code in [
        ('{"op":"Abs","op":"Neg","args":[]}', "UNKNOWN_KEY"),          # 重复键
        ('{"const": NaN}', "NON_FINITE_CONST"),
        ('{"const": Infinity}', "NON_FINITE_CONST"),
        ('{"const": 1} trailing', "NOT_A_DICT"),
        ('[{"field":"close"}]', "NOT_A_DICT"),
        ('"Mean(close, 5)"', "NOT_A_DICT"),
        ('{"const": 1' , "NOT_A_DICT"),
        ('{"pad": "' + "x" * (A.MAX_TEXT_BYTES) + '"}', "WIDTH"),
    ]:
        with pytest.raises(ASTRejected) as ei:
            parse_json_text(bad)
        assert ei.value.code == code, bad[:40]


# ---------------------------------------------------------------- 规范化与哈希（只消表示差异）
def test_canonical_is_key_order_and_number_form_invariant():
    a = {"args": [{"field": "close"}, {"const": 1}], "op": "Add"}
    b = op("Add", F("close"), C(1.0))
    c = json.loads('{"op":"Add","args":[{"field":"close"},{"const":1e0}]}')
    assert canonical_hash(a) == canonical_hash(b) == canonical_hash(c)
    assert A.canonical_json(a) == '{"args":[{"field":"close"},{"const":1}],"op":"Add"}'
    assert canonical_hash(op("Sub", F("close"), C(-0.0))) == canonical_hash(op("Sub", F("close"), C(0)))
    assert '"const":0.1' in A.canonical_json(op("Sub", F("close"), C(0.1)))
    assert '"const":0.1' in A.canonical_json(op("Sub", F("close"), C(1e-1)))


def test_no_algebraic_rewrites():
    """ADR §2：不排序可交换参数、不做 Lt→Gt、不消 Ref(0)、不折叠常量——只消表示差异。"""
    assert canonical_hash(op("Add", F("close"), F("open"))) != canonical_hash(op("Add", F("open"), F("close")))
    assert canonical_hash(op("Lt", F("close"), F("open"))) != canonical_hash(op("Gt", F("open"), F("close")))
    assert canonical_hash(op("Ref", F("close"), params={"lag": 0})) != canonical_hash(F("close"))
    assert canonical_hash(op("Sub", F("close"), F("open"))) != canonical_hash(op("Sub", F("open"), F("close")))
    assert canonicalize(op("Lt", F("close"), F("open")))["op"] == "Lt"


def test_hash_sensitive_to_window_and_params():
    h = lambda m: canonical_hash(op("Mean", F("close"), window=W(m)))   # noqa: E731
    assert h(300) != h(60)
    assert canonical_hash(op("Mean", F("close"), window=R(20))) != canonical_hash(op("Mean", F("close"), window=W(20)))
    assert canonical_hash(op("Ref", F("close"), params={"lag": 1})) != canonical_hash(op("Ref", F("close"), params={"lag": 2}))
    assert canonical_hash(op("Std", F("close"), window=R(20))) != canonical_hash(op("Mean", F("close"), window=R(20)))
    assert canonical_hash(op("Abs", F("close"))) != canonical_hash(op("Abs", F("open")))


def test_canonicalize_does_not_mutate_and_is_idempotent():
    src = copy.deepcopy(GOOD)
    c1 = canonicalize(GOOD)
    assert GOOD == src
    assert canonicalize(c1) == c1 and canonical_hash(c1) == canonical_hash(GOOD)
    assert A.canonical_json(json.loads(A.canonical_json(GOOD))) == A.canonical_json(GOOD)   # 解析规范串再规范化字节一致
    assert " " not in A.canonical_json(GOOD) and "\n" not in A.canonical_json(GOOD)


def test_fixed_hash_vector():
    """固定向量：规范化版本升级必须改这里，防止静默改变缓存/账本身份。"""
    import hashlib
    expected = hashlib.sha256(b'{"args":[{"args":[{"field":"close"}],"op":"Mean","window":{"minutes":300,"unit":"wallclock"}},{"field":"close"}],"op":"SafeDiv"}').hexdigest()
    assert canonical_hash(GOOD) == expected


def test_lookback_accounting():
    rep = lint(op("Delta", op("Mean", F("close"), window=R(20)), params={"lag": 5}))
    assert rep.lookback_rows == 24 and rep.lookback_minutes == 0
    rep = lint(op("Corr", op("Ref", F("close"), params={"lag": 3}), F("volume"), window=W(120)))
    assert rep.lookback_minutes == 120 and rep.lookback_rows == 3 and rep.unit == "ratio"
    rep = lint(op("And", op("Gt", F("close"), F("open")), op("Lt", F("volume"), C(10))))
    assert rep.unit == "bool" and rep.depth == 3


def test_unit_algebra_scalar_and_ratio():
    assert lint(op("Mul", F("close"), C(2))).unit == "price"
    assert lint(op("SafeDiv", F("close"), F("open"))).unit == "ratio"
    assert lint(op("Mul", F("close"), F("volume"))).unit == "derived"
    assert lint(op("LogPositive", F("volume"))).unit == "log"
    assert lint(op("TSRank", F("close"), window=R(10))).unit == "rank"
    assert lint(op("Gt", F("close"), C(100))).unit == "bool"
    assert lint(op("Add", op("LogPositive", F("close")), op("LogPositive", F("open")))).unit == "log"


def test_no_string_execution_surface():
    """铁律：不接受字符串表达式，不存在 eval/exec/compile/from_string 调用。"""
    with pytest.raises(ASTRejected):
        lint("Mean(close, 5)")   # type: ignore[arg-type]
    import ast as pyast
    import inspect
    tree = pyast.parse(inspect.getsource(A))
    calls = {n.func.id for n in pyast.walk(tree) if isinstance(n, pyast.Call) and isinstance(n.func, pyast.Name)}
    attrs = {n.func.attr for n in pyast.walk(tree) if isinstance(n, pyast.Call) and isinstance(n.func, pyast.Attribute)}
    assert not ({"eval", "exec", "compile"} & calls) and not ({"from_string", "eval"} & attrs)
