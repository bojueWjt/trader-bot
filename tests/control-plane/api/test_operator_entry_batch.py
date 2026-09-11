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
