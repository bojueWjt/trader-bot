# 已退役：断言的是 G0 R-10 裁定 §2.2/§2.3 移除的「通用运行状态反射」。
# 保留为历史记录（A43：裁决只关分歧不关发现），不参与测试套。

def test_R7H_digest_covers_defaults_closures_and_constants(inject, restore):
    """R7-H：函数体字节码完全不变也能改变实际生成的数据——默认值/闭包/模块常量都在定义时绑定。"""
    d0 = NM.executing_code_digest()
    disk0 = NM.research_code_digest()
    if inject == "defaults":
        old = NM._garch_path.__defaults__
        NM._garch_path.__defaults__ = (.5, .10, .85)
        try:
            assert NM.executing_code_digest() != d0
            assert NM.research_code_digest() == disk0          # 磁盘视图一字未变
        finally:
            NM._garch_path.__defaults__ = old
    elif inject == "kwdefaults":
        kw = NM.run_mc.__kwdefaults__
        if not kw:
            pytest.skip("run_mc 无关键字默认值")
        old = dict(kw); kw["label"] = "R7-resident-default"
        try:
            assert NM.executing_code_digest() != d0
        finally:
            kw.clear(); kw.update(old)
    else:
        old = NM.PLANTED
        NM.PLANTED = "R7-resident-constant"
        try:
            assert NM.executing_code_digest() != d0
        finally:
            NM.PLANTED = old
    assert NM.executing_code_digest() == d0                     # 还原后回到原值

def test_R7H_real_workers_with_default_injection_are_refused():
    """两个真实 spawn worker 只改默认参数：磁盘摘要全等、零异常，但生成的数据已不同——必须拒绝发布。"""
    frozen, frozen_exec = NM.research_code_digest(), NM.executing_code_digest()
    kw = dict(mechanism="common_shock", kind="null", n_rep=1, seed0=1,
              world_cfg_data=NM._config_to_data(NM.WorldConfig(n_clusters=150)), pipe_cfg_data=NM._config_to_data(PipelineConfig(B=100)))
    with ProcessPoolExecutor(max_workers=2, initializer=_inject_defaults) as ex:
        bad = [f.result() for f in [ex.submit(NM._run_mc_job, **kw) for _ in range(2)]]
    for p in bad:
        assert p["digest_start"] == frozen and p["digest_end"] == frozen     # 磁盘视图完全一致
        with pytest.raises(SystemExit, match="不是被冻结的那一版"):
            NM.check_worker_receipt(p, frozen, frozen_exec)
    # 注入确实改变了生成的数据，不只是改变了摘要
    # 对照用直接调用 run_mc：它收的是活配置对象，kw 现在是纯数据，要转回来
    baseline = NM.run_mc(mechanism=kw["mechanism"], kind=kw["kind"], n_rep=kw["n_rep"], seed0=kw["seed0"],
                         world_cfg=NM.WorldConfig(**kw["world_cfg_data"]),
                         pipe_cfg=PipelineConfig(**{**kw["pipe_cfg_data"],
                                                    "block_len_sensitivity": tuple(kw["pipe_cfg_data"]["block_len_sensitivity"])}))
    assert bad[0]["result"].diagnostics["base_R_sd"] != pytest.approx(
        baseline.diagnostics["base_R_sd"], rel=1e-6)

def test_R7H_module_constant_injection_in_real_workers_is_refused():
    frozen, frozen_exec = NM.research_code_digest(), NM.executing_code_digest()
    kw = dict(mechanism="common_shock", kind="null", n_rep=1, seed0=1,
              world_cfg_data=NM._config_to_data(NM.WorldConfig(n_clusters=150)), pipe_cfg_data=NM._config_to_data(PipelineConfig(B=100)))
    with ProcessPoolExecutor(max_workers=2, initializer=_inject_module_constant) as ex:
        bad = [f.result() for f in [ex.submit(NM._run_mc_job, **kw) for _ in range(2)]]
    for p in bad:
        assert p["digest_start"] == frozen
        with pytest.raises(SystemExit, match="不是被冻结的那一版"):
            NM.check_worker_receipt(p, frozen, frozen_exec)

