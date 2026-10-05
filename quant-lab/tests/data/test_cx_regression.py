"""Fabricated gold and replies in pytest tmp_path; never read pilot files."""
from copy import deepcopy
import json

import pytest

from quant_lab.data import cx_batch as cx, cx_regression as regression
from test_cx_v2 import action, envelope, number
from test_cx_batch import TEXT


def corpus(tmp_path):
    source = tmp_path / 'source.jsonl'
    truths = []
    inputs = []
    for i in range(4):
        identity = f'fiction-{i}'
        inputs.append(dict(item_id=identity, channel='虚构频道', message_time=str(i), text=TEXT, previous_text=None))
        truths.append(dict(item_id=identity, channel='虚构频道', truth_op='open' if i < 3 else 'analysis',
                           symbol='BTC', side='long', entry=dict(kind='market_ref', lo=100, hi=100), stop=90, tps=[110],
                           image_affected_fields=['stop'] if i == 2 else []))
    source.write_text(''.join(cx.dumps(i) + '\n' for i in inputs))
    truth = tmp_path / 'truth.json'
    truth.write_text(cx.dumps(dict(source_path=str(source), items=truths)))
    prompts = tmp_path / 'prompts'
    regression.prepare(truth, prompts)
    return truth, prompts, list(cx.read_jsonl(prompts))


def test_message_denominators_include_discard_and_wrong_label_fields(tmp_path):
    truth, prompts, rows = corpus(tmp_path)
    responses = tmp_path / 'responses'
    records = [dict(key=rows[0]['key'], response=envelope(action(op='analysis'))),
               dict(key=rows[1]['key'], abstain=dict(note='model_undecidable')),
               dict(key=rows[2]['key'], response=envelope(action())),
               dict(key=rows[3]['key'], response=envelope(action()))]
    responses.write_text(''.join(cx.dumps(r) + '\n' for r in records))
    report = regression.check(truth, prompts, responses)
    metrics = report['overall']['metrics']
    assert metrics['open_false_discovery']['numerator'] == 1 and metrics['open_false_discovery']['denominator'] == 2
    assert metrics['open_miss']['numerator'] == 2 and metrics['open_miss']['denominator'] == 3
    assert metrics['entry']['numerator'] == 2 and metrics['entry']['denominator'] == 3
    assert metrics['stop']['numerator'] == 1 and metrics['stop']['denominator'] == 2
    assert metrics['whole_message_discarded']['numerator'] == 1
    assert metrics['model_uncertain']['numerator'] == 1
    assert metrics['unjustified_discard']['numerator'] == 1 and metrics['unjustified_discard']['denominator'] == 1
    assert not report['overall']['passes'] and report['blind_test'] is False


def test_field_failure_counted_without_message_discard_and_evidence_rechecked(tmp_path):
    truth, prompts, rows = corpus(tmp_path)
    responses = tmp_path / 'responses'
    records = [dict(key=r['key'], response=envelope(action(stop=dict(kind='price', price=number(91, '90'), condition=None)))) for r in rows]
    responses.write_text(''.join(cx.dumps(r) + '\n' for r in records))
    result = regression.check(truth, prompts, responses)
    assert result['overall']['field_evidence_failed'] == 4
    assert result['overall']['metrics']['whole_message_discarded']['numerator'] == 0
    assert result['overall']['metrics']['stop']['numerator'] == 0
    with pytest.raises(ValueError, match='cannot_approve'):
        regression.check(truth, prompts, responses, adjudications={'fiction-0': {'stop': dict(correct=True, reason='test')}})


def test_missing_responses_are_not_dropped_and_unsupported_semantics_never_pass(tmp_path):
    truth, prompts, rows = corpus(tmp_path)
    doc = regression.load(truth)
    doc['items'][0]['entry']['qualifier'] = '超过'
    cx.write_json(truth, doc)
    responses = tmp_path / 'responses'
    responses.write_text(cx.dumps(dict(key=rows[0]['key'], response=envelope(action()))) + '\n')
    result = regression.check(truth, prompts, responses)
    assert result['population'] == 4 and result['missing_responses'] == 3
    assert result['overall']['metrics']['entry']['denominator'] == 3
    assert result['overall']['pending_fields'] == 1
    assert not result['items'][0]['fields']['entry']['correct']
    assert not result['overall']['passes']
    rows[0]['text'] = 'tampered'
    cx._atomic_text(prompts, ''.join(cx.dumps(r) + '\n' for r in rows))
    with pytest.raises(ValueError, match='prompt_source_mismatch'):
        regression.check(truth, prompts, responses)


