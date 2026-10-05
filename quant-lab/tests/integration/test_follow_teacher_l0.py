"""Synthetic graph + follow silver + synthetic market lake -> L0 artifacts."""
from datetime import timedelta
from decimal import Decimal as D
import json

import polars as pl
import pytest

from quant_lab.data.lake import Layout
from quant_lab.market import contract as c, l0_replay as l0
from tests.market.test_l0_replay import built
from tests.data.l0_fixtures import CHANNEL, SECRET

DEC = pl.Decimal(38, 12)
SCHEMA = {"channel_id": pl.Int64, "message_id": pl.Int64, "available_at": pl.Datetime("us", "UTC"),
          "episode_id": pl.String, "action": pl.String, "fraction": DEC, "stop_price": DEC,
          "to_entry": pl.Boolean, "uncertain": pl.Boolean, "episode_ambiguity": pl.String,
          "evidence": pl.String, "graph_version": pl.String}


def row(episode, kind="close_all", seconds=75, **kw):
    result = dict(channel_id=CHANNEL, message_id=7, available_at=episode["t_dec"] + timedelta(seconds=seconds),
                  episode_id=episode["episode_id"], action=kind, fraction=None, stop_price=None,
                  to_entry=False, uncertain=False, episode_ambiguity=None, evidence=SECRET, graph_version="l0-test")
    result.update(kw)
    return result


def source_rows():
    return l0.load_episodes("l0-test").filter(pl.col("channel_id") == CHANNEL).sort("t_dec").to_dicts()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("version", ["base-v1-timeexit-follow", "base-v1-timeexit-live-follow",
                                     "base-v1-timeexit-w60-follow", "base-v1-timeexit-w60-live-follow"])
def test_follow_cli_default_and_explicit_path_reports_filters_and_executed_commands(built, monkeypatch, explicit, version):
    root, _ = built
    if "live" in version:
        # The synthetic G1 build publishes no bronze message_version; since eaf2b2d a live replay with no root text is a
        # batch error. Supply synthetic root texts (as tests/market/test_live_replay.py does) so this test keeps testing
        # follow-teacher reporting rather than the missing-bronze guard.
        monkeypatch.setattr(l0, "load_message_texts", lambda ids: {value: "BTC 做多 入场 100" for value in ids})
    episodes = source_rows()
    path = root / "explicit.parquet" if explicit else Layout.from_root(None).silver_dir / "followup_action.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [row(episodes[0]), row(episodes[0], seconds=-1), row(episodes[0], uncertain=True),
            row(episodes[1], "reduce", seconds=30), row(episodes[1], "add", seconds=45, fraction=D(2)),
            row(episodes[1], "cancel_pending", seconds=60)]
    pl.DataFrame(rows, schema=SCHEMA).write_parquet(path)
    out = root / "follow"
    args = ["--graph-version", "l0-test", "--channel", str(CHANNEL), "--out", str(out), "--policy", version]
    if explicit:
        args += ["--followup-actions", str(path)]
    assert l0.main(args) == 0
    report = json.loads((out / "summary.json").read_text())
    summary = report["follow_teacher"]
    assert summary["path"] == str(path)
    assert (summary["n_read"], summary["n_adopted"], summary["n_discarded"]) == (6, 4, 2)
    assert summary["discard_reason_counts"] == {"at_or_before_t_dec": 1, "uncertain": 1}
    assert summary["kind_counts"] == {"close_all": 1, "reduce": 1, "move_stop": 0, "cancel_pending": 1, "add": 1}
    assert summary["n_reduce_fraction_defaulted"] == 1
    assert summary["execution"]["n_processed"] == 4 and summary["execution"]["n_executed"] == 2
    assert summary["execution"]["ignored_counts"] == {"ignored_no_position": 1, "ignored_add": 1}
    assert summary["execution"]["n_reduce_fraction_defaulted"] == 1
    table = pl.read_parquet(out / "trades.parquet")
    assert table["n_teacher_actions_executed"].to_list() == [1, 1]
    assert table["last_teacher_action_kind"].to_list() == ["close_all", "cancel_pending"]
    assert set(table["outcome_kind"]) == {"filled_closed", "unfilled_expired"}
    assert report["policy"]["follow_teacher"] is True
    if "live-follow" in version:
        assert report["live_execution_profile"]["n_applied"] == 2
    for artifact in ("summary.json", "summary.md"):
        text = (out / artifact).read_text()
        assert SECRET not in text and "follow_teacher" in text


@pytest.mark.parametrize("empty_channel", [False, True])
def test_missing_follow_file_fails_batch_before_market_loading_or_writing(built, monkeypatch, empty_channel):
    root, _ = built
    def forbidden(*args, **kwargs):
        raise AssertionError("missing follow file must fail before market loading")
    monkeypatch.setattr(l0, "load_market_from_lake", forbidden)
    out = root / "missing"
    with pytest.raises(c.ContractError, match="requires followup actions file"):
        l0.replay(graph_version="l0-test", channel=-1009999999999 if empty_channel else CHANNEL,
                  out=out, policy_version="base-v1-timeexit-follow")
    assert not out.exists()


def test_empty_follow_input_keeps_economics_and_empty_channel_schema(built):
    root, _ = built
    path = Layout.from_root(None).silver_dir / "followup_action.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(schema=SCHEMA).write_parquet(path)
    base = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "base", policy_version="base-v1-timeexit")
    follow = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "follow", policy_version="base-v1-timeexit-follow")
    for key in ("overall", "by", "cumulative_R"):
        assert base[key] == follow[key]
    assert follow["follow_teacher"]["n_read"] == 0
    table = pl.read_parquet(root / "follow" / "trades.parquet")
    assert table["n_teacher_actions_executed"].to_list() == [0, 0] and table["last_teacher_action_kind"].null_count() == 2
    empty = l0.replay(graph_version="l0-test", channel=-1009999999999, out=root / "empty", policy_version="base-v1-timeexit-follow")
    assert empty["overall"]["n_trades"] == 0
    assert empty["follow_teacher"]["execution"]["n_processed"] == 0
    assert pl.read_parquet(root / "empty" / "trades.parquet").schema["last_teacher_action_kind"] == pl.String


def test_non_follow_does_not_read_followup_file(built, monkeypatch):
    root, _ = built
    def forbidden(*args, **kwargs):
        raise AssertionError("non-follow must not read management input")
    monkeypatch.setattr(l0, "load_followup_actions", forbidden)
    report = l0.replay(graph_version="l0-test", channel=CHANNEL, out=root / "off", followup_actions=root / "missing.parquet")
    assert "follow_teacher" not in report
    table = pl.read_parquet(root / "off" / "trades.parquet")
    assert table["n_teacher_actions_executed"].to_list() == [0, 0]
