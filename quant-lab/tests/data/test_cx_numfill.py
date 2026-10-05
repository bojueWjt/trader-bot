"""v8 F5b cx.numfill.v1 side pass. Invented messages and recordings only."""
from decimal import Decimal
import json

import polars as pl
import pytest

from quant_lab.data import cx_batch as cx, cx_numfill, cx_v2, extract, validate
from quant_lab.data.llm import record_key
from test_cx_batch import T0, mutant
from test_cx_v2 import action, envelope, number
from test_chart_fill import prepare

TEXT = '仿写 BTC 6万3附近多，小幅跌破6万2止损，目标6万8'
REFUSED = [dict(field='entry', reason='原文省略单位，不能猜'), dict(field='stop', reason='6万2 需要换算，不可擅自补成数字')]


def refused_action(**changes):
    row = action(entry=None, stop=None, tps=[dict(kind='price', value=number(68000, '6万8'))], field_issues=list(REFUSED))
    row.update(changes)
    return row


def clean(text, *actions):
    return cx_v2.validate_response(envelope(*actions), text)['actions']


def test_targets_only_refused_numeric_fields_in_x_wan_y_messages():
    targets = cx_numfill.targets_for(clean(TEXT, refused_action()), TEXT)
    assert [(t['branch_index'], t['field'], t['known']) for t in targets] == [(0, 'entry.price', ['68000']), (0, 'stop.price', ['68000'])]
    # Rejected evidence is a validator matter (F5a), not a refusal; a message without X万Y has nothing to fill.
    rejected = refused_action(field_issues=[dict(field='entry', reason='evidence_rejected:quote_not_in_current_text')])
    assert cx_numfill.targets_for(clean(TEXT, rejected), TEXT) == []
    assert cx_numfill.targets_for(clean('仿写 BTC 63000附近多', refused_action(tps=[])), '仿写 BTC 63000附近多') == []
    # A field that already has a value is never a target.
    filled = refused_action(stop=dict(kind='price', price=number(62000, '6万2'), condition=None))
    assert [t['field'] for t in cx_numfill.targets_for(clean(TEXT, filled), TEXT)] == ['entry.price']


def test_validation_uses_the_tokenizer_magnitude_and_targets():
    context = {'targets': [dict(branch_index=0, field='entry.price', known=['68000']), dict(branch_index=0, field='tps', known=['68000'])]}
    item = dict(schema_version=cx_numfill.SCHEMA_NAME, fills=[
        dict(branch_index=0, field='entry.price', value=dict(value='63000', quote='6万3')),
        dict(branch_index=0, field='entry.price', value=dict(value='63000', quote='6万3')),
        dict(branch_index=0, field='stop.price', value=dict(value='62000', quote='6万2')),
        dict(branch_index=0, field='tps[0].value', value=dict(value='6', quote='6万8')),
        dict(branch_index=0, field='tps', value=None)])
    response = cx_numfill.validate_response(item, TEXT, context)['response']
    assert response['fills'] == [dict(branch_index=0, field='entry.price', value=dict(value='63000', quote='6万3'))]
    assert [r['reason'] for r in response['rejected']] == ['duplicate_fill', 'not_a_target', 'evidence_rejected']
    assert response['stats'] == dict(filled=1, rejected=3, unfilled=1)
    far = dict(item, fills=[dict(branch_index=0, field='entry.price', value=dict(value='6', quote='6万3附近多'))])
    assert cx_numfill.validate_response(far, TEXT, context)['response']['fills'] == []
    small = {'targets': [dict(branch_index=0, field='entry.price', known=['680000'])]}
    assert cx_numfill.validate_response(item, TEXT, small)['response']['rejected'][0]['reason'] == 'magnitude_mismatch'
    assert 'abstain' in cx_numfill.validate_response(dict(fills=[]), TEXT, context)


def lake_with_numfill(tmp_path, actions, fills, text=TEXT):
    layout, mv, fixture = prepare(tmp_path, actions, text)
    prompts = tmp_path / 'main.jsonl'
    cx.export_prompts(layout, prompts)
    main_key = next(cx.read_jsonl(prompts))['key']
    recording = tmp_path / 'recorded-main.json'
    recording.write_text(cx.dumps(dict(version='cx-batch-v2', items={main_key: dict(response=envelope(*actions))})))
    stats = cx_numfill.export(prompts, recording, tmp_path / 'l2.jsonl')
    rows = list(cx.read_jsonl(tmp_path / 'l2.jsonl'))
    responses = tmp_path / 'responses.jsonl'
    answers = []
    for row in rows:
        context = cx_numfill.contexts_from_user(row['user'])
        answers.append(dict(key=row['key'], **cx.quote_response(dict(schema_version=cx_numfill.SCHEMA_NAME, fills=fills), row['text'],
                                                                 candidates=context, expected_schema=row['schema_name'])))
    responses.write_text(''.join(cx.dumps(a) + '\n' for a in answers))
    numfill = tmp_path / 'recorded-numfill.json'
    cx.import_responses(responses, numfill)
    return layout, fixture, numfill, stats, rows


