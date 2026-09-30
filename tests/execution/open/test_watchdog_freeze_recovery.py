from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest

from test_entry_batch_strategy import batch_fixture
from test_intent_execution_strategy_shell import _LiveEntrySubmitStrategy, _watchdog_stash
from runtime.intent_execution_inbox import IntentExecutionState
from strategy.intent_execution_planner import encode_client_order_id


REASON = 'protection order repair failed twice'
SYMBOL = 'ETHUSDT'
INSTRUMENT = 'ETHUSDT-PERP.BINANCE'


class RecoveryStrategy(_LiveEntrySubmitStrategy):
    @property
    def cache(self):
        return self._test_cache

    @property
    def log(self):
        return self._test_log


def frozen_fixture(tmp_path):
    strategy = RecoveryStrategy(inventory=(), state_dir=tmp_path, environment='testnet')
    key = str(uuid4())
    stash = _watchdog_stash(key)
    stash.update(instrument_id=INSTRUMENT, watchdog_repair_failure_count=2)
    strategy._entry_protection_stash = {key: stash}
    strategy._symbol_open_freezes = {SYMBOL: REASON}
    snapshot = dict(fetched_at=strategy._now(), positions_fetched_at=strategy._now(),
                    positions=[dict(symbol=SYMBOL, position_side='LONG', quantity='0')],
                    regular_orders=[], algo_orders=[])
    strategy._exchange_evidence_provider = SimpleNamespace(
        cached_snapshot=Mock(return_value=snapshot), snapshot=Mock(return_value=snapshot))
    strategy._test_cache = SimpleNamespace(orders=Mock(return_value=[]))
    strategy._test_log = SimpleNamespace(warning=Mock())
    return strategy, snapshot


def tick(strategy, snapshot, seconds=0):
    now = snapshot['fetched_at'] + timedelta(seconds=seconds)
    snapshot.update(fetched_at=now, positions_fetched_at=now)
    with patch.object(strategy, '_now', return_value=now):
        strategy._on_protection_watchdog_timer()


def robot_order(strategy, **values):
    order = dict(client_order_id=encode_client_order_id(uuid4(), 1),
                 instrument_id=INSTRUMENT, status='NEW', filled_qty='0',
                 ts_last=int(strategy._now().timestamp() * 1e9))
    order.update(values)
    return SimpleNamespace(**order)


@pytest.mark.parametrize('case', [
    'flat_new', 'long', 'both', 'short_stash', 'missing', 'stale',
    'read_error', 'cache_error', 'cache_none', 'cache_missing', 'cache_type_error',
    'late_fill', 'late_partial_canceled', 'stash_fill', 'stash_naive', 'stash_invalid',
    'other_reason', 'unknown_side', 'manual_fill', 'old_fill', 'multi_flat',
    'protection_fill', 'protection_frozen', 'pending_fallback',
])
def test_watchdog_flat_recovery(tmp_path, case):
    strategy, snapshot = frozen_fixture(tmp_path)
    stash = next(iter(strategy._entry_protection_stash.values()))
    order = robot_order(strategy)
    strategy.cache.orders.return_value = [order]
    if case in {'long', 'both', 'short_stash'}:
        snapshot['positions'][0].update(quantity='8', position_side={
            'long': 'LONG', 'both': 'BOTH', 'short_stash': 'SHORT'}[case])
    if case in {'short_stash', 'multi_flat'}:
        strategy._entry_protection_stash[str(uuid4())] = dict(stash, entry_side='SELL')
    if case == 'missing':
        strategy._exchange_evidence_provider.cached_snapshot.return_value = None
    if case == 'stale':
        snapshot['regular_orders_fetched_at'] = strategy._now() - timedelta(days=1)
    if case == 'read_error':
        strategy._exchange_evidence_provider.cached_snapshot.side_effect = RuntimeError('unavailable')
    if case in {'cache_error', 'cache_type_error'}:
        strategy.cache.orders.side_effect = RuntimeError('history unavailable') if case == 'cache_error' else TypeError('bad signature')
    if case == 'cache_none':
        strategy.cache.orders.return_value = None
    if case == 'cache_missing':
        strategy._test_cache = None
    if case in {'late_fill', 'late_partial_canceled', 'manual_fill', 'old_fill', 'protection_fill'}:
        order.status = 'CANCELED' if case == 'late_partial_canceled' else 'FILLED'
        order.filled_qty = '1'
        order.ts_last += 1_000_000_000
        if case == 'protection_fill':
            order.client_order_id = encode_client_order_id(uuid4(), 11)
        if case == 'manual_fill':
            order.client_order_id = 'aos_manual'
        if case == 'old_fill':
            order.ts_last -= 2_000_000_000
    if case in {'stash_fill', 'stash_naive', 'stash_invalid'}:
        stash['last_entry_fill_at'] = {
            'stash_fill': (strategy._now() + timedelta(seconds=1)).isoformat(),
            'stash_naive': strategy._now().replace(tzinfo=None).isoformat(),
            'stash_invalid': 'broken',
        }[case]
    if case == 'other_reason':
        strategy._symbol_open_freezes[SYMBOL] = 'durable intent confirmation pending'
    if case == 'unknown_side':
        stash['entry_side'] = 'UNKNOWN'
    if case == 'protection_frozen':
        stash['protection_frozen'] = True
    if case == 'pending_fallback':
        stash['tp_market_fallbacks'] = {'x': {'status': 'submitted', 'remaining_quantity': '1'}}
    order_before = vars(order).copy()
    tick(strategy, snapshot)
    assert SYMBOL in strategy._symbol_open_freezes
    tick(strategy, snapshot, 60)
    recovered = case in {'flat_new', 'manual_fill', 'old_fill', 'multi_flat'}
    assert (SYMBOL not in strategy._symbol_open_freezes) == recovered
    if recovered:
        assert stash['watchdog_repair_failure_count'] == 0
    if case == 'protection_frozen':
        assert stash['protection_frozen'] is True
    if case == 'pending_fallback':
        assert stash['tp_market_fallbacks']['x']['status'] == 'submitted'
    assert vars(order) == order_before


