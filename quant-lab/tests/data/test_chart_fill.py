"""Invented readings only; all data artifacts are temporary."""
from copy import deepcopy
from decimal import Decimal
import json
import polars as pl
import pytest
from quant_lab.data import api, cx_v2, extract, lifecycle, linker, validate
from quant_lab.data.llm import record_key
from test_cx_batch import T0, TEXT, mutant, synthetic_lake
from test_cx_v2 import action, envelope, number


def chart_file(tmp_path, **changes):
    item = dict(readable=True, entry=[], stop=91, tps=[111, 121], final_target=None, note='仿写图')
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
    _, _, _, cp, _ = build(tmp_path, [action(stop=None, tps=[]), action(symbol_raw='ETH', stop=None, tps=[])],
                          chart_changes=dict(entry=[62500]))
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


def test_chart_entry_fill_and_conditional_stop_is_preserved(tmp_path):
    text = '仿写 BTC 做多，日线收盘低于61000才止损。'
    stop = dict(kind='condition', price=number(61000), condition='日线收盘低于61000才止损')
    _, _, _, cp, _ = build(tmp_path, [action(entry=None, stop=stop, tps=[])], text=text,
                          chart_changes=dict(entry=[62500], stop=61050, tps=[65000]))
    row = llm_row(cp)
    checks = json.loads(row['checks'])
    assert row['entry'] == dict(kind='limit', lo=62500, hi=62500) and row['stop'] == Decimal(61000)
    assert checks['chart_fill']['fields'] == ['entry', 'tps']
    assert checks['action']['stop'] == stop
    assert checks['stop_trigger'] == dict(basis='close', timeframe='1d', condition=stop['condition'])
    assert json.loads(row['eligibility_by_estimand'])['execution']
    plan = lifecycle._order_plan(row, row['stop'], row['tps'], None)
    assert plan['stop'] == dict(price=Decimal(61000), trigger='close', timeframe='1d')


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


@pytest.mark.parametrize('final_only', [False, True])
def test_all_percent_tps_replaced_by_chart_and_execute(tmp_path, final_only):
    text = '仿写 BTC 入场62500做多；止损61000；目标25%、50%、75%、100%，价位见仿写图。'
    tps = [dict(kind='percent', value=number(v, f'{v}%')) for v in (25, 50, 75, 100)]
    original = action(entry=dict(kind='limit', price=number(62500), lo=None, hi=None, levels=[]),
                      stop=dict(kind='price', price=number(61000), condition=None), tps=tps)
    changes = dict(stop=61000, tps=[] if final_only else [64000, 65000], final_target=66000 if final_only else None)
    _, _, _, cp, _ = build(tmp_path, [original], text=text, chart_changes=changes)
    row = llm_row(cp)
    assert [t['level'] for t in row['tps']] == ([66000] if final_only else [64000, 65000])
    checks = json.loads(row['checks'])
    assert checks['chart_fill']['fields'] == ['tps']
    assert checks['chart_fill']['replaced_percent_tps'] == ['25', '50', '75', '100']
    assert checks['action']['tps'] == tps
    assert 'tp_pct_converted' not in checks
    assert json.loads(row['eligibility_by_estimand'])['execution']


def test_mixed_price_percent_tps_are_not_replaced(tmp_path):
    text = '仿写 BTC 入场62500做多；止损61000；目标64000和10%。'
    tps = [dict(kind='price', value=number(64000)), dict(kind='percent', value=number(10, '10%'))]
    original = action(entry=dict(kind='limit', price=number(62500), lo=None, hi=None, levels=[]),
                      stop=dict(kind='price', price=number(61000), condition=None), tps=tps)
    _, _, _, cp, _ = build(tmp_path, [original], text=text, chart_changes=dict(stop=61000, tps=[65000]))
    row = llm_row(cp)
    checks = json.loads(row['checks'])
    assert [t['level'] for t in row['tps']] == [64000, 68750]
    assert checks['chart_conflict'] == [dict(field='tps', text=[dict(kind='price', level='64000'),
        dict(kind='pct', level='10')], chart=['65000'])]
    assert 'chart_fill' not in checks
    assert json.loads(row['eligibility_by_estimand'])['execution']


