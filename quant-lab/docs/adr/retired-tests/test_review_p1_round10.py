"""十审 R10-H 回归：执行状态的值类型协议——每一类要么按值可分，要么双方具名拒绝。"""
from __future__ import annotations

import collections
import datetime as dt
import enum
import dataclasses
import functools
import operator
import pathlib
import re
import sys
import types
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace

import numpy as np
import pytest

from quant_lab.research import nullmodel as NM
from quant_lab.research.api import PipelineConfig
from quant_lab.research.paths import UncodableExecutionState, _const_key


def _key(x):
    try:
        return _const_key(x)
    except UncodableExecutionState:
        return "REFUSED"


class _Cfg:
    def __init__(self, omega): self.omega = omega
    def __call__(self): return self.omega


class _Slotted:
    __slots__ = ("omega",)
    def __init__(self, omega): self.omega = omega


class _Holder:
    def __init__(self, v): self.v = v
    def m(self): return self.v


class _E(enum.IntEnum):
    A = 1
    B = 2


def _outer(w):
    def inner(): return w
    return inner


@pytest.mark.parametrize("name,a,b", [
    ("可调用实例不同取值", _Cfg(.05), _Cfg(.5)),
    ("slots 对象不同取值", _Slotted(.05), _Slotted(.5)),
    ("绑定方法不同 self", _Holder(1).m, _Holder(2).m),
    ("外部函数不同闭包", _outer(.05), _outer(.5)),
    ("IntEnum 成员 vs 裸 int", _E.A, 1),
    ("IntEnum 不同成员", _E.A, _E.B),
    ("deque 不同内容", collections.deque([1, 2]), collections.deque([1, 3])),
    ("deque 不同 maxlen", collections.deque([1], 2), collections.deque([1], 3)),
    ("object 数组不同元素", np.array([{"a": 1}], dtype=object), np.array([{"a": 2}], dtype=object)),
    ("datetime 不同时区", dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
     dt.datetime(2024, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=8)))),
    ("datetime 不同 fold", dt.datetime(2024, 1, 1, fold=0), dt.datetime(2024, 1, 1, fold=1)),
    ("Decimal 同值不同精度", Decimal("1.0"), Decimal("1.00")),
    ("类方法体不同", type("C", (), {"f": lambda s: 1}), type("C", (), {"f": lambda s: 2})),
    ("类基类不同", type("C", (int,), {}), type("C", (str,), {})),
    ("mappingproxy 不同", MappingProxyType({"a": 1}), MappingProxyType({"a": 2})),
    ("partial 不同关键字", functools.partial(print, sep="-"), functools.partial(print, sep="+")),
    ("正则 flags 不同", re.compile("a"), re.compile("a", re.I)),
    ("SimpleNamespace 含 set", SimpleNamespace(s={1, 2}), SimpleNamespace(s={1, 3})),
])
def test_R10H_every_named_surface_is_distinguishable_or_refused(name, a, b):
    """十审点名的每一类：要么「不同键」，要么双方具名拒绝——不得一律相等。"""
    ka, kb = _key(a), _key(b)
    assert ka != kb or ka == kb == "REFUSED", name


def test_R10H_encoding_is_stable_across_hash_seeds_and_interpreters():
    """deque(set) 与 object 数组在三个 hash seed、各自的新解释器下必须稳定。"""
    import os
    import subprocess

    code = ("import collections\n"
            "import numpy as np\n"
            "from types import SimpleNamespace\n"
            "from quant_lab.research.paths import _const_key\n"
            "print(_const_key(SimpleNamespace(d=collections.deque([{3,1,2}], 4),\n"
            "                                a=np.array([{'k':{2,1}}], dtype=object))))\n")
    outs = []
    for seed in ("0", "1", "999"):
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONHASHSEED=seed))
        assert r.returncode == 0, r.stderr[-400:]
        outs.append(r.stdout.strip())
    assert len(set(outs)) == 1, outs


