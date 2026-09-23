"""P1-OPEN-5 的判别性证明：陈旧 pyc 的结构性关闭、逐 job 绑定、结果摘要，每条各带一个「把门去掉就变红」的突变。

能力文档 §2.1 原先把「陈旧 __pycache__」记为已关闭，依据却只是磁盘源码摘要——摘要相等推不出执行字节码相同。
这里先证明威胁真实存在（突变：不设新前缀就会执行陈旧 pyc），再证明正式入口确实把 worker 放在新前缀下、
并逐 job 核对派发内容与结果。
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from quant_lab.research import nullmodel as NM
from quant_lab.research.api import PipelineConfig
from quant_lab.research.nullmodel import WorldConfig, _config_to_data

_KW = dict(mechanism="common_shock", kind="null", n_rep=1, seed0=1)


def _data_kwargs():
    return dict(_KW, world_cfg_data=_config_to_data(WorldConfig(n_clusters=150)),
                pipe_cfg_data=_config_to_data(PipelineConfig(B=100)))


def _plant_stale_pyc(root: Path) -> None:
    """造一个事故形态的陈旧 pyc：头里记的源码 mtime / size 与磁盘源码完全相符，字节码却来自另一份源码。"""
    src = root / "stalemod.py"
    src.write_text('VALUE = "source"\n', encoding="utf-8")
    st = src.stat()
    code = compile('VALUE = "stale!"\n', str(src), "exec")
    from importlib._bootstrap_external import _code_to_timestamp_pyc
    pyc = Path(importlib.util.cache_from_source(str(src)))
    pyc.parent.mkdir(parents=True, exist_ok=True)
    pyc.write_bytes(bytes(_code_to_timestamp_pyc(code, int(st.st_mtime), st.st_size)))


def _import_value(root: Path, prefix: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPYCACHEPREFIX", "PYTHONPATH")}
    env["PYTHONPATH"] = str(root)
    if prefix is not None:
        env["PYTHONPYCACHEPREFIX"] = prefix
    out = subprocess.run([sys.executable, "-c", "import stalemod; print(stalemod.VALUE)"],
                         env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


# ---------------------------------------------------------------- 陈旧 pyc

def test_mutation_without_a_fresh_prefix_the_stale_pyc_is_executed(tmp_path):
    """威胁真实存在：源码与 pyc 头逐项相符时，解释器执行的是陈旧字节码，而源码摘要看不出任何差别。"""
    _plant_stale_pyc(tmp_path)
    assert _import_value(tmp_path, None) == "stale!"


def test_a_fresh_prefix_executes_the_source_not_the_stale_pyc(tmp_path):
    """同一份陈旧 pyc，在新建的空前缀下读不到，只能从源码编译。"""
    _plant_stale_pyc(tmp_path)
    fresh = tmp_path / "fresh-prefix"
    fresh.mkdir()
    assert _import_value(tmp_path, str(fresh)) == "source"


def test_workers_spawned_inside_the_context_run_under_the_fresh_prefix():
    """正式入口的 worker 由 spawn 在 fresh_pycache_prefix 期间启动，实际使用的正是那个新目录。"""
    with NM.fresh_pycache_prefix() as prefix:
        assert os.listdir(prefix) == []                    # 新建且为空：运行前的 pyc 一个都不在里面
        with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as ex:
            assert ex.submit(NM._current_pycache_prefix).result() == prefix
    assert not os.path.exists(prefix)


def test_mutation_workers_spawned_outside_the_context_do_not_get_it():
    """突变：不经上下文启动的 worker 拿不到新前缀——没有这道设置，关闭就不存在。"""
    with NM.fresh_pycache_prefix() as prefix:
        pass
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as ex:
        assert ex.submit(NM._current_pycache_prefix).result() != prefix


def test_cli_parent_reexecs_itself_under_a_fresh_prefix(monkeypatch):
    """CLI 父进程不在新前缀下时，以新建的空前缀原样重启自己；已在新前缀下则不再重启。"""
    monkeypatch.delenv(NM._FRESH_PARENT_ENV, raising=False)
    calls = []
    NM._reexec_with_fresh_pycache_prefix(["--n-rep", "2"], execve=lambda *a: calls.append(a))
    (exe, argv, env), = calls
    assert argv[1:] == ["-m", "quant_lab.research.nullmodel", "--n-rep", "2"]
    assert env["PYTHONPYCACHEPREFIX"] == env[NM._FRESH_PARENT_ENV]
    assert os.listdir(env["PYTHONPYCACHEPREFIX"]) == []
    os.rmdir(env["PYTHONPYCACHEPREFIX"])


def test_handshake_is_recognised_only_when_the_prefix_really_is_the_marker(tmp_path):
    """握手只在「解释器实际前缀 == 标记目录」时成立；只有标记、前缀不符，不算新前缀启动。"""
    probe = "from quant_lab.research import nullmodel as NM; print(NM._parent_started_with_fresh_prefix())"
    d, other = tmp_path / "d", tmp_path / "other"
    d.mkdir(); other.mkdir()

    def run(prefix, marker):
        env = dict(os.environ, PYTHONPYCACHEPREFIX=str(prefix), **{NM._FRESH_PARENT_ENV: str(marker)})
        return subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                              text=True, check=True).stdout.strip()

    assert run(d, d) == "True"
    assert run(other, d) == "False"


# ---------------------------------------------------------------- 逐 job 绑定与结果摘要

def _expected_for(kw: dict, prefix: str | None) -> dict:
    inputs = {k: v for k, v in kw.items() if k not in ("world_cfg_data", "pipe_cfg_data")}
    return NM.job_binding(kw["world_cfg_data"], kw["pipe_cfg_data"], inputs, prefix)


@pytest.fixture(scope="module")
def real_payload():
    """真实 job 回执（in-process）：绑定按本进程实际前缀算，父侧期望按同一前缀算。"""
    kw = _data_kwargs()
    return kw, NM._run_mc_job(**kw)


def _frozen():
    return NM.research_code_digest(), NM.artifact_identity_digest()


def test_matching_binding_is_accepted(real_payload):
    kw, payload = real_payload
    prefix = payload["binding"]["pycache_prefix"] or "/stand-in-prefix"
    payload = dict(payload, binding=dict(payload["binding"], pycache_prefix=prefix))
    assert NM.check_worker_receipt(payload, *_frozen(), _expected_for(kw, prefix)) is payload["result"]


@pytest.mark.parametrize("key,forged", [("config_sha256", "0" * 64), ("inputs_sha256", "0" * 64),
                                        ("seed0", 999), ("n_rep", 7), ("pycache_prefix", "/some/old/prefix")])
def test_any_binding_mismatch_is_refused(real_payload, key, forged):
    kw, payload = real_payload
    expected = _expected_for(kw, "/stand-in-prefix")
    bad = dict(payload, binding=dict(expected, **{key: forged}))
    with pytest.raises(SystemExit, match=key):
        NM.check_worker_receipt(bad, *_frozen(), expected)


def test_mutation_without_expected_binding_the_forgery_would_publish(real_payload):
    """突变：父进程不传期望绑定时，同一份伪造回执会被放行——拒绝来自这道门，不是来自别处。"""
    kw, payload = real_payload
    bad = dict(payload, binding=dict(_expected_for(kw, "/stand-in-prefix"), seed0=999))
    assert NM.check_worker_receipt(bad, *_frozen()) is payload["result"]


def test_missing_binding_or_missing_prefix_is_refused(real_payload):
    kw, payload = real_payload
    expected = _expected_for(kw, "/stand-in-prefix")
    with pytest.raises(SystemExit, match="逐 job 绑定"):
        NM.check_worker_receipt({k: v for k, v in payload.items() if k != "binding"}, *_frozen(), expected)
    with pytest.raises(SystemExit, match="pyc 前缀"):
        NM.check_worker_receipt(payload, *_frozen(), _expected_for(kw, None))


def test_result_changed_after_the_worker_hashed_it_is_refused(real_payload):
    """结果摘要：worker 跑完即取摘要，父进程对收到的对象独立重算；中途被改过就不相等。"""
    kw, payload = real_payload
    expected = _expected_for(kw, "/stand-in-prefix")
    tampered = dataclasses.replace(payload["result"], n_positive=payload["result"].n_positive + 1)
    bad = dict(payload, binding=expected, result=tampered)
    with pytest.raises(SystemExit, match="结果摘要"):
        NM.check_worker_receipt(bad, *_frozen(), expected)
    # 突变：不核结果摘要（不传期望绑定）时，改过的结果照样发布
    assert NM.check_worker_receipt(bad, *_frozen()) is tampered


def test_worker_binds_what_it_received_not_what_the_parent_meant():
    """worker 的绑定按它**收到**的输入算：父进程派发 seed0=1、worker 收到 seed0=2，绑定必然不等。"""
    kw = _data_kwargs()
    sent = _expected_for(kw, NM._current_pycache_prefix())
    got = NM._run_mc_job(**dict(kw, seed0=2))["binding"]
    assert got["seed0"] == 2 and got["inputs_sha256"] != sent["inputs_sha256"]


def test_job_inputs_must_be_pure_data():
    """输入同样只收纯数据：活对象（可调用的进度回调）在绑定时具名拒绝，而不是悄悄过界。"""
    with pytest.raises(NM.UnsupportedConfigValue):
        NM._run_mc_job(**_data_kwargs(), progress=print)


# ---------------------------------------------------------------- 正式入口端到端

def _run_main(out: Path) -> dict:
    NM._main(["--out", str(out), "--n-rep", "2", "--no-ext", "--jobs", "2", "--B", "100",
              "--n-clusters", "200", "--mechanisms", "common_shock", "--power-mechanisms", ""])
    text = out.read_text(encoding="utf-8")
    return json.loads(text[text.rindex("```json") + 7:text.rindex("```")])


def test_real_run_binds_every_job(tmp_path):
    data = _run_main(tmp_path / "r.md")
    receipts = data["meta"]["job_receipts"]
    assert len(receipts) == len(data["results"]) >= 1
    for jr, row in zip(receipts, data["results"]):
        assert jr["mechanism"] == row["mechanism"] and jr["n_rep"] == 2
        assert all(len(jr[k]) == 64 for k in ("config_sha256", "inputs_sha256", "result_sha256"))
    assert data["meta"]["parent_started_with_fresh_pycache_prefix"] is False   # pytest 进程不是经 CLI 重启的


def test_mutation_real_run_refuses_workers_that_did_not_get_the_fresh_prefix(tmp_path, monkeypatch):
    """突变：把「让 worker 继承新前缀」这一步去掉（只建目录、不设环境），正式入口必须拒绝落盘。"""
    @contextlib.contextmanager
    def no_env(real=NM.fresh_pycache_prefix):
        with real() as d:
            os.environ.pop("PYTHONPYCACHEPREFIX", None)
            yield d
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    monkeypatch.setattr(NM, "fresh_pycache_prefix", no_env)
    out = tmp_path / "r.md"
    with pytest.raises(SystemExit, match="pycache_prefix"):
        _run_main(out)
    assert not out.exists()


# ---------------------------------------------------------------- 判读侧

def _official():
    return Path("docs/adr/report-G3-null-model.md").read_text(encoding="utf-8")


def _with_meta(text: str, **changes) -> str:
    start, end = text.rindex("```json") + 7, text.rindex("```")
    q = json.loads(text[start:end])
    for k, v in changes.items():
        if v is None:
            q["meta"].pop(k, None)
        else:
            q["meta"][k] = v
    return text[:start] + json.dumps(q) + text[end:]


def test_official_report_carries_the_binding_and_was_made_by_a_fresh_parent():
    NM.verify_report_text(_official())


@pytest.mark.parametrize("change", [dict(job_receipts=None), dict(job_receipts=[]),
                                    dict(parent_started_with_fresh_pycache_prefix=False)])
def test_report_without_binding_evidence_is_rejected(change):
    """删字段不得等于摘掉门：缺逐 job 回执、或父进程不是经新前缀启动，判读侧一律拒收。"""
    with pytest.raises(ValueError):
        NM.verify_report_text(_with_meta(_official(), **change))