@pytest.mark.parametrize('prices,kind,entries', [([62500], 'limit', []), ([62600, 62400], 'zone', []),
                                               ([62600, 62400, 62500], 'ladder', [62400, 62500, 62600])])
def test_missing_entry_chart_fill_executes(tmp_path, prices, kind, entries):
    text = '仿写 BTC 做多；入场见仿写图中方框；止损61000；止盈65000。'
    original = action(entry=None, stop=dict(kind='price', price=number(61000), condition=None),
                      tps=[dict(kind='price', value=number(65000))])
    _, _, ex, cp, _ = build(tmp_path, [original], text=text,
        chart_changes=dict(entry=prices, stop=61000, tps=[65000]))
    row = llm_row(cp)
    assert row['entry'] == dict(kind=kind, lo=min(prices), hi=max(prices))
    assert row['entries'] == entries and row['entry_mode'] == 'price'
    extracted = ex.filter(pl.col('extractor').struct.field('name') == 'llm').row(0, named=True)
    assert extracted['entry_mode'] == 'price'
    checks = json.loads(row['checks'])
    assert checks['chart_fill']['fields'] == ['entry'] and checks['action']['entry'] is None
    assert row['scale_gate'] == 'ok'
    assert json.loads(row['eligibility_by_estimand'])['execution']
    assert lifecycle._order_plan(row, row['stop'], row['tps'], None) is not None


@pytest.mark.parametrize('with_price', [False, True])
def test_market_ref_entry_is_not_filled(tmp_path, with_price):
    text = '仿写 BTC 现价62500做多；止损61000；止盈65000。'
    entry = dict(kind='market_ref', price=number(62500) if with_price else None, lo=None, hi=None, levels=[])
    original = action(entry=entry, stop=dict(kind='price', price=number(61000), condition=None),
                      tps=[dict(kind='price', value=number(65000))])
    _, _, _, cp, _ = build(tmp_path, [original], text=text, chart_changes=dict(entry=[62400], stop=61000, tps=[65000]))
    row = llm_row(cp)
    assert row['entry_mode'] == 'market_ref'
    assert row['entry'] == (dict(kind='market_ref', lo=62500, hi=62500) if with_price else None)
    checks = json.loads(row['checks'])
    assert checks['chart_conflict'] == [dict(field='entry', text=['62500'] if with_price else [], chart=['62400'])]
    assert 'chart_fill' not in checks
    assert json.loads(row['eligibility_by_estimand'])['execution']


def test_text_entry_only_records_chart_conflict(tmp_path):
    _, _, _, cp, _ = build(tmp_path, [action()], chart_changes=dict(entry=[99, 101], stop=90, tps=[110]))
    row = llm_row(cp)
    assert row['entry'] == dict(kind='market_ref', lo=100, hi=100)
    checks = json.loads(row['checks'])
    assert checks['chart_conflict'] == [dict(field='entry', text=['100'], chart=['99', '101'])]
    assert 'chart_fill' not in checks


def test_all_percent_tps_without_chart_targets_are_preserved(tmp_path):
    text = '仿写 BTC 入场62500做多；止损61000；目标10%。'
    original = action(entry=dict(kind='limit', price=number(62500), lo=None, hi=None, levels=[]),
                      stop=dict(kind='price', price=number(61000), condition=None),
                      tps=[dict(kind='percent', value=number(10, '10%'))])
    rows, _ = cx_v2.parse_actions(envelope(original), text)
    chart = extract.load_chart_fixture(chart_file(tmp_path, stop=61000, tps=[], final_target=None))
    extract.apply_chart_fill(rows, 's0', chart)
    assert rows[0].tps == [dict(kind='pct', level=10, fraction=None)]
    assert 'chart_fill' not in rows[0].checks


