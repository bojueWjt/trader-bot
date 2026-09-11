"""A24 三道机制并行：禁令捕获重复，登记捕获绕过，A28 差分捕获偏离。

G0 换层依据：语法机制的防线边界等于“枚举到的写法集合”，原则上不可能封闭。
人工横扫三次失败 → A24 语法门被常规写法穿过；审查方两次明确拆行和类型注解
不是别名也不是混淆。穿过的不是攻击，是日常写法，这更说明语法边界的问题。
禁令在不可达区域仍有效（B14 的 multiplier 位置行为测试跑不到）；登记检查结构；
差分与写法无关，但只能在生成器实际走到的路径上发现偏离，不能取代禁令。

本层不宣称不可穿过。它把绕过的代价从“换一种写法”提高到“写出一个真正不同的
答案”：在已生成路径上后者会被差分抓住，前者不会。若第三层也被穿过，应换层，
不是补枚举。下列预期直接来自 contract 单一来源，断言实际消费方的结果/拒绝行为，
不以调用次数或正向委派断言代替行为。固定种子随机化 + 必经边界类，而非均匀碰运气。

force_close_net_R：无消费方，差分不适用，义务顺延；G3 接入时须同时补上差分与
哨兵，证明其 θ 随返回值改变。不能在夹具中重写估值公式来制造等式左边。
跨窗口登记为空集、夹具不冒充 G3 的 θ 消费证据、无消费方的来源差分不适用，
三次都是同一原则：不为不存在的东西制造证据。否则门会因错误的原因通过，
在最该说话的时候沉默。此处明确记录原因与后续义务，不用 skip 冒充覆盖。
"""
import pytest

from quant_lab.market import single_source as gate


def test_single_source_call_sites_match_registry():
    actual = gate.collect_call_sites(gate.SRC, set(gate.ALLOWED_CALLERS))
    assert gate.diff_call_sites(gate.ALLOWED_CALLERS, actual) == []


def test_no_inline_reexpression_of_single_sources_anywhere_in_src():
    assert gate.violations(gate.lint_tree(gate.SRC)) == []


def test_foreign_hits_registry_is_exact():
    hits = {(p, w) for p, _, w, _ in gate.lint_tree(gate.SRC) if not w.startswith("market/")}
    assert hits == gate.FOREIGN_HITS


@pytest.mark.parametrize("deviation", ["missing", "extra"])
def test_registry_gate_fails_when_mutated(tmp_path, deviation):
    package = tmp_path / "quant_lab" / "market"
    package.mkdir(parents=True)
    snippet = "def registered():\n    grid_points_between(a, b, 60)\n"
    if deviation == "missing":
        snippet = "def registered():\n    return 0\n"
    if deviation == "extra":
        snippet += "def unregistered():\n    grid_points_between(a, b, 60)\n"
    (package / "injected.py").write_text(snippet)
    actual = gate.collect_call_sites(package.parent, {"grid_points_between"})
    declared = {"grid_points_between": {"market/injected.py:registered"}}
    message = gate.diff_call_sites(declared, actual)
    assert len(message) == 1
    expected = "不再调用它"
    if deviation == "extra":
        expected = "未登记"
    assert expected in message[0]


@pytest.mark.parametrize("pattern,snippet", [
    ("P1_inline_latency", "return t + dt.timedelta(seconds=policy.latency_s)"),
    ("P2_int_total_seconds", "return int((b - a).total_seconds())"),
    ("P3_duration_div", "return (b - a).total_seconds() // interval_s"),
    ("P4_t_start_or_t_dec", "return req.t_start or req.t_dec"),
    ("P5_inline_entry_ttl", "return t + dt.timedelta(seconds=req.entry_ttl_s)"),
    ("P6_inline_grid_comparison", "return o - prev > iv"),
    ("P6_inline_grid_comparison", "return prev + iv < end"),
    ("P7_inline_ttl_resolution", "ttl = plan.expiry.entry_ttl_s if plan.expiry.entry_ttl_s is not None else policy.entry_ttl_s"),
    ("P7_inline_ttl_resolution", "ttl = plan.expiry.entry_ttl_s\n    if ttl is None:\n        ttl = policy.entry_ttl_s"),
], ids=["latency", "int-seconds", "duration-div", "start-or", "expiry-add", "middle-grid", "tail-grid", "ttl-expression", "ttl-branches"])
def test_lint_gate_fails_on_injected_violation(tmp_path, pattern, snippet):
    package = tmp_path / "quant_lab" / "market"
    package.mkdir(parents=True)
    (package / "injected.py").write_text("def f():\n    " + snippet + "\n")
    bad = gate.violations(gate.lint_tree(package.parent))
    assert {p for p, _, _, _ in bad} == {pattern}


