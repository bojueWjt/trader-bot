"""Shared dependency index gives the same closures as a per-plan rebuild (synthetic data only)."""
from copy import deepcopy
from datetime import datetime, timedelta, UTC
import json

from quant_lab.data import graph

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def versions():
    rows = {}
    for i in range(12):
        rows[f"v{i}"] = dict(
            channel_id=-100 - (i % 2), source_id={"message_id": i}, available_at=T0 + timedelta(minutes=i),
            media_hashes=[f"m{i}"] if i % 3 == 0 else [], grouped_id=(7 if i in (3, 5, 9) else (8 if i in (4, 6) else None)),
            reply_to_message_id=(i - 2 if i in (6, 7, 11) else (99 if i == 10 else None)))
    rows["v13"] = dict(channel_id=-100, source_id={"message_id": 13}, available_at=None, media_hashes=[], grouped_id=None, reply_to_message_id=None)
    return rows


def roots():
    out = []
    for i, src in enumerate(["v3", "v5", "v6", "v7", "v10", "v13", "missing"]):
        checks = {"dependencies": [{"ref": f"ocr:{i}", "available_at": (T0 + timedelta(hours=1)).isoformat(), "purpose": "price_check"}]}
        out.append(dict(source_version_id=src, extract_id=f"x{i}", plan_id=f"p{i}", available_at=T0 + timedelta(minutes=30),
                        entry={"lo": 1}, stop=None, tps=[1], size_hint=None, checks=json.dumps(checks)))
    return out


def test_shared_index_matches_per_plan_rebuild_and_is_not_written():
    table = versions()
    index = graph.dependency_index(table)
    frozen = deepcopy(index)
    rules = {"normalize": "v1", "missing": ""}
    for root in roots():
        for purpose in ("entry_decision", "price_check"):
            assert graph.plan_dependencies(root, table, rules, purpose=purpose, index=index) == \
                graph.plan_dependencies(root, table, rules, purpose=purpose)
    # Root-specific nodes (fields, rules, OCR checks) must not leak into the shared layer.
    assert index == frozen


def test_album_members_come_from_the_same_channel_only():
    table = versions()
    table["v4"]["grouped_id"] = 7  # same group id, other channel (-101)
    _, refs = graph.plan_dependencies(roots()[0], table, {}, index=graph.dependency_index(table))
    assert "v4" not in refs and {"v3", "v5", "v9"} <= set(refs)
