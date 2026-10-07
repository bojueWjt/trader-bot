from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

import pytest
import read_api
from test_operator_protection_and_dedup import (
    client, _open_body, _post_open, _intent_count, _intent_order_plan,
)


def test_batch_sizing_and_poll_keep_same_two_orders():
    checks = []
    with patch.object(read_api, '_account_financial_state', return_value={
        'real_equity': 1000, 'available_balance': 1000,
    }), patch.object(read_api, '_symbol_risk_ratio', return_value=0.02), patch.object(
        read_api, '_binance_mark_price', return_value=100,
    ):
        total, batch = read_api._size_entry_batch(
            None, 'BTCUSDT', 'account-b', 'long', 'market', None, 90,
            80, None, read_api._operator_caps(), checks, 0,
        )
    op = {'side': 'long', 'entry': {'type': 'market'}, 'stop_loss': 80,
          'entry_batch': batch, 'authorization': {'source_message_id': 'm1'}}
    before = deepcopy(op)
    with patch.object(read_api, '_binance_mark_price', side_effect=AssertionError('must not reprice')):
        first = read_api._execution_order_plan(op, {'max_notional': total}, 'BTCUSDT', 'open_position')
        second = read_api._execution_order_plan(op, {'max_notional': total}, 'BTCUSDT', 'open_position')
    assert first == second
    assert op == before
    assert first['type'] == 'entry_batch'
    assert len(first['tranches']) == 2
    assert first['authorization'] == op['authorization']
    assert Decimal(batch['estimated_stop_risk']) <= Decimal('20.000000001')


def test_single_market_quantity_leaves_room_for_node_mark():
    op = {'side': 'long', 'entry': {'type': 'market'}, 'stop_loss': 80}
    with patch.object(read_api, '_binance_mark_price', return_value=100):
        plan = read_api._execution_order_plan(op, {'max_notional': 1000}, 'BTCUSDT', 'open_position')
    quantity = Decimal(plan['quantity'])
    assert quantity * Decimal('100.49') <= 1000
    assert quantity * Decimal('100') > Decimal('994')


def test_batch_bad_version_never_falls_back_to_single():
    with pytest.raises(ValueError):
        read_api._execution_order_plan({'entry_batch': {'version': '999'}}, {})


def test_http_batch_persists_once_and_rejects_changed_second_price(client, migrated_db, monkeypatch):
    body = _open_body('operator-batch-integration', stop_loss=1.4)
    body.pop('notional_usdt')
    body['entry'] = {'type': 'market', 'second_price': 1.5}
    monkeypatch.setattr(read_api, '_binance_mark_price', lambda symbol: 1.6)
    response = _post_open(client, body)
    assert response.status_code == 200, response.text
    payload = response.json()
    persisted = _intent_order_plan(migrated_db, payload['intent_id'])
    assert len(persisted['entry_batch']['tranches']) == 2
    assert persisted['entry_batch']['allocation'] == 'equal_notional'
    monkeypatch.setattr(read_api, '_binance_mark_price', lambda symbol: 1.8)
    replay = _post_open(client, body)
    assert replay.status_code == 200, replay.text
    assert replay.json()['intent_id'] == payload['intent_id']
    assert _intent_order_plan(migrated_db, payload['intent_id']) == persisted
    body['entry']['second_price'] = 1.51
    conflict = _post_open(client, body)
    assert conflict.status_code == 409, conflict.text
    assert _intent_count(migrated_db) == 1


def _leg(seq, price, kind='limit'):
    return {
        'seq': seq, 'tranche_id': f'e{seq}', 'type': kind,
        'quantity': '1', 'sizing_price': str(price), 'price': price,
    }


def test_execution_plan_accepts_two_or_three_and_rejects_other_shapes():
    def plan(tranches):
        return read_api._execution_order_plan({
            'side': 'short', 'stop_loss': 3167,
            'entry_batch': {
                'version': '1', 'allocation': 'equal_notional',
                'estimated_stop_risk': '1', 'tranches': tranches,
            },
        }, {})

    two = plan([_leg(1, 100, 'market'), _leg(2, 90)])
    assert two['type'] == 'entry_batch'
    assert len(two['tranches']) == 2
    three = plan([_leg(1, 2877), _leg(2, 2967), _leg(3, 3067)])
    assert [row['price'] for row in three['tranches']] == [2877, 2967, 3067]
    for tranches in (
        [_leg(1, 2877)],
        [_leg(index, 2877) for index in range(1, 5)],
        [_leg(1, 2877), _leg(2, 2967), _leg(3, 3067, 'market')],
    ):
        with pytest.raises(ValueError):
            plan(tranches)