def test_R7H_digest_is_not_hostage_to_caller_io_mocking():
    """摘要不得取决于调用方是否 mock 了 open：判读脚本与测试夹具都会 mock 它来喂假输入。

    实测反例：polars_ta 后端在 mock 生效期间**首次**导入会拿不到 polars_ta.__version__，
    于是同一份源码在两种调用方式下算出两个摘要——那是环境意外，不是执行修订不同。
    """
    import subprocess
    import sys

    base = ("import io, sys\n"
            "from unittest.mock import patch\n"
            "from quant_lab.research.nullmodel import verify_report_text\n"     # 只加载一部分模块
            "from quant_lab.research.paths import executing_code_digest\n"
            "%s\n"
            "print(d)\n")
    patched = base % ('with patch("builtins.open", return_value=io.StringIO("x")):\n'
                      '    d = executing_code_digest()')
    clean = base % "d = executing_code_digest()"
    out = [subprocess.run([sys.executable, "-c", c], capture_output=True, text=True) for c in (patched, clean)]
    for r in out:
        assert r.returncode == 0, r.stderr
    assert out[0].stdout.strip() == out[1].stdout.strip() != ""

def test_SELF_frozen_config_cross_checked_against_its_other_encodings():
    """结构化冻结配置本身是自报的：只改 meta 一处 + 逐结果三处就能跳过 auto 计数门。
    报告里另有两处独立编码同一件事（PipelineConfig repr 与 §1 的 L），要求三者一致把成本抬到六处。
    **这不消除溯源边界**：三处全改的协调伪造仍会通过，那属 A39。"""
    text, base, start, end = _restamped_report()

    def forge(*, repr_too=False, meta_L_too=False):
        q = copy.deepcopy(base)
        q["meta"]["pipeline_block_len_days"] = 3
        for r in q["results"]:
            d = r["diagnostics"]
            d["block_len_mode"] = "fixed"; d["block_len_fixed"] = 3; d["chosen_block_len"] = {}
        if repr_too:
            q["meta"]["pipeline"] = q["meta"]["pipeline"].replace("block_len_days=None", "block_len_days=3")
        if meta_L_too:
            q["meta"]["L"] = 3
        return q

    # 三道独立不变式都能拦住这一族构造：meta.pipeline 交叉核对、meta.L 交叉核对、
    # 以及"命令是本 CLI 就不可能是 fixed"。哪一道先触发是实现顺序，断言不把顺序写死。
    _any = "meta.pipeline|meta.L|无固定 L 选项|不同源"
    with pytest.raises(ValueError, match=_any):
        verify_report_text(_render(text, start, end, forge()))
    with pytest.raises(ValueError, match=_any):
        verify_report_text(_render(text, start, end, forge(repr_too=True)))
    # 九审补第七处：本模块 CLI 无条件构造 block_len_days=None，没有任何选项能设固定 L，
    # 因此"命令是本 CLI"与"冻结为 fixed"互相矛盾——三处全改的协调伪造现在也被拒。
    # 这没有推翻 A39：溯源边界仍在（见 test_R9_* 与报告 §4 的限制声明），只是这一条具体构造被堵上了。
    with pytest.raises(ValueError, match="无固定 L 选项|不同源"):
        verify_report_text(_render(text, start, end, forge(repr_too=True, meta_L_too=True)))

def test_SELF_unknown_objects_are_distinguishable_by_value():
    """未知对象只记类型名时，同类型不同取值不可分——那等于把两种行为当成同一个。"""
    import functools
    import re as _re
    import polars as pl
    from quant_lab.research.paths import _const_key

    assert _const_key(pl.Datetime("us", "UTC")) != _const_key(pl.Datetime("ms", "UTC"))
    assert _const_key(_re.compile(r"^a$")) != _const_key(_re.compile(r"^b$"))
    assert _const_key(_re.compile(r"^a$")) != _const_key(_re.compile(r"^a$", _re.I))

    def add(x, y):
        return x + y
    assert _const_key(functools.partial(add, 1)) != _const_key(functools.partial(add, 2))
    assert _const_key(functools.partial(add, y=1)) != _const_key(functools.partial(add, y=2))

def test_SELF_nested_foreign_globals_are_reached():
    """外来 globals 里的函数如果自己也绑着另一本字典，改动藏在第二层——一层指纹看不见。"""
    import types
    d0 = NM.executing_code_digest()
    orig_sw, orig_gp = NM.synth_world, NM._garch_path
    try:
        g2 = dict(NM.__dict__)
        g1 = dict(NM.__dict__)
        g1["_garch_path"] = types.FunctionType(orig_gp.__code__, g2, orig_gp.__name__,
                                               orig_gp.__defaults__, orig_gp.__closure__)
        NM.synth_world = types.FunctionType(orig_sw.__code__, g1, orig_sw.__name__,
                                            orig_sw.__defaults__, orig_sw.__closure__)
        d1 = NM.executing_code_digest()
        assert d1 != d0                                  # 第一层换绑可见
        g2["INSTRUMENTS"] = ("X", "Y", "Z")              # 只动第二层
        assert NM.executing_code_digest() != d1          # 第二层也必须可见
    finally:
        NM.synth_world, NM._garch_path = orig_sw, orig_gp
    assert NM.executing_code_digest() == d0              # 环检测没有让摘要漂移


