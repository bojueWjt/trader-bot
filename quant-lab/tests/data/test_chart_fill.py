"""Invented readings only; all data artifacts are temporary."""
from copy import deepcopy
import json
import polars as pl
import pytest
from quant_lab.data import api, cx_v2, extract, lifecycle, linker, validate
from quant_lab.data.llm import record_key
from test_cx_batch import T0, TEXT, mutant, synthetic_lake
from test_cx_v2 import action, envelope, number


def chart_file(tmp_path, **changes):
    item = dict(readable=True, entry=[999], stop=91, tps=[111, 121], final_target=None, note='仿写图')
    item.update(changes)
    path = tmp_path / 'chart.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(version='chart-read-v1', model='synthetic-vision', items={'s0': item})))
    return path


def prepare(tmp_path, actions, text=TEXT):
    layout, mv = synthetic_lake(tmp_path)
    mv = mv.filter(pl.col('source_version_id') == 's0').with_columns(pl.lit(text).alias('text'))
    mv.write_parquet(layout.message_version)
    row = mv.row(0, named=True)
    system, user = cx_v2.build_prompt(text, channel_name=row['channel_name'], message_date=row['message_date'].isoformat())
    key = record_key(system, user, cx_v2.SCHEMA_NAME)
    fixture = tmp_path / 'llm.json'
    fixture.write_text(json.dumps(dict(version='cx-batch-v2', items={key: dict(response=envelope(*actions))})))
    return layout, mv, fixture


def build(tmp_path, actions, *, text=TEXT, chart_changes=None):
    layout, mv, fixture = prepare(tmp_path, actions, text)
    summary = extract.run(layout, llm_fixture=fixture, chart_fixture=chart_file(tmp_path, **(chart_changes or {})), ingested_at=T0)
    ex = pl.read_parquet(layout.extracted_event)
    validate.run(layout, synthetic=True, ingested_at=T0)
    return layout, mv, ex, pl.read_parquet(layout.canonical_plan), summary


def llm_row(df):
    return df.filter(pl.col('extractor_name') == 'llm').row(0, named=True)


def assert_filled(tmp_path):
    _, mv, ex, cp, _ = build(tmp_path, [action(stop=None, tps=[])], text='仿写 BTC 现价100做多。')
    row = llm_row(cp)
    assert row['stop'] == 91
    assert [t['level'] for t in row['tps']] == [111, 121]
    checks = json.loads(row['checks'])
    assert checks['chart_fill'] == dict(fields=['stop', 'tps'], model='synthetic-vision', version='chart-read-v1')
    assert 'fields_from_chart' in checks['notes']
    assert checks['sl_direction'] and checks['tp_direction']
    assert not any(i['reason'].startswith('evidence_rejected') for i in checks['field_issues'])
    assert checks['action']['stop'] is None
    assert row['entry']['lo'] == 100
    edges, adj, _ = linker.build_candidates(cp, mv, ex, plan_source='llm', ingested_at=T0)
    episodes, *_ = lifecycle.build_graph(cp, mv, edges, adj, None, graph_version='chart-test',
                                         plan_source='llm', extracted_event=ex, ingested_at=T0)
    plans = [p for p in episodes['order_plan'] if p is not None]
    assert len(plans) == 1
    assert plans[0]['stop']['price'] == 91
    assert [t['level'] for t in plans[0]['tps']] == [111, 121]
    parser = ex.filter(pl.col('extractor').struct.field('name') == 'parser').row(0, named=True)
    assert parser['stop'] is None and parser['tps'] == []


def test_single_open_chart_fields_reach_order_plan(tmp_path):
    assert_filled(tmp_path)


def assert_text_wins(tmp_path):
    _, _, _, cp, _ = build(tmp_path, [action()])
    row = llm_row(cp)
    assert row['stop'] == 90
    assert [t['level'] for t in row['tps']] == [110]
    checks = json.loads(row['checks'])
    assert checks['chart_conflict'] == [dict(field='stop', text='90', chart='91'),
        dict(field='tps', text=[dict(kind='price', level='110')], chart=['111', '121'])]
    assert 'chart_fill' not in checks


