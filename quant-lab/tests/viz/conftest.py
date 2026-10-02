"""Synthetic chat, market and published L0 reports; never reads personal data."""
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import shutil

import polars as pl
import pytest

from quant_lab.data.api import load_episodes
from quant_lab.data.followup import FOLLOWUP_SCHEMA
from quant_lab.data.lake import Layout
from quant_lab.market.l0_replay import replay
from quant_lab.viz.data import Dashboard
from tests.data.l0_fixtures import CHANNEL, T0, account, market_lake

HTML_TEXT = 'BTC 做多 入场 100 止损 90 目标 110\n<script>alert("synthetic")</script><b>合成原文</b>'


@pytest.fixture(scope="session")
def synthetic(tmp_path_factory):
    root = tmp_path_factory.mktemp("viz-synthetic")
    source = account(root) / "account" / "result.json"
    doc = json.loads(source.read_text())
    messages = doc["chats"]["list"][0]["messages"]
    messages[0]["text"] = HTML_TEXT
    for mid, text, minutes in ((10, 'BTC 合成管理消息', 2), (11, 'BTC 合成未确定消息', 3),
                               (12, 'BTC 合成歧义消息', 4), (13, 'BTC 合成窗口评论', 6),
                               (14, 'BTC 合成孤立指令', 5)):
        at = T0 + timedelta(minutes=minutes)
        messages.append({"id": mid, "type": "message", "date": at.isoformat(), "date_unixtime": str(int(at.timestamp())), "text": text})
    source.write_text(json.dumps(doc))
    lake = market_lake(root, days=8).root
    project = Path(__file__).resolve().parents[2]
    import os
    env = dict(os.environ, QUANT_LAB_DATA_ROOT=str(root))
    subprocess.run(["nice", "-n", "19", str(project / ".venv-g1/bin/python"), "-m", "quant_lab.data.api",
                    "--build", "--market", "real", "--graph-version", "viz-test"], cwd=project, env=env,
                   check=True, capture_output=True, text=True)
    layout = Layout.from_root(root)
    # The real-build CLI stages bronze/silver under _build; the dashboard fixture
    # represents a per-channel Layout.from_root with the same exact versions.
    work = next((layout.gold_dir.parent / "_build").iterdir())
    shutil.copytree(work / "bronze", layout.bronze_dir, dirs_exist_ok=True)
    shutil.copytree(work / "silver", layout.silver_dir, dirs_exist_ok=True)
    episode = load_episodes("viz-test", layout=layout).filter((pl.col("channel_id") == CHANNEL) & (pl.col("root_message_id") == 1)).to_dicts()[0]
    versions = pl.read_parquet(layout.message_version).to_dicts()
    sources = {r["source_id"]["message_id"]: r["source_version_id"] for r in versions if r["channel_id"] == CHANNEL}
    rows = []
    for mid, uncertain, ambiguity in ((10, False, None), (11, True, None), (12, True, "ambiguous_root_episode"), (14, True, None)):
        rows.append({"instruction_id": f"synthetic-{mid}", "channel_id": CHANNEL, "channel_name": "合成老师",
                     "message_id": mid, "source_version_id": sources[mid], "available_at": T0 + timedelta(minutes=mid-8),
                     "target_message_id": None if mid == 14 else 1,
                     "episode_id": episode["episode_id"] if ambiguity is None and mid != 14 else None,
                     "episode_ambiguity": ambiguity, "target_symbol": None if mid == 14 else "BTC", "action": "move_stop",
                     "fraction_pct": None, "fraction": None, "stop_price": Decimal("95"), "to_entry": False,
                     "evidence": "合成", "uncertain": uncertain, "graph_version": "viz-test", "rule_version": "synthetic"})
    follow_path = layout.silver_dir / "followup_action.parquet"
    pl.DataFrame(rows, schema=FOLLOWUP_SCHEMA).write_parquet(follow_path)
    reports = root / "reports"
    # replay's legacy entry remains environment based; the dashboard never mutates it.
    previous = os.environ.get("QUANT_LAB_DATA_ROOT")
    os.environ["QUANT_LAB_DATA_ROOT"] = str(root)
    try:
        for suffix, policy in (("", "base-v1-timeexit"), ("-live", "base-v1-timeexit-live"), ("-follow", "base-v1-timeexit-follow")):
            replay(graph_version="viz-test", channel=CHANNEL, out=reports / f"l0-v7{suffix}" / "demo",
                   market_lake=lake, policy_version=policy, followup_actions=follow_path)
    finally:
        if previous is None:
            os.environ.pop("QUANT_LAB_DATA_ROOT", None)
        else:
            os.environ["QUANT_LAB_DATA_ROOT"] = previous
    config = {"reports": str(reports), "tag": "v7", "market_lake": str(lake), "channels": [
        {"key": "demo", "name": "合成老师", "channel_id": CHANNEL, "data_root": str(root), "graph_version": "viz-test"}]}
    path = root / "dashboard.json"
    path.write_text(json.dumps(config))
    return {"root": root, "lake": lake, "reports": reports, "config": path, "episode": episode, "follow_path": follow_path}


@pytest.fixture
def dashboard(synthetic):
    return Dashboard(synthetic["config"])
