"""Watchdog transitions must survive the actual reporter/actor/disk path."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path
import sys

import pytest

from test_production_lifecycle_rules import BTC, REASON, robot_stop, watchdog_fixture
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_order_denied_projection_chain import (
    _build_protection_event_reporter, _runtime, _stop_background_workers,
    ProjectionActor, ProjectionConfig, JsonExecutionSpool,
)


@pytest.mark.parametrize("without_stash", [False, True])
def test_watchdog_freeze_and_thaw_are_durable_events(tmp_path, without_stash):
    strategy, stash, evidence, _ = watchdog_fixture()
    spool_path = tmp_path / "watchdog.json"
    # Unavailable egress leaves evidence in the existing durable queue.
    sink = SimpleNamespace(post_events=Mock(side_effect=RuntimeError("offline")))
    actor = ProjectionActor(
        config=ProjectionConfig(node_id="node-b", account_id="account-b",
            require_robot_order_ownership=True,
            allowed_instrument_ids=frozenset({"BTCUSDT-PERP.BINANCE"})),
        sink=sink, spool=JsonExecutionSpool(spool_path), now=strategy._now,
    )
    runtime = _runtime(actor)
    reporter = _build_protection_event_reporter(runtime)
    strategy.set_protection_event_reporter(reporter)
    try:
        with patch.object(strategy, "_repair_missing_protection_orders", return_value=False):
            strategy._check_protection_watchdog(str(BTC))
            strategy._check_protection_watchdog(str(BTC))
        assert strategy.symbol_open_freezes["BTCUSDT"] == REASON
        assert runtime.background_workers[0].wait_empty(timeout_seconds=1)
        if without_stash:
            strategy._entry_protection_stash.clear()
        evidence["algo_orders"] = [robot_stop()]
        strategy._release_symbol_watchdog_freezes()
        assert "BTCUSDT" not in strategy.symbol_open_freezes
        assert runtime.background_workers[0].wait_empty(timeout_seconds=1)
    finally:
        _stop_background_workers(runtime)
        strategy.on_stop()
    events = list(JsonExecutionSpool(spool_path).pending_events())
    assert [event.event_type for event in events] == [
        "ProtectionWatchdogSymbolStopped", "ProtectionWatchdogSymbolRecovered",
    ]
    assert [event.payload["action"] for event in events] == [
        "symbol_new_open_frozen", "symbol_watchdog_released",
    ]
    assert all(event.account_id == "account-b" for event in events)
    assert len({event.event_id for event in events}) == 2
