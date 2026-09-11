"""A36 有限输入族的判别矩阵：四组均经过相同请求和分区消费路径。

每组只改变自己的维度，其余维度固定为精确整数、完整列、正常网格和省略字段。
矩阵证明范围是这里明确列出的输入族，不声称覆盖该类所有可能输入。
"""
import datetime as dt
from decimal import Decimal

import polars as pl
import pytest

from quant_lab.market import contract as c, partition_check as pc, execution
from quant_lab.market.kernel_a import KernelA
from tests.market.test_single_source import BASE_REQUEST, ABSENT, DECIMAL_EDGES, _request, _write_lake
from tests.market.test_partition_check import bars, rules, INST

START = dt.datetime(2024, 1, 1, 1, tzinfo=dt.UTC)
END = START + dt.timedelta(minutes=3)
GRID = [START + dt.timedelta(minutes=i) for i in range(3)]
US = dt.timedelta(microseconds=1)


def probe(*, opens=GRID, risk=Decimal(5), spelling=ABSENT, allocation=ABSENT):
    payload = {**BASE_REQUEST, 't_dec': START, 'horizon_end': END, 'risk_budget': risk}
    payload.pop('t_start', None)
    if spelling is not ABSENT:
        payload['t_start'] = spelling
    if allocation is not ABSENT:
        payload['entry_fractions'] = allocation
    req = c.ExecutionRequest.model_validate(payload)
    assert req.risk_budget == risk
    assert req.resolved_t_start(c.resolve_policy(req.policy_version)) == START
    assert KernelA(req, c.MarketView(manifest_id=req.market_manifest)).t_start == START
    frame = bars(max(1, len(opens)), start=START).head(len(opens)).with_columns(
        pl.Series('open_time', opens, dtype=pl.Datetime('us', 'UTC')),
        pl.Series('close_time', [t + dt.timedelta(minutes=1) for t in opens],
                  dtype=pl.Datetime('us', 'UTC')))
    out, qs, report = partition(frame)
    present = sorted(set(opens) & set(GRID))
    assert out['open_time'].to_list() == present
    assert report.expected_rows == 3
    assert report.missing == 3 - len(present)
    assert sum(q['reason_code'] == pc.R_TIME_GRID for q in qs) == len(set(opens) - set(GRID))
    return req, out, qs, report


def partition(frame):
    return pc.check_bars(frame, pid='a36', data_type='markPriceKlines', interval='1m',
                         inst=INST, period='2024-01', rules=rules(eff_from=START, eff_to=END))


def test_boundary_time():
    probe()
    # 首、中、尾网格点都做 −1/0/+1µs；另测半开区间外端点。
    for index in range(3):
        for offset in (-1, 0, 1):
            opens = list(GRID)
            opens[index] += offset * US
            probe(opens=opens)
    for endpoint in (START - US, END - US, END, END + US):
        probe(opens=sorted(set(GRID + [endpoint])))
    _, out, qs, report = probe(opens=[START, START + dt.timedelta(microseconds=59999999), GRID[2]])
    assert (report.expected_rows, report.missing, report.quarantine_n) == (3, 1, 2)
    assert out.height == 2


def test_boundary_numeric(tmp_path):
    probe()
    for risk in DECIMAL_EDGES:
        probe(risk=risk)
    # 正确的 Decimal 消费层是银层 parquet → loader；分区原始体检使用 Float64。
    end = START + dt.timedelta(minutes=len(DECIMAL_EDGES))
    opens = [START + dt.timedelta(minutes=i) for i in range(len(DECIMAL_EDGES))]
    _write_lake(tmp_path, START, end, opens)
    req = _request(t_dec=START, t_start=None, horizon_end=end)
    loaded = execution.load_market_from_lake(req, lake_root=tmp_path)
    assert loaded.bars_complete and loaded.bars_quality_ok
    assert [bar.c for bar in loaded.bars_last] == list(DECIMAL_EDGES)
    for risk in (Decimal('1e-13'), Decimal('10000.0000000000001'), Decimal('1e26')):
        with pytest.raises(c.ContractError, match='Decimal'):
            probe(risk=risk)


def test_boundary_empty():
    probe()
    assert probe(allocation=None)[0].entry_fractions == (Decimal(1),)
    with pytest.raises(c.ContractError, match='长度|推导不一致'):
        probe(allocation=[])
    assert probe(opens=[])[3].missing == 3
    frame = bars(3, start=START)
    for unknown in (frame.drop('open_time'), frame.with_columns(
            pl.lit(None, dtype=pl.Datetime('us', 'UTC')).alias('open_time'))):
        with pytest.raises(ValueError, match='unknown bar time'):
            partition(unknown)


def test_boundary_equivalence():
    omitted = probe()[0]
    explicit = probe(spelling=START, allocation=(Decimal(1),))[0]
    assert omitted.resolved_t_start(c.resolve_policy(omitted.policy_version)) == explicit.t_start
    assert omitted.entry_fractions == explicit.entry_fractions


@pytest.mark.parametrize('reason,cov_key', [
    ('SYMBOL_TIME_INVALID', 'rules_ok'), ('RULE_HISTORY_MISSING', 'rules_ok'),
    ('BAR_GAP', 'bars_ok'), ('MARK_STALE', 'mark_ok'),
    ('FUNDING_SCHEDULE_GAP', 'funding_ok'),
])
@pytest.mark.parametrize('label_first', [False, True])
def test_a37_evidence_wins_over_label(reason, cov_key, label_first):
    """真实优先级消费方，两种到达顺序；预期不从被测优先级表推导。"""
    req = _request()
    kernel = KernelA(req, c.MarketView(manifest_id=req.market_manifest))
    candidates = [(reason, cov_key), ('LABEL_RIGHT_CENSORED', None)]
    if label_first:
        candidates.reverse()
    for candidate, key in candidates:
        kernel.censor_now(candidate, key)
    result = kernel.result()
    assert result.censor_reason == reason
    assert not getattr(result.coverage_mask, cov_key)
    assert c.outcome_kind(result) == 'unevaluable'
