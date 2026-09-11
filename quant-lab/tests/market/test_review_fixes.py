"""G2 四审实现回归；期望为手算/行为边界，不从内核回填金标。"""
import datetime as dt
from decimal import Decimal as D

import polars as pl
import pytest

from quant_lab.market import contract as c, execution as x, nautilus_adapter as nb
from quant_lab.market import partition_check as pc, vision as v
from tests.market.test_review_p1 import e03, pt, T0, FIX
from tests.market.test_partition_check import bars, run, codes


@pytest.mark.parametrize('field', ['open', 'high', 'low', 'close', 'volume'])
def test_s05_partition_unknown_ohlc_is_quarantined(field):
    # 四审原反例：null 布尔被 filter/any 跳过，未知 OHLC 获得合格状态。
    frame = bars(60).with_columns(pl.when(pl.arange(0, 60) == 7).then(None).otherwise(pl.col(field)).alias(field))
    out, qs, report = run(frame, data_type='markPriceKlines')
    assert out['ohlc_valid'][7] is False
    assert pc.R_OHLC in codes(qs)
    assert report.status == 'quarantined'


@pytest.mark.parametrize('column', ['gap_flag', 'ohlc_valid'])
@pytest.mark.parametrize('value', ['missing', 'null'])
def test_s05_loader_unknown_quality(column, value, tmp_path):
    # 四审 S05 同类反例：gap_flag 缺失/null 不得像已知 false 一样放行。
    import json
    req, _ = e03()
    req = req.model_copy(update={'horizon_end': T0 + dt.timedelta(seconds=120)})
    lake = v.LakePaths(tmp_path / 'lake')
    for typ, interval in [('klines', '1m'), ('markPriceKlines', '1m'), ('fundingRate', '8h')]:
        pid = v.partition_id(typ, interval, 'BTCUSDT', '2024-01')
        v.atomic_write_json(lake.manifest(pid), {'partition_id': pid, 'source_sha256': 'current', 'check_status': 'ok'})
        if typ == 'fundingRate':
            continue
        frame = pl.DataFrame({'open_time': [T0, T0 + dt.timedelta(seconds=60)],
                              **{k: [100., 100.] for k in ['open', 'high', 'low', 'close', 'volume']},
                              'ohlc_valid': [True, True], 'gap_flag': [False, False], 'source_sha256': ['current', 'current']})
        if typ == 'klines':
            if value == 'missing':
                frame = frame.drop(column)
            else:
                frame = frame.with_columns(pl.Series(column, [frame[column][0], None]))
        v.atomic_write_parquet(lake.silver_dir(typ, interval, 'BTCUSDT') / 'date=2024-01-01' / 'part.parquet', frame)
    market = x.load_market_from_lake(req, lake_root=lake.root)
    assert market.bars_quality_ok is False
    assert market.bars_complete is False


@pytest.mark.parametrize('cutoff', ['hold', 'horizon', 'closed', 'funding_censor'])
def test_s16_exposure_uses_observation_boundary(cutoff):
    # 四审 X16：T+5 mark102，hold10，entry1/risk5，MFE 必须 .4，最后事件 T 不是截止。
    req, market = e03(plan_upd={'expiry': c.Expiry(entry_ttl_s=3600, max_holding_s=10)},
                      market_upd={'last': [pt(0, 100), pt(60, 105)], 'mark': [pt(0, 100), pt(5, 102), pt(30, 1000), pt(60, 100)]})
    if cutoff == 'horizon':
        req = req.model_copy(update={'horizon_end': T0 + dt.timedelta(seconds=10),
                                    'order_plan': req.order_plan.model_copy(update={'expiry': c.Expiry(entry_ttl_s=3600)})})
    if cutoff == 'closed':
        market = market.model_copy(update={'last': [pt(0, 100), pt(8, 105), pt(60, 105)]})
    if cutoff == 'funding_censor':
        # 单独检验删失截止，不能让 hold10 掩盖漏记 censor_at 的错误。
        req = req.model_copy(update={'order_plan': req.order_plan.model_copy(update={'expiry': c.Expiry(entry_ttl_s=3600)})})
        market = market.model_copy(update={'last': [pt(0, 100), pt(10, 100), pt(60, 105)], 'funding_schedule_complete': False})
    result = nb.simulate_b(req, market)
    expected = D(1) if cutoff == 'closed' else D('.4')
    assert result.mfe_R == expected
    future = market.model_copy(update={'mark': [pt(0, 100), pt(5, 102), pt(30, 5), pt(60, 100)]})
    again = nb.simulate_b(req, future)
    assert (result.mae_R, result.mfe_R, result.canonical_events) == (again.mae_R, again.mfe_R, again.canonical_events)