def test_single_source_use_counts_match_registry():
    assert gate.collect_call_counts(gate.SRC, set(gate.ALLOWED_CALL_COUNTS)) == gate.ALLOWED_CALL_COUNTS


@pytest.mark.parametrize("snippet", [
    "ttl: int = plan.expiry.entry_ttl_s if flag else policy.entry_ttl_s",
    "ttl *= plan.expiry.entry_ttl_s if flag else policy.entry_ttl_s",
    "return (ttl := plan.expiry.entry_ttl_s if flag else policy.entry_ttl_s)",
    "return plan.expiry.entry_ttl_s if flag else policy.entry_ttl_s",
    "return [(ttl := plan.expiry.entry_ttl_s if flag else policy.entry_ttl_s) for x in xs]",
    "return policy.entry_ttl_s",
], ids=["annotated", "augmented", "named", "return", "comprehension", "return-field"])
def test_ttl_standard_nodes(snippet):
    lint = gate._Lint("market/injected.py", "def f():\n    " + snippet + "\n")
    import ast
    lint.visit(ast.parse(lint.text))
    expected = {"P7_inline_ttl_resolution"}
    assert {p for p, _, _, _ in lint.hits} == expected


@pytest.mark.parametrize("assignment", ["delta = o - prev", "delta: int = o - prev", "delta -= prev"])
def test_grid_local_definition(assignment):
    import ast
    source = "def f():\n    " + assignment + "\n    return delta > iv\n"
    lint = gate._Lint("market/injected.py", source)
    lint.visit(ast.parse(source))
    assert {p for p, _, _, _ in lint.hits} == {"P6_inline_grid_comparison"}


@pytest.mark.parametrize("pattern,snippet", [
    ("P3_duration_div", "duration /= interval_s"),
    ("P5_inline_entry_ttl", "t += dt.timedelta(seconds=req.entry_ttl_s)"),
], ids=["duration-augmented", "expiry-augmented"])
def test_augmented_binary_patterns(pattern, snippet):
    import ast
    lint = gate._Lint("market/injected.py", "def f():\n    " + snippet + "\n")
    lint.visit(ast.parse(lint.text))
    assert {p for p, _, _, _ in lint.hits} == {pattern}


# A28：生成器只负责输入；不在测试中再实现网格、延迟、窗口或 TTL 公式。
import datetime as dt
from decimal import Decimal
import random

import polars as pl
from pydantic import ValidationError

from quant_lab.market import contract as c, execution, partition_check as pc, vision
from quant_lab.market.kernel_a import KernelA
from tests.market.test_partition_check import bars as fixture_bars, rules as fixture_rules, INST
import json
from pathlib import Path

# 只读取原始输入，避免在 pytest 收集时构造请求；突变必须由测试实际执行路径检出。
_EPISODE = next((Path(__file__).parent / 'fixtures/episodes').glob('E03*.json'))
BASE_REQUEST = json.loads(_EPISODE.read_text())['request']


US = dt.timedelta(microseconds=1)
ABSENT = object()  # None/[] 是已提供的值，不与缺失合并。
DECIMAL_EDGES = tuple(map(Decimal, (
    '0.00000001', '0.000000000001', '10000.000000000001',
    '99999999999999999999999999', '99999999999999999999999999.000000000001',
)))


def _times():
    rng = random.Random(2801)
    anchors = [dt.datetime(2024, 1, 31, 23, 59, tzinfo=dt.UTC),
               dt.datetime(2024, 2, 29, 23, 59, tzinfo=dt.UTC),
               dt.datetime(2023, 12, 31, 23, 59, tzinfo=dt.UTC)]
    anchors += [dt.datetime(2024, rng.randint(1, 12), rng.randint(2, 27),
                            rng.randint(0, 23), rng.randint(0, 59), tzinfo=dt.UTC)
                for _ in range(3)]
    for anchor in anchors:
        for offset in (-1, 0, 1):
            yield anchor + offset * US


