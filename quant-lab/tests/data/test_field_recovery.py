"""Invented text and synthetic prices only; no network or production data."""
from decimal import Decimal as D
from datetime import UTC, datetime
import json

import pytest
from quant_lab.data import extract, validate, lifecycle, cx_v2
from quant_lab.data.close_stop import parse_close_stop
from quant_lab.data.market_stub import InstrumentRegistry, InstrumentRule, SyntheticMarks, instrument_id_for

T = datetime(2025, 1, 1, tzinfo=UTC)
BTC = instrument_id_for('BTC')
REG = InstrumentRegistry([InstrumentRule('BTC', BTC, datetime(2020, 1, 1, tzinfo=UTC), None, .1, .001)])
MARK = SyntheticMarks({BTC: [(T, 62500)]})


def row(**kwargs):
    r = dict(symbol_raw='BTC', side='long', entry=dict(kind='limit', lo=D(62500), hi=D(62500)), entries=[],
             stop=D(61000), tps=[dict(kind='price', level=D(65000), fraction=None)],
             available_at=T, time_grade='H0', kind='entry_proposal', checks='{}', entry_mode='price')
    r.update(kwargs)
    return r


def canonical(**kwargs):
    return validate.canonicalize_row(row(**kwargs), registry=REG)


def market(c):
    return validate.market_check_row(c, t_a=T, marks=MARK, t_plaus=None, channel_id=1)


@pytest.mark.parametrize('condition', ['每日收盘低于61000', '日线关闭低于61000', '每天收线低于61000'])
def test_i26_daily(condition):
    assert parse_close_stop(condition, D(61000))['timeframe'] == '1d'
    assert parse_close_stop('每日低于61000', D(61000)) is None
    assert parse_close_stop('每日关闭和4h关闭', D(61000)) is None


@pytest.mark.parametrize('label,second', [('止损价', '0.3312'), ('目标价位', '3312'), ('', '0.3312')])
def test_i35_gauls(label, second):
    text = f'仿写 DOGE 做多 入场：CMP 0.34 和 {label} {second}；止损：0.32；目标：0.38'
    repair = extract.gauls_second_entry(text)
    assert repair['price'] == D('.3312')
    parsed = extract.parse_message(text)
    assert parsed.entries == [D('.34'), D('.3312')]
    assert parsed.stop == D('.32') and parsed.checks['gauls_second_entry']
    assert extract.gauls_second_entry(text.replace('0.32', '0.335')) is None
    assert extract.gauls_second_entry(text.replace('入场：', '讨论：')) is None


def test_i22_tp_scale_only():
    c, _, checks = canonical(tps=[dict(kind='price', level=D(65000), fraction=None), dict(kind='price', level=D(650000), fraction=None)])
    m, reasons, audit = market(c)
    assert m['scale_gate'] == 'ok' and not reasons and checks['eligibility']['execution']
    assert [t['level'] for t in c['tps']] == [65000] and audit['tps_dropped_scale']
    c, _, _ = canonical(stop=D(61))
    assert 'UNIT_SCALE_CONFLICT' in market(c)[1]


@pytest.mark.parametrize('kind', ['zone', 'ladder'])
def test_i25_all_entry_bounds(kind):
    c, _, audit = canonical(entry=dict(kind=kind, lo=D(62000), hi=D(63000)), entries=[D(63000), D(62000)] if kind == 'ladder' else [],
        tps=[dict(kind='price', level=D(62500), fraction=None), dict(kind='price', level=D(65000), fraction=None)])
    assert c['direction_ok'] and [t['level'] for t in c['tps']] == [65000]
    assert audit['tps_dropped_direction'] and audit['eligibility']['execution']
    c, reasons, audit = canonical(stop=D(64000))
    assert not c['direction_ok'] and reasons and not audit['eligibility']['execution']


@pytest.mark.parametrize('kind', ['zone', 'ladder', 'unpriced'])
def test_i21_percent_drop(kind):
    entry = None if kind == 'unpriced' else dict(kind=kind, lo=D(62000), hi=D(63000))
    c, _, audit = canonical(entry=entry, entry_mode='market_ref' if entry is None else 'price',
        tps=[dict(kind='pct', level=D(5), fraction=None)])
    assert c['tps'] == [] and audit['tps_dropped_percent'] and audit['eligibility']['execution']
    c, _, audit = canonical(tps=[dict(kind='pct', level=D(5), fraction=None)])
    assert c['tps'][0]['level'] == D(65625) and audit['tp_pct_converted'] == 1


def ladder_root(fractions=(30, 30)):
    levels = [dict(kind='limit', price=dict(value=str(p)), fraction=None if f is None else dict(value=str(f))) for p, f in zip((60000, 62500), fractions)]
    return dict(instrument_id=BTC, side='long', entry=dict(kind='ladder', lo=D(60000), hi=D(62500)), entries=[D(60000), D(62500)],
                checks=json.dumps(dict(schema_version=2, action=dict(entry=dict(kind='ladder', levels=levels)))))