def test_R8H_rebound_globals_are_detected():
    """R8-H：同 code/defaults/closure，只把函数绑到另一份 globals——模块重载后旧函数引用就是这个形状。"""
    import types
    d0 = NM.executing_code_digest()
    orig = NM.synth_world
    g = dict(NM.__dict__)
    old = NM._garch_path
    g["_garch_path"] = types.FunctionType(old.__code__, dict(old.__globals__), old.__name__,
                                          (.5, .10, .85), old.__closure__)
    try:
        NM.synth_world = types.FunctionType(orig.__code__, g, orig.__name__, orig.__defaults__, orig.__closure__)
        assert NM.synth_world.__code__ is orig.__code__                 # code 完全相同
        assert NM.synth_world.__defaults__ == orig.__defaults__
        assert NM.executing_code_digest() != d0                         # 绑定不同即被检出
    finally:
        NM.synth_world = orig
    assert NM.executing_code_digest() == d0

def test_R8H_registry_values_are_encoded_not_just_typed():
    """R8-H 附带：注册表里的 OpSpec / 类配置 / 日期要按值编码，只记类型名等于没比。"""
    from quant_lab.research.paths import _const_key
    import dataclasses, datetime as dt
    from decimal import Decimal

    @dataclasses.dataclass
    class Cfg:
        a: int
        b: str

    assert _const_key(Cfg(1, "x")) != _const_key(Cfg(2, "x"))
    assert _const_key(dt.datetime(2024, 1, 1)) != _const_key(dt.datetime(2024, 1, 2))
    assert _const_key(Decimal("1.10")) != _const_key(Decimal("1.20"))
    import numpy as np
    assert _const_key(np.array([1.0, 2.0])) != _const_key(np.array([1.0, 3.0]))
    from quant_lab.research.ops import REGISTRY                          # 真注册表按值编码后彼此可分
    keys = sorted(REGISTRY)
    assert _const_key(REGISTRY[keys[0]]) != _const_key(REGISTRY[keys[1]])

def test_R8M_bound_is_numerically_stable_near_the_edge():
    """R8-M：|m|→1 时用离差求和，避免 Σx²−n·m² 的相消。对照八审给的高精度真值。"""
    assert NM.max_realizable_sd(0.9999999999, 1000) == pytest.approx(3.162277921816406e-09, rel=1e-5)
    assert NM.max_realizable_sd(-0.9999999999, 1000) == pytest.approx(3.162277921816406e-09, rel=1e-5)  # 正负对称
    assert NM.max_realizable_sd(0.99999999, 3) == pytest.approx(1.7320508162720156e-08, rel=1e-5)
    assert NM.max_realizable_sd(0.0, 1000) == pytest.approx(1.0005003753127737, abs=1e-12)   # 不回退
    assert NM.max_realizable_sd(-0.9, 2) == pytest.approx(math.sqrt(2) * 0.1, rel=1e-12)

@pytest.mark.parametrize("damage", ["fixed_lie", "fixed_value_mismatch", "meta_field_missing", "auto_lie"])
def test_R7L_declared_mode_must_match_frozen_config(damage):
    """R7-L：自报 fixed 就能跳过 auto 的计数门——模式必须与 meta 里结构化的冻结配置一致。"""
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base)
    d = q["results"][0]["diagnostics"]
    if damage == "fixed_lie":
        d["block_len_mode"] = "fixed"; d["block_len_fixed"] = 3; d["chosen_block_len"] = {}
    elif damage == "fixed_value_mismatch":
        d["block_len_fixed"] = 3
    elif damage == "meta_field_missing":
        q["meta"].pop("pipeline_block_len_days")
    elif damage == "auto_lie":
        q["meta"]["pipeline_block_len_days"] = 3        # 冻结为 fixed，却逐条自报 auto
    with pytest.raises(ValueError):
        verify_report_text(_render(text, start, end, q))