def _install_config(omega):
    mod = sys.modules.get("r10_config")
    if mod is None:
        mod = types.ModuleType("r10_config")
        exec("class Cfg:\n"
             "    def __init__(self, omega): self.omega = omega\n"
             "    def __call__(self): return self.omega\n", mod.__dict__)
        sys.modules["r10_config"] = mod
    NM.R10_CONFIG = mod.Cfg(omega)
    old = NM._garch_path
    NM._garch_path = types.FunctionType(old.__code__, old.__globals__, old.__name__,
                                        (NM.R10_CONFIG(), .10, .85), old.__closure__)


def _normal(): _install_config(.05)
def _variant(): _install_config(.5)


def test_R10H_callable_config_variant_workers_are_refused():
    """十审主反例：父与 worker 用同一个带 __call__ 的配置类，只有 omega 不同。
    正常组必须照常发布；异参组两份回执必须被拒且不生成新报告。"""
    saved_path, saved_cfg = NM._garch_path, getattr(NM, "R10_CONFIG", None)
    try:
        _install_config(.05)
        frozen, fexec = NM.research_code_digest(), NM.executing_code_digest()
        kw = dict(mechanism="common_shock", kind="null", n_rep=1, seed0=1,
                  world_cfg_data=NM._config_to_data(NM.WorldConfig(n_clusters=150)), pipe_cfg_data=NM._config_to_data(PipelineConfig(B=100)))

        with ProcessPoolExecutor(max_workers=2, initializer=_normal) as ex:
            good = [f.result() for f in [ex.submit(NM._run_mc_job, **kw) for _ in range(2)]]
        for p in good:
            assert NM.check_worker_receipt(p, frozen, fexec) is p["result"]

        with ProcessPoolExecutor(max_workers=2, initializer=_variant) as ex:
            bad = [f.result() for f in [ex.submit(NM._run_mc_job, **kw) for _ in range(2)]]
        for p in bad:
            assert p["digest_start"] == frozen                      # 磁盘源码一字未改
            assert p["result"].diagnostics["base_R_sd"] != good[0]["result"].diagnostics["base_R_sd"]
            with pytest.raises(SystemExit, match="不是被冻结的那一版"):
                NM.check_worker_receipt(p, frozen, fexec)
    finally:
        NM._garch_path = saved_path
        if saved_cfg is None:
            NM.__dict__.pop("R10_CONFIG", None)
        else:
            NM.R10_CONFIG = saved_cfg


# ================================================================ 十一审 R11-H1..H4
class _Priv:
    __slots__ = ("__omega",)
    def __init__(self, w): self.__omega = w          # 私有槽会被改写成 _Priv__omega
    def __call__(self): return self.__omega


class _SlotBase:
    __slots__ = ("v",)


class _SlotChild(_SlotBase):
    __slots__ = ("v",)                               # 与基类重名，各自独立存储
    def __init__(self, bv, cv):
        _SlotBase.v.__set__(self, bv)
        _SlotChild.v.__set__(self, cv)


class _CallBase:
    def __call__(self): return 1


class _CallInherit(_CallBase):
    pass


class _CallOverride(_CallBase):
    def __call__(self): return 2


class _ReprLiar(str):
    def __repr__(self): return "SAME"


@pytest.mark.parametrize("name,a,b", [
    ("私有槽不同取值", _Priv(.05), _Priv(.5)),
    ("重名槽不同取值", _SlotChild(1, 2), _SlotChild(2, 1)),
    ("继承 __call__ 不同实现", _CallInherit(), _CallOverride()),
    ("内置函数分派", sum, max),
    ("partial 包不同内置", functools.partial(sum, [1, 2]), functools.partial(max, [1, 2])),
    ("defaultdict 不同 factory", collections.defaultdict(list), collections.defaultdict(set)),
    ("str 子类覆写 repr", _ReprLiar("a"), _ReprLiar("b")),
    ("绑定 C 方法不同 self", [1].append, [2].append),
    ("slice 不同", slice(1, 2), slice(1, 3)),
    ("dtype metadata 不同", np.dtype("f8", metadata={"a": 1}), np.dtype("f8", metadata={"a": 2})),
    ("dtype 结构不同", np.dtype([("a", "f8")]), np.dtype([("b", "f8")])),
    ("同 offset 不同时区对象", dt.timezone(dt.timedelta(0)), dt.timezone(dt.timedelta(0), "X")),
])
def test_R11H_named_surfaces_are_distinguishable_or_refused(name, a, b):
    ka, kb = _key(a), _key(b)
    assert ka != kb or ka == kb == "REFUSED", name


