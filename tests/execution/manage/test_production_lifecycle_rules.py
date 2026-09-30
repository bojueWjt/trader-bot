"""R2/R3 production sequences; assert venue actions, not old proof helpers."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest

from test_production_six_rules import _HistoryHarness
from test_intent_execution_strategy_manage_shell import INSTRUMENT_ID, _protection_stash
from runtime.intent_execution_inbox import IntentExecutionState
from strategy.intent_execution_planner import encode_client_order_id

GOLD = UUID('9a6c4b89-1111-4111-8111-111111111111')
BTC = UUID('3bb0dab5-c980-4787-9424-2f6d47468305')
REASON = 'protection order repair failed twice'


def snapshot(strategy, symbol='BTCUSDT', quantity='0.482'):
    return dict(fetched_at=strategy._now(), positions_fetched_at=strategy._now(),
                positions=[dict(symbol=symbol, position_side='LONG', quantity=quantity)],
                regular_orders=[], algo_orders=[])


def attach_snapshot(strategy, evidence):
    strategy.set_exchange_evidence_provider(SimpleNamespace(
        cached_snapshot=lambda **kwargs: evidence, snapshot=lambda **kwargs: evidence))


@pytest.mark.parametrize('case,expected', [
    ('gold_adds_open', False), ('flat', True), ('own_stop', True),
    ('never_filled', True), ('default_48', True), ('explicit', True),
    ('stale', False), ('missing', False), ('add_filled', False),
    ('other_book', True), ('both_nonzero', False), ('partial_exit', False),
])
def test_gold_plan_entry_lifetime(case, expected):
    with patch('test_intent_execution_strategy_manage_shell.ACCOUNT_ID', 'account-c'):
        strategy = _HistoryHarness()
    instrument = 'XAUUSDT-PERP.BINANCE'
    ids = [encode_client_order_id(GOLD, seq) for seq in (1, 2, 3)]
    fills = {ids[0]: {'role': 'entry', 'filled': '0.212', 'trades': {}}}
    stash = dict(instrument_id=instrument, entry_side='BUY', batch_entry_ids=ids,
                 batch_fills=fills, batch_exit_ids=[], batch_expires_at='')
    strategy._entry_protection_stash[str(GOLD)] = stash
    # Add fills enlarge the venue book without enlarging GOLD's owned ledger.
    for prefix, qty in [('a8fd581a', '0.167'), ('95251e1f', '0.103')]:
        key = UUID(prefix+'-2222-4222-8222-222222222222')
        strategy._entry_protection_stash[str(key)] = dict(
            instrument_id=instrument, entry_side='BUY', action='add_position',
            batch_fills={encode_client_order_id(key, 1): {'role': 'entry', 'filled': qty, 'trades': {}}})
    evidence = snapshot(strategy, 'XAUUSDT')
    attach_snapshot(strategy, evidence)
    plan = {'type': 'zone_ladder', 'expire_hours': 48, 'side': 'buy'}
    if case in {'never_filled', 'default_48'}:
        fills.clear()
    if case == 'default_48':
        plan.pop('expire_hours')
    if case == 'own_stop':
        strategy._record_batch_fill(SimpleNamespace(client_order_id=encode_client_order_id(GOLD, 11),
            last_qty='0.212', trade_id='production-stop-fill'))
    if case == 'partial_exit':
        fills[encode_client_order_id(GOLD, 11)] = {'role': 'exit', 'filled': '0.100', 'trades': {}}
    if case == 'flat':
        evidence['positions'][0]['quantity'] = '0'
    if case == 'other_book':
        evidence['positions'][0]['position_side'] = 'SHORT'
    if case == 'both_nonzero':
        evidence['positions'][0]['position_side'] = 'BOTH'
    if case == 'stale':
        evidence['fetched_at'] -= timedelta(minutes=1)
        evidence['positions_fetched_at'] = evidence['fetched_at']
    if case == 'missing':
        strategy.set_exchange_evidence_provider(False)
    if case == 'explicit':
        plan['entry_expires_at'] = (strategy._now()-timedelta(seconds=1)).isoformat()
    if case == 'add_filled':
        stash['action'] = 'add_position'
    record = SimpleNamespace(account_id=str(strategy.config.account_id),
        action='add_position' if case == 'add_filled' else 'open_position',
        intent_id=str(GOLD), instrument_id=instrument, client_order_ids=ids,
        state=IntentExecutionState.DISPATCHED,
        intent_payload={'order_plan': plan, 'approved_at': (strategy._now()-timedelta(hours=49)).isoformat()})
    orders = [SimpleNamespace(client_order_id=cid, reduce_only=False, order_kind='regular') for cid in ids[1:]]
    orders += [SimpleNamespace(client_order_id=encode_client_order_id(GOLD, 11), reduce_only=True, order_kind='algo'),
               SimpleNamespace(client_order_id='aos_manual', reduce_only=False, order_kind='regular'),
               SimpleNamespace(client_order_id='stToAg_manual', reduce_only=False, order_kind='regular')]
    strategy._exchange_state_mirror = SimpleNamespace(orders_for_instrument=lambda _: orders)
    try:
        with patch.object(strategy, '_cancel_via_exchange_adapter', return_value=True) as cancel:
            strategy._check_entry_expiry([record])
        assert {call.args[1] for call in cancel.call_args_list} == (set(ids[1:]) if expected else set())
        assert not stash.get('batch_closing')  # Expiry or stale evidence is never a permanent closing marker.
    finally:
        strategy.on_stop()


def watchdog_fixture():
    with patch('test_intent_execution_strategy_manage_shell.ACCOUNT_ID', 'account-b'):
        strategy = _HistoryHarness(positions=[SimpleNamespace(id='P-1', instrument_id=INSTRUMENT_ID,
            side='LONG', quantity='0.062', entry_price='75000')])
    stash = _protection_stash()
    stash.update(stop_loss='75793', take_profits=('80000',),
        protection_ids=(encode_client_order_id(uuid4(), 31),),
        batch_fills={encode_client_order_id(BTC, 1): {'role': 'entry', 'filled': '0.015', 'trades': {}}})
    strategy._entry_protection_stash[str(BTC)] = stash
    evidence = snapshot(strategy, quantity='0.062')
    attach_snapshot(strategy, evidence)
    events = []
    strategy.set_protection_event_reporter(lambda event: events.append(event) or True)
    return strategy, stash, evidence, events


def robot_stop(**overrides):
    row = dict(symbol='BTCUSDT', position_side='LONG', reduce_only=True,
               client_order_id='B3bb0dab5c980478794242f6d4746830531', order_type='STOP_MARKET',
               trigger_price='73650', quantity='0.062')
    row.update(overrides)
    return row


def test_account_b_existing_different_price_robot_stop_thaws_without_repair():
    strategy, stash, evidence, events = watchdog_fixture()
    strategy._symbol_open_freezes['BTCUSDT'] = REASON
    stash['watchdog_repair_failure_count'] = 2
    evidence['algo_orders'] = [robot_stop(), robot_stop(order_type='TAKE_PROFIT_MARKET',
        client_order_id='B3bb0dab5c980478794242f6d4746830535', trigger_price='80000')]
    try:
        with patch.object(strategy, '_repair_missing_protection_orders') as repair:
            strategy._check_protection_watchdog(str(BTC))
        repair.assert_not_called()
        assert 'BTCUSDT' not in strategy.symbol_open_freezes
        assert stash['stop_loss'] == '75793'
        assert any(e['payload']['action'] == 'symbol_watchdog_released' for e in events)
    finally:
        strategy.on_stop()


@pytest.mark.parametrize('unprotected', [
    None,
    {'client_order_id': 'aos_manual'}, {'client_order_id': 'stToAg_manual'},
    {'position_side': 'SHORT'}, {'reduce_only': False}, {'order_type': 'TAKE_PROFIT_MARKET'},
])
def test_missing_robot_stop_two_failures_freeze_then_actual_stop_thaws(unprotected):
    strategy, stash, evidence, events = watchdog_fixture()
    evidence['algo_orders'] = [robot_stop(**unprotected)] if unprotected is not None else []
    try:
        with patch.object(strategy, '_repair_missing_protection_orders', return_value=False) as repair:
            strategy._check_protection_watchdog(str(BTC))
            assert 'BTCUSDT' not in strategy.symbol_open_freezes
            strategy._check_protection_watchdog(str(BTC))
            assert strategy.symbol_open_freezes['BTCUSDT'] == REASON
            assert repair.call_count == 2
            evidence['algo_orders'].append(robot_stop(client_order_id=encode_client_order_id(uuid4(), 11)))
            strategy._check_protection_watchdog(str(BTC))
            assert repair.call_count == 2
        assert 'BTCUSDT' not in strategy.symbol_open_freezes
        assert [e['payload']['action'] for e in events] == ['symbol_new_open_frozen', 'symbol_watchdog_released']
    finally:
        strategy.on_stop()
