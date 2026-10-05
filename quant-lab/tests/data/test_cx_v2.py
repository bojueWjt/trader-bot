"""Invented messages only. No production text, calls, or gold-file dependencies."""
from copy import deepcopy
from decimal import Decimal
import json

import polars as pl
import pytest

from quant_lab.data import cx_batch as cx, cx_v2 as v2, extract, lifecycle, linker, plan_source, validate
from quant_lab.data.llm import RecordedClient, record_key
from test_cx_batch import T0, TEXT, synthetic_lake, mutant, canonical_pair


def number(value, quote=None):
    return dict(value=str(value), quote=str(value) if quote is None else quote)


def action(**changes):
    row = dict(op='open', time_ref='now', symbol_raw='BTC', side='long',
               entry=dict(kind='market_ref', price=number(100), lo=None, hi=None, levels=[]),
               stop=dict(kind='price', price=number(90), condition=None),
               tps=[dict(kind='price', value=number(110))], field_issues=[])
    row.update(changes)
    return row


def envelope(*actions):
    return dict(schema_version=2, actions=list(actions))


@pytest.mark.parametrize('literal,value', [('9.87万', '98700'), ('9.87w', '98700'), ('98.7k', '98700'),
                                          ('98.7K', '98700'), ('98,700', '98700'), ('９８，７００', '98700'),
                                          ('9 . 87 万', '98700'), ('98 700', '98700'), ('98 , 700', '98700')])
def test_units_exact_and_wrong_value_mutant(literal, value):
    text = '仿写 BTC 现价' + literal + '做多'
    cleaned, span = v2.exact_number(number(value, literal), text)
    assert Decimal(cleaned['value']) == Decimal(value)
    assert text[span['start']:span['end']] == literal
    with pytest.raises(ValueError):
        v2.exact_number(number(Decimal(value) + 1, literal), text)
    broken = mutant(v2.exact_number, 'value == number and is_percent == percent', 'is_percent == percent')
    def invariant(fn):
        rejected = False
        try:
            fn(number(Decimal(value) + 1, literal), text)
        except ValueError:
            rejected = True
        assert rejected
    invariant(v2.exact_number)
    with pytest.raises(AssertionError):
        invariant(broken)


@pytest.mark.parametrize('text,quote,value', [('价1000', '100', 100), ('价9万', '9', 9), ('价-90', '90', 90),
                                              ('价90k', '90k', 90), ('价９０，０００', '０００', 1000),
                                              ('价90', '旧文90', 90), ('目标10%', '10%', 10)])
def test_evidence_does_not_accept_partial_units_or_percentage_as_price(text, quote, value):
    with pytest.raises(ValueError):
        v2.exact_number(number(value, quote), text)


def test_field_failure_preserves_action_siblings_and_replay_revalidates():
    first = action(stop=dict(kind='price', price=number(91, '90'), condition=None))
    second = action(symbol_raw='ETH', side='short')
    result = cx.quote_response(envelope(first, second), TEXT)['response']
    assert len(result['actions']) == 2
    assert result['actions'][0]['op'] == 'open'
    assert result['actions'][0]['stop']['price'] is None
    assert result['actions'][0]['entry']['price']['value'] == '100'
    assert result['actions'][1]['stop']['price']['value'] == '90'
    assert result['stats'] == dict(model_uncertain=0, field_evidence_failed=1, whole_message_discarded=0)
    result['actions'][1]['entry']['price']['value'] = '101'
    rows, stats = v2.parse_actions(result, TEXT)
    assert len(rows) == 2 and rows[1].entry is None
    assert stats['field_evidence_failed'] == 2
    assert v2.parse_actions(result, TEXT)[1] == stats


def test_missing_numeric_fields_do_not_erase_open_or_turn_into_market():
    clean = v2.validate_response(envelope(action(entry=None, stop=None, tps=[], symbol_raw=None, side=None)), '仿写：这里再开一笔')
    assert clean['actions'][0]['op'] == 'open'
    assert {i['field'] for i in clean['actions'][0]['field_issues']} >= {'entry', 'stop', 'tps', 'symbol_raw', 'side'}
    rows, _ = v2.parse_actions(clean, '仿写：这里再开一笔')
    assert rows[0].entry_mode == 'unknown'
    assert not clean['stats']['whole_message_discarded']


