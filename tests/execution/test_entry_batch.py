from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/execution-domain'))
from execution_domain.entry_batch import (
    MARKET_QUANTITY_HEADROOM, batch_reference_price, build_entry_batch, record_fill, owned_quantity,
)


def _quote_notionals(plan):
    # Notional each leg was budgeted at; a market leg is sized under its quote.
    return [
        Decimal(leg['quantity']) * Decimal(leg['sizing_price'])
        * (MARKET_QUANTITY_HEADROOM if leg['type'] == 'market' else 1)
        for leg in plan['tranches']
    ]


def test_equal_notional_uses_one_total_stop_budget():
    first, second, stop = Decimal('100'), Decimal('90'), Decimal('80')
    reference = batch_reference_price(first, second, stop, 'long')
    total = Decimal('20') / ((reference - stop) / reference)
    plan = build_entry_batch('market', first, second, total, stop)
    legs = plan['tranches']
    amounts = _quote_notionals(plan)
    assert abs(amounts[0] - amounts[1]) < Decimal('0.000000001')
    loss = sum(Decimal(leg['quantity']) * (Decimal(leg['sizing_price']) - stop) for leg in legs)
    assert loss <= Decimal('20')
    assert loss == Decimal(plan['estimated_stop_risk'])
    assert legs[0]['type'] == 'market'
    assert legs[1]['price'] == 90


def test_bad_stop_or_entry_rejected_before_sizing():
    for values in [('100', '90', '95', 'long'), ('100', 'NaN', '80', 'long')]:
        with pytest.raises(ValueError):
            batch_reference_price(*values)


def test_fill_dedup_and_cumulative_snapshot_never_double_count():
    state = {}
    record_fill(state, 'e1', 'entry', '10', 't1', False)
    record_fill(state, 'e1', 'entry', '10', 't1', False)
    record_fill(state, 'e1', 'entry', '10', 't1', '10')
    record_fill(state, 'e2', 'entry', '5', 't2', '5')
    record_fill(state, 'exit', 'exit', '3', 't3', '3')
    assert owned_quantity(state) == Decimal('12')
    record_fill(state, 'e1', 'entry', '2', 't4', '12')
    assert owned_quantity(state) == Decimal('14')


def test_ambiguous_last_qty_without_identity_is_not_counted():
    with pytest.raises(ValueError):
        record_fill({}, 'e1', 'entry', '10', '', False)


def test_eth_three_limits_share_one_equal_notional_budget():
    prices = (Decimal('2877'), Decimal('2967'), Decimal('3067'))
    stop = Decimal('3167')
    reference = batch_reference_price(prices[0], prices[1], stop, 'short', prices[2])
    assert reference == Decimal(3) / sum((Decimal(1) / price for price in prices), Decimal(0))
    budget = Decimal('20')
    total = budget / (abs(reference - stop) / reference)
    plan = build_entry_batch('limit', prices[0], prices[1], total, stop, prices[2])
    legs = plan['tranches']
    assert [leg['type'] for leg in legs] == ['limit', 'limit', 'limit']
    assert [leg['price'] for leg in legs] == [2877.0, 2967.0, 3067.0]
    amounts = [Decimal(leg['quantity']) * Decimal(leg['sizing_price']) for leg in legs]
    assert max(amounts) - min(amounts) < Decimal('0.000001')
    assert Decimal(plan['estimated_stop_risk']) <= budget
    assert budget - Decimal(plan['estimated_stop_risk']) < Decimal('0.000001')


def test_market_plus_two_limits_and_stop_on_any_leg():
    plan = build_entry_batch('market', '100', '90', '30', '70', '80')
    assert [leg['type'] for leg in plan['tranches']] == ['market', 'limit', 'limit']
    amounts = _quote_notionals(plan)
    assert max(amounts) - min(amounts) < Decimal('0.000001')
    for bad in (
        ('2877', '2967', '3000', 'short', '3067'),
        ('100', '90', '85', 'long', '80'),
        ('100', '90', '80', 'long', '0'),
        ('100', 'NaN', '80', 'long', None),
    ):
        with pytest.raises(ValueError):
            batch_reference_price(*bad)


def test_third_leg_fill_counts_once_toward_owned_quantity():
    state = {}
    record_fill(state, 'e1', 'entry', '1', 't1', '1')
    record_fill(state, 'e3', 'entry', '2', 't3', '2')
    record_fill(state, 'e3', 'entry', '2', 't3', '2')
    assert owned_quantity(state) == Decimal('3')


@pytest.mark.parametrize('kind,first,second,stop,side', [
    ('market', '2.70', '2.662', '2.58', 'long'),
    ('limit', '62000', '63457', '64229', 'short'),
])
def test_two_entry_price_scales_and_directions(kind, first, second, stop, side):
    reference = batch_reference_price(first, second, stop, side)
    budget = Decimal('10')
    total = budget / (abs(reference - Decimal(stop)) / reference)
    plan = build_entry_batch(kind, first, second, total, stop)
    assert Decimal(plan['estimated_stop_risk']) <= budget
    amounts = _quote_notionals(plan)
    assert abs(amounts[0] - amounts[1]) < Decimal('0.000001')


def test_market_leg_survives_node_repricing_within_headroom():
    # Titan MET m4619 (2026-10-04): approved 2378.0044, the node priced the
    # market leg at 0.31122 two seconds after the 0.311 quote and denied
    # approved_max_notional_exceeded at 2378.7863.
    approved = Decimal('2378.0044157164857')
    plan = build_entry_batch('market', '0.311', '0.28741', approved, '0.27029')
    market, limit = plan['tranches']
    market_qty = Decimal(market['quantity']).to_integral_value(rounding='ROUND_DOWN')
    limit_qty = Decimal(limit['quantity']).to_integral_value(rounding='ROUND_DOWN')
    limit_notional = limit_qty * Decimal('0.28741')
    assert market_qty * Decimal('0.31122') + limit_notional <= approved
    assert market_qty * Decimal('0.311') * MARKET_QUANTITY_HEADROOM + limit_notional <= approved
    assert limit['quantity'] == format(
        (approved / 2 / Decimal('0.28741')).quantize(Decimal('0.000000000001'), rounding='ROUND_DOWN'), 'f',
    )
