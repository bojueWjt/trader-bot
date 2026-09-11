from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/execution-domain'))
from execution_domain.entry_batch import (
    batch_reference_price, build_entry_batch, record_fill, owned_quantity,
)


def test_equal_notional_uses_one_total_stop_budget():
    first, second, stop = Decimal('100'), Decimal('90'), Decimal('80')
    reference = batch_reference_price(first, second, stop, 'long')
    total = Decimal('20') / ((reference - stop) / reference)
    plan = build_entry_batch('market', first, second, total, stop)
    legs = plan['tranches']
    amounts = [Decimal(leg['quantity']) * Decimal(leg['sizing_price']) for leg in legs]
    assert abs(amounts[0] - amounts[1]) < Decimal('0.000000001')
    loss = sum(Decimal(leg['quantity']) * (Decimal(leg['sizing_price']) - stop) for leg in legs)
    assert loss <= Decimal('20')
    assert Decimal('20') - loss < Decimal('0.000000001')
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
    amounts = [Decimal(leg['quantity']) * Decimal(leg['sizing_price']) for leg in plan['tranches']]
    assert abs(amounts[0] - amounts[1]) < Decimal('0.000001')