def test_model_uncertain_empty_actions_and_partial_uncertainty_are_distinct():
    unknown = action(op='undecidable', entry=None, stop=None, tps=[])
    clean = v2.validate_response(envelope(unknown), '')
    assert clean['stats']['model_uncertain'] == clean['stats']['whole_message_discarded'] == 1
    assert v2.validate_response(envelope(), '')['stats']['whole_message_discarded'] == 0
    clean = v2.validate_response(envelope(unknown, action()), TEXT)
    assert [a['op'] for a in clean['actions']] == ['open']
    assert clean['stats']['whole_message_discarded'] == 0
    assert clean['actions'][0]['branch_index'] == 0
    assert v2.validate_response(clean, TEXT)['stats'] == clean['stats']


def test_length_batches_bound_count_chars_and_oversize_singleton():
    rows = [dict(text='x' * 1500)] * 25
    batches = list(cx.length_batches(rows))
    assert all(len(cx.dumps(r)) <= 12000 for r in rows)
    assert all(sum(len(cx.dumps(r)) for r in batch) <= 12000 and len(batch) <= 20 for batch in batches)
    assert sum(map(len, batches)) == 25
    assert list(cx.length_batches([dict(text='x' * 13000), dict(text='x')])) == [[dict(text='x' * 13000)], [dict(text='x')]]
    assert [len(b) for b in cx.length_batches([{}] * 21)] == [20, 1]


def v2_lake(tmp_path, actions, text=TEXT):
    layout, mv = synthetic_lake(tmp_path)
    mv = mv.filter(pl.col('source_version_id') == 's0').with_columns(pl.lit(text).alias('text'))
    mv.write_parquet(layout.message_version)
    prompt_path = tmp_path / 'prompts'
    cx.export_prompts(layout, prompt_path)
    prompt = next(cx.read_jsonl(prompt_path))
    client = RecordedClient({prompt['key']: dict(response=envelope(*actions))}, version='cx-batch-v2')
    ex, _, _, report = extract.extract_frame(mv, None, client=client, ingested_at=T0)
    ex.write_parquet(layout.extracted_event)
    validate.run(layout, synthetic=True, ingested_at=T0)
    return layout, mv, ex, pl.read_parquet(layout.canonical_plan), report


def test_multiaction_time_refs_only_now_open_decision_and_default_parser(tmp_path, monkeypatch):
    # Without a stop the past/conditional opens are not F11 setup cards; promotion has its own tests.
    actions = [action(), action(time_ref='past', stop=None), action(time_ref='conditional', stop=None), action(symbol_raw='ETH')]
    _, mv, ex, cp, report = v2_lake(tmp_path, actions)
    llm = ex.filter(pl.col('extractor').struct.field('name') == 'llm')
    assert llm['branch_index'].to_list() == [0, 1, 2, 3]
    assert len(set(llm['extract_id'])) == 4 and report['llm']['rows'] == 4
    assert [json.loads(c)['time_ref'] for c in llm['checks']] == ['now', 'past', 'conditional', 'now']

    def build(mode):
        edges, adj, _ = linker.build_candidates(cp, mv, ex, plan_source=mode, ingested_at=T0)
        return lifecycle.build_graph(cp, mv, edges, adj, None, graph_version='v2-test', plan_source=mode, extracted_event=ex, ingested_at=T0)

    def invariant():
        for mode in ('llm', 'reconciled'):
            episodes, events, *_ = build(mode)
            decision_ids = set(episodes.filter(pl.col('t_dec').is_not_null())['root_extract_id']) if 'root_extract_id' in episodes.columns else set()
            roots = episodes.filter(pl.col('t_dec').is_not_null())
            assert roots.height == 2
            assert events.filter(pl.col('extract_id').is_in(llm['extract_id'])).height == 4
            assert len([p for p in episodes['order_plan'] if p is not None]) == 2
    invariant()
    monkeypatch.setattr(plan_source, 'descriptive_only', lambda _: False)
    # The extraction mapping is also a guard; mutate the time mapping to make
    # the former all-open behavior observable at graph construction.
    altered = cp.with_columns(pl.when(pl.col('extractor_name') == 'llm').then(pl.lit('entry_proposal')).otherwise(pl.col('kind')).alias('kind'))
    original_cp = cp
    cp = altered
    with pytest.raises(AssertionError):
        invariant()
    cp = original_cp
    parser_only = ex.filter(pl.col('extractor').struct.field('name') == 'parser')
    baseline, *_ = extract.extract_frame(mv, None, ingested_at=T0)
    assert parser_only.equals(baseline)
    assert plan_source.select_plans(cp).equals(plan_source.select_plans(cp, 'parser'))