@pytest.mark.parametrize('timestamp', [0, -1, None, '123', 1.5, True])
@pytest.mark.parametrize('status,quantity', [('FILLED', '0'), ('PARTIALLY_FILLED', '0'), ('CANCELED', '1')])
def test_invalid_robot_fill_timestamp_blocks_thaw(tmp_path, timestamp, status, quantity):
    strategy, snapshot = frozen_fixture(tmp_path)
    strategy.cache.orders.return_value = [
        robot_order(strategy, status=status, filled_qty=quantity, ts_last=timestamp)]
    tick(strategy, snapshot)
    tick(strategy, snapshot, 60)
    assert strategy._symbol_open_freezes[SYMBOL] == REASON
    assert SYMBOL not in strategy._watchdog_flat_observations


def test_two_independent_snapshots_and_intervening_fill_reset(tmp_path):
    strategy, snapshot = frozen_fixture(tmp_path)
    tick(strategy, snapshot)
    first = snapshot['positions_fetched_at']
    tick(strategy, snapshot)
    tick(strategy, snapshot, 59)
    assert strategy._symbol_open_freezes[SYMBOL] == REASON
    assert strategy._watchdog_flat_observations[SYMBOL] == first
    order = robot_order(strategy, status='FILLED', filled_qty='1',
                        ts_last=int((first + timedelta(seconds=30)).timestamp() * 1e9))
    strategy.cache.orders.return_value = [order]
    tick(strategy, snapshot, 1)
    assert strategy._symbol_open_freezes[SYMBOL] == REASON
    assert SYMBOL not in strategy._watchdog_flat_observations
    tick(strategy, snapshot, 1)
    assert strategy._watchdog_flat_observations[SYMBOL] == snapshot['positions_fetched_at']
    tick(strategy, snapshot, 60)
    assert SYMBOL not in strategy._symbol_open_freezes


