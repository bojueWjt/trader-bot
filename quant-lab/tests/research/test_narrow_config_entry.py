"""窄配置入口（顾问建议）：正式 MC 不再把父进程的活对象传给 worker。

顾问诊断：`_const_key` 同时承担「值怎么编码 / 哪些状态影响行为 / 哪些差异不影响行为」三件事，
在「任意外部 Python callable」支持域下没有收敛点。窄入口从另一头解决——让活对象根本进不到
验收路径，而不是靠反射去证明它们已被完整编码。
"""
from __future__ import annotations

import dataclasses
import inspect
import math

import pytest

from quant_lab.research import nullmodel as NM
from quant_lab.research.api import PipelineConfig
from quant_lab.research.nullmodel import (UnsupportedConfigValue, WorldConfig, _config_to_data,
                                          as_pure_data)


@pytest.mark.parametrize("value,why", [
    (lambda: 1, "函数"),
    (object(), "普通对象"),
    (float("nan"), "非有限浮点"),
    (float("inf"), "无穷"),
    ({1: "x"}, "非字符串键"),
    (WorldConfig(), "活的配置对象"),
])
def test_live_objects_are_refused_at_the_entry(value, why):
    with pytest.raises(UnsupportedConfigValue):
        as_pure_data({"x": value})


def test_pure_data_round_trips():
    data = {"a": 1, "b": 2.5, "c": "s", "d": None, "e": True, "f": [1, [2, {"g": 3}]]}
    assert as_pure_data(data) == data


def test_nesting_budget_is_a_named_refusal():
    deep = v = {}
    for _ in range(NM._PURE_MAX_DEPTH + 2):
        nxt = {}; v["k"] = nxt; v = nxt
    with pytest.raises(UnsupportedConfigValue, match="嵌套超过"):
        as_pure_data(deep)


def test_int_subclasses_are_not_silently_accepted():
    """精确类型：IntEnum 之类有自己的取值语义，不能当成裸 int 混进配置。"""
    import enum

    class E(enum.IntEnum):
        A = 1

    with pytest.raises(UnsupportedConfigValue):
        as_pure_data({"x": E.A})


@pytest.mark.parametrize("cfg", [WorldConfig(), WorldConfig(n_clusters=150, mechanism="circular_shift"),
                                 PipelineConfig(), PipelineConfig(B=100, block_len_days=3)])
def test_real_configs_are_pure_data_and_reconstruct_exactly(cfg):
    """两个正式配置本来就是标量加简单容器：转成数据再重建必须**逐字段相等**。"""
    data = _config_to_data(cfg)
    kw = dict(data)
    if isinstance(kw.get("block_len_sensitivity"), list):
        kw["block_len_sensitivity"] = tuple(kw["block_len_sensitivity"])
    rebuilt = type(cfg)(**kw)
    for f in dataclasses.fields(cfg):
        assert getattr(rebuilt, f.name) == getattr(cfg, f.name), f.name


def test_job_entry_takes_data_not_live_objects():
    """job 入口的签名本身就是边界：只收 *_data，不再收活配置对象。"""
    sig = inspect.signature(NM._run_mc_job)
    assert "world_cfg_data" in sig.parameters and "pipe_cfg_data" in sig.parameters
    assert "world_cfg" not in sig.parameters and "pipe_cfg" not in sig.parameters
    with pytest.raises(TypeError):
        NM._run_mc_job(world_cfg=WorldConfig(), pipe_cfg=PipelineConfig())


def test_worker_rebuilds_config_and_results_are_unchanged():
    """窄入口不得改变数值：同 seed 同配置，经数据过界重建后结果逐位相同。"""
    wc, pc = WorldConfig(n_clusters=150), PipelineConfig(B=100)
    direct = NM.run_mc("common_shock", kind="null", n_rep=1, seed0=1, world_cfg=wc, pipe_cfg=pc)
    via_data = NM._run_mc_job(world_cfg_data=_config_to_data(wc), pipe_cfg_data=_config_to_data(pc),
                              mechanism="common_shock", kind="null", n_rep=1, seed0=1)["result"]
    assert via_data.seeds == direct.seeds
    assert via_data.diagnostics["base_R_sd"] == direct.diagnostics["base_R_sd"]
    assert via_data.n_done == direct.n_done and via_data.n_positive == direct.n_positive


def test_pool_uses_an_explicit_spawn_context():
    """不依赖平台默认启动方式：显式 spawn，保证 worker 是全新解释器、不继承父进程已加载模块。"""
    src = inspect.getsource(NM._main)
    assert 'mp_context=get_context("spawn")' in src