def test_condition_and_percent_mapping_no_guessed_basis(tmp_path, monkeypatch):
    from quant_lab.data.market_stub import SyntheticMarks, instrument_id_for
    # Keep this invented 100/90 signal within the independent scale gate.
    monkeypatch.setattr(validate, "fixture_marks", lambda: SyntheticMarks({instrument_id_for("BTC"): [(T0, 100)]}))
    text = '仿写 BTC 现价100做多；日线收盘低于90才止损；目标10%、20%。'
    stop = dict(kind='condition', price=number(90), condition='日线收盘低于90才止损')
    tps = [dict(kind='percent', value=number(v, f'{v}%')) for v in (10, 20)]
    _, _, ex, cp, _ = v2_lake(tmp_path, [action(stop=stop, tps=tps)], text)
    row = cp.filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    assert [t['level'] for t in row['tps']] == [Decimal(110), Decimal(120)]
    assert row['stop'] == Decimal(90)
    checks = json.loads(row['checks'])
    assert 'mapping_issues' not in checks
    assert checks['stop_trigger'] == dict(basis='close', timeframe='1d', condition=stop['condition'])
    assert json.loads(row['eligibility_by_estimand'])['execution']
    plan = lifecycle._order_plan(row, row['stop'], row['tps'], None)
    assert plan['stop'] == dict(price=Decimal(90), trigger='close', timeframe='1d')
    assert plan['tps'][0]['level'] == 110
    assert checks['action']['stop']['condition'] == stop['condition']


def test_percent_short_mixed_ladder_fractions_and_ambiguous_basis(tmp_path):
    text = '仿写 BTC 现价100和挂单110做空，各50%；止损120，目标10%。'
    entry = dict(kind='ladder', price=None, lo=None, hi=None, levels=[
        dict(kind='market_ref', price=number(100), fraction=number(50, '50%')),
        dict(kind='limit', price=number(110), fraction=number(50, '50%'))])
    _, _, _, cp, _ = v2_lake(tmp_path, [action(side='short', entry=entry,
        stop=dict(kind='price', price=number(120), condition=None), tps=[dict(kind='percent', value=number(10, '10%'))])], text)
    row = cp.filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    assert row['tps'] == []
    assert row['entry_ref'] == Decimal(100000)  # Unique mark-based unit restoration.
    assert json.loads(row['checks'])['tps_dropped_percent']
    assert json.loads(row['eligibility_by_estimand'])['execution']
    plan = lifecycle._order_plan(row, row['stop'], row['tps'], None)
    assert [e['kind'] for e in plan['entries']] == ['market_ref', 'limit']
    assert [e['fraction'] for e in plan['entries']] == [Decimal('.5')] * 2
    # Single known entry makes the short percent target unambiguous.
    _, _, _, single, _ = v2_lake(tmp_path / 'single', [action(side='short', stop=dict(kind='price', price=number(120), condition=None),
                                                         tps=[dict(kind='percent', value=number(10, '10%'))])], text)
    assert single.filter(pl.col('extractor_name') == 'llm')['tps'][0][0]['level'] == 90000


def test_reconciled_matches_actions_without_suppressing_other_symbol(tmp_path):
    _, _, ex, cp = canonical_pair(tmp_path)
    rows = cp.to_dicts()
    source = next(r for r in rows if r['extractor_name'] == 'parser' and r['source_version_id'] == 's0')
    other = deepcopy(source)
    other.update(plan_id='other-symbol', branch_index=1, symbol_raw='ETH', instrument_id='ETH-USDT')
    for row in rows:
        if row['extractor_name'] == 'llm' and row['source_version_id'] == 's0':
            row.update(branch_index=9, checks=json.dumps(dict(schema_version=2, llm_evidence_valid=True, time_ref='now')))
    cp = pl.DataFrame(rows + [other], schema=cp.schema)
    chosen = plan_source.select_plans(cp, 'reconciled')
    assert 'other-symbol' in chosen['plan_id']
    assert source['plan_id'] not in chosen['plan_id']


def test_prompt_reply_key_is_v2_and_no_unit_inference(tmp_path):
    layout, mv = synthetic_lake(tmp_path)
    path = tmp_path / 'prompts'
    cx.export_prompts(layout, path)
    rows = list(cx.read_jsonl(path))
    parent = next(p for p in rows if p['source_version_id'] == 's1')
    assert json.loads(parent['user'])['previous_text'] == TEXT
    assert parent['schema_name'] == v2.SCHEMA_NAME
    client = RecordedClient({parent['key']: dict(response=envelope(action(op='close', entry=None, stop=None, tps=[])))})
    ex, *_ = extract.extract_frame(mv, None, client=client, ingested_at=T0)
    assert ex.filter(pl.col('extractor').struct.field('name') == 'llm').height == 1
    with pytest.raises(ValueError):
        v2.exact_number(number(119700, '11.97'), '仿写上方12万，防守11.97')