def test_gold_grouping_and_percent_condition_comparison():
    a = action(symbol_raw='AAA', entry=dict(kind='limit', price=number(100), lo=None, hi=None, levels=[]))
    b = action(symbol_raw='BBB', entry=dict(kind='limit', price=number(200), lo=None, hi=None, levels=[]))
    gold = dict(entry=dict(by_symbol={'AAA': dict(kind='limit', lo=100, hi=100), 'BBB': dict(kind='limit', lo=200, hi=200)}))
    assert regression.compare_field('entry', gold, [a, b]) == (True, None)
    b['entry']['price'] = number(100)
    assert regression.compare_field('entry', gold, [a, b]) == (False, None)
    a['tps'] = [dict(kind='percent', value=number(10, '10%'))]
    assert regression.compare_field('tps', dict(tps=dict(relative_percent=[10])), [a]) == (True, None)
    a['stop'] = dict(kind='condition', condition='周线收盘跌破均线', price=None)
    assert regression.compare_field('stop', dict(stop=dict(condition='周线收盘跌破均线')), [a]) == (True, None)


def test_numberless_market_entry_is_not_absent_entry():
    a = action(entry=dict(kind='market_ref', price=None, lo=None, hi=None, levels=[]))
    assert regression.compare_field('entry', dict(entry=dict(kind='market_ref', lo=None, hi=None)), [a]) == (True, None)
    assert regression.compare_field('entry', dict(entry=None), [a]) == (False, None)


def test_open_fields_scored_on_now_open_actions_not_sibling_analysis(tmp_path):
    truth, prompts, rows = corpus(tmp_path)
    responses = tmp_path / 'responses'
    sibling = action(op='analysis', symbol_raw='ETH', side='short')
    records = [dict(key=rows[0]['key'], response=envelope(action(), sibling)),
               dict(key=rows[1]['key'], response=envelope(action(op='analysis'), sibling))]
    responses.write_text(''.join(cx.dumps(r) + '\n' for r in records))
    items = {r['item_id']: r for r in regression.check(truth, prompts, responses)['items']}
    assert items['fiction-0']['fields']['symbol']['correct'] and items['fiction-0']['fields']['side']['correct']
    # Without a predicted open, every action is still scored, so a missed open cannot look field-perfect.
    assert not items['fiction-1']['fields']['symbol']['correct']


def test_cmp_leg_compared_only_where_gold_states_it():
    ladder = action(entry=dict(kind='ladder', price=None, lo=None, hi=None, levels=[
        dict(kind='market_ref', price=None, fraction=None), dict(kind='limit', price=number(95), fraction=None)]))
    limit_only = action(entry=dict(kind='limit', price=number(95), lo=None, hi=None, levels=[]))
    gold = dict(entry=dict(levels=['95']), entry_market_leg=True)
    assert regression.compare_field('entry', dict(gold), [ladder]) == (True, None)
    assert regression.compare_field('entry', dict(gold), [limit_only]) == (False, None)
    assert regression.compare_field('entry', dict(gold, entry_market_leg=False), [ladder]) == (False, None)
    unstated = dict(entry=dict(levels=['95']))
    assert regression.compare_field('entry', unstated, [ladder]) == (True, None)
    assert unstated['_leg_seen'] == [False]
    grouped = dict(entry=dict(by_symbol={'BTC': dict(levels=['95'])}), entry_market_leg=dict(by_symbol={'BTC': True}))
    assert regression.compare_field('entry', grouped, [ladder]) == (True, None) and grouped['_leg_seen'] == [True]
    assert regression.compare_field('symbol', dict(symbol='UNI'), [action(symbol_raw='#UNI/USDT')]) == (True, None)