def test_text_limit_entry_is_preserved_and_equal_chart_is_not_conflict(tmp_path):
    text = '仿写 BTC 入场62500做多；止损61000；止盈65000。'
    original = action(entry=dict(kind='limit', price=number(62500), lo=None, hi=None, levels=[]),
                      stop=dict(kind='price', price=number(61000), condition=None),
                      tps=[dict(kind='price', value=number(65000))])
    for prices in ([62400], [62500]):
        rows, _ = cx_v2.parse_actions(envelope(original), text)
        chart = extract.load_chart_fixture(chart_file(tmp_path, entry=prices, stop=61000, tps=[65000]))
        extract.apply_chart_fill(rows, 's0', chart)
        assert rows[0].entry == dict(kind='limit', lo=62500, hi=62500)
        assert 'chart_fill' not in rows[0].checks
        if prices == [62400]:
            assert rows[0].checks['chart_conflict'] == [dict(field='entry', text=['62500'], chart=['62400'])]
        else:
            assert 'chart_conflict' not in rows[0].checks


@pytest.mark.parametrize('bad', [None, True, '62500', 0, -1, float('nan'), float('inf'), 1e30, 0.0000000000001])
def test_invalid_chart_entry_prices_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        extract.load_chart_fixture(chart_file(tmp_path, entry=[bad]))


@pytest.mark.parametrize('bad', [None, True, 62500, '62500', {}])
def test_invalid_chart_entry_container_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        extract.load_chart_fixture(chart_file(tmp_path, entry=bad))


def test_optional_chart_entry_defaults_to_empty_and_is_decimal(tmp_path):
    path = chart_file(tmp_path)
    raw = json.loads(path.read_text())
    del raw['items']['s0']['entry']
    path.write_text(json.dumps(raw))
    assert extract.load_chart_fixture(path)['items']['s0']['entry'] == []
    path = chart_file(tmp_path, entry=[62500])
    path.write_text(path.read_text().replace('"entry": [62500]', '"entry": [62500.123456789123]'))
    assert str(extract.load_chart_fixture(path)['items']['s0']['entry'][0]) == '62500.123456789123'


def test_percent_tp_and_entry_mutations_are_killed(tmp_path, monkeypatch):
    original = extract.apply_chart_fill
    test_all_percent_tps_replaced_by_chart_and_execute(tmp_path / 'good-tp', False)
    test_missing_entry_chart_fill_executes(tmp_path / 'good-entry', [62500], 'limit', [])
    mutations = [
        ('if not result.tps or all_percent:', 'if not result.tps:',
         lambda path: test_all_percent_tps_replaced_by_chart_and_execute(path, False)),
        ('if chart_entry:', 'if False:',
         lambda path: test_missing_entry_chart_fill_executes(path, [62500], 'limit', [])),
    ]
    for index, (before, after, invariant) in enumerate(mutations):
        with monkeypatch.context() as patch:
            patch.setattr(extract, 'apply_chart_fill', mutant(original, before, after))
            with pytest.raises(AssertionError):
                invariant(tmp_path / f'mutant-{index}')
    with monkeypatch.context() as patch:
        patch.setattr(extract, 'load_chart_fixture', mutant(extract.load_chart_fixture,
            'entry=[price(v) for v in entries]', 'entry=[]'))
        with pytest.raises(AssertionError):
            test_missing_entry_chart_fill_executes(tmp_path / 'mutant-loader', [62500], 'limit', [])


def test_mapping_version_changes_extractor_and_build_identity(tmp_path, monkeypatch):
    layout, _, fixture = prepare(tmp_path, [action()])
    path = chart_file(tmp_path)
    first = extract.run(layout, llm_fixture=fixture, chart_fixture=path, ingested_at=T0)
    first_row = pl.read_parquet(layout.extracted_event).filter(pl.col('extractor').struct.field('name') == 'llm').row(0, named=True)
    monkeypatch.setattr(extract, 'RULE_VERSION', 'synthetic-previous-mapping')
    second = extract.run(layout, llm_fixture=fixture, chart_fixture=path, ingested_at=T0)
    second_row = pl.read_parquet(layout.extracted_event).filter(pl.col('extractor').struct.field('name') == 'llm').row(0, named=True)
    assert first_row['extract_id'] != second_row['extract_id']
    assert first['build_id'] != second['build_id']