def test_unseen_export_excludes_previous_text_samples(tmp_path):
    layout, _ = synthetic_lake(tmp_path)
    old = tmp_path / 'old'
    old.write_text(cx.dumps(dict(text=TEXT)) + '\n')
    path = tmp_path / 'new'
    stats = cx.export_prompts(layout, path, exclude_prompts=[old])
    assert stats['excluded_seen_text'] == 2
    assert all(p['text'] != TEXT for p in cx.read_jsonl(path))


@pytest.mark.parametrize('field,value', [('tps', 42), ('entry', {'kind': 'ladder', 'levels': 42}), ('field_issues', 42)])
def test_malformed_field_container_does_not_erase_clear_action(field, value):
    result = v2.validate_response(envelope(action(**{field: value})), TEXT)
    assert result['actions'][0]['op'] == 'open'
    assert result['stats']['whole_message_discarded'] == 0


def test_zero_percent_is_a_proved_value_not_missing():
    atom, _ = v2.exact_number(number(0, '0%'), '仿写目标0%', percent=True)
    assert atom['value'] == '0'
    with pytest.raises(ValueError):
        v2.exact_number(number(0), '仿写价0')


def test_cmp_legs_and_raw_symbols_reach_an_executable_plan(tmp_path):
    # Prices sit near the synthetic BTC mark so the scale gate does not mask the ladder rule.
    text = '仿写 $BTC/USDT CMP 和挂单 61000 做多；止损 60000。'
    entry = dict(kind='ladder', price=None, lo=None, hi=None, levels=[
        dict(kind='market_ref', price=None, fraction=None), dict(kind='limit', price=number(61000), fraction=None)])
    stop = dict(kind='price', price=number(60000), condition=None)
    _, _, _, cp, _ = v2_lake(tmp_path, [action(symbol_raw='$BTC/USDT', entry=entry, stop=stop, tps=[])], text)
    row = cp.filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    assert row['symbol_raw'] == 'BTC' and row['instrument_id'] is not None
    plan = lifecycle._order_plan(row, row['stop'], row['tps'], None)
    # The CMP leg is priced from the as-of mark at t_dec in replay; the limit leg keeps its quoted price.
    assert [(e['kind'], e['price_lo']) for e in plan['entries']] == [('market_ref', None), ('limit', Decimal(61000))]
    assert row['scale_gate'] == 'ok'
    # A plan is only executable if the canonical layer agrees: an unpriced CMP leg is not an incomplete ladder.
    assert 'mapping_issues' not in json.loads(row['checks'])
    assert json.loads(row['eligibility_by_estimand'])['execution']
    # An unpriced limit leg is still not guessable.
    entry['levels'][1]['price'] = None
    _, _, _, gap, _ = v2_lake(tmp_path / 'gap', [action(entry=entry, stop=stop, tps=[])], text)
    gap_row = gap.filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    assert lifecycle._order_plan(gap_row, gap_row['stop'], gap_row['tps'], None) is None
    assert json.loads(gap_row['checks'])['mapping_issues'] == [dict(field='entry', reason='incomplete_ladder')]
    assert not json.loads(gap_row['eligibility_by_estimand'])['execution']


def test_numberless_cmp_open_is_a_market_entry_not_a_missing_one(tmp_path):
    text = '仿写 #BTC 现价做多；止损 90。'
    _, _, _, cp, _ = v2_lake(tmp_path, [action(symbol_raw='#BTC', tps=[],
        entry=dict(kind='market_ref', price=None, lo=None, hi=None, levels=[]))], text)
    row = cp.filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    assert row['entry_mode'] == 'market_ref' and lifecycle.dec_plan_possible(row)
    plan = lifecycle._order_plan(row, row['stop'], row['tps'], None)
    assert [(e['kind'], e['price_lo']) for e in plan['entries']] == [('market_ref', None)]


@pytest.mark.parametrize('raw,code', [('$ENA', 'ENA'), ('#UNI/USDT', 'UNI'), ('INJ/USDT', 'INJ'), ('near', 'NEAR'),
                                      ('大饼', 'BTC'), ('BTCUSDT.P', 'BTC'), ('ＥＴＨ', 'ETH'), ('原油', 'CL'), ('比特幣', 'BTC'), (None, None)])
def test_canonical_symbol(raw, code):
    assert extract.canonical_symbol(raw) == code