def test_quoted_cmp_price_matches_gold_levels_through_the_leg_flag():
    quoted = action(entry=dict(kind='ladder', price=None, lo=None, hi=None, levels=[
        dict(kind='market_ref', price=number(104), fraction=None), dict(kind='limit', price=number(95), fraction=None)]))
    gold = dict(entry=dict(levels=['104', '95']), entry_market_leg=True)
    assert regression.compare_field('entry', dict(gold), [quoted]) == (True, None)
    # Same prices without any CMP leg is still wrong when gold says there is one.
    both_limits = deepcopy(quoted)
    both_limits['entry']['levels'][0]['kind'] = 'limit'
    assert regression.compare_field('entry', dict(gold), [both_limits]) == (False, None)
    assert regression.compare_field('entry', dict(entry=dict(kind='market_ref', lo='459', hi='459')),
                                    [action(entry=dict(kind='limit', price=number(459), lo=None, hi=None, levels=[]))]) == (False, None)


def test_condition_stops_compare_cited_levels_and_split_entries_merge():
    cond = action(stop=dict(kind='condition', price=number(6.36), condition='小幅跌破6.36一点。'))
    assert regression.compare_field('stop', dict(stop=dict(condition='小幅跌破6.36一点')), [cond]) == (True, None)
    assert regression.compare_field('stop', dict(stop=dict(condition='小幅跌破6.3一点')), [cond]) == (False, None)
    a = action(entry=dict(kind='limit', price=number(108000), lo=None, hi=None, levels=[]))
    b = action(entry=dict(kind='limit', price=number(106000), lo=None, hi=None, levels=[]))
    gold = dict(entry=dict(levels=['108000', '106000']), entry_market_leg=False)
    assert regression.compare_field('entry', dict(gold), [a, b]) == (True, None)
    assert regression.compare_field('entry', dict(gold), [a]) == (False, None)
    other = action(symbol_raw='ETH', entry=dict(kind='limit', price=number(106000), lo=None, hi=None, levels=[]))
    # Different symbols never pool their prices.
    assert regression.compare_field('entry', dict(gold), [a, other]) == (False, None)


def test_zone_and_two_level_ladder_with_same_endpoints_compare_equal():
    ladder = action(entry=dict(kind='ladder', price=None, lo=None, hi=None, levels=[
        dict(kind='market_ref', price=number(85500), fraction=None), dict(kind='limit', price=number(84000), fraction=None)]))
    gold = dict(entry=dict(kind='zone', lo='84000', hi='85500'), entry_market_leg=True)
    assert regression.compare_field('entry', dict(gold), [ladder]) == (True, None)
    assert regression.compare_field('entry', dict(gold, entry=dict(kind='zone', lo='84000', hi='86000')), [ladder]) == (False, None)


def test_non_numeric_gold_value_is_pending_not_a_crash():
    assert regression.compare_field('stop', dict(stop='0.26美元'), [action()]) == (False, 'semantic_comparison_requires_independent_review')


def test_second_scenario_scored_apart_and_counted(tmp_path):
    truth, prompts, rows = corpus(tmp_path)
    responses = tmp_path / 'responses'
    other = action(side='short', stop=None, tps=[])
    responses.write_text(cx.dumps(dict(key=rows[0]['key'], response=envelope(action(), other))) + '\n')
    report = regression.check(truth, prompts, responses)
    item = report['items'][0]
    assert all(item['fields'][f]['correct'] for f in ('side', 'stop', 'tps')) and item['extra_open_actions'] == 1
    assert report['overall']['messages_with_extra_open_actions'] == 1
    # Without an open on the gold side, nothing is aligned and the fields fail.
    responses.write_text(cx.dumps(dict(key=rows[0]['key'], response=envelope(other))) + '\n')
    assert not regression.check(truth, prompts, responses)['items'][0]['fields']['side']['correct']


def test_promoted_open_is_a_separate_column(tmp_path):
    # TEXT is a labelled setup card ("入场 100，止损 90"): a conditional open there is promoted (v8 F11).
    truth, prompts, rows = corpus(tmp_path)
    responses = tmp_path / 'responses'
    responses.write_text(''.join(cx.dumps(dict(key=r['key'], response=envelope(action(time_ref='conditional')))) + '\n' for r in rows))
    report = regression.check(truth, prompts, responses)
    item = report['items'][0]
    # The model's own classification stays the scored one.
    assert item['predicted_open'] is False and item['predicted_open_promoted'] is True and item['promoted_open_actions'] == 1
    assert report['overall']['metrics']['open_miss']['numerator'] == 3
    assert report['overall']['open_after_promotion'] == dict(predicted=4, promoted_only=4, promoted_only_truth_open=3)