def _grid(a, b, sec=60):
    first = c.first_grid_point(a, sec)
    return [first + dt.timedelta(seconds=sec * i)
            for i in range(c.grid_points_between(a, b, sec))]


def _request(**changes):
    return c.ExecutionRequest.model_validate({**BASE_REQUEST, **changes})


def _build(plan, t_dec, policy, **extra):
    row = {**BASE_REQUEST, 'order_plan': plan, 't_dec': t_dec}
    return c.build_request(row, policy_version=policy.version, policy_hash=policy.content_hash,
                           risk_budget=Decimal(5), market_manifest='a28', **extra)


def _policy(monkeypatch, **changes):
    # 政策是输入，不替换被测函数；注册只在内存，不触碰 policy_hashes.json。
    pol = c.ExecutionPolicy.model_validate({**c.POLICIES['fixture-zero-v1'].model_dump(),
                                          'version': 'a28-input', **changes})
    registry = {**c.load_policy_registry(), pol.version: pol.content_hash}
    monkeypatch.setitem(c.POLICIES, pol.version, pol)
    monkeypatch.setattr(c, 'load_policy_registry', lambda: registry)
    return pol


def _plan(ttl=ABSENT, hold=ABSENT):
    expiry = {}
    if ttl is not ABSENT:
        expiry['entry_ttl_s'] = ttl
    if hold is not ABSENT:
        expiry['max_holding_s'] = hold
    return c.OrderPlan.model_validate({**BASE_REQUEST['order_plan'], 'expiry': expiry})


def _accepts(payload):
    try:
        c.ExecutionRequest.model_validate(payload)
    except (c.ContractError, ValidationError):
        return False
    return True


def _partition_interior_variants(expected):
    # S45 独立消费方义务：移动窗口内网格点，短生命周期窗口不硬造点。
    if len(expected) < 3:
        return []
    first, middle, *tail = expected
    return [[first, middle + offset * US, *tail] for offset in (-1, 0, 1)]


def test_differential_partition_grid():
    rng = random.Random(2802)
    for start in _times():
        period = start.strftime('%Y-%m')
        pa, pb = vision.period_bounds(period)
        for end_offset in (-1, 0, 1):
            end = start.replace(microsecond=0) + dt.timedelta(minutes=4) + end_offset * US
            a, b = max(pa, start), min(pb, end)
            expected = _grid(a, b)
            variants = [expected, [], expected[1:], expected[:-1], expected[::2],
                        [t for t in expected if rng.getrandbits(1)],
                        [t + US for t in expected], [t - US for t in expected],
                        *_partition_interior_variants(expected), sorted(set(expected + [a - US, b]))]
            for opens in variants:
                frame = fixture_bars(max(1, len(opens))).head(len(opens)).with_columns(
                    pl.Series('open_time', opens, dtype=pl.Datetime('us', 'UTC')),
                    pl.Series('close_time', [t + dt.timedelta(minutes=1) for t in opens],
                              dtype=pl.Datetime('us', 'UTC')))
                out, quarantines, report = pc.check_bars(
                    frame, pid='a28', data_type='markPriceKlines', interval='1m', inst=INST,
                    period=period, rules=fixture_rules(eff_from=start, eff_to=end))
                present = sorted(set(opens) & set(expected))
                assert report.expected_rows == c.grid_points_between(a, b, 60)
                assert out['open_time'].to_list() == present
                missing = set(expected) - set(present)
                assert report.missing == len(missing)
                reported = []
                for gap in report.gaps:
                    left, right = dt.datetime.fromisoformat(gap['from']), dt.datetime.fromisoformat(gap['to'])
                    points = _grid(left, right)
                    assert gap['n'] == len(points)
                    reported.extend(points)
                assert sorted(reported) == sorted(missing)
                previous = a
                flags = []
                for point in present:
                    flags.append(c.grid_points_between(previous, point, 60) > 0)
                    previous = point + dt.timedelta(minutes=1)
                assert out['gap_flag'].to_list() == flags
                off_grid = set(opens) - set(expected)
                assert sum(q['reason_code'] == pc.R_TIME_GRID for q in quarantines) == len(off_grid)
    for frame in (fixture_bars(1).drop('open_time'),
                  fixture_bars(2).with_columns(pl.lit(None, dtype=pl.Datetime('us', 'UTC')).alias('open_time'))):
        with pytest.raises(ValueError, match='unknown bar time'):
            pc.check_bars(frame, pid='a28', data_type='klines', interval='1m', inst=INST,
                          period='2024-01', rules=None)


