"""Pre-deploy review fixes for node-release-20260930.

1. A stale mirror never blocks or defers a reduce-only close; entry cancels are best effort.
2. Deferred management is re-planned (never replayed), one per instrument per timer tick.
3. A batch plan whose own protected quantity is zero is not repaired, counted, or frozen.
4. Only an exit-side robot STOP protects a book.
5. A failed lazy mirror refresh fails fast for 10s instead of blocking the loop again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError
from uuid import UUID

import pytest

import test_intent_execution_strategy_manage_shell as shell
from test_intent_execution_strategy_manage_shell import (
    INSTRUMENT_ID,
    _RecordingTerminalWorker,
    _intent as _shell_intent,
    _pump_durable,
)
from test_production_six_rules import (
    ACCOUNT_D,
    CLOSE_9A4B8FF9,
    PLAN_64F425AD,
    PLAN_CA73523A,
    _complete_latest_terminal,
    _historical_cancel_ids,
    _HistoryHarness,
)
from runtime.exchange_cancel_adapter import ControlPlaneExchangeStateMirror, ExchangeCancelError
from strategy.intent_execution_planner import encode_client_order_id
from strategy.intent_execution_strategy import _intent_execution_identity, _intent_execution_payload


class _StaleMirror:
    """Every read fails the way an unreachable control-plane mirror does."""

    def refresh(self):
        raise ExchangeCancelError("exchange state mirror refresh failed: offline")

    def orders_for_instrument(self, instrument_id):
        raise ExchangeCancelError("exchange state mirror is not fresh")

    def find_order(self, instrument_id, client_order_id):
        raise ExchangeCancelError("exchange state mirror is not fresh")


class _FlakyMirror(_StaleMirror):
    """Fresh for the first `ok` reads (planning), then unreachable (entry-cancel step)."""

    def __init__(self, ok):
        self.ok = ok
        self.reads = 0

    def orders_for_instrument(self, instrument_id):
        self.reads += 1
        if self.reads <= self.ok:
            return ()
        raise ExchangeCancelError("exchange state mirror is not fresh")


def _account_d_close_harness(mirror):
    position = SimpleNamespace(id=f"{INSTRUMENT_ID}-LONG", instrument_id=INSTRUMENT_ID,
                               side="LONG", quantity="0.210", entry_price="80000")
    strategy = _HistoryHarness(positions=[position], orders=[])
    acks = []
    strategy.set_denial_reporter(lambda intent, denial: acks.append((intent, denial)))
    strategy.set_exchange_cancel_adapter(False, mirror)
    worker = _RecordingTerminalWorker()
    strategy.set_terminal_exchange_worker(worker)
    for owner, qty in ((PLAN_64F425AD, "0.100"), (PLAN_CA73523A, "0.110")):
        open_intent = _shell_intent(
            intent_id=owner, action="open_position",
            order_plan={"type": "limit", "side": "buy", "quantity": qty, "price": "80000"},
            target_position_id=None,
        )
        identity = _intent_execution_identity(open_intent)
        strategy._intent_execution_inbox.register_received(identity, _intent_execution_payload(open_intent))
        strategy._intent_execution_inbox.begin_dispatch(identity, (encode_client_order_id(owner, 1),))
    close_intent = _shell_intent(
        intent_id=CLOSE_9A4B8FF9, action="close_position",
        order_plan={"type": "market", "position_side": "LONG"},
        target_position_id=f"{INSTRUMENT_ID}-LONG",
    )
    return strategy, worker, acks, close_intent


def test_mirror_lost_after_planning_still_closes_without_replaying_a_deferred_plan():
    previous_account = shell.ACCOUNT_ID
    shell.ACCOUNT_ID = ACCOUNT_D
    mirror = _FlakyMirror(ok=1)
    strategy, worker, acks, close_intent = _account_d_close_harness(mirror)
    try:
        strategy._handle_intent(close_intent)
        _complete_latest_terminal(strategy, error="")
        assert mirror.reads >= 2, "entry-cancel step must have met the unreachable mirror"
        assert len(strategy.submitted_plans) == 1, (strategy.submitted_plans, strategy.denials, acks)
        assert strategy.submitted_plans[0].quantity == "0.210"
        assert strategy.submitted_plans[0].reduce_only
        assert strategy._deferred_management_intents == {}
        assert any(d.reason == "close_entry_cancel_skipped" for d in strategy.denials)
        assert _historical_cancel_ids(worker) == set()
        assert acks == []
    finally:
        shell.ACCOUNT_ID = previous_account
        strategy.on_stop()


def test_unreachable_mirror_defers_close_then_replans_when_it_returns():
    previous_account = shell.ACCOUNT_ID
    shell.ACCOUNT_ID = ACCOUNT_D
    mirror = _FlakyMirror(ok=0)
    strategy, worker, acks, close_intent = _account_d_close_harness(mirror)
    try:
        strategy._handle_intent(close_intent)
        _complete_latest_terminal(strategy, error="")
        assert strategy.submitted_plans == []
        assert acks == [], "a transient mirror outage inside valid_until is not a rejection"
        assert str(close_intent.intent_id) in strategy._deferred_management_intents
        mirror.ok = 10 ** 6  # mirror is back
        strategy._retry_deferred_management_intents()
        _pump_durable(strategy)
        assert len(strategy.submitted_plans) == 1, (strategy.submitted_plans, strategy.denials, acks)
        assert strategy.submitted_plans[0].quantity == "0.210"
        assert strategy.submitted_plans[0].reduce_only
        assert acks == []
    finally:
        shell.ACCOUNT_ID = previous_account
        strategy.on_stop()


def test_deferred_management_replans_one_per_instrument_per_tick():
    strategy = _HistoryHarness()
    try:
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        first = SimpleNamespace(intent_id=UUID(int=1), instrument_id=INSTRUMENT_ID, valid_until=future)
        second = SimpleNamespace(intent_id=UUID(int=2), instrument_id=INSTRUMENT_ID, valid_until=future)
        other = SimpleNamespace(intent_id=UUID(int=3), instrument_id="ETHUSDT-PERP.BINANCE", valid_until=future)
        for intent in (first, second, other):
            strategy._deferred_management_intents[str(intent.intent_id)] = (intent, {"stale": "continuation"})
        calls = []
        with patch.object(strategy, "_handle_intent_ready", side_effect=lambda i, **_k: calls.append(i)):
            strategy._retry_deferred_management_intents()
            assert [str(i.intent_id) for i in calls] == [str(first.intent_id), str(other.intent_id)]
            assert list(strategy._deferred_management_intents) == [str(second.intent_id)]
            strategy._retry_deferred_management_intents()
        assert [str(i.intent_id) for i in calls][-1] == str(second.intent_id)
        assert strategy._deferred_management_intents == {}
    finally:
        strategy.on_stop()


def _book_snapshot(*, quantity="0.5", stops=()):
    return {
        "positions": [{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": quantity}],
        "regular_orders": [],
        "algo_orders": list(stops),
    }


def test_zero_owned_batch_plan_is_not_repaired_counted_or_frozen():
    strategy = _HistoryHarness()
    try:
        key = str(PLAN_CA73523A)
        strategy._entry_protection_stash[key] = {
            "instrument_id": INSTRUMENT_ID, "entry_side": "BUY",
            "batch_entry_ids": [encode_client_order_id(PLAN_CA73523A, 1)],
            "batch_fills": {}, "stop_loss": "81000",
        }
        repairs, syncs = [], []
        with patch.object(strategy, "_cached_venue_evidence", return_value=_book_snapshot()), \
                patch.object(strategy, "_defer_flat_or_unknown_protection", return_value=False), \
                patch.object(strategy, "_protection_position", return_value=SimpleNamespace(quantity="0.5")), \
                patch.object(strategy, "_protection_quantity", return_value="0"), \
                patch.object(strategy, "_sync_protection", side_effect=lambda k: syncs.append(k)), \
                patch.object(strategy, "_repair_missing_protection_orders",
                             side_effect=lambda *a, **k: repairs.append(a) or False):
            for _ in range(3):
                strategy._check_protection_watchdog(key)
        assert repairs == []
        assert syncs == [key, key, key]
        assert "watchdog_repair_failure_count" not in strategy._entry_protection_stash[key]
        assert "BTCUSDT" not in strategy._symbol_open_freezes
    finally:
        strategy.on_stop()


@pytest.mark.parametrize("side, protected", [("SELL", True), ("BUY", False), (None, True)])
def test_only_exit_side_robot_stop_protects_long_book(side, protected):
    strategy = _HistoryHarness()
    try:
        row = {"symbol": "BTCUSDT", "position_side": "LONG", "client_order_id": encode_client_order_id(PLAN_CA73523A, 11),
               "reduce_only": True, "type": "STOP_MARKET"}
        if side is not None:
            row["side"] = side
        assert strategy._snapshot_book_has_robot_stop(_book_snapshot(stops=[row]), "BTCUSDT", "LONG") is protected
    finally:
        strategy.on_stop()


def test_failed_lazy_refresh_fails_fast_for_ten_seconds():
    mirror = ControlPlaneExchangeStateMirror(account_id="account-d", node_id="node-d",
                                             base_url="http://control-plane.invalid", token="test-token")
    clock = [1000.0]
    with patch("runtime.exchange_cancel_adapter.time.monotonic", side_effect=lambda: clock[0]), \
            patch("runtime.exchange_cancel_adapter.urllib.request.urlopen",
                  side_effect=URLError("offline")) as network:
        with pytest.raises(ExchangeCancelError):
            mirror.orders_for_instrument(INSTRUMENT_ID)
        calls_after_first = network.call_count
        assert calls_after_first >= 1
        clock[0] += 5
        with pytest.raises(ExchangeCancelError):
            mirror.orders_for_instrument(INSTRUMENT_ID)
        assert network.call_count == calls_after_first
        clock[0] += 6
        with pytest.raises(ExchangeCancelError):
            mirror.orders_for_instrument(INSTRUMENT_ID)
        assert network.call_count > calls_after_first
