"""Two explicit entries: fixed equal notionals and per-order fill evidence.

No I/O. Fill evidence is stored in the existing protection stash; it never
infers ownership from a combined exchange position.
"""
from decimal import Decimal, InvalidOperation, ROUND_DOWN


def positive(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('entry batch requires finite positive numbers') from exc
    if not number.is_finite() or number <= 0:
        raise ValueError('entry batch requires finite positive numbers')
    return number


def batch_reference_price(first, second, stop, side):
    first, second, stop = (positive(value) for value in (first, second, stop))
    if side not in ('long', 'short'):
        raise ValueError('entry batch side must be long or short')
    if side == 'long' and stop >= min(first, second):
        raise ValueError('long stop must be below both entries')
    if side == 'short' and stop <= max(first, second):
        raise ValueError('short stop must be above both entries')
    # Equal notionals: total stop fraction is the mean of the two fractions.
    return Decimal(2) / (Decimal(1) / first + Decimal(1) / second)


def build_entry_batch(first_type, first, second, total_notional, stop):
    if first_type not in ('market', 'limit'):
        raise ValueError('batch first entry must be market or limit')
    prices = (positive(first), positive(second))
    per_leg = positive(total_notional) / 2
    tranches = []
    for index, price in enumerate(prices):
        kind = 'limit'
        if index == 0:
            kind = first_type
        quantity = (per_leg / price).quantize(Decimal('0.000000000001'), rounding=ROUND_DOWN)
        if quantity <= 0:
            raise ValueError('entry batch quantity rounded to zero')
        leg = {
            'seq': index + 1, 'tranche_id': f'e{index + 1}', 'type': kind,
            'quantity': format(quantity, 'f'), 'sizing_price': format(price, 'f'),
            # For MARKET this is the approval quote, never a limit on the order.
            'price': float(price),
        }
        tranches.append(leg)
    return {
        'version': '1', 'allocation': 'equal_notional', 'tranches': tranches,
        'estimated_stop_risk': format(sum(
            positive(leg['quantity']) * abs(positive(leg['sizing_price']) - positive(stop))
            for leg in tranches
        ), 'f'),
    }


def record_fill(state, client_id, role, last_quantity, trade_id, cumulative):
    if role not in ('entry', 'exit'):
        raise ValueError('invalid batch fill role')
    if not trade_id and cumulative is False:
        raise ValueError('batch fill needs a trade id or cumulative quantity')
    row = state.setdefault(client_id, {'role': role, 'trades': {}, 'filled': '0'})
    if row['role'] != role:
        raise ValueError('batch order role changed')
    trades = row['trades']
    if trade_id:
        quantity = positive(last_quantity)
        previous = trades.get(str(trade_id))
        if previous is not None and Decimal(previous) != quantity:
            raise ValueError('batch trade quantity changed')
        trades[str(trade_id)] = format(quantity, 'f')
    total = sum((Decimal(value) for value in trades.values()), Decimal(0))
    total = max(total, Decimal(row['filled']))
    if cumulative is not False:
        total = max(total, positive(cumulative))
    row['filled'] = format(total, 'f')


def owned_quantity(state):
    quantity = Decimal(0)
    for row in state.values():
        filled = Decimal(row['filled'])
        if row['role'] == 'entry':
            quantity += filled
        else:
            quantity -= filled
    return max(quantity, Decimal(0))
