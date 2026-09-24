"""本机 grok 协议离线验收：PATH 假程序 + 每个失败分支的可执行突变。"""
from __future__ import annotations

import inspect
import json
import os
import pathlib
import subprocess
import sys
import textwrap
from decimal import Decimal

import polars as pl
import pytest

from quant_lab.data import api, extract, llm, normalize
from quant_lab.data.lake import Layout
from quant_lab.data.llm import Abstention, GrokCliClient, LLMResponse, SCHEMA_NAME_EXTRACT

SECRET = "GROK_PRIVATE_BODY_DO_NOT_LOG"
FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample" / "AlphaSignals"
VALID = {"kind": "chatter", "symbol_raw": None, "side": None, "entry": None, "entries": [], "stop": None, "tps": [], "spans": [], "reason_codes": []}


@pytest.fixture(autouse=True)
def fake_grok(tmp_path, monkeypatch):
    """所有测试含突变都只能命中假可执行文件，永不调用真实 grok。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    capture = tmp_path / "calls.jsonl"
    script = bindir / "grok"
    script.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys, time
prompt = sys.stdin.read()
args = sys.argv[1:]
assert args[args.index("--prompt-file") + 1] == "/dev/stdin"
assert args[args.index("--output-format") + 1] == "json"
assert args[args.index("--tools") + 1] == ""
assert "--disable-web-search" in args and "--no-subagents" in args
json.loads(args[args.index("--json-schema") + 1])
with open(os.environ["FAKE_GROK_CAPTURE"], "a") as f:
    f.write(json.dumps({{"args": args, "prompt": prompt, "cwd": os.getcwd()}}) + "\\n")
mode = os.environ.get("FAKE_GROK_MODE", "ok")
if mode == "timeout":
    time.sleep(0.4)
payload = {VALID!r}
if mode == "schema":
    payload["side"] = "invented"
if mode == "evidence":
    payload["stop"] = 123
    payload["spans"] = [{{"field": "stop", "start": 0, "end": 5}}]
if mode == "decimal":
    payload["stop"] = 0.123456789012
if mode == "bad_json":
    print("not json: " + prompt)
else:
    print(json.dumps({{"type": "result", "text": json.dumps(payload), "usage": {{"input_tokens": 11, "output_tokens": 7, "text": prompt}}, "model": prompt}}))
if mode == "nonzero":
    print(prompt, file=sys.stderr)
    sys.exit(7)
''')
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_GROK_CAPTURE", str(capture))
    monkeypatch.setenv("QUANT_LAB_ALLOW_LLM", "1")
    monkeypatch.delenv("QUANT_LAB_LLM_MAX_CALLS", raising=False)
    monkeypatch.delenv("FAKE_GROK_MODE", raising=False)
    return capture


def calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def complete(client):
    return client.complete_json(system="extract only", user=SECRET, schema_name=SCHEMA_NAME_EXTRACT)


def mutant_method(before, after):
    source = textwrap.dedent(inspect.getsource(GrokCliClient.complete_json))
    assert source.count(before) == 1
    namespace = dict(llm.__dict__)
    exec(compile(source.replace(before, after), "<grok-mutant>", "exec"), namespace)
    return namespace["complete_json"]


def assert_abstention(result, note):
    assert isinstance(result, Abstention)
    assert result.note == note
    assert SECRET not in repr(result)


@pytest.mark.parametrize("mode,note,before,after", [
    ("timeout", "grok_timeout", "timeout=self.timeout", "timeout=2"),
    ("nonzero", "grok_nonzero_exit", "if result.returncode != 0:", "if False:"),
    ("bad_json", "grok_invalid_json_or_schema", "envelope = json.loads(result.stdout, parse_float=Decimal)", f"envelope = {VALID!r}"),
    ("schema", "grok_invalid_json_or_schema", "_GrokExtract.model_validate(payload).model_dump()", "payload"),
])
def test_failures_abstain_and_mutants(fake_grok, monkeypatch, mode, note, before, after, capsys):
    monkeypatch.setenv("FAKE_GROK_MODE", mode)
    timeout = 0.15 if mode == "timeout" else 5
    client = GrokCliClient(timeout=timeout)
    assert_abstention(complete(client), note)
    assert client.calls == 1
    broken = mutant_method(before, after)
    monkeypatch.setattr(GrokCliClient, "complete_json", broken)
    with pytest.raises(AssertionError):
        assert_abstention(complete(GrokCliClient(timeout=timeout)), note)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_gate_blocks_calls_and_mutant(fake_grok, monkeypatch):
    monkeypatch.delenv("QUANT_LAB_ALLOW_LLM")
    client = GrokCliClient()
    assert_abstention(complete(client), "llm_gated:grok")
    assert client.calls == 0 and calls(fake_grok) == []
    with pytest.raises(PermissionError):
        llm.extraction_client(llm="grok")
    broken = mutant_method("gate(self)", "None")
    monkeypatch.setattr(GrokCliClient, "complete_json", broken)
    with pytest.raises(AssertionError):
        assert_abstention(complete(GrokCliClient()), "llm_gated:grok")
    assert len(calls(fake_grok)) == 1  # 证明缺闸门突变确实触发了子进程。


def test_call_limit_and_mutant(fake_grok, monkeypatch):
    monkeypatch.setenv("QUANT_LAB_LLM_MAX_CALLS", "2")
    client = GrokCliClient()
    assert isinstance(complete(client), LLMResponse)
    monkeypatch.setenv("FAKE_GROK_MODE", "nonzero")
    assert_abstention(complete(client), "grok_nonzero_exit")
    assert_abstention(complete(client), "llm_call_limit")
    assert client.calls == len(calls(fake_grok)) == 2
    assert client.budget_report()["stop_reason"] == "llm_call_limit"
    broken = mutant_method("if self.calls >= self.max_calls:", "if False:")
    monkeypatch.setattr(GrokCliClient, "complete_json", broken)
    with pytest.raises(AssertionError):
        assert_abstention(complete(client), "llm_call_limit")
    assert len(calls(fake_grok)) == 3


@pytest.mark.parametrize("limit", ["0", "-1", "invalid"])
def test_invalid_or_zero_budget_never_calls(fake_grok, monkeypatch, limit):
    monkeypatch.setenv("QUANT_LAB_LLM_MAX_CALLS", limit)
    result = complete(GrokCliClient())
    assert isinstance(result, Abstention)
    assert not calls(fake_grok)


def test_stdin_schema_usage_and_decimal(fake_grok, monkeypatch):
    client = GrokCliClient()
    assert client.max_calls == 2000
    out = complete(client)
    assert isinstance(out, LLMResponse)
    assert out.usage == {"input_tokens": 11, "output_tokens": 7}
    assert out.model == "grok-4.5" and len(out.response_hash) == 64
    call = calls(fake_grok)[0]
    assert SECRET in call["prompt"] and SECRET not in json.dumps(call["args"])
    assert not pathlib.Path(call["cwd"]).exists()
    monkeypatch.setenv("FAKE_GROK_MODE", "decimal")
    out = complete(client)
    assert out.payload["stop"] == Decimal("0.123456789012")


def test_missing_executable_and_unsupported_schema(fake_grok):
    assert_abstention(complete(GrokCliClient(executable="/missing/grok")), "grok_transport_error")
    assert_abstention(GrokCliClient().complete_json(system="s", user=SECRET, schema_name="unknown"), "grok_unsupported_schema")
    assert calls(fake_grok) == []


def test_extract_budget_report_metadata_and_mutant(lake, fake_grok, monkeypatch):
    layout = Layout.from_root(lake)
    normalize.run(FIX, layout, tdesktop_only=True)
    monkeypatch.setenv("QUANT_LAB_LLM_MAX_CALLS", "2")
    monkeypatch.setenv("FAKE_GROK_MODE", "evidence")
    summary = extract.run(layout, llm="grok")
    budget = summary["llm"]
    assert budget["calls"] == budget["max_calls"] == len(calls(fake_grok)) == 2
    assert budget["limit_reached"] and budget["stop_reason"] == "llm_call_limit" and budget["skipped"] > 0
    df = pl.read_parquet(layout.extracted_event)
    for encoded in df["llm_meta"].drop_nulls():
        assert set(json.loads(encoded)) <= {"usage", "model", "response_hash"}
        assert SECRET not in encoded
    assert SECRET not in json.dumps(summary)
    # 绕过真实 provider 的脱敏分支，证据错误会回显原文切片；断言应杀死突变。
    source = textwrap.dedent(inspect.getsource(extract.llm_extract))
    namespace = dict(extract.__dict__)
    exec(compile(source.replace("if isinstance(client, GrokCliClient):", "if False:"), "<privacy-mutant>", "exec"), namespace)
    broken = namespace["llm_extract"]
    _, abstention, meta = extract.llm_extract(SECRET, client=GrokCliClient(), channel_name="synthetic", message_date=None)
    assert abstention.note == "evidence_rejected" and "rejected" not in meta
    _, abstention, meta = broken(SECRET, client=GrokCliClient(), channel_name="synthetic", message_date=None)
    with pytest.raises(AssertionError):
        assert abstention.note == "evidence_rejected" and "rejected" not in meta


def test_default_offline_even_when_gate_open(lake, fake_grok):
    layout = Layout.from_root(lake)
    normalize.run(FIX, layout, tdesktop_only=True)
    extract.run(layout)
    assert calls(fake_grok) == []
    assert llm.extraction_client() is None


@pytest.mark.parametrize("entry", ["extract", "api"])
def test_cli_selection_and_report(lake, fake_grok, monkeypatch, entry):
    monkeypatch.setenv("QUANT_LAB_LLM_MAX_CALLS", "1")
    layout = Layout.from_root(lake)
    if entry == "extract":
        normalize.run(FIX, layout, tdesktop_only=True)
        command = [sys.executable, "-m", "quant_lab.data.extract", "--llm", "grok"]
    else:
        command = [sys.executable, "-m", "quant_lab.data.api", "--build", "--fixture", str(FIX), "--llm", "grok", "--graph-version", "grok-fake-test"]
    result = subprocess.run(command, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    if entry == "api":
        report = report["extract"]
    assert report["llm"]["calls"] == 1 and report["llm"]["limit_reached"]
    assert len(calls(fake_grok)) == 1
    # 关闭闸门的输入突变：同一入口必须非 0，且不增加调用。
    monkeypatch.delenv("QUANT_LAB_ALLOW_LLM")
    denied = subprocess.run(command, text=True, capture_output=True)
    assert denied.returncode != 0 and "QUANT_LAB_ALLOW_LLM" in denied.stderr
    assert len(calls(fake_grok)) == 1
    conflict = subprocess.run(command + ["--llm-fixture", "unused.json"], text=True, capture_output=True)
    assert conflict.returncode != 0 and len(calls(fake_grok)) == 1