@pytest.mark.parametrize('text,quote,value', [('止盈 527-540', '527-540', '540'), ('止盈 527-540', '540', '540'),
                                             ('入场 CMP-3290', 'CMP-3290', '3290'), ('区间 9万-10万', '10万', '100000'),
                                             ('入场 7.28-7.32万附近', '7.28-7.32万', '72800'), ('入场 7.28-7.32万附近', '7.28-7.32万', '73200'),
                                             ('入场 7.28-7.32万附近', '7.28', '7.28'), ('区间 91-93k', '91-93k', '91000')])
def test_range_ends_and_range_units_are_evidence(text, quote, value):
    atom, span = v2.exact_number(dict(value=value, quote=quote), text)
    assert atom['value'] == value and text[span['start']:span['end']].strip()


@pytest.mark.parametrize('text,quote,value', [('入场 7.28-7.32万附近', '7.28', '72800'), ('入场 7.28-7.32万附近', '7.28-7.32', '72800'),
                                             ('跌 -540', '-540', '540'), ('入场 7.28 和 7.32万', '7.28 和 7.32万', '72800'),
                                             ('止盈 527-540', '527-540', '527540')])
def test_range_unit_needs_the_unit_in_the_quote_and_a_real_range(text, quote, value):
    with pytest.raises(ValueError):
        v2.exact_number(dict(value=value, quote=quote), text)


@pytest.mark.parametrize('text,quote,value,percent', [('当前1810入10%底仓，1860挂单10%', '1860', '1860', False),
                                                     ('当前1810入10%底仓，1860挂单10%', '10%', '10', True),
                                                     ('2.5u趋势线支撑位做多', '2.5u', '2.5', False),
                                                     ('1860挂单10%，1930挂单10%', '10%，', '10', True)])
def test_chinese_comma_and_u_suffix_do_not_hide_prices(text, quote, value, percent):
    assert v2.exact_number(dict(value=value, quote=quote), text, percent=percent)[0]['value'] == value


@pytest.mark.parametrize('text,quote,value', [('1,86', '86', '86'), ('5Uniswap', '5', '5'), ('12,345', '345', '345')])
def test_grouping_guard_still_blocks_partial_tokens(text, quote, value):
    with pytest.raises(ValueError):
        v2.exact_number(dict(value=value, quote=quote), text)


def unit_zone_action():
    text = '仿写 BTC 做多；入场6.24-6.26万；止损6.1；止盈6.4、6.5。'
    entry = dict(kind='zone', price=None, lo=number(62400, '6.24-6.26万'),
                 hi=number(62600, '6.24-6.26万'), levels=[])
    return text, action(entry=entry, stop=dict(kind='price', price=number('6.1'), condition=None),
                        tps=[dict(kind='price', value=number(v)) for v in ('6.4', '6.5')])


def test_action_unit_inheritance_preserves_evidence_and_executes(tmp_path):
    text, original = unit_zone_action()
    rows, stats = v2.parse_actions(envelope(original), text)
    row = rows[0]
    assert row.stop == 61000 and [t['level'] for t in row.tps] == [64000, 65000]
    assert row.checks['unit_inherited'] == [
        dict(field='stop.price', quote='6.1', factor='10000'),
        dict(field='tps[0].value', quote='6.4', factor='10000'),
        dict(field='tps[1].value', quote='6.5', factor='10000')]
    assert row.checks['action']['stop']['price'] == original['stop']['price']
    assert row.checks['action']['tps'] == original['tps']
    assert stats['field_evidence_failed'] == 0
    _, _, _, cp, _ = v2_lake(tmp_path, [original], text)
    canonical = cp.filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    assert canonical['scale_gate'] == 'ok'
    assert json.loads(canonical['eligibility_by_estimand'])['execution']


@pytest.mark.parametrize('literal,scaled,factor', [('6.25万', 62500, '10000'), ('6.25w', 62500, '10000'),
                                                 ('6.25W', 62500, '10000'), ('62.5k', 62500, '1000'), ('62.5K', 62500, '1000')])