def test_differential_vision_count():
    rng = random.Random(2803)
    periods = ['2024-02', '2023-02', '2024-02-29', '2024-12', '2023-12-31']
    periods += [t.strftime('%Y-%m-%d') for t in _times()]
    intervals = list(vision.INTERVAL_SECONDS)
    rng.shuffle(intervals)
    for period in periods:
        a, b = vision.period_bounds(period)
        for interval in intervals:
            for kind in sorted(vision.BAR_TYPES):
                assert vision.expected_rows(kind, interval, period) == c.grid_points_between(
                    a, b, vision.INTERVAL_SECONDS[interval])
        for kind in ('fundingRate', 'metrics'):
            assert vision.expected_rows(kind, '8h', period) is None


def _interior_bar_variants(expected):
    # S42：移动的是窗口内 bar.open_time，而不是请求边界。条数始终不变。
    first, middle, *tail = expected
    for offset in (-1, 0, 1):
        yield [first, middle + offset * US, *tail]


def test_differential_kernel_grid():
    rng = random.Random(2804)
    for start in _times():
        for offset in (-1, 0, 1):
            end = start.replace(microsecond=0) + dt.timedelta(minutes=5) + offset * US
            req = _request(t_dec=start, t_start=None, horizon_end=end)
            kernel = KernelA(req, c.MarketView(manifest_id=req.market_manifest))
            expected = _grid(start, end)
            variants = [expected, [], expected[1:], expected[:-1], expected[::2],
                        [t for t in expected if rng.getrandbits(1)], *_interior_bar_variants(expected)]
            for opens in variants:
                bars = [c.Bar(open_time=t, o=Decimal(100), h=Decimal(100), l=Decimal(100), c=Decimal(100))
                        for t in opens]
                missing = sorted(set(expected) - set(opens))
                want = None
                # 空流由显式 points / bars_complete 处理，_first_bar_gap 无可定位证据。
                if opens and missing:
                    want = missing[0]
                assert kernel._first_bar_gap(bars, end) == want
    for value in (ABSENT, [], None):
        payload = {'manifest_id': 'a28'}
        if value is not ABSENT:
            payload['bars_last'] = value
        if value is None:
            with pytest.raises(ValidationError):
                c.MarketView.model_validate(payload)
        else:
            assert c.MarketView.model_validate(payload).bars_last == []


def _write_lake(root, start, end, opens, quality='valid'):
    lake = vision.LakePaths(root)
    day = start.date()
    while day <= end.date():
        local = [t for t in opens if t.date() == day]
        frame = pl.DataFrame({
            'open_time': pl.Series(local, dtype=pl.Datetime('us', 'UTC')),
            **{key: pl.Series([DECIMAL_EDGES[i % len(DECIMAL_EDGES)] for i in range(len(local))],
                              dtype=pl.Decimal(38, 12)) for key in ('open', 'high', 'low', 'close', 'volume')},
            'gap_flag': pl.Series([False] * len(local), dtype=pl.Boolean),
            'ohlc_valid': pl.Series([True] * len(local), dtype=pl.Boolean),
            'source_sha256': pl.Series(['h' * 64] * len(local), dtype=pl.String),
        })
        if quality.endswith('-missing'):
            frame = frame.drop(quality.removesuffix('-missing'))
        if quality.endswith('-null'):
            key = quality.removesuffix('-null')
            frame = frame.with_columns(pl.lit(None, dtype=frame.schema[key]).alias(key))
        for kind in ('klines', 'markPriceKlines'):
            vision.atomic_write_parquet(lake.silver_dir(kind, '1m', 'BTCUSDT') /
                                         f'date={day.isoformat()}' / 'part.parquet', frame)
            pid = vision.partition_id(kind, '1m', 'BTCUSDT', day.strftime('%Y-%m'))
            vision.atomic_write_json(lake.manifest(pid), {'partition_id': pid, 'source_sha256': 'h' * 64,
                                                        'check_status': 'ok'})
        day += dt.timedelta(days=1)
    return lake


