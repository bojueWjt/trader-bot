"""v8 F4/F9 silver stop derivation. Invented messages only; no channel text."""
from decimal import Decimal

import pytest

from quant_lab.data import cx_v2, stop_rules
from test_cx_batch import mutant


def n(value, quote=None):
    return dict(value=str(value), quote=str(value) if quote is None else quote)


def act(symbol, side, entry=None, stop=None, time_ref='now'):
    return dict(op='open', time_ref=time_ref, symbol_raw=symbol, side=side, entry=entry, stop=stop, tps=[], field_issues=[])


def limit(value, quote=None):
    return dict(kind='limit', price=n(value, quote), lo=None, hi=None, levels=[])


def zone(lo, hi):
    return dict(kind='zone', price=None, lo=n(lo), hi=n(hi), levels=[])


def market():
    return dict(kind='market_ref', price=None, lo=None, hi=None, levels=[])


def price_stop(value, quote=None):
    return dict(kind='price', price=n(value, quote), condition=None)


def condition_stop(condition, value=None, quote=None):
    return dict(kind='condition', price=n(value, quote) if value is not None else None, condition=condition)


def derive(text, *actions):
    rows, _ = cx_v2.parse_actions(dict(schema_version=2, actions=list(actions)), text)
    stop_rules.derive_stops(rows, text)
    return rows


def one(text, action):
    row = derive(text, action)[0]
    return row.stop, row.checks.get('stop_rule'), row.checks.get('nostop_hint')


def test_fuzzy_break_without_number_uses_the_entry_and_widens_once():
    stop, rule, hint = one('仿写 BTC 6万6附近接多，小幅跌破就止损', act('BTC', 'long', limit(66000, '6万6')))
    assert stop == Decimal('65802') and hint is None
    assert rule['rule'] == 'r9_fuzzy_break' and rule['base'] == '66000' and rule['base_source'] == 'entry_low'
    assert rule['widened'] == '0.003' and rule['version'] == stop_rules.STOP_RULES_VERSION
    # The model's stop equal to the entry is a zero distance, so the paragraph rule still applies.
    stop, rule, _ = one('仿写 BTC 6万6附近接多，小幅跌破就止损', act('BTC', 'long', limit(66000, '6万6'), price_stop(66000, '6万6')))
    assert stop == Decimal('65802') and rule['rule'] == 'r9_fuzzy_break'


def test_quoted_fuzzy_stop_widens_short_side():
    stop, rule, _ = one('仿写 ETH 3170-3190 空，小幅涨破3220一点止损', act('ETH', 'short', zone(3170, 3190), price_stop(3220)))
    assert stop == Decimal('3229.660') and rule['rule'] == 'r9_fuzzy_break' and rule['base'] == '3220'
    assert rule['base_source'] == 'quoted_number'


@pytest.mark.parametrize('text', ['仿写 SOL 150 附近多，跌破145就止损', '仿写 SOL 150 附近多，跌破145附近就走'])
def test_plain_break_keeps_the_price(text):
    stop, rule, _ = one(text, act('SOL', 'long', limit(150), price_stop(145)))
    assert stop == Decimal('145') and rule['rule'] == 'plain_break' and rule['widened'] == '0'


def test_close_wording_never_becomes_an_intraday_stop():
    text = '仿写 BTC 7万多，日线收盘跌破6.8万止损'
    # Condition stop: left to the close-stop path.
    stop, rule, hint = one(text, act('BTC', 'long', limit(70000, '7万'), condition_stop('日线收盘跌破6.8万', 68000, '6.8万')))
    assert stop is None and rule is None and hint is None
    # The same wording recorded as a price stop is a 1d close stop, not widened.
    stop, rule, _ = one(text, act('BTC', 'long', limit(70000, '7万'), price_stop(68000, '6.8万')))
    assert stop == Decimal('68000') and rule['rule'] == 'close_from_clause' and rule['timeframe'] == '1d' and rule['widened'] == '0'
    stop, rule, _ = one('仿写 BTC 7万多，止损：日线收盘小幅跌破68000', act('BTC', 'long', limit(70000, '7万'), price_stop(68000)))
    assert stop == Decimal('68000') and rule['rule'] == 'close_from_clause'
    # Close wording without a parsable timeframe keeps the model price and no r9.
    stop, rule, hint = one('仿写 BTC 7万多，止损：跌破65000没有收回', act('BTC', 'long', limit(70000, '7万'), price_stop(65000)))
    assert stop == Decimal('65000') and rule is None and hint == 'close_like_kept_price'


