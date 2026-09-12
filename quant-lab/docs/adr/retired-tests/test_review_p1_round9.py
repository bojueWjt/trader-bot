# 已退役：断言的是 G0 R-10 裁定 §2.2/§2.3 移除的「通用运行状态反射」。
# 保留为历史记录（A43：裁决只关分歧不关发现），不参与测试套。

def test_R9H_deep_configs_are_distinguished_by_value():
    """截断不得等于相等：审查者用 11 层配置装 omega，两份取值不同却编码成同一串，
    两个异参 worker 因此拿到相同执行身份并被发布。"""
    from quant_lab.research.paths import _const_key

    def nest(levels, leaf):
        root = cur = []
        for _ in range(levels):
            nxt = []; cur.append(nxt); cur = nxt
        cur.append(leaf)
        return root

    assert _const_key(nest(10, 0.05)) != _const_key(nest(10, 0.5))       # 审查者用的深度
    assert _const_key(nest(20, 0.05)) != _const_key(nest(20, 0.5))

def test_R9H_beyond_budget_is_a_named_refusal_not_an_equality():
    """超出深度预算必须**具名拒绝**并给出引用路径，不能退成统一截断串。"""
    import pytest as _pytest
    from quant_lab.research.paths import CONST_DEPTH_BUDGET, UncodableExecutionState, _const_key

    def nest(levels, leaf):
        root = cur = []
        for _ in range(levels):
            nxt = []; cur.append(nxt); cur = nxt
        cur.append(leaf)
        return root

    for leaf in (0.05, 0.5):
        with _pytest.raises(UncodableExecutionState, match="超出深度预算"):
            _const_key(nest(CONST_DEPTH_BUDGET + 2, leaf))

def test_R9H_unstably_encodable_object_is_refused_by_name():
    """既无结构化入口、repr 又带地址的对象：具名拒绝，不当成"类型相同即相等"。"""
    import pytest as _pytest
    from quant_lab.research.paths import UncodableExecutionState, _const_key

    class Opaque:
        __slots__ = ()                      # 无 __dict__，repr 落到默认带地址形式

    with _pytest.raises(UncodableExecutionState, match="无法稳定按值编码"):
        _const_key(Opaque())

def test_R9H_encoding_is_stable_across_hash_seeds():
    """含 set 的 SimpleNamespace 与 MappingProxyType 在不同 PYTHONHASHSEED 下必须编码相同。"""
    import os
    import subprocess
    import sys

    code = ("from types import MappingProxyType, SimpleNamespace\n"
            "from quant_lab.research.paths import _const_key\n"
            "ns = SimpleNamespace(a={'x','y','z','w'}, b=MappingProxyType({'k':1,'j':2}))\n"
            "print(_const_key(ns))\n")
    outs = []
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout.strip())
    assert len(set(outs)) == 1, outs

def test_R9H_long_repr_no_longer_relies_on_a_repr_fallback():
    """九审时这里断言「超长 repr 取内容摘要」。十一审 H3 之后**通用 repr 兜底整个移除**：
    「repr 无地址」既不证明稳定（slice(set) 三解释器三 repr），也不证明无损（dtype 的 metadata 不进 repr）。
    现在外部类型没有明确适配器就具名拒绝——比原来的摘要更严，原意（不得塌成类型名）仍然成立。"""
    import pytest as _pytest
    from quant_lab.research.paths import UncodableExecutionState, _const_key

    class Long:
        def __init__(self, n): self.payload = "x" * 600 + str(n)
        def __repr__(self): return "Long(" + self.payload + ")"

    Long.__module__ = "some_third_party"
    for n in (1, 2):
        with _pytest.raises(UncodableExecutionState, match="缺少明确适配器"):
            _const_key(Long(n))

def test_R9H_descriptors_and_callables_encode_by_identity():
    from quant_lab.research.paths import _const_key

    class A:
        @property
        def v(self): return 1

        @staticmethod
        def s(): return 1

    class B:
        @property
        def v(self): return 2

        @staticmethod
        def s(): return 2

    assert _const_key(vars(A)["v"]) != _const_key(vars(B)["v"])
    assert _const_key(vars(A)["s"]) != _const_key(vars(B)["s"])

def test_R9M_tolerance_cannot_flip_a_structural_decision():
    """接收容差与结构判定分开：sd 只抬高 5e-7 相对量就能把带撑宽约 3.8e-8，
    足以让临界判定由失败变通过——判定必须用钳到精确上界的 sd。"""
    from quant_lab.research.nullmodel import aggregate_pair_ok, max_realizable_sd

    cap = max_realizable_sd(0.0, 1000)
    text, base, start, end = _restamped_report()

    def render(sd):
        q = copy.deepcopy(base)
        g = q["results"][0]["diagnostics"]["grid_dependence"]
        g["fitted_cross"][0] = 0.18983163711681963
        g["null_mean_cross"][0] = 0.0
        g["null_sd_cross"][0] = sd
        g["n_cross"][0] = 1000
        g["band"] = [aggregate_pair_ok(f, m, s, k)[2]
                     for f, m, s, k in zip(g["fitted_cross"], g["null_mean_cross"], g["null_sd_cross"], g["n_cross"])]
        return _render(text, start, end, q)

    for sd in (cap, cap * (1 + 5e-7), cap * (1 + 2e-6)):
        with pytest.raises(ValueError):
            verify_report_text(render(sd))

def test_R9H_no_collisions_across_the_new_encoding_branches():
    """自审清单：新加的每条编码分支都要证明它**能区分**，不只证明它不崩。

    本场两次自伤都出在这里：带 __dict__ 的分支排在描述符之前，把所有函数塌成同一串；
    只记类名让同名不同配置的类碰撞。两条都是"截断/回退当成相等"的同一形状。
    """
    from quant_lab.research.paths import _const_key

    def f1(a=1): return a
    def f2(a=2): return a
    def g1(a=1): return a + 1

    pairs = {
        "同名不同默认值": (f1, f2),
        "同默认值不同函数体": (f1, g1),
        "同名不同属性的类": (type("A", (), {"x": 1}), type("A", (), {"x": 2})),
        "不同 bytes": (b"ab", b"ac"),
        "不同嵌套 dict": ({"a": {"b": [1, 2]}}, {"a": {"b": [1, 3]}}),
        "不同 set": ({1, 2, 3}, {1, 2, 4}),
        "空容器 vs None": ([], None),
        "0 vs False": (0, False),
        "0 vs 0.0": (0, 0.0),
        "字符串 vs 数字": ("1", 1),
    }
    for name, (a, b) in pairs.items():
        assert _const_key(a) != _const_key(b), name

    d1 = {}; d1["self"] = d1                      # 自环：回边序号而不是整体拒收
    d2 = {"pad": 1}; d2["self"] = d2
    assert "backref" in _const_key(d1)
    assert _const_key(d1) != _const_key(d2)

def test_R9H_digest_is_stable_across_processes():
    import subprocess
    import sys
    code = ("import quant_lab.research.nullmodel\n"
            "from quant_lab.research.paths import executing_code_digest\n"
            "print(executing_code_digest())\n")
    outs = [subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
            for _ in range(3)]
    assert len(set(outs)) == 1 and outs[0], outs