def test_R11H4_class_exclusions_are_conditional_on_kind():
    """按名排除必须核种类：同名的 property 不能借排除名单藏起来；非规范 slotnames 要拒收。"""
    class B1:
        def f(self): return 1

    class B2:
        def f(self): return 2

    assert _const_key(type("C", (B1,), {})) != _const_key(type("C", (B2,), {}))   # 父类实现进比较

    class Fake1:
        __slotnames__ = property(lambda s: 1)

    with pytest.raises(UncodableExecutionState, match="不是 copyreg 的规范缓存"):
        _const_key(Fake1)


def test_R11H_no_generic_repr_fallback_remains():
    """通用 repr 兜底已移除：repr 无地址既不证明稳定也不证明无损。"""
    import inspect
    from quant_lab.research import paths

    src = inspect.getsource(paths._const_key)
    assert '" at 0x" not in r' not in src and "reprhash:" not in src


# ================================================================ 十二审 R12-H2 / H3 / 缓存误报
@dataclasses.dataclass(init=False)
class _DCExtra:
    label: str = "garch"

    def __init__(self, omega):
        self.omega = omega                      # 非字段的额外实例状态


class _DequeSub(collections.deque):
    def __init__(self, it, w):
        super().__init__(it)
        self.w = w


@pytest.mark.parametrize("name,a,b", [
    ("dataclass 非字段状态", _DCExtra(.05), _DCExtra(.5)),
    ("deque 子类附加状态", _DequeSub([1], .05), _DequeSub([1], .5)),
    ("同名不同偏移时区", dt.timezone(dt.timedelta(hours=1), "same"),
     dt.timezone(dt.timedelta(hours=9), "same")),
    ("嵌套该时区的 datetime",
     dt.datetime(2024, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=1), "same")),
     dt.datetime(2024, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=9), "same"))),
    ("object 数组 dtype metadata", np.array([1, 2], dtype=object),
     np.array([1, 2], dtype=np.dtype(object, metadata={"omega": .5}))),
    ("itemgetter 不同参数", operator.itemgetter(1), operator.itemgetter(2)),
    ("attrgetter 不同参数", operator.attrgetter("a"), operator.attrgetter("b")),
    ("method-wrapper 不同 self", (1).__add__, (2).__add__),
    ("路径不同", pathlib.Path("/a"), pathlib.Path("/b")),
])
def test_R12_named_surfaces_are_distinguishable_or_refused(name, a, b):
    ka, kb = _key(a), _key(b)
    assert ka != kb or ka == kb == "REFUSED", name


def test_R12_lazy_caches_do_not_change_the_executing_digest():
    """正常使用不得改变执行身份：`hash()` 会在 pathlib.Path 上留下 _hash 缓存，
    若把外部对象的实例状态直接当取值编码，仅仅 hash 过一次就会拒收未变的报告。
    这类误报比漏检更危险——最终结局是把门关掉。"""
    from quant_lab.research import paths as P

    before = P.executing_code_digest()
    hash(P.REPO_ROOT)
    str(P.REPO_ROOT).upper()
    assert P.executing_code_digest() == before


def test_R12_unsupported_external_types_are_refused_by_name():
    """外部类型没有明确适配器时具名拒绝，而不是猜它的实例状态是不是取值。"""
    class _Outside:
        def __init__(self): self.v = 1

    _Outside.__module__ = "some_third_party"
    with pytest.raises(UncodableExecutionState, match="缺少明确适配器"):
        _const_key(_Outside())