def test_differential_lake_grid(tmp_path):
    rng = random.Random(2805)
    for index, start in enumerate(_times()):
        end = start.replace(microsecond=0) + dt.timedelta(minutes=5) + rng.choice((-1, 0, 1)) * US
        expected = _grid(start, end)
        req = _request(t_dec=start, t_start=None, horizon_end=end)
        variants = [expected, expected[:-1], [], expected[1:], *_interior_bar_variants(expected)]
        for variant, opens in enumerate(variants):
            root = tmp_path / f'{index}-{variant}'
            # 文件包含半开区间两侧的行，装载必须排除。
            supplied = sorted(set(opens + [start - US, end]))
            _write_lake(root, start, end, supplied)
            market = execution.load_market_from_lake(req, lake_root=root)
            present = set(opens) & set(expected)
            off_grid = set(opens) - set(expected)
            assert market.bars_complete == (present == set(expected) and not off_grid)
            assert market.bars_quality_ok == (not off_grid)
            for opened in off_grid:
                assert any("off-grid" in note and opened.isoformat() in note for note in market.quality_notes)
            for bars in (market.bars_last, market.bars_mark):
                assert [bar.open_time for bar in bars] == opens
            # Decimal 同路径载荷不能浮点往返；这不是估值公式的副本。
            for day in sorted({t.date() for t in supplied}):
                local = [t for t in supplied if t.date() == day]
                for i, time in enumerate(local):
                    if time in opens:
                        actual = next(bar for bar in market.bars_last if bar.open_time == time)
                        assert actual.c == DECIMAL_EDGES[i % len(DECIMAL_EDGES)]
    start = dt.datetime(2024, 2, 29, 12, tzinfo=dt.UTC)
    end = start + dt.timedelta(minutes=5)
    req = _request(t_dec=start, t_start=None, horizon_end=end)
    for key in ('gap_flag', 'ohlc_valid', 'source_sha256'):
        for form in ('missing', 'null'):
            root = tmp_path / f'{key}-{form}'
            _write_lake(root, start, end, _grid(start, end), f'{key}-{form}')
            market = execution.load_market_from_lake(req, lake_root=root)
            assert not market.bars_quality_ok and not market.bars_complete


def test_differential_start(monkeypatch, tmp_path):
    rng = random.Random(2806)
    for latency in (0, 1, 60, rng.randint(61, 3600)):
        policy = _policy(monkeypatch, latency_s=latency)
        for t_dec in _times():
            start = c.derived_t_start(t_dec, policy)
            req = _build(_plan(), t_dec, policy)
            assert req.resolved_t_start(policy) == start
            assert KernelA(req, c.MarketView(manifest_id='a28')).t_start == start
            explicit = c.ExecutionRequest.model_validate({**req.model_dump(), 't_start': start})
            assert explicit.resolved_t_start(policy) == start
            # 同一个带 latency 的请求，两种拼写均须按解析后启动时刻装载真实 parquet。
            end = start + dt.timedelta(minutes=3)
            root = tmp_path / f'{latency}-{t_dec.isoformat()}'
            opens = _grid(start, end)
            _write_lake(root, t_dec, end, sorted(set(opens + [t_dec - US, end])))
            for spelling in (None, start):
                bounded = c.ExecutionRequest.model_validate({**req.model_dump(), 't_start': spelling,
                    'horizon_end': end, 'horizon_source': 'caller'})
                actual = execution.load_market_from_lake(bounded, lake_root=root)
                assert actual.bars_complete
                assert [bar.open_time for bar in actual.bars_last] == opens
            for spelling in (ABSENT, None, start - US, start, start + US):
                for offset in (-1, 0, 1):
                    data = {**req.model_dump(), 'horizon_source': 'caller', 'horizon_end': start + offset * US}
                    data.pop('t_start')
                    if spelling is not ABSENT:
                        data['t_start'] = spelling
                    valid_spelling = spelling is ABSENT or spelling is None or spelling == start
                    assert _accepts(data) == (valid_spelling and data['horizon_end'] > start)
    for latency in (-1, -rng.randint(2, 3600)):
        with pytest.raises(c.ContractError, match='latency_s'):
            _policy(monkeypatch, latency_s=latency)