def test_unit_inheritance_scales_all_price_paths_not_percent_or_fraction(literal, scaled, factor):
    bare = '6.3' if factor == '10000' else '63'
    text = f'仿写 BTC 做多；分批{literal}、{bare}各50%；目标10%；日线收盘低于6才止损。'
    entry = dict(kind='ladder', price=None, lo=None, hi=None, levels=[
        dict(kind='market_ref', price=number(scaled, literal), fraction=number(50, '50%')),
        dict(kind='limit', price=number(bare), fraction=number(50, '50%'))])
    stop = dict(kind='condition', price=number(6), condition='日线收盘低于6才止损')
    row = v2.parse_actions(envelope(action(entry=entry, stop=stop,
        tps=[dict(kind='percent', value=number(10, '10%'))])), text)[0][0]
    assert row.entries == [62500, 63000]
    assert row.tps[0]['level'] == 10 and row.stop is None
    expected = [dict(field='entry.levels[1].price', quote=bare, factor=factor)]
    if factor == '10000':  # The quoted close-stop level inherits 万 as well (60000 sits next to 62500); 6k would not.
        expected.append(dict(field='stop.price', quote='6', factor=factor))
    assert row.checks['unit_inherited'] == expected
    assert row.checks['action']['entry']['levels'][1]['fraction']['value'] == '50'


@pytest.mark.parametrize('entry_literal,entry_value,stop_literal,stop_value,tp_literal,tp_value', [
    ('6.25万', 62500, '61k', 61000, '6.4', '6.4'),  # Different explicit factors.
    ('6.25万', 62500, '2', 2, '20', 20),  # Outside both bounds.
    ('2.5k', 2500, '2400', 2400, '2600', 2600),  # Already full ETH prices.
    ('6.25', '6.25', '6.1', '6.1', '6.4', '6.4'),  # No anchor.
])
def test_unit_inheritance_requires_one_factor_and_nearby_scaled_value(entry_literal, entry_value, stop_literal, stop_value, tp_literal, tp_value):
    text = f'仿写 入场{entry_literal}；止损{stop_literal}；止盈{tp_literal}。'
    original = action(entry=dict(kind='limit', price=number(entry_value, entry_literal), lo=None, hi=None, levels=[]),
                      stop=dict(kind='price', price=number(stop_value, stop_literal), condition=None),
                      tps=[dict(kind='price', value=number(tp_value, tp_literal))])
    row = v2.parse_actions(envelope(original), text)[0][0]
    assert row.stop == Decimal(str(stop_value)) and row.tps[0]['level'] == Decimal(str(tp_value))
    assert 'unit_inherited' not in row.checks


def test_unit_inheritance_is_action_local_and_updates_entry_atoms():
    text = '仿写 BTC 入场6.25；止损6.1；目标6.4万。另 ETH 入场2.5；止损2.4。'
    first = action(entry=dict(kind='limit', price=number('6.25'), lo=None, hi=None, levels=[]),
                   stop=dict(kind='price', price=number('6.1'), condition=None),
                   tps=[dict(kind='price', value=number(64000, '6.4万'))])
    second = action(symbol_raw='ETH', entry=dict(kind='limit', price=number('2.5'), lo=None, hi=None, levels=[]),
                    stop=dict(kind='price', price=number('2.4'), condition=None), tps=[])
    rows, _ = v2.parse_actions(envelope(first, second), text)
    assert rows[0].entry['lo'] == 62500 and rows[0].stop == 61000
    assert rows[1].entry['lo'] == Decimal('2.5') and rows[1].stop == Decimal('2.4')
    assert 'unit_inherited' not in rows[1].checks
    first['entry'] = dict(kind='zone', price=None, lo=number('6.1'), hi=number('6.25'), levels=[])
    row = v2.parse_actions(envelope(first), text)[0][0]
    assert row.entry == dict(kind='zone', lo=61000, hi=62500)


def test_unit_inheritance_mutation_is_killed(tmp_path, monkeypatch):
    test_action_unit_inheritance_preserves_evidence_and_executes(tmp_path / 'good')
    monkeypatch.setattr(v2, 'inherited_prices', lambda action: ({}, []))
    with pytest.raises(AssertionError):
        test_action_unit_inheritance_preserves_evidence_and_executes(tmp_path / 'mutant')


def test_inherited_ladder_leg_reaches_the_order_plan(tmp_path):
    # The order plan rebuilds ladder legs from checks.action, which keeps the quoted value; the inherited unit must survive that.
    text = '仿写 BTC 做多；CMP 6.25万 和 6.2 各一半；止损6.1万；止盈6.5万。'
    entry = dict(kind='ladder', price=None, lo=None, hi=None, levels=[
        dict(kind='market_ref', price=number(62500, '6.25万'), fraction=None), dict(kind='limit', price=number('6.2'), fraction=None)])
    original = action(entry=entry, stop=dict(kind='price', price=number(61000, '6.1万'), condition=None),
                      tps=[dict(kind='price', value=number(65000, '6.5万'))])
    _, _, _, cp, _ = v2_lake(tmp_path, [original], text)
    row = cp.filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    assert json.loads(row['checks'])['unit_inherited'] == [dict(field='entry.levels[1].price', quote='6.2', factor='10000')]
    assert row['scale_gate'] == 'ok' and json.loads(row['eligibility_by_estimand'])['execution']
    plan = lifecycle._order_plan(row, row['stop'], row['tps'], None)
    assert [(e['kind'], e['price_lo']) for e in plan['entries']] == [('market_ref', Decimal(62500)), ('limit', Decimal(62000))]