@pytest.mark.parametrize('text,hint', [('仿写 BTC 7万多，4小时级别小幅跌破就止损', 'close_like'),
                                       ('仿写 BTC 7万多，跌破没有收回就止损', 'close_like'),
                                       ('仿写 BTC 7万多，跌破EMA200止损', 'close_like')])
def test_timeframe_close_and_indicator_wording_is_not_derived(text, hint):
    stop, rule, got = one(text, act('BTC', 'long', limit(70000, '7万')))
    assert stop is None and rule is None and got == hint


def test_indicator_condition_is_not_derived():
    stop, rule, _ = one('仿写 BTC 7万多，跌破EMA200止损', act('BTC', 'long', limit(70000, '7万'), condition_stop('跌破EMA200')))
    assert stop is None and rule is None


def test_condition_with_a_plain_price_becomes_the_stop():
    stop, rule, _ = one('仿写 SOL 150 附近多，如果跌破145就走', act('SOL', 'long', limit(150), condition_stop('如果跌破145', 145)))
    assert stop == Decimal('145') and rule['rule'] == 'plain_break'


@pytest.mark.parametrize('text,side,expected', [
    ('仿写 BTC 7万空，突破就止损', 'short', ('zero_distance_break', None)),
    ('仿写 BTC 7万空，小幅突破就止损', 'short', (None, Decimal('70210'))),
    ('仿写 ETH 1820多，跌破就小幅止损', 'long', ('zero_distance_break', None)),
    ('仿写 BTC 9万4多，小幅跌破前低就止损', 'long', ('reference_level', None)),
    ('仿写 BTC 7.6万多，小幅跌破前低7万4就止损', 'long', (None, Decimal('73778'))),
    ('仿写 BTC 7万空，跌破就止损', 'short', ('break_wrong_side', None)),
    ('仿写 BTC 7万多，小幅跌破6.8万止损，跌破6.5万离场', 'long', ('ambiguous_break', None)),
])
def test_paragraph_rules(text, side, expected):
    entry = {'7万': limit(70000, '7万'), '1820': limit(1820), '9万4': limit(94000, '9万4'), '7.6万': limit(76000, '7.6万')}
    key = next(k for k in entry if k in text)
    stop, rule, hint = one(text, act('ETH' if 'ETH' in text else 'BTC', side, entry[key]))
    assert (hint, stop) == expected
    if stop is not None:
        assert rule['rule'] == 'r9_fuzzy_break'


def test_x_wan_y_stop_from_the_model_is_widened_by_the_clause():
    stop, rule, _ = one('仿写 BTC 7.6万多，小幅跌破前低7万4就止损', act('BTC', 'long', limit(76000, '7.6万'), price_stop(74000, '7万4')))
    assert stop == Decimal('73778') and rule['base'] == '74000' and rule['base_source'] == 'quoted_number'


def test_other_coin_wording_does_not_leak_into_this_paragraph():
    rows = derive('仿写 BTC 6万6多，小幅跌破止损；ETH 3200多', act('BTC', 'long', limit(66000, '6万6')), act('ETH', 'long', limit(3200)))
    assert rows[0].stop == Decimal('65802') and rows[0].checks['stop_rule']['rule'] == 'r9_fuzzy_break'
    assert rows[1].stop is None and 'stop_rule' not in rows[1].checks and 'nostop_hint' not in rows[1].checks
    rows = derive('仿写 ETH 3200多\nBTC 6万6多，小幅跌破止损', act('ETH', 'long', limit(3200)), act('BTC', 'long', limit(66000, '6万6')))
    assert rows[0].stop is None and rows[1].stop == Decimal('65802')


def test_numberless_market_entry_defers_to_the_mark():
    row = derive('仿写 BTC 现价多，小幅跌破就止损', act('BTC', 'long', market()))[0]
    assert row.stop is None
    assert row.checks['stop_rule']['base_source'] == 'mark_at_t_a' and row.checks['stop_rule']['base'] is None
    assert row.checks['stop_rule']['rule'] == 'r9_fuzzy_break' and row.checks['stop_rule']['widened'] == '0.003'