def test_s10_missing_a_cannot_match():
    # 四审 S10：缺结果须 NOT_RUN/UNEXPLAINED；空 diff 不构成执行证据。
    result = nb.simulate_b(FIX['E03'].request, FIX['E03'].market)
    assert nb.classify('E03', [], None, result) == 'NOT_RUN'


def test_s10_report_balance_evidence(tmp_path):
    # 四审原反例：同一报告“余额已核”与 not_run 矛盾；校验报告实际表格字段。
    path = tmp_path / 'report.md'
    nb.ab_report(out=str(path), reps=1)
    rows = [line.split('|') for line in path.read_text().splitlines() if line.startswith('| 永续语义 |')]
    assert rows[0][3].strip() == 'mark SL：adapter（SimulationModule）；funding：adapter（adjust_account，引擎余额核对 not_run）；路径：adapter 合成 TradeTick'


def test_s13_event_decimal_precision_rejected():
    # 四审 S13 精度残余：事件 price/qty 超过 Decimal(38,12) 仍能入规范事件。
    for field in ['price', 'qty', 'fee', 'cash_delta']:
        with pytest.raises(c.ContractError, match='Decimal'):
            c.CanonicalEvent(seq=0, ts=T0, kind='submitted', order_id='e', leg='entry', **{field: D('0.0000000000001')})


def test_s05_spike_unknown_gap_is_not_clean():
    # 四审同类横扫：gap=null 不得参与收益计算后被标为 spike=false。
    score, flag = pc.spike_scores(pl.Series([100., 101., 102.]), pl.Series([False, None, False]))
    assert flag[1] is True
    assert score[1] is None


def test_s06_vision_unknown_duplicate_values_quarantined(monkeypatch, tmp_path):
    # 四审冲突同类：两个同键同 null 候选不能被 unique 折叠为可信数据。
    frame = bars(2).with_columns(pl.lit(None, dtype=pl.Float64).alias('close'))
    frame = pl.concat([frame, frame])
    monkeypatch.setattr(v, 'parse_zip', lambda *args: (frame, 'x.csv'))
    lake = v.LakePaths(tmp_path / 'lake')
    manifest = v.ingest_bytes(b'local-test', data_type='markPriceKlines', interval='1m', symbol='BTCUSDT',
                              period='2024-01', source_uri='synthetic', source_sha256='h' * 64,
                              checksum_source='computed', head_meta={}, lake=lake)
    assert manifest.status == 'quarantined'
    assert manifest.days == []
    assert manifest.quarantine_n > 0


def test_s11_report_cli_exception_returns_failure(monkeypatch, tmp_path, capsys):
    # 四审 S11 CLI 门禁残余：B 失败虽归类 NOT_RUN，性能重跑又抛异常，无法输出审计摘要。
    def broken(*args, **kwargs):
        raise RuntimeError('synthetic engine failure')
    monkeypatch.setattr(nb, 'simulate_b', broken)
    assert nb.main(['report', '--reps', '1', '--out', str(tmp_path / 'report.md')]) == 1
    import json
    output = json.loads(capsys.readouterr().out)
    assert output['codes'] == {'NOT_RUN': 22}