def test_text_values_win_and_conflicts_are_recorded(tmp_path):
    assert_text_wins(tmp_path)


def test_multi_open_skips_all_candidates(tmp_path):
    _, _, _, cp, _ = build(tmp_path, [action(stop=None, tps=[]), action(symbol_raw='ETH', stop=None, tps=[])])
    rows = cp.filter(pl.col('extractor_name') == 'llm').to_dicts()
    assert len(rows) == 2
    for row in rows:
        assert row['stop'] is None and row['tps'] == []
        checks = json.loads(row['checks'])
        assert checks['chart_fill_skipped'] == 'multi_open' and 'chart_fill' not in checks


def test_wrong_direction_is_still_blocked(tmp_path):
    _, _, _, cp, _ = build(tmp_path, [action(stop=None, tps=[])], chart_changes=dict(stop=101))
    row = llm_row(cp)
    checks = json.loads(row['checks'])
    assert checks['sl_direction'] is False and checks['direction_conflict']
    assert 'INTENT_AMBIGUOUS' in row['reason_codes']
    assert not json.loads(row['eligibility_by_estimand'])['execution']


@pytest.mark.parametrize('changes', [dict(readable=False), dict(stop=None, tps=[], final_target=None)])
def test_unreadable_or_empty_chart_does_not_fill(tmp_path, changes):
    _, _, _, cp, _ = build(tmp_path, [action(stop=None, tps=[])], chart_changes=changes)
    row = llm_row(cp)
    assert row['stop'] is None and row['tps'] == []
    assert 'chart_fill' not in json.loads(row['checks'])


def test_final_target_and_one_now_among_other_actions(tmp_path):
    actions = [action(stop=None, tps=[]), action(time_ref='past', stop=None, tps=[]),
               action(time_ref='conditional', stop=None, tps=[]), action(op='add', stop=None, tps=[])]
    _, _, _, cp, _ = build(tmp_path, actions, chart_changes=dict(tps=[], final_target=125))
    rows = cp.filter(pl.col('extractor_name') == 'llm').sort('branch_index').to_dicts()
    assert rows[0]['tps'][0]['level'] == 125
    for row in rows[1:]:
        assert row['stop'] is None and row['tps'] == []
        assert 'chart_fill' not in json.loads(row['checks'])


def test_no_entry_fill_and_conditional_stop_is_preserved(tmp_path):
    text = '仿写 BTC 做多，日线收盘低于90才止损。'
    stop = dict(kind='condition', price=number(90), condition='日线收盘低于90才止损')
    _, _, _, cp, _ = build(tmp_path, [action(entry=None, stop=stop, tps=[])], text=text)
    row = llm_row(cp)
    checks = json.loads(row['checks'])
    assert row['entry'] is None and row['stop'] is None
    assert checks['chart_fill']['fields'] == ['tps']
    assert checks['action']['stop'] == stop
    assert not json.loads(row['eligibility_by_estimand'])['execution']


def test_v1_and_wrong_source_unchanged(tmp_path):
    chart = extract.load_chart_fixture(chart_file(tmp_path))
    rows = [extract.ParseResult(kind='entry_proposal')]
    before = deepcopy(rows)
    extract.apply_chart_fill(rows, 's0', chart)
    assert rows == before
    rows, _ = cx_v2.parse_actions(envelope(action(stop=None, tps=[])), TEXT)
    before = deepcopy(rows)
    extract.apply_chart_fill(rows, 'unknown-source', chart)
    assert rows == before