FILLS = [dict(branch_index=0, field='entry.price', value=dict(value='63000', quote='6万3')),
         dict(branch_index=0, field='stop.price', value=dict(value='62000', quote='6万2'))]


def test_export_fixture_and_extract_merge_then_rule_nine(tmp_path):
    layout, fixture, numfill, stats, rows = lake_with_numfill(tmp_path, [refused_action()], FILLS)
    assert stats == dict(prompts=1, recorded=1, messages=1, targets=2, invalid_recording=0)
    user = json.loads(rows[0]['user'])
    assert rows[0]['system'] == cx_numfill.RULES and user['schema_version'] == cx_numfill.SCHEMA_NAME and user['source_key']
    summary = extract.run(layout, llm_fixture=fixture, numfill_fixture=numfill, ingested_at=T0)
    assert summary['llm']['numfill_applied'] == 1 and summary['numfill_fixture_sha256']
    row = pl.read_parquet(layout.extracted_event).filter(pl.col('extractor').struct.field('name') == 'llm').row(0, named=True)
    checks = json.loads(row['checks'])
    assert row['entry']['lo'] == Decimal(63000)
    # Filled 62000 sits in a fuzzy break clause: F4 widens it once, after the merge.
    assert row['stop'] == Decimal('61814') and checks['stop_rule']['rule'] == 'r9_fuzzy_break' and checks['stop_rule']['base'] == '62000'
    assert checks['numfill']['fields'] == ['entry.price', 'stop.price'] and checks['numfill']['recording_version'] == cx_numfill.IMPORT_VERSION
    assert checks['action']['entry']['price'] == {'value': '63000', 'quote': '6万3'}
    assert checks['action']['stop']['price'] == {'value': '62000', 'quote': '6万2'}
    assert {(s['field'], TEXT[s['start']:s['end']]) for s in row['spans']} >= {('entry.price', '6万3'), ('stop.price', '6万2')}
    assert ':numfill:' in row['extractor']['version']
    validate.run(layout, synthetic=True, ingested_at=T0)
    plan = pl.read_parquet(layout.canonical_plan).filter(pl.col('extractor_name') == 'llm').row(0, named=True)
    assert plan['stop'] == Decimal('61814') and plan['entry']['lo'] == Decimal(63000)


def test_merge_only_fills_nulls_and_missing_recording_is_counted(tmp_path):
    layout, fixture, numfill, _, _ = lake_with_numfill(tmp_path, [refused_action()], FILLS)
    doc = json.loads(numfill.read_text())
    doc['items'] = {}
    numfill.write_text(json.dumps(doc))
    summary = extract.run(layout, llm_fixture=fixture, numfill_fixture=numfill, ingested_at=T0)
    assert summary['llm']['numfill_missing'] == 1 and summary['llm']['numfill_applied'] == 0
    row = pl.read_parquet(layout.extracted_event).filter(pl.col('extractor').struct.field('name') == 'llm').row(0, named=True)
    assert row['entry'] is None and 'numfill' not in json.loads(row['checks'])
    with pytest.raises(ValueError):
        bad = tmp_path / 'bad.json'
        bad.write_text(json.dumps(dict(version='cx-batch-v2', items={})))
        cx_numfill.load_fixture(bad)


def test_only_null_mutant_is_killed(tmp_path):
    action_with_stop = refused_action(stop=dict(kind='price', price=number(68000, '6万8'), condition=None))
    actions = clean(TEXT, action_with_stop)
    assert not cx_numfill._set_atom(actions[0], 'stop.price', dict(value='62000', quote='6万2'))
    broken = mutant(cx_numfill._set_atom, 'if stop["kind"] == "price" and stop.get("price") is None:', 'if True:')
    assert broken(clean(TEXT, action_with_stop)[0], 'stop.price', dict(value='62000', quote='6万2'))


def test_prompt_key_follows_the_main_prompt(tmp_path):
    targets = [dict(branch_index=0, op='open', symbol='BTC', side='long', field='entry.price', known=[])]
    a = cx_numfill.build_prompt(TEXT, channel_name='仿写', message_date='2024-07-01', source_key='k1', targets=targets)
    b = cx_numfill.build_prompt(TEXT, channel_name='仿写', message_date='2024-07-01', source_key='k2', targets=targets)
    assert record_key(*a, cx_numfill.SCHEMA_NAME) != record_key(*b, cx_numfill.SCHEMA_NAME)
    assert a == cx_numfill.build_prompt(TEXT, channel_name='仿写', message_date='2024-07-01', source_key='k1', targets=targets)
