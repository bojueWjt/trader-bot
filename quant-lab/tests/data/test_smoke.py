"""D-01 冒烟：包可导入、原因码与契约（§4 + §9 增补）逐字一致、每张表带三时钟、quarantine 幂等且不覆盖处置、湖根目录由环境变量决定、API 签名与契约一致。"""
from __future__ import annotations

import importlib
import inspect
import re
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

import quant_lab.data as qd
from quant_lab.data import dedup, extract, lake, lifecycle, linker, normalize, reasons, validate

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "research-schema.md"
MODULES = ["normalize", "dedup", "extract", "validate", "linker", "lifecycle", "graph", "audit", "harvest", "api", "llm", "market_stub", "lake", "reasons", "sources", "codes"]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    m = importlib.import_module(f"quant_lab.data.{name}")
    assert m.__doc__, f"{name} 缺模块 docstring"


def test_no_services_import():
    src = Path(__file__).resolve().parents[2] / "src" / "quant_lab" / "data"
    for p in src.glob("*.py"):
        assert "services" not in [ln.split()[1] for ln in p.read_text().splitlines() if ln.startswith(("import ", "from ")) and len(ln.split()) > 1 and ln.split()[1].startswith("services")], p


def test_pipeline_order_and_layers():
    assert qd.PIPELINE == ("normalize", "dedup", "extract", "validate", "linker", "lifecycle")
    assert len(lake.LAYER_NAMES) == 6 and lake.LAYER_NAMES[5] == "market_check"


def test_reason_codes_match_contract_exactly():
    text = CONTRACT.read_text(encoding="utf-8")
    block = text.split("## 4. 原因码")[1].split("严重度")[0]
    in_contract = set(re.findall(r"\b[A-Z][A-Z_]{3,}\b", block)) | set(re.findall(r"§4 增原因码 `([A-Z_]+)`", text))
    in_code = {str(c) for c in reasons.Reason}
    assert in_code == in_contract, f"仅代码有: {in_code - in_contract}; 仅契约有: {in_contract - in_code}"
    assert len(reasons.Reason) == 29
    from quant_lab.data import codes
    assert codes.ReasonCode is reasons.Reason and codes.FATAL_REASONS is reasons.FATAL  # §9.5 公共路径 = 纯别名


def test_fatal_severity_is_contract_three():
    assert reasons.FATAL == {reasons.Reason.UNIT_SCALE_CONFLICT, reasons.Reason.ENTRY_LINK_AMBIGUOUS, reasons.Reason.SYMBOL_TIME_INVALID}
    assert reasons.severity("UNIT_SCALE_CONFLICT") == "fatal" and reasons.severity("BAR_GAP") == "general"
    assert reasons.primary_reason(["MEDIA_MISSING", "TIME_UNIT_INVALID"]) == "TIME_UNIT_INVALID"


def test_weakest_time_grade():
    assert lifecycle.weakest(["V", "H0", "H2"]) == "H2" and lifecycle.weakest([]) == "U"


@pytest.mark.parametrize("name,schema", [
    ("message_version", normalize.MESSAGE_VERSION_SCHEMA), ("duplicate_group", dedup.DUPLICATE_GROUP_SCHEMA), ("extracted_event", extract.EXTRACTED_EVENT_SCHEMA),
    ("canonical_plan", validate.CANONICAL_PLAN_SCHEMA), ("episode", lifecycle.EPISODE_SCHEMA), ("episode_event", lifecycle.EVENT_SCHEMA),
    ("candidate_edges", linker.CANDIDATE_EDGE_SCHEMA), ("quarantine", lake.QUARANTINE_SCHEMA), ("loss", lake.LOSS_SCHEMA), ("mapping", lake.MAP_SCHEMA),
])
def test_every_table_carries_three_clocks(name, schema):
    for col in ("event_time", "available_at", "ingested_at"):
        assert schema[col] == pl.Datetime("us", "UTC"), (name, col)


def test_empty_frames_keep_columns(tmp_path):
    for schema in (normalize.MESSAGE_VERSION_SCHEMA, lifecycle.EPISODE_SCHEMA, lake.QUARANTINE_SCHEMA, lake.LOSS_SCHEMA):
        df = pl.DataFrame(schema=schema)
        p = tmp_path / "x.parquet"
        lake.write_parquet_atomic(df, p)
        back = pl.read_parquet(p)
        assert back.height == 0 and back.width == len(schema)


def test_quarantine_idempotent_and_disposition_preserved(tmp_path):
    p = tmp_path / "q.parquet"
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    rec = lambda: lake.quarantine_row(batch_id="b1", object_kind="message_version", object_id="mv1", object_version="v1", partition_id="c", reason_codes=["MEDIA_MISSING"], rule_version="r1", schema_hash_="s", ingested_at=ts)
    assert lake.append_quarantine(p, [rec()]).height == 1
    assert lake.append_quarantine(p, [rec()]).height == 1
    df = pl.read_parquet(p).with_columns(pl.lit("reviewed").alias("status"))
    df.write_parquet(p)
    after = lake.append_quarantine(p, [rec()])
    assert after.row(0, named=True)["status"] == "reviewed"
    assert lake.append_quarantine(p, [lake.quarantine_row(batch_id="b1", object_kind="message_version", object_id="mv1", object_version="v1", partition_id="c", reason_codes=["MEDIA_MISSING"], rule_version="r2", schema_hash_="s", ingested_at=ts)]).height == 2


def test_lake_root_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path))
    lay = lake.Layout.from_root()
    assert lay.bronze_dir == tmp_path / "lake" / "telegram" / "bronze" and lay.quarantine_path == tmp_path / "quarantine" / "telegram.parquet"
    monkeypatch.delenv("QUANT_LAB_DATA_ROOT")
    assert lake.Layout.from_root().bronze_dir.parts[-5:] == ("quant-lab", "data", "lake", "telegram", "bronze")


def test_api_signatures_match_contract():
    from quant_lab.data import api

    sig = inspect.signature(api.load_episodes)
    assert list(sig.parameters) == ["graph_version", "decision_graph"]
    assert sig.parameters["decision_graph"].kind is inspect.Parameter.KEYWORD_ONLY and sig.parameters["decision_graph"].default is True
    assert list(inspect.signature(api.load_episode_events).parameters) == ["graph_version"]
    assert list(inspect.signature(api.loss_table).parameters) == ["batch_id"]
    qs = inspect.signature(api.quarantine)
    assert list(qs.parameters) == ["flow", "status"] and qs.parameters["flow"].default is inspect.Parameter.empty and qs.parameters["status"].kind is inspect.Parameter.KEYWORD_ONLY