@pytest.mark.parametrize('failure', ['position', 'history', 'stash_time', 'protection', 'fallback', 'snapshot'])
def test_invalid_intermediate_evidence_clears_first_observation(tmp_path, failure):
    strategy, snapshot = frozen_fixture(tmp_path)
    stash = next(iter(strategy._entry_protection_stash.values()))
    tick(strategy, snapshot)
    assert SYMBOL in strategy._watchdog_flat_observations
    if failure == 'position':
        snapshot['positions'][0]['quantity'] = '1'
    if failure == 'history':
        strategy.cache.orders.side_effect = RuntimeError('unavailable')
    if failure == 'stash_time':
        stash['last_entry_fill_at'] = 'invalid'
    if failure == 'protection':
        stash['protection_frozen'] = True
    if failure == 'fallback':
        stash['tp_market_fallbacks'] = {'x': {'status': 'submitted', 'remaining_quantity': '1'}}
    if failure == 'snapshot':
        strategy._exchange_evidence_provider.cached_snapshot.return_value = None
    tick(strategy, snapshot, 30)
    assert SYMBOL not in strategy._watchdog_flat_observations
    assert SYMBOL in strategy._symbol_open_freezes
    snapshot['positions'][0]['quantity'] = '0'
    strategy.cache.orders.side_effect = None
    stash.pop('last_entry_fill_at', None)
    stash.pop('protection_frozen', None)
    stash.pop('tp_market_fallbacks', None)
    strategy._exchange_evidence_provider.cached_snapshot.return_value = snapshot
    tick(strategy, snapshot, 30)
    assert SYMBOL in strategy._symbol_open_freezes
    tick(strategy, snapshot, 60)
    assert SYMBOL not in strategy._symbol_open_freezes


def test_denial_warning_is_per_symbol_rate_limited(tmp_path):
    strategy, snapshot = frozen_fixture(tmp_path)
    strategy.cache.orders.side_effect = RuntimeError('history unavailable')
    tick(strategy, snapshot)
    tick(strategy, snapshot, 60)
    strategy.log.warning.assert_called_once()
    assert 'history unavailable' in strategy.log.warning.call_args.args[0]
    tick(strategy, snapshot, 540)
    assert strategy.log.warning.call_count == 2


def test_manual_flat_then_robot_position_refreezes_after_two_failed_repairs(tmp_path):
    strategy, snapshot = frozen_fixture(tmp_path)
    key, stash = next(iter(strategy._entry_protection_stash.items()))
    entry = robot_order(strategy, client_order_id=encode_client_order_id(UUID(key), 1))
    strategy.cache.orders.return_value = [entry]
    snapshot['regular_orders'] = [
        dict(symbol=SYMBOL, position_side='LONG', client_order_id=entry.client_order_id,
             reduce_only=False, order_type='LIMIT')]
    # 9/24: a manual position kept the symbol occupied while robot entry stayed NEW.
    snapshot['positions'][0]['quantity'] = '2'
    tick(strategy, snapshot)
    assert SYMBOL not in strategy._watchdog_flat_observations
    snapshot['positions'][0]['quantity'] = '0'
    tick(strategy, snapshot, 1)
    assert SYMBOL in strategy._symbol_open_freezes
    tick(strategy, snapshot, 60)
    assert SYMBOL not in strategy._symbol_open_freezes
    assert entry.status == 'NEW'
    assert stash['watchdog_repair_failure_count'] == 0
    # A subsequent attributed fill and matching real venue/cache position need protection again.
    now = snapshot['fetched_at'] + timedelta(seconds=1)
    entry.status = 'FILLED'
    entry.filled_qty = '1'
    entry.ts_last = int(now.timestamp() * 1e9)
    stash['last_entry_fill_at'] = now.isoformat()
    snapshot['positions'][0]['quantity'] = '1'
    snapshot['regular_orders'] = []
    position = SimpleNamespace(instrument_id=INSTRUMENT, side='LONG', quantity='1',
                               position_id=INSTRUMENT + '-LONG', entry_price='100')
    with patch.object(strategy, '_cache_positions', return_value=(position,)), patch.object(
        strategy, '_submit_order_plan', return_value=False,
    ) as submit:
        tick(strategy, snapshot, 1)
        assert SYMBOL not in strategy._symbol_open_freezes
        assert stash['watchdog_repair_failure_count'] == 1
        assert submit.call_count == 1
        tick(strategy, snapshot, 1)
        assert strategy._symbol_open_freezes[SYMBOL] == REASON
        assert stash['watchdog_repair_failure_count'] == 2
        assert submit.call_count == 2