def test_R12_unsupported_tzinfo_is_refused():
    class _Weird(dt.tzinfo):
        def utcoffset(self, _): return dt.timedelta(0)
        def tzname(self, _): return "W"
        def dst(self, _): return None

    with pytest.raises(UncodableExecutionState, match="tzinfo 不在支持域内"):
        _const_key(_Weird())


# ================================================================ 十三审 R13-H1..H5
@pytest.mark.parametrize("name,a,b", [
    ("polars 类型本体", "pl.Int64", "pl.Float64"),
    ("List 嵌套", "pl.List(pl.Int64)", "pl.List(pl.Float64)"),
    ("Field 名字", "pl.Field('a', pl.Int64)", "pl.Field('b', pl.Int64)"),
    ("Struct 递归", "pl.Struct([pl.Field('a', pl.Int64)])", "pl.Struct([pl.Field('b', pl.Int64)])"),
    ("Array 长度", "pl.Array(pl.Int64, 2)", "pl.Array(pl.Int64, 3)"),
    ("Enum categories", "pl.Enum(['a'])", "pl.Enum(['b'])"),
    ("Datetime 单位", "pl.Datetime('us')", "pl.Datetime('ms')"),
])
def test_R13H1_polars_types_are_distinguishable(name, a, b):
    """类本体也是取值：pl.Int64 与 pl.Float64 都是 DataTypeClass 的实例，
    按 type(c) 的限定名编码会得到同一串。"""
    import polars as pl  # noqa: F401
    ka, kb = _key(eval(a)), _key(eval(b))
    assert ka != kb or ka == kb == "REFUSED", name


def test_R13H2_operator_uses_reconstructable_args_not_repr():
    """repr 既有损（外部键的 repr 可以相同）又不稳定（集合参数随 hash seed 变）。"""
    import os
    import subprocess

    class Key:
        def __init__(self, v): self.v = v
        def __repr__(self): return "Key()"
        def __hash__(self): return hash(self.v)
        def __eq__(self, o): return isinstance(o, Key) and self.v == o.v

    ka, kb = _key(operator.itemgetter(Key(1))), _key(operator.itemgetter(Key(2)))
    assert ka != kb or ka == kb == "REFUSED"          # 外部键不在支持域 → 双方具名拒绝

    code = ("import operator\nfrom quant_lab.research.paths import _const_key\n"
            "print(_const_key(operator.methodcaller('intersection', {'alpha','beta','gamma'})))\n")
    outs = []
    for seed in ("0", "1", "999"):
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONHASHSEED=seed))
        assert r.returncode == 0, r.stderr[-300:]
        outs.append(r.stdout.strip())
    assert len(set(outs)) == 1, outs                  # 集合参数三 hash seed 稳定


def test_R13H3_deque_subclass_maxlen_and_opaque_c_callables():
    import ctypes

    class _DQ(collections.deque):
        pass

    assert _key(_DQ([1], 2)) != _key(_DQ([1], 3))     # maxlen 改变后续 extend 的结果，是取值
    cb = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    assert _key(cb(lambda x: x + 1)) == "REFUSED"     # 行为整个在 C 层，空 Python 状态不是证明


def test_R13H3_python_visible_callables_are_still_encoded():
    """判据是「行为是否在 Python 里可见」，不是「是不是本包」——否则正常外部配置无法发布。"""
    mod = types.ModuleType("probe_cfg_test")
    exec("class Cfg:\n"
         "    def __init__(self, w): self.omega = w\n"
         "    def __call__(self): return self.omega\n", mod.__dict__)
    sys.modules["probe_cfg_test"] = mod
    try:
        assert _key(mod.Cfg(.05)) not in ("REFUSED",)
        assert _key(mod.Cfg(.05)) != _key(mod.Cfg(.5))
    finally:
        sys.modules.pop("probe_cfg_test", None)