def test_far_fuzzy_word_in_the_same_stop_clause_still_counts():
    text = '仿写 BTC 7万多\n止损：如果价格后面小幅跌破下方那个重要的支撑位置也就是65000就离场'
    stop, rule, _ = one(text, act('BTC', 'long', limit(70000, '7万'), price_stop(65000)))
    assert stop == Decimal('64805') and rule['rule'] == 'r9_fuzzy_break'


def test_explicit_stop_has_no_mark_and_non_opens_are_untouched():
    stop, rule, hint = one('仿写 BTC 7万多 止损68000', act('BTC', 'long', limit(70000, '7万'), price_stop(68000)))
    assert stop == Decimal('68000') and rule is None and hint is None
    rows = derive('仿写 BTC 小幅跌破就止损', dict(act('BTC', 'long', limit(70000, '7万')), op='stop_move'))
    assert 'stop_rule' not in rows[0].checks


def test_widening_and_side_mutants_are_killed(monkeypatch):
    text = '仿写 BTC 6万6多，小幅跌破就止损'

    def invariant():
        rows, _ = cx_v2.parse_actions(dict(schema_version=2, actions=[act('BTC', 'long', limit(66000, '6万6'))]), text)
        stop_rules.derive_stop(rows[0], text, rows)
        assert rows[0].stop == Decimal('65802')
    invariant()
    original = stop_rules._derive_from_paragraph
    for before, after in [('    stop = _widen(base, long) if fuzzy else base\n    if entries', '    stop = base\n    if entries'),
                          ('    if verb not in (_LONG_BREAK if long else _SHORT_BREAK):', '    if verb not in _SHORT_BREAK:')]:
        monkeypatch.setattr(stop_rules, '_derive_from_paragraph', mutant(original, before, after))
        with pytest.raises(AssertionError):
            invariant()


# ---------------------------------------------------------------- live v3 parity (FE-16, test 18)
PARITY = [
    ('止损：小幅跌破65000', 'long', 66000, 65000),
    ('止损：65000', 'long', 66000, 65000),
    ('入场66000附近，止损小幅跌破65000一点', 'long', 66000, 65000),
    ('66000多（止损65000附近）', 'long', 66000, 65000),
    ('止损 略破3220', 'short', 3190, 3220),
    ('止损：稍微涨破3220就走', 'short', 3190, 3220),
    ('跌破145一点止损', 'long', 150, 145),
    ('小幅跌破145止损', 'long', 150, 145),
    ('145附近止损', 'long', 150, 145),
    ('止损：跌破145', 'long', 150, 145),
    ('SL 0.2660', 'short', Decimal('0.25'), Decimal('0.2660')),
    ('SL：小幅突破0.2660', 'short', Decimal('0.25'), Decimal('0.2660')),
    ('BTC 66000多\n止损 小幅跌破65000', 'long', 66000, 65000),
    ('BTC 66000多\nETH 止损 小幅跌破65000', 'long', 66000, 65000),
    ('止损：小幅跌破65000\n止损：65000', 'long', 66000, 65000),
    ('入场 66000 止损 65000 目标 70000', 'long', 66000, 65000),
    ('止损650（小幅跌破一点）', 'long', 660, 650),
    ('止损：稍微跌破6.5万', 'long', 66000, 65000),
    ('止损：6.5万一点', 'long', 66000, 65000),
    ('防守位小幅跌破65000', 'long', 66000, 65000),
    ('止损 小幅涨破 3,220', 'short', 3190, 3220),
    ('止损：$3220 一点', 'short', 3190, 3220),
    ('止损：3220美金一点', 'short', 3190, 3220),
    ('止盈 3000，止损 小幅涨破3220', 'short', 3190, 3220),
    ('小幅跌破145就离场；目标160', 'long', 150, 145),
    ('止损145；小幅跌破140也可以', 'long', 150, 145),
    ('入场：150-152\n止损：小幅跌破 145', 'long', 150, 145),
    ('止损: 145。仓位轻一点', 'long', 150, 145),
    ('stop 小幅跌破 145', 'long', 150, 145),
    ('失效：略破145', 'long', 150, 145),
]


