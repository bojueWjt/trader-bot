"""Follow input filtering/time/ordering diagnostics; only synthetic rows."""
from datetime import timedelta
from decimal import Decimal as D

import polars as pl
import pytest

from quant_lab.market import contract as c, l0_replay as l0
from tests.market.test_follow_teacher import T0, request

CHANNEL = -100123


def action(kind="reduce", seconds=75, **kwargs):
    row = dict(channel_id=CHANNEL, message_id=7, episode_id="teacher", available_at=T0 + timedelta(seconds=seconds),
               action=kind, fraction=None, stop_price=None, to_entry=False, uncertain=False, episode_ambiguity=None,
               graph_version="synthetic")
    row.update(kwargs)
    return row


def attach(rows, req=None):
    req = request() if req is None else req
    return l0.attach_management([req], rows, policy=c.resolve_policy(req.policy_version), channel=CHANNEL, graph_version="synthetic")


def test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none():
    rows = [action(), action("move_stop", 60, to_entry=True, message_id=6),
            action("close_all", 170), action("cancel_pending", 160), action("add", 155, fraction=D(2)),
            action("close_all", -1), action("close_all", 0), action(uncertain=True),
            action(episode_ambiguity="ambiguous_root_episode"), action(episode_id="other"), action("none"),
            action("close_all", 180), action("close_all", 181), action(channel_id=CHANNEL - 1),
            action(graph_version="old"), action(fraction=D("1.1")), action(uncertain=None), action("move_stop")]
    requests, report = attach(rows)
    assert [a.kind for a in requests[0].management] == ["move_stop", "reduce", "add", "cancel_pending", "close_all"]
    assert report["n_read"] == 18 and report["n_adopted"] == 5 and report["n_discarded"] == 13
    assert report["discard_reason_counts"] == {
        "at_or_before_t_dec": 2, "uncertain": 2, "episode_ambiguity": 1, "episode_not_replayed": 1,
        "none": 1, "at_or_after_horizon": 2, "channel_mismatch": 1, "graph_version_mismatch": 1, "invalid_contract": 2}
    assert report["kind_counts"] == dict.fromkeys(c.MANAGEMENT_KINDS, 1)
    assert report["read_kind_counts"] == {"add": 1, "cancel_pending": 1, "close_all": 5, "move_stop": 2, "none": 1, "reduce": 8}
    assert report["n_reduce_fraction_defaulted"] == 1


def test_table_built_for_another_graph_is_a_batch_error():
    # An unresolved alias ("x-v7" instead of "x-v7@hash") once discarded a whole channel silently.
    with pytest.raises(c.ContractError, match="graph_version"):
        attach([action(graph_version="synthetic-alias"), action("close_all", 170, graph_version="synthetic-alias")])
    _, report = attach([action(graph_version="synthetic-alias"), action("close_all", 170)])
    assert report["discard_reason_counts"] == {"graph_version_mismatch": 1} and report["n_adopted"] == 1
    _, report = attach([action(channel_id=CHANNEL - 1, graph_version="other")])
    assert report["discard_reason_counts"] == {"channel_mismatch": 1}


def test_equal_times_order_by_message_and_instruction_id_independent_of_parquet_order():
    rows = [action("close_all", message_id=9, instruction_id="z"),
            action("move_stop", to_entry=True, message_id=8, instruction_id="b"),
            action("reduce", message_id=8, instruction_id="a"),
            action("add", message_id=7, instruction_id="q")]
    forward, report = attach(rows)
    backward, other = attach(list(reversed(rows)))
    assert [a.kind for a in forward[0].management] == ["add", "reduce", "move_stop", "close_all"]
    assert forward[0].model_dump_json() == backward[0].model_dump_json() and report == other


def test_processing_latency_is_same_as_decision_and_late_execution_is_counted(monkeypatch):
    policy = c.resolve_policy("base-v1-timeexit-follow").model_copy(update={"version": "fixture-follow-latency", "latency_s": 30})
    registry = c.load_policy_registry()
    monkeypatch.setitem(c.POLICIES, policy.version, policy)
    monkeypatch.setattr(c, "load_policy_registry", lambda: {**registry, policy.version: policy.content_hash})
    req = request(version=policy.version)
    attached, report = attach([action(seconds=1), action(seconds=120), action(seconds=150)], req)
    assert req.resolved_t_start(policy) == T0 + timedelta(seconds=30)
    assert [a.at for a in attached[0].management] == [T0 + timedelta(seconds=31), T0 + timedelta(seconds=150)]
    assert report["discard_reason_counts"] == {"execution_at_or_after_horizon": 1}
    assert report["n_adopted"] == 2 and report["n_read"] == 3
    with pytest.raises(c.ContractError, match="strictly"):
        c.ExecutionRequest.model_validate({**req.model_dump(), "management": [c.ManagementAction(
            at=T0 + timedelta(seconds=30), kind="close_all", source_message_id=9)]})


def test_follow_file_empty_schema_valid_missing_and_bad_schema_are_batch_errors(tmp_path):
    path = tmp_path / "actions.parquet"
    with pytest.raises(c.ContractError, match="requires followup"):
        l0.load_followup_actions(path)
    pl.DataFrame({"x": [1]}).write_parquet(path)
    with pytest.raises(c.ContractError, match="schema invalid"):
        l0.load_followup_actions(path)
    df = pl.DataFrame([action()]).clear()
    df.write_parquet(path)
    assert l0.load_followup_actions(path) == []
    reqs, report = attach([])
    assert reqs[0].management == [] and report["n_read"] == report["n_adopted"] == report["n_discarded"] == 0
