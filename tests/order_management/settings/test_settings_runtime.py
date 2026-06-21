from __future__ import annotations

import pytest

from runtime.settings_consumer import SettingsConsumer
from settings.apply_plan import HotReloadAckTracker, HotReloadTimeout, build_apply_plan


def test_node_consumer_applies_only_schema_valid_versions_and_keeps_old_on_failure() -> None:
    consumer = SettingsConsumer()

    assert consumer.apply_event(
        {
            "event_type": "settings.changed",
            "payload": {
                "version": 1,
                "settings": {"entry": {"max_slippage_bps": 7}},
            },
        }
    ) is True
    assert consumer.status()["desired_version"] == 1
    assert consumer.status()["effective_version"] == 1

    assert consumer.apply_event(
        {
            "event_type": "settings.changed",
            "payload": {
                "version": 2,
                "settings": {"entry": {"max_slippage_bps": -1}},
            },
        }
    ) is False

    status = consumer.status()
    assert status["desired_version"] == 2
    assert status["effective_version"] == 1
    assert status["findings"][0]["severity"] == "critical"
    assert status["findings"][0]["type"] == "settings_apply_failed"


def test_apply_plan_separates_hot_reload_from_restart_required_without_auto_restart() -> None:
    before = {
        "general": {"execution_mode": "shadow"},
        "entry": {"max_slippage_bps": 25},
    }
    after = {
        "general": {"execution_mode": "testnet"},
        "entry": {"max_slippage_bps": 15},
    }

    plan = build_apply_plan(before, after)

    assert plan["auto_restart"] is False
    assert plan["hot_reload"] == [
        {"path": "entry.max_slippage_bps", "before": 25, "after": 15, "apply_mode": "hot_reload"}
    ]
    assert plan["restart_required"] == [
        {
            "path": "general.execution_mode",
            "before": "shadow",
            "after": "testnet",
            "apply_mode": "restart_required",
        }
    ]


def test_hot_reload_ack_tracker_times_out_when_effective_version_does_not_advance() -> None:
    tracker = HotReloadAckTracker(timeout_seconds=0, poll_interval_seconds=0)

    with pytest.raises(HotReloadTimeout):
        tracker.wait_for_ack(2, lambda: 1)

    assert tracker.wait_for_ack(2, lambda: 2) == {"acked": True, "effective_version": 2}