@pytest.mark.parametrize('case,expected', [
    ('approved_expired', True), ('created_expired', True), ('not_due', False),
    ('halted', False), ('no_anchor', False), ('created_priority', False),
    ('explicit_priority', False), ('explicit_expired', True), ('naive', False),
    ('batch_no_fallback', False), ('batch_explicit', True), ('wrong_account', False),
    ('wrong_action', False), ('zone_add', True),
    ('rejected_naive_anchor', True), ('rejected_bad_anchor', True),
    ('closing_naive_anchor', True), ('naive_anchor', False), ('bad_anchor', False),
    ('hours_nan', False), ('hours_inf', False), ('hours_zero', False), ('hours_negative', False),
    ('rejected_hours_nan', True), ('rejected_hours_inf', True),
    ('rejected_hours_zero', True), ('rejected_hours_negative', True),
])
def test_entry_expiry_anchor_and_cancel_scope(tmp_path, case, expected):
    strategy, _, _ = batch_fixture(tmp_path)
    key = uuid4()
    entry = encode_client_order_id(key, 1)
    ids = (entry, encode_client_order_id(key, 9), encode_client_order_id(key, 10),
           encode_client_order_id(key, 11), encode_client_order_id(uuid4(), 1), 'aos_manual')
    now = strategy._now()
    payload = {'order_plan': {'type': 'limit', 'expire_hours': 48},
               'approved_at': (now - timedelta(hours=49)).isoformat()}
    if case in {'created_expired', 'created_priority'}:
        payload['created_at'] = (now - timedelta(hours=49 if case == 'created_expired' else 1)).isoformat()
    if 'naive_anchor' in case:
        payload['approved_at'] = (now - timedelta(hours=49)).replace(tzinfo=None).isoformat()
    if 'bad_anchor' in case:
        payload['approved_at'] = 'broken'
    if 'hours_' in case:
        payload['order_plan']['expire_hours'] = {
            'nan': 'NaN', 'inf': 'Infinity', 'zero': 0, 'negative': -1,
        }[case.rsplit('_', 1)[1]]
    if case == 'not_due':
        payload['approved_at'] = (now - timedelta(hours=47)).isoformat()
    if case == 'no_anchor':
        payload.pop('approved_at')
        payload['valid_until'] = (now - timedelta(days=5)).isoformat()
    if case in {'explicit_priority', 'explicit_expired', 'batch_explicit', 'naive'}:
        expiry = now + timedelta(hours=1) if case == 'explicit_priority' else now - timedelta(hours=1)
        payload['order_plan']['entry_expires_at'] = (expiry.replace(tzinfo=None) if case == 'naive' else expiry).isoformat()
    if case.startswith('batch_'):
        payload['order_plan']['type'] = 'entry_batch'
    if case == 'zone_add':
        payload['order_plan']['type'] = 'zone_ladder'
    action = 'add_position' if case == 'zone_add' else 'open_position'
    record = SimpleNamespace(account_id='other' if case == 'wrong_account' else str(strategy.config.account_id),
                             action='close_position' if case == 'wrong_action' else action,
                             intent_payload=payload, intent_id=str(key), state=IntentExecutionState.DISPATCHED,
                             client_order_ids=ids, instrument_id=INSTRUMENT)
    if case.startswith('rejected_'):
        record.state = IntentExecutionState.REJECTED
    if case.startswith('closing_'):
        strategy._entry_protection_stash[str(key)] = {'batch_closing': True}
    orders = [SimpleNamespace(client_order_id=cid, reduce_only=False, order_kind='regular', status='NEW') for cid in ids]
    # Even an entry-range ID is excluded when reduce-only or an algo order.
    orders[1].reduce_only = True
    orders.append(SimpleNamespace(client_order_id=entry, reduce_only=False, order_kind='algo'))
    strategy._exchange_state_mirror = SimpleNamespace(orders_for_instrument=lambda _: orders)
    strategy._terminal_exchange_worker = None
    with patch.object(strategy, '_trading_state', return_value='HALTED' if case == 'halted' else 'ACTIVE'), patch.object(
        strategy, '_cancel_via_exchange_adapter',
    ) as cancel:
        strategy._check_entry_expiry([record])
        if expected:
            cancel.assert_called_once_with(INSTRUMENT, entry)
        else:
            cancel.assert_not_called()
        if 'naive_anchor' in case or 'bad_anchor' in case or 'hours_' in case:
            assert any(denial.reason == 'entry_expiry_invalid' for denial in strategy.denials)