def test_R13H4_normal_pickle_does_not_break_signability():
    """正常 pickle 会让 copyreg 在类上写 __slotnames__（私有槽会被改写），
    规范核对必须按完整规则复算并逐项相等，否则正常操作之后就签不出身份。"""
    import pickle

    from quant_lab.research.paths import _canonical_slotnames

    class Private:
        __slots__ = ("__x",)
        def __init__(self, v): self.__x = v

    import copyreg
    before = _const_key(Private)
    # pickle 一个**局部**类会因找不到限定名而失败，这里直接调用 pickle 内部所用的同一个 helper——
    # 它正是在类上写 __slotnames__ 缓存的那一步，被测行为完全相同。
    assert copyreg._slotnames(Private) == _canonical_slotnames(Private)
    assert "__slotnames__" in vars(Private)
    assert _const_key(Private) == before
    assert pickle.dumps(_DCExtra(.05))            # 模块级类的真实 pickle 路径同样不改变可签发性


def test_R13H5_module_globals_are_not_treated_as_sentinels():
    """「在某个模块里有名字」证明不了按值适配：任何普通有状态配置都能被模块变量引用。"""
    class Cfg:
        def __init__(self, w): self.omega = w

    Cfg.__module__ = "some_third_party"
    main = sys.modules["__main__"]
    saved = getattr(main, "CFG_PROBE", None)
    try:
        main.CFG_PROBE = Cfg(.05)
        assert _key(main.CFG_PROBE) == "REFUSED"      # 不再因为「有名字」就当哨兵
    finally:
        if saved is None:
            main.__dict__.pop("CFG_PROBE", None)
        else:
            main.CFG_PROBE = saved


def test_R13H5_whitelisted_sentinels_use_canonical_names():
    import dataclasses as _dc
    from quant_lab.research.paths import _const_key as ck

    assert ck(_dc.MISSING) == "sentinel:dataclasses.MISSING"
    _dc.ALIAS_FOR_MISSING = _dc.MISSING               # 加别名不得改变身份
    try:
        assert ck(_dc.MISSING) == "sentinel:dataclasses.MISSING"
    finally:
        del _dc.ALIAS_FOR_MISSING


def test_R13_normal_operations_do_not_change_the_digest():
    """hash / str / pickle 这些正常操作都不得改变执行身份。"""
    import pickle

    from quant_lab.research import nullmodel as _NM
    from quant_lab.research import paths as P

    before = P.executing_code_digest()
    hash(P.REPO_ROOT)
    str(P.REPO_ROOT).upper()
    pickle.dumps(_NM.WorldConfig(n_clusters=10))
    assert P.executing_code_digest() == before


# ================================================================ 十四审 R14-H1 / H2
def _make_cfg_module(name, src, w):
    mod = types.ModuleType(name)
    # 必须**先注册再 exec**：dataclasses 解析 ClassVar 时要按 cls.__module__ 回查 sys.modules，
    # 模块还没注册就会拿到 None 并炸在 None.__dict__ 上。
    sys.modules[name] = mod
    exec(src.format(w=w), mod.__dict__)
    return mod


_SRC_GLOBAL = "OMEGA = {w}\nclass Cfg:\n    def __call__(self): return OMEGA\n"
_SRC_CLASSATTR = "class Cfg:\n    omega = {w}\n    def __call__(self): return self.omega\n"


_SRC_DC_CLASSVAR = ("import dataclasses\nfrom typing import ClassVar\n"
                    "@dataclasses.dataclass\nclass Cfg:\n"
                    "    dummy: int = 0\n    omega: ClassVar[float] = {w}\n"
                    "    def __call__(self): return self.omega\n")
_SRC_SELF = ("class Cfg:\n    def __init__(self): self.omega = {w}\n"
             "    def __call__(self): return self.omega\n")
_SRC_CLOSURE = ("def mk(w):\n    def f(): return w\n    return f\n"
                "class Cfg:\n    def __init__(self): self.f = mk({w})\n"
                "    def __call__(self): return self.f()\n")
