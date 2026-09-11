"""ADR §13 test_replay：同输入/合同/seed 的 trace 跨进程、批次重排、单条与批量一致；非决定性字段不进 hash。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import polars as pl

from quant_lab.market import contract as c
from quant_lab.market import execution as x

EP = Path(__file__).parent / "fixtures" / "episodes"
ROOT = Path(__file__).resolve().parents[2]


def test_trace_hash_identical_across_processes():
    fx = c.load_fixtures(EP)
    here = {f.id: x.simulate(f.request, market=f.market).trace_hash for f in fx}
    out = subprocess.run([sys.executable, "-m", "quant_lab.market.execution", "replay", "--fixtures", str(EP), "--kernel", "A", "--json", "--repeat", "1"],
                         capture_output=True, text=True, cwd=ROOT, check=True)
    other = {r["id"]: r["trace_hash"] for r in json.loads(out.stdout)["results"]}
    assert other == here


def test_batch_order_and_single_vs_batch_consistent():
    fx = c.load_fixtures(EP)
    markets = {f.market.manifest_id: f.market for f in fx}
    a = x.simulate_batch([f.request for f in fx], markets=markets).sort("episode_id")
    b = x.simulate_batch([f.request for f in reversed(fx)], markets=markets).sort("episode_id")
    assert a["trace_hash"].to_list() == b["trace_hash"].to_list()
    single = {f.id: x.simulate(f.request, market=f.market).trace_hash for f in fx}
    assert dict(zip(a["episode_id"], a["trace_hash"])) == single
    # 事件列逐行一致（Decimal/时间序列化稳定）
    assert a.select(pl.col("canonical_events").hash()).equals(b.select(pl.col("canonical_events").hash()))
