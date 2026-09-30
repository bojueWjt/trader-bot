"""R5/R6: real mirror reads inside strategy callbacks and the existing ack seam."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError
from uuid import UUID

import pytest

from test_production_six_rules import _HistoryHarness
from test_exchange_cancel_adapter import _JsonResponse
from test_intent_execution_strategy_manage_shell import _intent, _pump_durable, INSTRUMENT_ID, _RecordingTerminalWorker, _protection_stash
from runtime.exchange_cancel_adapter import ControlPlaneExchangeStateMirror
from runtime.intent_execution_inbox import IntentExecutionState
from strategy.intent_execution_planner import encode_client_order_id
from strategy.intent_execution_strategy import _intent_execution_identity, _intent_execution_payload


def fixture():
    with patch('test_intent_execution_strategy_manage_shell.ACCOUNT_ID', 'account-d'):
        strategy = _HistoryHarness()
    mirror = ControlPlaneExchangeStateMirror(account_id=str(strategy.config.account_id), node_id='node-d',
        base_url='http://control-plane.invalid', token='test-token')
    adapter = SimpleNamespace(cancel=lambda *_a, **_k: SimpleNamespace(terminal_status='CANCELED'))
    strategy.set_exchange_cancel_adapter(adapter, mirror)
    intent = _intent(account_id='account-d', intent_id=UUID('b64d04b0-1111-4111-8111-111111111111'),
                     order_plan={'stop_price': '82954.5', 'position_side': 'LONG'})
    identity = _intent_execution_identity(intent)
    strategy._intent_execution_inbox.register_received(identity, _intent_execution_payload(intent))
    stash = _protection_stash()
    old_id = 'B3bb0dab5c980478794242f6d4746830531'
    stash['protection_roles'] = {old_id: {'role': 'stop_loss'}}
    stash['protection_ids'] = (old_id,)
    strategy._entry_protection_stash['3bb0dab5-c980-4787-9424-2f6d47468305'] = stash
    acks = []
    strategy.set_denial_reporter(lambda i, d: acks.append((str(i.intent_id), d.reason, d.detail)))
    response = _JsonResponse({'account_id': str(strategy.config.account_id), 'stale': False,
        'payload': {'open_orders': [], 'algo_orders': [dict(symbol='BTCUSDT', position_side='LONG',
            client_order_id='B3bb0dab5c980478794242f6d4746830531', venue_order_id='123',
            order_id='123', type='STOP_MARKET', side='SELL', quantity='0.5',
            trigger_price='73650', reduce_only=True)]}})
    return strategy, mirror, intent, identity, acks, response


def assert_moved(strategy):
    assert len(strategy.submitted_plans) == 1
    assert strategy.submitted_plans[0].trigger_price == '82954.50'
    assert strategy.submitted_plans[0].reduce_only


def test_stale_mirror_lazy_refresh_moves_stop_inside_callback():
    strategy, mirror, intent, identity, acks, response = fixture()
    try:
        # Mirrors can be invalidated between worker refresh and callback execution.
        assert mirror._fresh is False
        with patch('runtime.exchange_cancel_adapter.urllib.request.urlopen', return_value=response) as network:
            strategy._handle_intent_ready(intent, exchange_state_ready=True)
        assert network.call_count == 1
        assert_moved(strategy)
        assert acks == []
    finally:
        strategy.on_stop()


def test_stale_mirror_failure_retries_next_timer_without_premature_rejection():
    strategy, mirror, intent, identity, acks, response = fixture()
    try:
        with patch('runtime.exchange_cancel_adapter.urllib.request.urlopen', side_effect=URLError('offline')):
            strategy._handle_intent_ready(intent, exchange_state_ready=True)
            strategy._on_exchange_state_timer()
        assert strategy.submitted_plans == []
        assert acks == []
        assert strategy._intent_execution_inbox.get(identity).state is IntentExecutionState.RECEIVED
        with patch('runtime.exchange_cancel_adapter.urllib.request.urlopen', return_value=response):
            strategy._on_exchange_state_timer()
        assert_moved(strategy)
        assert acks == []
    finally:
        strategy.on_stop()


@pytest.mark.parametrize('async_refresh', [False, True])
def test_refresh_failure_until_valid_until_reports_rejected(async_refresh):
    strategy, mirror, intent, identity, acks, response = fixture()
    try:
        if async_refresh:
            strategy._complete_intent_refresh(SimpleNamespace(error='offline'),
                {'intent': intent, 'durable_async': True})
        else:
            with patch('runtime.exchange_cancel_adapter.urllib.request.urlopen', side_effect=URLError('offline')):
                strategy._handle_intent_ready(intent, exchange_state_ready=True)
        assert acks == []
        with patch.object(strategy, '_now', return_value=intent.valid_until + timedelta(seconds=1)), patch(
            'runtime.exchange_cancel_adapter.urllib.request.urlopen', side_effect=URLError('offline')):
            strategy._on_exchange_state_timer()
            _pump_durable(strategy)
        assert len(acks) == 1
        assert acks[0][1] == 'expired'
        assert 'exchange_state_refresh_failed' in acks[0][2]
        assert strategy._intent_execution_inbox.get(identity).state is IntentExecutionState.REJECTED
        assert strategy.submitted_plans == []
    finally:
        strategy.on_stop()


def test_final_management_submit_denial_uses_existing_ack_channel():
    strategy, mirror, intent, identity, acks, response = fixture()
    strategy._exchange_cancel_adapter = False
    try:
        with patch('runtime.exchange_cancel_adapter.urllib.request.urlopen', return_value=response):
            strategy._handle_intent_ready(intent, exchange_state_ready=True)
        assert len(acks) == 1
        assert acks[0][1] == 'exchange_cancel_adapter_unavailable'
    finally:
        strategy.on_stop()


def test_invalid_mirror_response_is_retryable_without_callback_exception():
    strategy, mirror, intent, identity, acks, response = fixture()
    try:
        with patch('runtime.exchange_cancel_adapter.urllib.request.urlopen', return_value=_JsonResponse([])):
            strategy._handle_intent_ready(intent, exchange_state_ready=True)
        assert acks == []
        assert strategy.submitted_plans == []
        with patch('runtime.exchange_cancel_adapter.urllib.request.urlopen', return_value=response):
            strategy._on_exchange_state_timer()
        assert_moved(strategy)
    finally:
        strategy.on_stop()


def test_async_refresh_retries_through_existing_periodic_worker_and_durable_lane():
    strategy, mirror, intent, identity, acks, response = fixture()
    worker = _RecordingTerminalWorker()
    strategy.set_terminal_exchange_worker(worker)
    try:
        strategy._complete_intent_refresh(SimpleNamespace(error='offline'),
            {'intent': intent, 'durable_async': True})
        assert acks == []
        strategy._on_exchange_state_timer()
        request = worker.requests[-1]
        with patch('runtime.exchange_cancel_adapter.urllib.request.urlopen', return_value=response):
            mirror.refresh()
            strategy._on_terminal_exchange_result(SimpleNamespace(request_id=request.request_id,
                account_id=str(strategy.config.account_id), error=''))
            _pump_durable(strategy)
        assert_moved(strategy)
        assert acks == []
        assert strategy._intent_execution_inbox.get(identity).state is IntentExecutionState.DISPATCHED
    finally:
        strategy.on_stop()


def test_dispatched_uncertain_management_refresh_is_not_replanned_into_second_order():
    strategy, mirror, intent, identity, acks, response = fixture()
    strategy._intent_execution_inbox.begin_dispatch(identity, (encode_client_order_id(intent.intent_id, 11),))
    from strategy.intent_execution_planner import OrderDenied
    try:
        strategy._report_denial(intent, OrderDenied('exchange_state_refresh_failed', 'cleanup outcome unknown'))
        with patch('runtime.exchange_cancel_adapter.urllib.request.urlopen', return_value=response):
            strategy._on_exchange_state_timer()
        assert strategy.submitted_plans == []
        assert len(acks) == 1
    finally:
        strategy.on_stop()