def test_i28_fractions_and_crossing_contract():
    root = ladder_root()
    plan = lifecycle._order_plan(root, D(59000), [dict(level=D(65000), fraction=None)], None)
    assert [e['fraction'] for e in plan['entries']] == [D('.5'), D('.5')]
    assert json.loads(root['checks'])['fractions_normalized']
    root = ladder_root()
    plan = lifecycle._order_plan(root, D(61000), [dict(level=D(62000), fraction=None), dict(level=D(65000), fraction=None)], None)
    assert len(plan['entries']) == 1 and plan['entries'][0]['fraction'] == 1
    audit = json.loads(root['checks'])
    assert audit['entries_dropped_direction'] and audit['tps_dropped_direction']
    from quant_lab.market.contract import OrderPlan
    assert OrderPlan.model_validate(plan)
    root = ladder_root((None, None))
    plan = lifecycle._order_plan(root, D(59000), [], None)
    assert all(e['fraction'] is None for e in plan['entries'])
    assert 'fractions_normalized' not in json.loads(root['checks'])
    assert lifecycle._order_plan(ladder_root(), D(64000), [], None) is None


@pytest.mark.parametrize('alias,code', [('GOLD','XAU'),('SILVER','XAG'),('闪迪','SNDK'),('川普','TRUMP'),('狗币','DOGE'),('狗狗币','DOGE'),('DOGE狗币','DOGE'),('ETHEREUM','ETH'),('SOLANA','SOL'),('莱特币','LTC'),('寿司','SUSHI'),('寿司SUSHI','SUSHI'),('名称(ETH)','ETH')])
def test_i24_registry_guard(alias, code):
    reg = InstrumentRegistry([InstrumentRule(code, instrument_id_for(code), datetime(2020,1,1,tzinfo=UTC), None, .1, .001)])
    c, _, checks = validate.canonicalize_row(row(symbol_raw=alias), registry=reg)
    assert c['instrument_id'] == instrument_id_for(code) and checks['symbol_alias']
    c, _, checks = validate.canonicalize_row(row(symbol_raw=alias), registry=REG)
    assert c['instrument_id'] is None and not checks['eligibility']['execution']
    assert canonical(symbol_raw='海力士')[0]['instrument_id'] is None
    assert extract.detect_symbol('仿写 以太坊(ETH) 做多')[0] == 'ETH'


def test_i23_unique_rescale_and_no_solution():
    c, _, _ = canonical(entry=dict(kind='limit',lo=D('6.25'),hi=D('6.25')), stop=D('6.1'), tps=[dict(kind='price',level=D('6.5'),fraction=None)])
    m, reasons, audit = market(c)
    assert m['scale_gate'] == 'ok' and not reasons
    assert audit['unit_rescaled'] == dict(factor='10000',basis='mark') and c['entry_ref'] == 62500 and c['stop'] == 61000
    assert c['tps'][0]['level'] == 65000
    c, _, _ = canonical(entry=dict(kind='limit',lo=D('6.25'),hi=D('6.25')), stop=D('6.1'))
    assert 'UNIT_SCALE_CONFLICT' in market(c)[1]
    c, _, _ = canonical(entry=dict(kind='limit',lo=D('6.25'),hi=D('6.25')), stop=D('6.1'), tps=[])
    c['unit_inherited'] = [dict(field='entry',factor='10000')]
    assert 'UNIT_SCALE_CONFLICT' in market(c)[1]
    # Adjacent powers differ by 10; the strict ln3 band spans a factor of 9.
    assert D(3) * 3 < D(10)


def test_i29_side_inference_and_contradiction():
    c, _, audit = canonical(side=None)
    assert c['side'] == 'long' and audit['side_inferred'] == 'long' and audit['eligibility']['execution']
    c, _, audit = canonical(side=None, stop=D(65000), tps=[dict(kind='price',level=D(61000),fraction=None)])
    assert c['side'] == 'short' and audit['side_inferred'] == 'short'
    for tps in ([], [dict(kind='price',level=D(60000),fraction=None)]):
        c, _, audit = canonical(side=None,tps=tps)
        assert c['side'] is None and 'side_inferred' not in audit and not audit['eligibility']['execution']