def test_differential_window(monkeypatch):
    rng = random.Random(2807)
    policy = _policy(monkeypatch, latency_s=60, research_horizon_s=123, max_horizon_s=400000)
    for hold in (ABSENT, None, 1, 60, rng.randint(61, 10000)):
        plan = _plan(60, hold)
        for t_dec in _times():
            ttl = c.resolve_entry_ttl_s(plan, policy)
            start = c.derived_t_start(t_dec, policy)
            window = dt.timedelta(seconds=c.derived_window_s(plan, policy, ttl))
            req = _build(plan, t_dec, policy)
            assert req.horizon_end == start + window
            assert req.horizon_source == 'policy'
            for boundary in (window, dt.timedelta(seconds=policy.max_horizon_s)):
                for offset in (-1, 0, 1):
                    end = start + boundary + offset * US
                    for source in ('policy', 'caller'):
                        data = {**req.model_dump(), 'horizon_end': end, 'horizon_source': source}
                        want = start < end <= start + dt.timedelta(seconds=policy.max_horizon_s)
                        if source == 'policy':
                            want = want and end == start + window
                        assert _accepts(data) == want
                    if end <= start + dt.timedelta(seconds=policy.max_horizon_s):
                        built = _build(plan, t_dec, policy, horizon_end=end)
                        assert built.horizon_end == end and built.horizon_source == 'caller'
                    else:
                        with pytest.raises(c.ContractError, match='安全上限'):
                            _build(plan, t_dec, policy, horizon_end=end)


def test_differential_ttl(monkeypatch):
    rng = random.Random(2808)
    policy = _policy(monkeypatch, entry_ttl_s=321)
    t_dec = next(_times())
    for supplied in (ABSENT, None, 1, 60, 321, rng.randint(322, 10000)):
        plan = _plan(supplied)
        expected = c.resolve_entry_ttl_s(plan, policy)
        req = _build(plan, t_dec, policy)
        assert req.entry_ttl_s == expected
        for representation in (plan, plan.model_dump()):
            for value in (ABSENT, None, [], 0, expected - 1, expected, expected + 1):
                data = {**req.model_dump(), 'order_plan': representation}
                data.pop('entry_ttl_s')
                if value is not ABSENT:
                    data['entry_ttl_s'] = value
                want = value is ABSENT or value is None or value == expected
                assert _accepts(data) == want
                if want:
                    actual = c.ExecutionRequest.model_validate(data)
                    assert actual.entry_ttl_s == expected
        for risk in DECIMAL_EDGES:
            assert _request(**{**req.model_dump(), 'risk_budget': risk}).risk_budget == risk
        # 显式空分配不能因 falsy 被修补为与省略等价（S22）。
        for field in ('entry_fractions', 'tp_fractions'):
            assert not _accepts({**req.model_dump(), field: []})
        for risk in (Decimal('0.0000000000001'), Decimal('10000.0000000000001')):
            assert not _accepts({**req.model_dump(), 'risk_budget': risk})


def _expiry_cases():
    rng = random.Random(2809)
    for start in _times():
        ttl = rng.choice((1, 60, 121))
        expiry = c.entry_expiry_at(start, ttl)
        for offset in (-1, 0, 1):
            end = expiry + offset * US
            req = _request(t_dec=start, t_start=None, order_plan=_plan(ttl), entry_ttl_s=None,
                           horizon_end=end, horizon_source='caller')
            yield req, expiry


def test_differential_expiry_timeline():
    for req, expiry in _expiry_cases():
        kernel = KernelA(req, c.MarketView(manifest_id=req.market_manifest))
        moments = kernel.timeline()
        expected = []
        if expiry <= req.horizon_end:
            expected = [expiry]
        assert [moment.ts for moment in moments if moment.expiry] == expected


def test_differential_expiry_orders():
    for req, expiry in _expiry_cases():
        kernel = KernelA(req, c.MarketView(manifest_id=req.market_manifest))
        kernel.submit_entries(kernel.t_start)
        orders = [order for order in kernel.orders.values() if order.leg == 'entry']
        assert orders
        assert all(order.status != 'rejected' for order in orders)
        assert [order.deadline for order in orders] == [expiry] * len(orders)


