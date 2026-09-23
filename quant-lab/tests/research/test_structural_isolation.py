"""G0 R-10 裁定 §4.1 要求的三条**类级**判别性证明，每条各带一个「把门去掉就变红」的突变。

裁定同时规定不接受「这几个已知位置没问题」式的判据（A44），故三条都按类写：
未申报状态不得跨界、worker 制品身份必须等于父进程冻结身份、配置未申报字段必须拒绝而非忽略。
"""
from __future__ import annotations

import multiprocessing
import sys
import types
from concurrent.futures import ProcessPoolExecutor

import pytest

from quant_lab.research import nullmodel as NM
from quant_lab.research.api import PipelineConfig
from quant_lab.research.nullmodel import UnsupportedConfigValue, WorldConfig, _config_to_data

_KW = dict(mechanism="common_shock", kind="null", n_rep=1, seed0=1)


def _data_kwargs(**override):
    kw = dict(_KW, world_cfg_data=_config_to_data(WorldConfig(n_clusters=150)),
              pipe_cfg_data=_config_to_data(PipelineConfig(B=100)))
    kw.update(override)
    return kw


def _mutate_module_state():
    """在**父进程**里改一个未申报的模块级可变状态。spawn 出的 worker 不应看到它。"""
    NM.PLANTED = "mutated-in-parent"


# ---------------------------------------------------------------- 证明 1
def test_proof1_undeclared_state_does_not_cross_the_process_boundary():
    """类级命题：父进程里任何**未申报**的模块级状态改动，都不得影响 worker 的计算。

    这是结构性关闭，不是检测：worker 由全新解释器重新导入，父进程的内存改动根本不过界。
    """
    saved = NM.PLANTED
    ctx = multiprocessing.get_context("spawn")
    try:
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
            before = ex.submit(NM._run_mc_job, **_data_kwargs()).result()
        _mutate_module_state()
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
            after = ex.submit(NM._run_mc_job, **_data_kwargs()).result()
    finally:
        NM.PLANTED = saved
    assert after["result"].diagnostics["base_R_sd"] == before["result"].diagnostics["base_R_sd"]
    assert after["artifact_start"] == before["artifact_start"]