def test_i35_v2_evidence_and_unpriced_mark(tmp_path, monkeypatch):
    from test_cx_v2 import action, envelope, number, v2_lake
    quoted = '仿写 BTC 做多 入场：CMP 62500 和 目标价位 62000；止损：61000；目标：65000'
    payload = envelope(action(entry=dict(kind='market_ref', price=number(62500), lo=None, hi=None, levels=[]),
        stop=dict(kind='price', price=number(61000), condition=None), tps=[dict(kind='price',value=number(62000)),dict(kind='price',value=number(65000))]))
    parsed = cx_v2.parse_actions(payload, quoted)[0][0]
    assert parsed.entries == [62500,62000] and [t['level'] for t in parsed.tps] == [65000]
    assert parsed.checks['gauls_second_entry']
    monkeypatch.setattr(validate, 'fixture_marks', lambda: MARK)
    # The synthetic lake uses T0; make a mark visible at that as-of timestamp.
    from test_cx_v2 import T0
    monkeypatch.setattr(validate, 'fixture_marks', lambda: SyntheticMarks({BTC:[(T0,62500)]}))
    text = quoted.replace('CMP 62500','CMP')
    a = action(entry=dict(kind='market_ref',price=None,lo=None,hi=None,levels=[]),
        stop=dict(kind='price',price=number(61000),condition=None),tps=[dict(kind='price',value=number(62000)),dict(kind='price',value=number(65000))])
    _, _, _, cp, _ = v2_lake(tmp_path,[a],text)
    r = cp.filter(__import__('polars').col('extractor_name') == 'llm').row(0,named=True)
    audit = json.loads(r['checks'])
    assert audit['gauls_second_entry']['basis'] == 'mark'
    assert json.loads(r['eligibility_by_estimand'])['execution']
    plan = lifecycle._order_plan(r,r['stop'],r['tps'],None)
    assert [e['kind'] for e in plan['entries']] == ['market_ref','limit']
    assert plan['entries'][0]['price_lo'] is None and plan['entries'][1]['price_lo'] == 62000


def test_i23_canonical_execution_and_lifecycle_rescale(tmp_path, monkeypatch):
    from test_cx_v2 import action, number, v2_lake, T0
    import polars as pl
    monkeypatch.setattr(validate,'fixture_marks',lambda:SyntheticMarks({BTC:[(T0,62500)]}))
    levels = [dict(kind='market_ref',price=number('6.25'),fraction=None),dict(kind='limit',price=number('6.2'),fraction=None)]
    a = action(side=None,entry=dict(kind='ladder',price=None,lo=None,hi=None,levels=levels),
        stop=dict(kind='price',price=number('6.1'),condition=None),tps=[dict(kind='price',value=number('6.5'))])
    _, _, _, cp, _ = v2_lake(tmp_path,[a],'仿写 BTC 入场6.25和6.2，止损6.1，止盈6.5')
    r = cp.filter(pl.col('extractor_name')=='llm').row(0,named=True)
    audit = json.loads(r['checks'])
    assert audit['unit_rescaled']['factor']=='10000' and audit['side_inferred']=='long'
    assert json.loads(r['eligibility_by_estimand'])['execution'] and r['scale_gate']=='ok'
    plan = lifecycle._order_plan(r,r['stop'],r['tps'],None)
    assert [e['price_lo'] for e in plan['entries']]==[62500,62000]
    assert plan['stop']['price']==61000 and plan['tps'][0]['level']==65000


def test_i28_crossing_stop_keeps_newly_valid_tp():
    c, reasons, audit = canonical(side='short',entry=dict(kind='ladder',lo=D(60000),hi=D(62500)),
        entries=[D(60000),D(62500)],stop=D(61000),tps=[dict(kind='price',level=D(59000),fraction=None)])
    assert c['direction_ok'] and not reasons and audit['eligibility']['execution']
    root = dict(c,entry_mode='price',checks=json.dumps(audit))
    plan = lifecycle._order_plan(root,c['stop'],c['tps'],None)
    assert len(plan['entries']) == 1 and plan['entries'][0]['price_lo']==60000
    assert plan['tps'][0]['level']==59000
    # Long mirror: the low entry is dropped and a target beyond the high survives.
    c, _, audit = canonical(entry=dict(kind='ladder',lo=D(60000),hi=D(62500)),entries=[D(60000),D(62500)],
        stop=D(61000),tps=[dict(kind='price',level=D(63000),fraction=None)])
    assert c['direction_ok'] and audit['entries_dropped_direction'] and c['tps'][0]['level']==63000


def test_i35_rule_parser_preserves_cmp_order_kind():
    parsed = extract.parse_message('仿写 BTC 做多 入场：CMP 62500 和 止损价 62000；止损：61000；目标：65000')
    r = row(entry=parsed.entry,entries=parsed.entries,stop=parsed.stop,tps=parsed.tps,checks=json.dumps(parsed.checks))
    c, _, audit = validate.canonicalize_row(r,registry=REG)
    root = dict(c,checks=json.dumps(parsed.checks | audit),entry_mode='price')
    plan = lifecycle._order_plan(root,c['stop'],c['tps'],None)
    assert [e['kind'] for e in plan['entries']]==['market_ref','limit']
    assert [e['tif'] for e in plan['entries']]==['IOC','GTC']