def test_differential_expiry_b():
    from quant_lab.market.nautilus_adapter import simulate_b
    # 真实 B 引擎产生到期事件，不用 mock order_factory 或夹具 theta 冒充消费证据。
    rng = random.Random(2810)
    for start in list(_times())[:6]:
        ttl = rng.randint(2, 70)
        expiry = c.entry_expiry_at(start, ttl)
        for offset in (-1, 0, 1):
            end = expiry + offset * US
            req = _request(t_dec=start, t_start=None, order_plan=_plan(ttl), entry_ttl_s=None,
                           horizon_end=end, horizon_source='caller')
            # B 时钟随行情推进；提供到期点以观测源时刻（已登记的 GTD 撮合顺序不在此重定义）。
            times = sorted({start, expiry - US, expiry, end})
            points = [c.PricePoint(ts=time, price=Decimal(110)) for time in times if time <= end]
            market = c.MarketView(manifest_id=req.market_manifest, last=points, mark=points)
            result = simulate_b(req, market)
            expired = [event for event in result.canonical_events if event.kind == 'expired']
            assert len(expired) == 1
            assert expired[0].ts == min(expiry, end)
            if expiry <= end:
                assert expired[0].reason == 'entry_ttl'
            else:
                assert expired[0].reason == 'horizon_end'


@pytest.mark.parametrize("offset_us", [-1, 0, 1])
@pytest.mark.parametrize("stream", ["bars_last", "bars_mark"])
def test_differential_kernel_public_interior_bar(offset_us, stream):
    """S42/S43：真实 Bar → MarketView → simulate，不绕验证、不替换生产函数。"""
    start = dt.datetime(2024, 1, 1, 1, tzinfo=dt.UTC)
    end = start + dt.timedelta(minutes=3)
    req = _request(t_dec=start, t_start=None, horizon_end=end)
    expected = _grid(start, end)
    opens = [start + dt.timedelta(microseconds=value)
             for value in (0, 60000000 + offset_us, 120000000)]

    def bars(times):
        return [c.Bar(open_time=time, interval_s=60, o=Decimal(100), h=Decimal(100),
                      l=Decimal(100), c=Decimal(100)) for time in times]

    payload = {"manifest_id": req.market_manifest, "bars_last": bars(expected),
               "bars_mark": bars(expected)}
    payload[stream] = bars(opens)
    market = c.MarketView.model_validate(payload)
    kernel = KernelA(req, market)
    result = execution.simulate(req, market=market)
    missing = sorted(set(expected) - set(opens))
    want_gap = None
    want_reason = "LABEL_RIGHT_CENSORED"
    if missing:
        want_gap = missing[0]
        want_reason = "BAR_GAP"
    assert result.censor_reason == want_reason
    assert result.coverage_mask.bars_ok == (not missing)
    assert kernel._first_bar_gap(getattr(market, stream), end) == want_gap


@pytest.mark.parametrize("offset_us", [-1, 0, 1])
@pytest.mark.parametrize("stream", ["klines", "markPriceKlines"])
def test_differential_lake_public_interior_bar(tmp_path, offset_us, stream):
    start = dt.datetime(2024, 1, 1, 1, tzinfo=dt.UTC)
    end = start + dt.timedelta(minutes=3)
    req = _request(t_dec=start, t_start=None, horizon_end=end)
    expected = _grid(start, end)
    lake = _write_lake(tmp_path, start, end, expected)
    path = lake.silver_dir(stream, "1m", "BTCUSDT") / "date=2024-01-01" / "part.parquet"
    opens = [start + dt.timedelta(microseconds=value)
             for value in (0, 60000000 + offset_us, 120000000)]
    frame = pl.read_parquet(path).with_columns(
        pl.Series("open_time", opens, dtype=pl.Datetime("us", "UTC")))
    vision.atomic_write_parquet(path, frame)
    market = execution.load_market_from_lake(req, lake_root=tmp_path)
    result = execution.simulate(req, market=market)
    assert market.bars_complete == (offset_us == 0)
    assert market.bars_quality_ok == (offset_us == 0)
    if offset_us != 0:
        assert not result.coverage_mask.bars_ok
        assert any(stream in note and opens[1].isoformat() in note for note in market.quality_notes)