def test_inherited_close_stop_level_reaches_the_order_plan(tmp_path):
    # "日线收盘跌破6.1" next to "6.25万" means 61000; without inheritance the close stop would fail the scale gate.
    text = '仿写 BTC 做多；入场6.25万；日线收盘跌破6.1止损；止盈6.5万。'
    original = action(entry=dict(kind='limit', price=number(62500, '6.25万'), lo=None, hi=None, levels=[]),
                      stop=dict(kind='condition', price=number('6.1'), condition='日线收盘跌破6.1止损'),
                      tps=[dict(kind='price', value=number(65000, '6.5万'))])
    _, _, _, cp, _ = v2_lake(tmp_path, [original], text)
    row = cp.filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    checks = json.loads(row['checks'])
    assert checks['unit_inherited'] == [dict(field='stop.price', quote='6.1', factor='10000')]
    assert row['stop'] == Decimal(61000) and checks['stop_trigger']['timeframe'] == '1d'
    assert row['scale_gate'] == 'ok' and json.loads(row['eligibility_by_estimand'])['execution']
    plan = lifecycle._order_plan(row, row['stop'], row['tps'], None)
    assert plan['stop'] == {'price': Decimal(61000), 'trigger': 'close', 'timeframe': '1d'}


# ---------------------------------------------------------------- v8 F5a: X万Y, glued names/labels, comma decimals
@pytest.mark.parametrize('literal,value', [('6万6', 66000), ('9万4', 94000), ('7万5千', 75000), ('5W6', 56000),
                                          ('5w6', 56000), ('6w1', 61000), ('6万65', 66500)])
def test_wan_y_shorthand_is_one_token(literal, value):
    text = f'仿写 BTC {literal}附近接多'
    cleaned, span = v2.exact_number(number(value, literal), text)
    assert Decimal(cleaned['value']) == value and text[span['start']:span['end']] == literal
    # The bare leading digit is not a separate price token.
    with pytest.raises(ValueError):
        v2.exact_number(number(literal[0], literal), text)


@pytest.mark.parametrize('text,value', [('仿写 6万6月行情', 66000), ('仿写 2万5倍收益', 25000), ('仿写 1万2千人在线', 12000),
                                        ('仿写 1万2千人在线', 10000)])
def test_wan_y_dates_and_counts_are_not_prices(text, value):
    assert all(v != value for v, *_ in v2.tokens(text))


def test_wan_y_does_not_change_ranges():
    values = {v for v, _, _, pct in v2.tokens('仿写 7.28-7.32万 区间') if not pct}
    assert {Decimal('72800'), Decimal('73200')} <= values
    values = {v for v, *_ in v2.tokens('仿写 9万-10万 区间')}
    assert values == {Decimal(90000), Decimal(100000)}


def test_glued_coin_names_and_labels():
    text = '仿写 bnb812止损btc 113666损'
    assert v2.exact_number(number(812, 'bnb812'), text)[0]['value'] == '812'
    assert v2.exact_number(number(113666, 'btc 113666'), text)[0]['value'] == '113666'
    assert v2.exact_number(number('0.2660', 'SL0.2660'), '仿写 空 SL0.2660')[0]['value'] == '0.2660'
    for text, value in [('仿写 ETH2 开始', 2), ('仿写 EMA200 支撑', 200), ('仿写 TP1:130', 1)]:
        assert all(v != value for v, *_ in v2.tokens(text)), text


def test_comma_decimal_only_when_every_other_atom_agrees():
    text = '仿写 XYZ 2.05 多，止损 2,188'
    rows, _ = v2.parse_actions(envelope(action(symbol_raw='XYZ', entry=dict(kind='limit', price=number('2.05'), lo=None, hi=None, levels=[]),
                                                stop=dict(kind='price', price=number(2188, '2,188'), condition=None), tps=[])), text)
    assert rows[0].stop == Decimal('2.188')
    assert rows[0].checks['comma_decimal'] == [{'field': 'stop.price', 'from': '2188', 'to': '2.188'}]
    assert rows[0].checks['unit_inherited'][-1]['factor'] == '0.001'
    text = '仿写 BTC 66000 多，止损 65,000'
    rows, _ = v2.parse_actions(envelope(action(entry=dict(kind='limit', price=number(66000), lo=None, hi=None, levels=[]),
                                                stop=dict(kind='price', price=number(65000, '65,000'), condition=None), tps=[])), text)
    assert rows[0].stop == Decimal(65000) and 'comma_decimal' not in rows[0].checks