def test_proof1_mutation_fork_would_let_it_cross():
    """突变自证：把启动方式换成 fork，父进程的未申报改动**就会**跨界——证明证明 1 有内容。

    macOS 上 fork 不安全，这里只验证语义差别：fork 的 worker 继承父进程模块对象。
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("本平台无 fork")
    saved = NM.PLANTED
    ctx = multiprocessing.get_context("fork")
    try:
        NM.PLANTED = "mutated-in-parent"
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
            seen = ex.submit(_read_planted).result()
    finally:
        NM.PLANTED = saved
    assert seen == "mutated-in-parent", "fork 下未申报状态确实跨界——这正是 spawn 要消除的"


def _read_planted():
    from quant_lab.research import nullmodel as _n
    return _n.PLANTED


# ---------------------------------------------------------------- 证明 2
def test_proof2_worker_artifact_identity_must_equal_the_frozen_one():
    """类级命题：任何一份回执的制品身份与父进程冻结身份不等，就不得发布。"""
    frozen = NM.artifact_identity_digest()
    R = NM.MCResult("m", "null", 1, 1, 0, 0, [1], None, None, None, None, {}, 0.0, {}, "not_run", 0, "")
    ok = {"result": R, "digest_start": NM.research_code_digest(), "digest_end": NM.research_code_digest(),
          "artifact_start": frozen, "artifact_end": frozen}
    assert NM.check_worker_receipt(ok, NM.research_code_digest(), frozen) is R
    for key in ("artifact_start", "artifact_end"):
        bad = dict(ok, **{key: "0" * 64})
        with pytest.raises(SystemExit, match="不是被冻结的那一份制品"):
            NM.check_worker_receipt(bad, NM.research_code_digest(), frozen)
    with pytest.raises(SystemExit):                       # 缺回执同样不得发布
        NM.check_worker_receipt({"result": R}, NM.research_code_digest(), frozen)


def test_proof2_mutation_without_the_check_it_would_publish():
    """突变自证：不传 frozen_artifact（等于把这道门去掉），同一份坏回执就会被放行。"""
    frozen = NM.artifact_identity_digest()
    R = NM.MCResult("m", "null", 1, 1, 0, 0, [1], None, None, None, None, {}, 0.0, {}, "not_run", 0, "")
    bad = {"result": R, "digest_start": NM.research_code_digest(), "digest_end": NM.research_code_digest(),
           "artifact_start": "0" * 64, "artifact_end": frozen}
    assert NM.check_worker_receipt(bad, NM.research_code_digest(), None) is R


# ---------------------------------------------------------------- 证明 3
def test_proof3_undeclared_config_fields_are_refused_not_ignored():
    """类级命题：schema 外字段必须拒绝；缺字段也不得用默认值悄悄补齐。"""
    data = _config_to_data(WorldConfig(n_clusters=150))
    with pytest.raises(UnsupportedConfigValue, match="未申报字段"):
        NM._checked_fields(dict(data, bogus=1), WorldConfig)
    with pytest.raises(UnsupportedConfigValue, match="缺字段"):
        NM._checked_fields({k: v for k, v in data.items() if k != "seed"}, WorldConfig)


def test_proof3_mutation_plain_construction_would_ignore_them():
    """突变自证：绕开 _checked_fields 直接构造，缺字段会被默认值悄悄补上——门确实在起作用。"""
    data = _config_to_data(WorldConfig(n_clusters=150))
    partial = {k: v for k, v in data.items() if k != "seed"}
    silently_filled = WorldConfig(**partial)
    assert silently_filled.seed == WorldConfig().seed        # 默认值补齐，没有任何报错


def test_proof3_reaches_the_real_job_entry():
    """不是只测 helper：真实 job 入口拿到 schema 外字段也必须具名拒绝。"""
    with pytest.raises(UnsupportedConfigValue, match="未申报字段"):
        NM._run_mc_job(**_data_kwargs(world_cfg_data=dict(_config_to_data(WorldConfig()), bogus=1)))


# ================================================================ R-11：依赖内容哈希（裁定 §10）
def test_r11_manifest_carries_content_hashes_not_just_versions():
    """版本串相同而包内容不同，可经换安装源 / 重打包 wheel / 直改 site-packages / 跨平台轮子发生，
    **四条都不需要向 worker 注入代码** → 事故类，必须挡住。"""
    from quant_lab.research.paths import DECLARED_DEPENDENCIES, dependency_manifest

    m = dependency_manifest()
    for name in DECLARED_DEPENDENCIES:
        assert name in m and f"{name}.content" in m, name
        h = m[f"{name}.content"]
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h), (name, h)
    assert m["python"]                                   # 解释器版本也在


def test_r11_content_hash_comes_from_the_installed_RECORD():
    """内容身份取该分发 RECORD 的摘要——装包时已对每个文件算好 sha256，不必自己遍历文件树。"""
    import hashlib
    import importlib.metadata as md

    from quant_lab.research.paths import DECLARED_DEPENDENCIES, dependency_manifest

    m = dependency_manifest()
    for name in DECLARED_DEPENDENCIES:
        rec = md.distribution(name).read_text("RECORD")
        assert rec is not None, name
        assert m[f"{name}.content"] == hashlib.sha256(rec.encode()).hexdigest(), name
        assert sum(1 for l in rec.splitlines() if ",sha256=" in l) > 0, name


def test_r11_unreadable_record_is_a_named_refusal():
    """RECORD 不可读的分发具名拒绝，与「版本不可得即拒绝」同构——不得静默跳过。"""
    from unittest.mock import patch

    from quant_lab.research import paths as P

    class _NoRecord:
        version = "9.9.9"
        def read_text(self, name): return None

    with patch.object(P, "_distribution_for", lambda name: _NoRecord()):
        with pytest.raises(P.UnsupportedArtifactState, match="RECORD 不可读"):
            P.dependency_manifest()


def test_r11_content_change_changes_the_artifact_identity():
    """内容哈希确实并入制品身份：改一个依赖的内容摘要，制品身份必须随之改变。"""
    from unittest.mock import patch

    from quant_lab.research import paths as P

    before = P.artifact_identity_digest()
    real = P.dependency_manifest()
    tampered = dict(real, **{"polars.content": "f" * 64})
    with patch.object(P, "dependency_manifest", lambda: tampered):
        assert P.artifact_identity_digest() != before
    assert P.artifact_identity_digest() == before        # 还原后回到原值