_SRC_NESTED = ("OMEGA = {w}\nclass Cfg:\n    def __call__(self):\n"
               "        def inner(): return OMEGA\n        return inner()\n")
_SRC_REFLECT = "OMEGA = {w}\nclass Cfg:\n    def __call__(self): return globals()['OMEGA']\n"


@pytest.mark.parametrize("src,label,must_sign", [
    (_SRC_SELF, "普通 self.omega", True),
    (_SRC_CLASSATTR, "普通类属性", True),
    (_SRC_DC_CLASSVAR, "dataclass ClassVar", True),
    (_SRC_GLOBAL, "直接 OMEGA", True),
    (_SRC_CLOSURE, "闭包", True),
    (_SRC_NESTED, "嵌套 code 读 OMEGA", True),
    (_SRC_REFLECT, "globals() 反射", False),      # 无法静态证明覆盖 → 双方具名拒绝
])
def test_R14H2_external_callable_behavioral_dependencies_are_encoded(src, label, must_sign):
    """十五审第 4 点：**两份配置必须放在同一个模块名下**，否则函数身份本身就带模块名，
    即使完全不编码 OMEGA 断言也会通过——那是伪对照，证明不了任何事。
    这里同名同限定名，只改唯一取值。"""
    name = "r15_probe_same_module"
    try:
        ka = _key(_make_cfg_module(name, src, .05).Cfg())
        kb = _key(_make_cfg_module(name, src, .5).Cfg())
        if must_sign:
            assert ka != "REFUSED" and kb != "REFUSED", f"{label} 同值应可签发"
            assert ka != kb, label
        else:
            assert ka == kb == "REFUSED", f"{label} 应双方具名拒绝"
    finally:
        sys.modules.pop(name, None)


def test_R14H2_same_module_same_value_is_stable():
    """同名同值必须稳定可签发——这是上一条「可分」断言的必要对照。"""
    name = "r15_probe_same_module_stable"
    try:
        ka = _key(_make_cfg_module(name, _SRC_GLOBAL, .05).Cfg())
        kb = _key(_make_cfg_module(name, _SRC_GLOBAL, .05).Cfg())
        assert ka == kb != "REFUSED"
    finally:
        sys.modules.pop(name, None)


def test_R15_unrelated_same_named_global_is_not_falsely_refused():
    """co_names 是名字集合不是「读了全局」的集合：`return self.omega` 里的 omega 是 LOAD_ATTR，
    模块里恰好有个无关的 omega = Lock() 不得导致误拒。"""
    name = "r15_probe_lock"
    src = ("import threading\nomega = threading.Lock()\nclass Cfg:\n"
           "    def __init__(self): self.omega = .05\n    def __call__(self): return self.omega\n")
    try:
        assert _key(_make_cfg_module(name, src, 0).Cfg()) != "REFUSED"
    finally:
        sys.modules.pop(name, None)


def test_R14H2_in_package_functions_stay_on_the_package_scan():
    """本包函数的模块全局已由整包扫描覆盖，不应再逐名展开一遍（否则重复且可能成环）。"""
    from quant_lab.research import nullmodel as _NM
    from quant_lab.research.paths import _referenced_globals_key

    assert _referenced_globals_key(_NM.synth_world) == "in-package"


def test_R14H1_polars_categorical_carries_its_categories():
    """Categories 的 name/namespace/physical 在本版是**方法**：直接 getattr 得到绑定方法，
    编的是方法身份而不是取值，三者会全部碰撞。"""
    import polars as pl

    assert _key(pl.Categorical(pl.Categories("x"))) != _key(pl.Categorical(pl.Categories("y")))
    assert (_key(pl.Categorical(pl.Categories("x", namespace="a")))
            != _key(pl.Categorical(pl.Categories("x", namespace="b"))))
    assert (_key(pl.Categorical(pl.Categories("x", physical=pl.UInt8)))
            != _key(pl.Categorical(pl.Categories("x", physical=pl.UInt32))))