# ---------------------------------------------------------------- v8 F11: deterministic promotion
def promoted(text, **changes):
    rows, _ = v2.parse_actions(envelope(action(**changes)), text)
    return rows[0]


SETUP = '仿写 ETH 交易策略：入场 3115-3070，止损 3010，回踩确认再进'


def setup_action(time_ref):
    return dict(symbol_raw='ETH', time_ref=time_ref, entry=dict(kind='zone', price=None, lo=number(3070), hi=number(3115), levels=[]),
                stop=dict(kind='price', price=number(3010), condition=None), tps=[])


def test_setup_card_promotes_conditional_to_main_and_past_to_wide():
    row = promoted(SETUP, **setup_action('conditional'))
    assert row.kind == 'entry_proposal' and row.checks['time_ref'] == 'conditional'
    assert row.checks['time_ref_promoted']['rule'] == 'setup_card' and row.checks['time_ref_promoted']['scope'] == 'main'
    assert row.checks['time_ref_promoted']['from'] == 'conditional' and v2.effective_time_ref(row.checks) == 'now'
    row = promoted(SETUP, **setup_action('past'))
    assert row.kind == 'entry_proposal' and row.checks['time_ref_promoted']['scope'] == 'wide'


def test_short_timeframe_confirmation_is_wide_and_imminent_is_main():
    text = '仿写 BTC 这根15分钟K线收阴就进空，防守114750'
    row = promoted(text, side='short', time_ref='conditional', entry=dict(kind='market_ref', price=None, lo=None, hi=None, levels=[]),
                   stop=dict(kind='price', price=number(114750), condition=None), tps=[])
    assert row.checks['time_ref_promoted']['rule'] == 'short_tf_confirm' and row.checks['time_ref_promoted']['scope'] == 'wide'
    row = promoted('仿写 BTC 准备中长线空一手', side='short', time_ref='conditional', entry=None, stop=None, tps=[])
    assert row.checks['time_ref_promoted']['rule'] == 'imminent' and row.checks['time_ref_promoted']['scope'] == 'main'


@pytest.mark.parametrize('text,time_ref', [('仿写 BTC 明天非农后收线确认再进 入场 100 止损 90', 'conditional'),
                                           ('仿写 BTC 昨夜凌晨发布：现价100做多 止损90', 'past'),
                                           ('仿写 BTC 已成交：入场100 止损90', 'past'),
                                           ('仿写 BTC ✅ TP1 已达，入场100 止损90', 'past'),
                                           ('仿写 BTC 等回踩再看，入场 100 止损 90', 'now')])
def test_promotion_exclusions(text, time_ref):
    row = promoted(text, time_ref=time_ref, entry=dict(kind='limit', price=number(100), lo=None, hi=None, levels=[]))
    assert 'time_ref_promoted' not in row.checks
    assert row.kind == ('entry_proposal' if time_ref == 'now' else 'entry_claimed')
    assert v2.effective_time_ref(row.checks) == time_ref


def test_result_words_are_shared():
    for text in ('止盈了', '✅', 'TP1 已', '+12.5%', '3R ', '已平仓'):
        assert v2.RESULT_WORDS.search(text), text
    assert not v2.RESULT_WORDS.search('入场 3100 止损 3050')


def test_promotion_mutant_is_killed(monkeypatch):
    def invariant():
        rows, _ = v2.parse_actions(envelope(action(**setup_action('conditional'))), SETUP)
        assert rows[0].kind == 'entry_proposal'
        rows, _ = v2.parse_actions(envelope(action(time_ref='past', entry=dict(kind='limit', price=number(100), lo=None, hi=None, levels=[]))),
                                   '仿写 BTC 已成交：入场100 止损90')
        assert rows[0].kind == 'entry_claimed'
    invariant()
    monkeypatch.setattr(v2, 'promote_time_ref', mutant(v2.promote_time_ref, 'if RESULT_WORDS.search(seg) or PROMOTE_EXCLUDE.search(seg):', 'if False:'))
    with pytest.raises(AssertionError):
        invariant()