@pytest.mark.parametrize('text,side,entry,stop', PARITY)
def test_breakout_matches_live_v3(text, side, entry, stop):
    from quant_lab.market import live_profile
    plan = dict(instrument_id='BTCUSDT-PERP', side=side, entries=[dict(price_lo=entry, price_hi=entry, kind='limit')],
                stop=dict(price=stop), tps=[])
    assert stop_rules.breakout_on(stop, text, symbol='BTCUSDT') == live_profile.wording_flags(plan, text)['stop_breakout']
    assert stop_rules._sentences(text) == live_profile._sentences(text)


def test_parity_corpus_is_not_trivial_and_word_lists_cover_live():
    from quant_lab.market import live_profile
    flags = [stop_rules.breakout_on(stop, text, symbol='BTCUSDT') for text, _, _, stop in PARITY]
    assert 8 <= sum(flags) <= len(flags) - 8
    for word in live_profile._BREAKOUT.pattern.split('|'):
        assert stop_rules.FUZZY_BREAK.search(word + '145'), word
    for name in ('_ROLE', '_ENTRY', '_STOP', '_TP', '_GROUP', '_NEAR_AFTER', '_NEAR_BEFORE', '_BREAK_BEFORE', '_BREAK_AFTER', '_BREAKOUT'):
        assert getattr(stop_rules, name).pattern == getattr(live_profile, name).pattern, name


def test_clause_offsets_point_into_the_text():
    text = '仿写 BTC 66000多；止损 小幅跌破65000\r\n目标 70000。止盈 72000'
    for _, start, clause in stop_rules._clauses(text):
        assert text[start:start + len(clause)] == clause


# ---------------------------------------------------------------- F9 relative stops
def relative(text, action):
    row = derive(text, action)[0]
    return row.checks.get('stop_relative'), row.checks.get('nostop_hint')


def test_relative_points_and_percent_detected_and_resolved():
    rel, _ = relative('仿写 BTC 现价多，带1000点防守', act('BTC', 'long', market()))
    assert rel['kind'] == 'points' and rel['value'] == '1000' and rel['ref'] == 'mark'
    assert stop_rules.resolve_relative(rel, 'long', Decimal(100000)) == Decimal(99000)
    rel, _ = relative('仿写 ETH 3100 多 防守50点', act('ETH', 'long', limit(3100)))
    assert rel['ref'] == 'entry_ref' and stop_rules.resolve_relative(rel, 'long', Decimal(3100)) == Decimal(3050)
    rel, _ = relative('仿写 SUI 3.90 多 止损2%', act('SUI', 'long', limit('3.90')))
    assert rel['kind'] == 'pct' and stop_rules.resolve_relative(rel, 'long', Decimal('3.90')) == Decimal('3.8220')
    assert stop_rules.resolve_relative(dict(kind='pct', value='2'), 'short', Decimal(100)) == Decimal(102)


def test_relative_leverage_and_distance_rules():
    rel, hint = relative('仿写 SUI 3.90 多，最大损失3%至3.5%（2倍杠杆）', act('SUI', 'long', limit('3.90')))
    assert rel is None and hint == 'relative_ambiguous'
    rel, hint = relative('仿写 SUI 3.90 多，最大损失就0.90%。或者用3倍杠杆最大损失是3.45%', act('SUI', 'long', limit('3.90')))
    assert rel['value'] == '0.90' and hint is None
    rel, _ = relative('仿写 SUI 3.90 多，止损3%至3.5%', act('SUI', 'long', limit('3.90')))
    assert rel['value'] == '3.5'
    rel, hint = relative('仿写 SUI 3.90 多，止损30%', act('SUI', 'long', limit('3.90')))
    assert rel is None and hint == 'relative_ambiguous'


def test_relative_binds_to_each_coin():
    text = '仿写 BTC 现价多 带1000点\nETH 现价多 带50点'
    rows = derive(text, act('BTC', 'long', market()), act('ETH', 'long', market()))
    assert [r.checks['stop_relative']['value'] for r in rows] == ['1000', '50']


def test_relative_runs_only_without_a_stop_or_mark():
    rel, _ = relative('仿写 ETH 3100 多 防守50点 止损3000', act('ETH', 'long', limit(3100), price_stop(3000)))
    assert rel is None
