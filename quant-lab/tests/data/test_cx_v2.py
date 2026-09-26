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
    actions = [action(), action(time_ref='past'), action(time_ref='conditional'), action(symbol_raw='ETH')]
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


def test_condition_and_percent_mapping_no_guessed_basis(tmp_path):
    text = '仿写 BTC 现价100做多；日线收盘低于90才止损；目标10%、20%。'
    stop = dict(kind='condition', price=number(90), condition='日线收盘低于90才止损')
    tps = [dict(kind='percent', value=number(v, f'{v}%')) for v in (10, 20)]
    _, _, ex, cp, _ = v2_lake(tmp_path, [action(stop=stop, tps=tps)], text)
    row = cp.filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    assert [t['level'] for t in row['tps']] == [Decimal(110), Decimal(120)]
    assert row['stop'] is None
    checks = json.loads(row['checks'])
    assert checks['mapping_issues'] == [dict(field='stop', reason='condition_not_supported_by_order_plan')]
    assert not json.loads(row['eligibility_by_estimand'])['execution']
    plan = lifecycle._order_plan(row, row['stop'], row['tps'], None)
    assert plan['stop'] is None and plan['tps'][0]['level'] == 110
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
    assert row['entry_ref'] == Decimal(100)  # Existing direction checks still use their reference.
    assert any(i['reason'] == 'percent_requires_unambiguous_entry_and_side' for i in json.loads(row['checks'])['mapping_issues'])
    plan = lifecycle._order_plan(row, row['stop'], row['tps'], None)
    assert [e['kind'] for e in plan['entries']] == ['market_ref', 'limit']
    assert [e['fraction'] for e in plan['entries']] == [Decimal('.5')] * 2
    # Single known entry makes the short percent target unambiguous.
    _, _, _, single, _ = v2_lake(tmp_path / 'single', [action(side='short', stop=dict(kind='price', price=number(120), condition=None),
                                                         tps=[dict(kind='percent', value=number(10, '10%'))])], text)
    assert single.filter(pl.col('extractor_name') == 'llm')['tps'][0][0]['level'] == 90


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