def test_no_chart_bytes_and_content_identity(tmp_path):
    layout, _, fixture = prepare(tmp_path, [action(stop=None, tps=[])])
    baseline = extract.run(layout, llm_fixture=fixture, ingested_at=T0)
    old_bytes = layout.extracted_event.read_bytes()
    baseline_ids = pl.read_parquet(layout.extracted_event)['extract_id'].to_list()
    assert extract.run(layout, llm_fixture=fixture, ingested_at=T0, chart_fixture=None) == baseline
    assert layout.extracted_event.read_bytes() == old_bytes
    path = chart_file(tmp_path)
    first = extract.run(layout, llm_fixture=fixture, ingested_at=T0, chart_fixture=path)
    first_bytes = layout.extracted_event.read_bytes()
    first_ids = pl.read_parquet(layout.extracted_event)['extract_id'].to_list()
    same = tmp_path / 'same-chart.json'
    same.write_bytes(path.read_bytes())
    assert extract.run(layout, llm_fixture=fixture, ingested_at=T0, chart_fixture=same) == first
    assert layout.extracted_event.read_bytes() == first_bytes
    chart_file(tmp_path, stop=92)
    second = extract.run(layout, llm_fixture=fixture, ingested_at=T0, chart_fixture=path)
    second_ids = pl.read_parquet(layout.extracted_event)['extract_id'].to_list()
    assert second['build_id'] != first['build_id']
    assert baseline_ids != first_ids != second_ids
    assert extract.run(layout, llm_fixture=fixture, ingested_at=T0) == baseline
    assert layout.extracted_event.read_bytes() == old_bytes


def test_mutations_are_killed(tmp_path, monkeypatch):
    assert_filled(tmp_path / 'good-fill')
    assert_text_wins(tmp_path / 'good-preserve')
    original = extract.apply_chart_fill
    with monkeypatch.context() as patch:
        patch.setattr(extract, 'apply_chart_fill', lambda *args: None)
        with pytest.raises(AssertionError):
            assert_filled(tmp_path / 'no-fill')
    broken = mutant(original, 'if result.stop is None and not condition_stop:', 'if not condition_stop:')
    with monkeypatch.context() as patch:
        patch.setattr(extract, 'apply_chart_fill', broken)
        with pytest.raises(AssertionError):
            assert_text_wins(tmp_path / 'overwrite-text')


def test_cli_passes_chart_fixture(tmp_path, monkeypatch):
    captured = {}
    def fake_build(fixture, layout, **kwargs):
        captured.update(kwargs)
        return {}
    monkeypatch.setattr(api, 'build', fake_build)
    path = chart_file(tmp_path)
    assert api.main(['--build', '--fixture', str(tmp_path), '--out', str(tmp_path / 'out'),
                     '--chart-fixture', str(path)]) == 0
    assert captured['chart_fixture'] == str(path)


def test_build_scopes_include_chart_content(tmp_path, monkeypatch):
    from quant_lab.data import dedup, normalize
    monkeypatch.setenv('QUANT_LAB_DATA_ROOT', str(tmp_path / 'data'))
    layout, _, _ = prepare(tmp_path, [action(stop=None, tps=[])])
    seen = []
    def fake_extract(work, **kwargs):
        seen.append((work, kwargs))
        return {}
    for module in (normalize, dedup, validate, linker, lifecycle):
        monkeypatch.setattr(module, 'run', lambda *args, **kwargs: {})
    monkeypatch.setattr(extract, 'run', fake_extract)
    path = chart_file(tmp_path)
    api.build(tmp_path, layout, graph_version='none')
    api.build(tmp_path, layout, graph_version='one', chart_fixture=path)
    api.build(tmp_path, layout, graph_version='same', chart_fixture=path)
    chart_file(tmp_path, stop=92)
    api.build(tmp_path, layout, graph_version='two', chart_fixture=path)
    scopes = [work.silver_dir for work, _ in seen]
    assert scopes[0] != scopes[1] == scopes[2] != scopes[3]
    assert seen[1][1]['chart_fixture'] == path
    assert 'chart_fixture' not in seen[0][1]


@pytest.mark.parametrize('bad', [True, '91', 0, -1, float('nan'), float('inf'), 1e30, 0.0000000000001])
def test_invalid_chart_prices_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        extract.load_chart_fixture(chart_file(tmp_path, stop=bad))


def test_decimal_chart_prices_exact(tmp_path):
    path = chart_file(tmp_path)
    path.write_text(path.read_text().replace('"stop": 91', '"stop": 91.123456789123'))
    chart = extract.load_chart_fixture(path)
    assert str(chart['items']['s0']['stop']) == '91.123456789123'
