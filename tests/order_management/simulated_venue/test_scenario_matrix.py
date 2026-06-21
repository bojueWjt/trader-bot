from __future__ import annotations

import pytest

from .scenarios import SCENARIOS, run_scenario


EXPECTED_SCENARIOS = {
    "market_open_sl_tp",
    "limit_timeout_cancel",
    "reprice_fill",
    "partial_fill_keep_remainder",
    "partial_fill_cancel_remainder",
    "stop_loss_fill",
    "batched_take_profit",
    "move_stop",
    "partial_close",
    "full_close",
    "cancel_all",
    "close_all_multi_position",
    "node_restart_recovery",
    "duplicate_out_of_order_no_duplicate_orders",
    "accept_then_unknown_recovery",
}


def test_matrix_declares_required_scenarios() -> None:
    assert set(SCENARIOS) == EXPECTED_SCENARIOS


@pytest.mark.parametrize("scenario_name", sorted(EXPECTED_SCENARIOS))
def test_simulated_exchange_scenario_matrix(scenario_name: str) -> None:
    result = run_scenario(None, scenario_name)

    assert result.name == scenario_name
    assert result.failed_checks == []
    assert result.event_count > 0