def test_three_leg_eth_sizes_once_and_splits_equal_notional():
    checks = []
    calls = {'n': 0}
    real = read_api._size_open_order

    def counted(*args, **kwargs):
        calls['n'] += 1
        return real(*args, **kwargs)

    with patch.object(read_api, '_account_financial_state', return_value={
        'real_equity': 10000, 'available_balance': 10000,
    }), patch.object(read_api, '_symbol_risk_ratio', return_value=0.02), patch.object(
        read_api, '_size_open_order', side_effect=counted,
    ):
        total, batch = read_api._size_entry_batch(
            None, 'ETHUSDT', 'account-b', 'short', 'limit', 2877, 2967,
            3167, None, read_api._operator_caps(), checks, 0, 3067,
        )
    assert calls['n'] == 1
    amounts = [
        Decimal(leg['quantity']) * Decimal(leg['sizing_price'])
        for leg in batch['tranches']
    ]
    assert len(amounts) == 3
    assert max(amounts) - min(amounts) < Decimal('0.000001')
    assert sum(amounts) <= Decimal(str(total))
    assert Decimal(batch['estimated_stop_risk']) <= Decimal('200')


def test_http_three_leg_persists_once_and_rejects_changed_third_price(client, migrated_db, monkeypatch):
    body = _open_body('operator-eth-three-entry', stop_loss=3167)
    body.pop('notional_usdt')
    body['symbol'] = 'ETHUSDT'
    body['side'] = 'short'
    body['entry'] = {
        'type': 'limit', 'price': 2877, 'second_price': 2967, 'third_price': 3067,
    }
    response = _post_open(client, body)
    assert response.status_code == 200, response.text
    payload = response.json()
    persisted = _intent_order_plan(migrated_db, payload['intent_id'])
    assert len(persisted['entry_batch']['tranches']) == 3
    assert [leg['type'] for leg in persisted['entry_batch']['tranches']] == ['limit', 'limit', 'limit']
    assert persisted['entry']['third_price'] == 3067
    assert _intent_count(migrated_db) == 1
    replay = _post_open(client, body)
    assert replay.status_code == 200, replay.text
    assert replay.json()['intent_id'] == payload['intent_id']
    assert _intent_count(migrated_db) == 1
    body['entry']['third_price'] = 3068
    conflict = _post_open(client, body)
    assert conflict.status_code == 409, conflict.text
    assert _intent_count(migrated_db) == 1


@pytest.mark.parametrize('mutate,detail', [
    (lambda body: body['entry'].pop('second_price'), 'third_price requires second_price'),
    (lambda body: body['entry'].update({'type': 'zone', 'price_min': 2800, 'price_max': 3100}), 'market/limit'),
    (lambda body: body.update({'action': 'add_position'}), 'open_position'),
    (lambda body: body.update({'canary_permit_id': '00000000-0000-4000-8000-000000000001'}), 'canary'),
    (lambda body: body.pop('stop_loss'), 'stop_loss'),
    (lambda body: body['entry'].update({'third_price': 0}), 'must be > 0'),
    (lambda body: body['entry'].update({'third_price': 'NaN'}), 'must be > 0'),
    (lambda body: body.update({'stop_loss': 3000}), 'above every entry'),
])
def test_http_three_leg_boundaries_persist_nothing(client, migrated_db, mutate, detail):
    body = _open_body('operator-eth-three-reject', stop_loss=3167)
    body.pop('notional_usdt')
    body['symbol'] = 'ETHUSDT'
    body['side'] = 'short'
    body['entry'] = {
        'type': 'limit', 'price': 2877, 'second_price': 2967, 'third_price': 3067,
    }
    mutate(body)
    before = _intent_count(migrated_db)
    response = _post_open(client, body)
    assert response.status_code == 400, response.text
    assert detail in response.text
    assert _intent_count(migrated_db) == before