@pytest.mark.parametrize('cost', ['base', 'stress'])
@pytest.mark.parametrize('path', ['primary', 'adverse', 'favorable'])
@pytest.mark.parametrize('kernel', ['A', 'B'])
def test_s11_cost_path_matrix(cost, path, kernel):
    # 四审 S11：22 个夹具全部 base、无 favorable；补全两个成本×三个路径×两个内核。
    from tests.market.test_review_p1_round3 import bar
    req, market = e03(policy='fixture-tick-v1')
    req = req.model_copy(update={'cost_scenario': cost, 'path_scenario': path, 'horizon_end': T0 + dt.timedelta(seconds=120)})
    market = market.model_copy(update={'last': [], 'mark': [], 'bars_last': [bar(0), bar(60,105)],
                                       'bars_mark': [bar(0), bar(60)]})
    result = x.simulate(req, kernel=kernel, market=market)
    c.check_invariants(req, result)
    assert result.filled_qty == D(1)
    assert result.gross_pnl == D(5)
    assert result.net_pnl == result.gross_pnl - result.fees + result.funding
    assert x.simulate(req, kernel=kernel, market=market).trace_hash == result.trace_hash


def test_s12_snapshot_request_explicitly_unsupported(tmp_path):
    req, _ = e03()
    with pytest.raises(c.ContractError, match='unsupported: versioned lake snapshot'):
        x.load_market_from_lake(req, lake_root=tmp_path, snapshot_id='historic-sha')


def test_s07_management_stream_rejected_at_request_boundary():
    from pydantic import ValidationError
    req, _ = e03()
    with pytest.raises(ValidationError, match='extra_forbidden'):
        c.ExecutionRequest.model_validate({**req.model_dump(), 'management_commands': [{'type': 'cancel', 'ts': T0}]})


def test_p2_report_exposes_unsupported_capabilities(tmp_path):
    report = nb.ab_report(out=str(tmp_path / 'report.md'), reps=1)
    assert {key: value['status'] for key, value in report['unsupported'].items()} == {
        'S02': 'unsupported', 'S06': 'unsupported', 'S07': 'unsupported', 'S12': 'unsupported'}


def test_s11_b_replay_across_processes():
    import json
    import subprocess
    import sys
    script = '''import json
from quant_lab.market import contract as c, nautilus_adapter as b
fixtures = c.load_fixtures("tests/market/fixtures/episodes")
print(json.dumps({f.id: b.simulate_b(f.request, f.market).trace_hash for f in fixtures}, sort_keys=True))
'''
    first = subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)
    second = subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)
    assert len(json.loads(first.stdout)) == 22
    assert first.stdout == second.stdout


@pytest.mark.parametrize('cost', ['base', 'stress'])
@pytest.mark.parametrize('path', ['primary', 'adverse', 'favorable'])
@pytest.mark.parametrize('kernel', ['A', 'B'])
def test_s11_all_episodes_cost_path_invariants(cost, path, kernel):
    # 四审矩阵缺口：全 22 例完整场景积；金标仍仅用于其原请求，派生请求验证不变量/确定性。
    for fixture in FIX.values():
        req = fixture.request.model_copy(update={'cost_scenario': cost, 'path_scenario': path})
        result = x.simulate(req, kernel=kernel, market=fixture.market)
        c.check_invariants(req, result, multiplier=fixture.market.rules.multiplier)
        again = x.simulate(req, kernel=kernel, market=fixture.market)
        assert result.trace_hash == again.trace_hash, (fixture.id, cost, path, kernel)


def test_s13_event_precision_checked_by_invariants():
    # 四审事件层/精度残余：归约对象的超精度 fee 被 quantize 后掩盖，不变量原先接受。
    from quant_lab.market.kernel_a import simulate_a
    req, market = e03()
    result = simulate_a(req, market)
    events = list(result.canonical_events)
    events[0] = events[0].model_copy(update={'fee': D('0.0000000000001')})
    with pytest.raises(c.ExecutionInvariantError, match='Decimal'):
        c.check_invariants(req, result.model_copy(update={'canonical_events': events}))


def test_s16_horizon_equal_mark_does_not_extend_exposure():
    # 四审 S16 截止语义：horizon 与 hold 一样先删失，同刻 mark 不能扩大观察窗。
    req, market = e03(market_upd={'last': [pt(0,100), pt(60,105)],
                                 'mark': [pt(0,100), pt(5,102), pt(10,1000)]})
    req = req.model_copy(update={'horizon_end': T0 + dt.timedelta(seconds=10)})
    result = nb.simulate_b(req, market)
    assert result.mfe_R == D('.4')
