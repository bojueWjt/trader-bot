"""D-08：合成 TDesktop 导入、目录隔离、无原文报告及可执行突变对照。"""
from __future__ import annotations

import hashlib
import inspect
import json
import pathlib
import shutil
import subprocess
import sys
import textwrap
from datetime import UTC, datetime

import polars as pl
import pytest

from quant_lab.data import harvest
from quant_lab.data.lake import Layout

FIX = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample" / "AlphaSignals"
SECRET = "D08_PRIVATE_MESSAGE_MUST_NOT_APPEAR_IN_REPORT"


def mutant(function, before, after):
    source = textwrap.dedent(inspect.getsource(function))
    assert source.count(before) == 1
    namespace = dict(function.__globals__)
    exec(compile(source.replace(before, after), "<harvest-mutant>", "exec"), namespace)
    return namespace[function.__name__]


@pytest.fixture
def exported(lake):
    directory = lake / "import" / "telegram"
    shutil.copytree(FIX, directory)
    doc = json.loads((directory / "result.json").read_text())
    # 给隐私断言一个唯一标记，且频道名也不能把正文带进摘要。
    doc["messages"][0]["text"] = SECRET
    doc["name"] = SECRET
    (directory / "result.json").write_text(json.dumps(doc, ensure_ascii=False))
    return lake, directory, doc


def assert_refused(function, code):
    try:
        function()
    except harvest.HarvestRefusal as exc:
        assert str(exc) == code
    else:
        raise AssertionError("guard missing")


def test_data_root_unset_and_mutant(monkeypatch, tmp_path):
    monkeypatch.delenv("QUANT_LAB_DATA_ROOT", raising=False)
    assert_refused(harvest._data_root, "DATA_ROOT_UNSET")
    broken = mutant(harvest._data_root, 'raise HarvestRefusal("DATA_ROOT_UNSET")', f'return pathlib.Path({str(tmp_path)!r})')
    with pytest.raises(AssertionError):
        assert_refused(broken, "DATA_ROOT_UNSET")


@pytest.mark.parametrize("path_kind", ["absolute", "relative", "symlink"])
def test_repository_root_refused_and_mutant(monkeypatch, tmp_path, path_kind):
    repo = harvest._repository_root()
    root = repo / "quant-lab" / "data"
    if path_kind == "relative":
        monkeypatch.chdir(repo)
        value = "quant-lab/data"
    elif path_kind == "symlink":
        link = tmp_path / "repo-link"
        link.symlink_to(repo, target_is_directory=True)
        value = str(link / "quant-lab/data")
    else:
        value = str(root)
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", value)
    assert_refused(harvest._data_root, "DATA_ROOT_IN_REPOSITORY")
    # 只调用根校验，突变对照绝不对仓库 data 执行 I/O。
    broken = mutant(harvest._data_root, 'raise HarvestRefusal("DATA_ROOT_IN_REPOSITORY")', "return root")
    with pytest.raises(AssertionError):
        assert_refused(broken, "DATA_ROOT_IN_REPOSITORY")


def test_lake_symlink_escape_refused(lake, tmp_path):
    (lake / "lake").symlink_to(tmp_path / "outside", target_is_directory=True)
    assert_refused(harvest._data_root, "DATA_ROOT_SYMLINK_ESCAPE")


@pytest.mark.parametrize("state,code", [("missing", "EXPORT_DIR_NOT_FOUND"), ("empty", "EXPORT_DIR_EMPTY"), ("other", "TDESKTOP_EXPORT_NOT_FOUND"), ("invalid", "TDESKTOP_EXPORT_INVALID")])
def test_export_refusals(lake, state, code):
    directory = lake / "import" / "telegram"
    if state != "missing":
        directory.mkdir(parents=True)
    if state == "other":
        (directory / "live.jsonl").write_text("{}\n")
    if state == "invalid":
        (directory / "result.json").write_text(SECRET)
    assert_refused(harvest.run, code)
    assert not (lake / "lake").exists()


def test_default_export_directory_and_mutant(exported):
    root, directory, doc = exported
    report = harvest.run()
    n = sum(m["type"] == "message" for m in doc["messages"])
    assert report["n_messages"] == n
    layout = Layout.from_root(root)
    assert layout.message_version.is_file() and layout.extracted_event.is_file()
    bronze = pl.read_parquet(layout.message_version)
    assert set(bronze["raw_hash"]) == {hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()}
    # 同目录现有 observations.jsonl 被明确排除，不会变成 V 级实收。
    assert "V" not in set(bronze["time_grade"])
    broken = mutant(harvest.run, 'root / "import" / "telegram"', 'root / "wrong-import"')
    with pytest.raises(harvest.HarvestRefusal, match="EXPORT_DIR_NOT_FOUND"):
        broken()
    # 同一导出重放不增加 bronze 版本。
    harvest.run(export_dir=directory)
    assert pl.read_parquet(layout.message_version).height == bronze.height


def assert_private(report):
    assert SECRET not in json.dumps(report, ensure_ascii=False)


def test_report_metrics_privacy_and_mutant(exported):
    _, directory, doc = exported
    report = harvest.run()
    ordinary = [m for m in doc["messages"] if m["type"] == "message"]
    n = len(ordinary)
    assert report["edit_ratio"] == sum("edited" in m or "edited_unixtime" in m for m in ordinary) / n
    assert report["image_ratio"] == sum(bool(m.get("photo")) for m in ordinary) / n
    assert report["reply_ratio"] == sum(m.get("reply_to_message_id") is not None for m in ordinary) / n
    expected_years = {}
    for m in ordinary:
        year = str(datetime.fromtimestamp(int(m["date_unixtime"]), UTC).year)
        expected_years[year] = expected_years.get(year, 0) + 1
    assert report["year_distribution"] == expected_years
    assert report["signal_rate"]["verified"] is False
    assert 0 < report["signal_rate"]["value"] < 1
    assert report["exports"][0]["sha256"] == hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()
    assert report["channels"] == [report["exports"][0]["channel_id"]]
    assert report["telethon_comparison"]["status"] == "not_run"
    assert "生产 watcher" in report["telethon_comparison"]["reason"]
    assert report["manual_review"]["status"] == report["ocr"]["status"] == "not_run"
    assert_private(report)
    # 真实执行泄漏突变；同一隐私断言必须杀死将消息挂进报告的实现。
    broken = mutant(harvest.run, 'report["batch_id"] = normalized["batch_id"]', 'report["text"] = messages[0].text')
    with pytest.raises(AssertionError):
        assert_private(broken())


def test_harvest_cli_report_and_named_refusal(exported, capsys, monkeypatch, tmp_path):
    root, _, _ = exported
    report_path = root / "reports" / "poc1a.json"
    assert harvest.main(["--report", str(report_path)]) == 0
    assert_private(json.loads(report_path.read_text()))
    assert SECRET not in capsys.readouterr().out
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path / "no-export"))
    result = subprocess.run([sys.executable, "-m", "quant_lab.data.harvest", "--report", str(root / "absent.json")], capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stderr)["reason"] == "EXPORT_DIR_NOT_FOUND"
    assert not (root / "absent.json").exists()