@pytest.mark.parametrize('case,expected', [
    ('in_progress', False), ('venue_flat', True), ('venue_missing', True), ('venue_stale', True),
    ('venue_short_only', True), ('no_owned_fill', True), ('exited', True),
    ('rejected', True), ('closing', True), ('not_due', False), ('explicit_in_progress', True),
    ('venue_larger', True), ('venue_smaller', True), ('closed_marker', True),
])
def test_started_plan_remaining_entries_do_not_expire(tmp_path, case, expected):
    strategy, _, _ = batch_fixture(tmp_path)
    key = uuid4()
    ids = tuple(encode_client_order_id(key, seq) for seq in (1, 2, 3))
    now = strategy._now()
    payload = {'order_plan': {'type': 'zone_ladder', 'expire_hours': 48},
               'approved_at': (now - timedelta(hours=47 if case == 'not_due' else 49)).isoformat()}
    if case == 'explicit_in_progress':
        payload['order_plan']['entry_expires_at'] = (now - timedelta(hours=1)).isoformat()
    fills = {} if case == 'no_owned_fill' else {ids[0]: {'role': 'entry', 'trades': {}, 'filled': '0.212'}}
    if case == 'exited':
        fills[encode_client_order_id(key, 11)] = {'role': 'exit', 'trades': {}, 'filled': '0.212'}
    stash = {'instrument_id': INSTRUMENT, 'entry_side': 'BUY', 'batch_entry_ids': list(ids),
             'batch_fills': fills, 'batch_exit_ids': [], 'batch_expires_at': ''}
    if case == 'closing':
        stash['batch_closing'] = True
    if case == 'closed_marker':
        stash['position_closed_at'] = (now - timedelta(minutes=5)).isoformat()
    strategy._entry_protection_stash[str(key)] = stash
    fetched = now - timedelta(seconds=10_000 if case == 'venue_stale' else 1)
    positions = [dict(symbol=SYMBOL, position_side='LONG', quantity='0.212')]
    if case == 'venue_flat':
        positions[0]['quantity'] = '0'
    if case == 'venue_short_only':
        positions[0]['position_side'] = 'SHORT'
    if case in {'venue_larger', 'venue_smaller'}:
        positions[0]['quantity'] = '0.5' if case == 'venue_larger' else '0.1'
    snapshot = dict(fetched_at=fetched, positions_fetched_at=fetched,
                    positions=positions, regular_orders=[], algo_orders=[])
    strategy._exchange_evidence_provider = (
        SimpleNamespace() if case == 'venue_missing'
        else SimpleNamespace(cached_snapshot=Mock(return_value=snapshot)))
    record = SimpleNamespace(account_id=str(strategy.config.account_id), action='open_position',
                             intent_payload=payload, intent_id=str(key),
                             state=IntentExecutionState.REJECTED if case == 'rejected' else IntentExecutionState.DISPATCHED,
                             client_order_ids=ids, instrument_id=INSTRUMENT)
    orders = [SimpleNamespace(client_order_id=cid, reduce_only=False, order_kind='regular', status='NEW')
              for cid in ids[1:]]
    strategy._exchange_state_mirror = SimpleNamespace(orders_for_instrument=lambda _: orders)
    strategy._terminal_exchange_worker = None
    with patch.object(strategy, '_trading_state', return_value='ACTIVE'), patch.object(
        strategy, '_cancel_via_exchange_adapter',
    ) as cancel:
        strategy._check_entry_expiry([record])
    if expected:
        assert {call.args for call in cancel.call_args_list} == {(INSTRUMENT, ids[1]), (INSTRUMENT, ids[2])}
    else:
        cancel.assert_not_called()


@pytest.mark.parametrize('in_progress', [True, False])
def test_explicit_stash_batch_expiry_still_closes_started_plan(tmp_path, in_progress):
    strategy, _, _ = batch_fixture(tmp_path)
    key = uuid4()
    ids = [encode_client_order_id(key, seq) for seq in (1, 2)]
    now = strategy._now()
    stash = {'instrument_id': INSTRUMENT, 'entry_side': 'BUY', 'batch_entry_ids': ids,
             'batch_fills': {ids[0]: {'role': 'entry', 'trades': {}, 'filled': '1'}},
             'batch_exit_ids': [], 'batch_expires_at': (now - timedelta(hours=1)).isoformat()}
    strategy._entry_protection_stash = {str(key): stash}
    snapshot = dict(fetched_at=now, positions_fetched_at=now, regular_orders=[], algo_orders=[],
                    positions=[dict(symbol=SYMBOL, position_side='LONG', quantity='1' if in_progress else '0')])
    strategy._exchange_evidence_provider = SimpleNamespace(cached_snapshot=Mock(return_value=snapshot))
    with patch.object(strategy, '_trading_state', return_value='ACTIVE'), patch.object(
        strategy, '_cancel_batch_entries',
    ) as cancel:
        strategy._check_entry_expiry([])
    assert stash.get('batch_closing') is True
    assert cancel.called
