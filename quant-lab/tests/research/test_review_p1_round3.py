"""三审 E/F/G 反例；独立身份、账本、结构及报告工程门。"""
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import polars as pl
import pytest

from quant_lab.research import api as A, features as F, nullmodel as N, synthetic as S
from quant_lab.research.contract_tests import T0, make_bars
from quant_lab.research.ledger import Ledger, LedgerError, MemoryLedger


def test_S03_probe_F_identity_mismatch():
    anchors = pl.DataFrame({'episode_id': ['e'], 'instrument_id': ['X'], 't_dec': [T0],
                            'graph_version': ['actual-revoked'], 'derivation_hash': ['actual-d']})
    ctx = F.SnapshotContext(market_manifest='m', graph_version='different-live', derivation_hash='different-d', consumable=lambda: True)
    with pytest.raises(F.SnapshotInvalid):
        F.feature_snapshot([{'field': 'close'}], anchors, bars=make_bars([1., 2., 3.]), ctx=ctx, cache=True)


@pytest.mark.parametrize('key,value', [('graph_version', 'changed'), ('derivation_hash', 'changed'), ('revocation_epoch', 2)])
def test_S03_identity_and_epoch_bound_callback(key, value):
    actual = dict(graph_version='g', derivation_hash='d', revocation_epoch=1)
    seen = []
    def gate(g, d, epoch):
        seen.append((g, d, epoch))
        return (g, d, epoch) == tuple(actual.values())
    ctx = F.SnapshotContext(market_manifest='m', graph_version='g', derivation_hash='d', revocation_epoch=1, consumable=gate)
    a = pl.DataFrame({'episode_id': ['e'], 'instrument_id': ['X'], 't_dec': [T0]})
    assert ctx.check_identity(a)
    actual[key] = value
    assert not ctx.check_identity(a)
    assert seen == [('g', 'd', 1)] * 2


def test_S03_zero_arg_true_is_not_evidence():
    ctx = F.SnapshotContext(market_manifest='m', graph_version='g', derivation_hash='d', consumable=lambda: True)
    with pytest.raises(F.SnapshotInvalid):
        ctx.check_identity(pl.DataFrame())


@pytest.mark.parametrize('dep', [None, 1000.0])
def test_S06_probe_F_candidate_dependency_rejects_original_budget(dep, monkeypatch):
    w = N.synth_world(N.WorldConfig(seed=1, n_clusters=900, n_candidates=2))
    seen = []
    def deff(panel, **kw):
        seen.append(panel.candidate_ids)
        if panel.candidate_ids == ('base',):
            return 1.0
        return dep
    monkeypatch.setattr(A, 'deff_from_panel', deff)
    with patch.object(A, 'max_t_panel', side_effect=AssertionError('selection must not run')):
        result = A.run_pipeline(w.inputs, N.default_candidates(2), A.PipelineConfig(B=100, config_cap=2), ledger=MemoryLedger())
    assert any(ids != ('base',) for ids in seen)
    gates = [v for f in result['folds'] for v in f.get('candidate_dependency_gate', {}).values()]
    assert gates and all(not g['admitted'] and g['config_cap'] == 0 for g in gates)
    assert result['n_selected_folds'] == 0


def reserve(led, key='h', **kw):
    return led.reserve(origin='human', canonical_hash=key, params={}, fold_id='f', visible_cutoff='c',
                       objective='theta', data_manifest='m', seed=1, **kw)


def test_S07_probe_F_old_active_run_not_recovered(tmp_path, monkeypatch):
    led = Ledger(root=tmp_path, run_id='other-active-run')
    monkeypatch.setattr('quant_lab.research.ledger._now', lambda: (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat())
    aid = reserve(led)
    other = Ledger(root=tmp_path)
    assert other.recover(max_age_s=0) == 0
    assert led.status_of(aid) == 'reserved'
    monkeypatch.setattr('quant_lab.research.ledger._pid_alive', lambda pid: False)
    assert other.recover() == 1
    assert led.status_of(aid) == 'interrupted'


@pytest.mark.parametrize('damage', ['seq', 'attempt', 'status', 'duplicate_seq', 'illegal_transition'])
def test_S07_events_fail_closed_even_with_current_projection(tmp_path, damage):
    led = Ledger(root=tmp_path)
    aid = reserve(led)
    p = next(led.events_dir.glob('*.json'))
    event = json.loads(p.read_text())
    if damage == 'seq':
        event['event_seq'] += 1
    elif damage == 'attempt':
        event['attempt_id'] = 'a' * 32
    elif damage == 'status':
        event['status'] = 'failed'
    elif damage == 'duplicate_seq':
        event['attempt_id'] = 'b' * 32
        p = p.with_name(f"{event['event_seq']:012d}-{event['attempt_id']}-reserved.json")
    else:
        led.mark(aid, 'completed')
        event.update(event_seq=3, status='failed')
        p = p.with_name(f'000000000003-{aid}-failed.json')
    p.write_text(json.dumps(event))
    with pytest.raises(LedgerError):
        led.read()


def synthetic_config():
    cfg = A.load_config(str(Path(__file__).parent / 'fixtures/protocol_synthetic.yaml'))
    cfg['data']['synthetic'].update(n_episodes=30, n_bars=100, span_days=2)
    cfg['candidates']['asts'] = [{'field': 'close'}, {'field': 'close'}]
    return cfg


@pytest.mark.parametrize('disk', [False, True])
@pytest.mark.parametrize('failure', ['later_invalid', 'backend', 'duplicate'])
def test_S08_probe_E_ast_occurrences_terminal(tmp_path, disk, failure):
    led = Ledger(root=tmp_path) if disk else MemoryLedger()
    cfg = synthetic_config()
    if failure == 'later_invalid':
        cfg['candidates']['asts'][1] = {'op': 'Div', 'args': [{'field': 'close'}, {'field': 'open'}]}
    calls = []
    original = A.feature_snapshot
    def compute(*args, **kw):
        live = led.read().filter(pl.col('status').is_in(['reserved', 'running']))
        calls.append(live.height)
        assert live.height == 2
        if failure == 'backend':
            raise RuntimeError('backend boom')
        return original(*args, **kw)
    with patch.object(A, 'feature_snapshot', compute):
        if failure == 'duplicate':
            A.build_inputs_from_synthetic(cfg, ledger=led)
        else:
            with pytest.raises((ValueError, RuntimeError)):
                A.build_inputs_from_synthetic(cfg, ledger=led)
    rows = led.read().to_dicts()
    assert all(r['status'] not in ('reserved', 'running') for r in rows)
    snaps = [r for r in rows if r['objective'] == 'feature_snapshot']
    assert len(snaps) == 2
    if failure == 'duplicate':
        assert calls == [2]
        assert snaps[1]['parent_id'] == snaps[0]['attempt_id']
    else:
        assert not any(r['status'] == 'completed' for r in snaps)
    for r in rows:
        assert r['protocol_hash'] and r['opportunity_set_hash'] and r['policy_hash'] and r['code_version']
        assert json.loads(r['cost'])['wall_seconds'] >= 0
        if r['status'] == 'completed':
            assert len(r['result_hash']) == 64


def test_S08_memory_disk_terminal_cost_parity(tmp_path):
    outputs = []
    for led in (Ledger(root=tmp_path), MemoryLedger()):
        aid = reserve(led)
        led.mark(aid, 'running')
        led.mark(aid, 'completed', objective=0.25, result_hash='f' * 64)
        dup = reserve(led)
        again = reserve(led, recompute_of=aid)
        led.mark(again, 'failed', reason='backend')
        with pytest.raises(LedgerError):
            led.mark(aid, 'failed')
        with pytest.raises(LedgerError):
            led.mark(again, 'invented')
        rows = led.read().to_dicts()
        assert led.status_of(dup) == 'duplicate'
        for r in rows:
            if r['status'] != 'duplicate':
                cost = json.loads(r['cost'])
                assert set(cost) >= {'wall_seconds', 'cpu_seconds', 'peak_bytes', 'api_cost'}
                assert all(v >= 0 for v in cost.values())
        outputs.append([(r['status'], r.get('objective_value'), r.get('result_hash'), r.get('reason_code')) for r in rows])
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize('reverse', [False, True])
def test_S12_probe_E_single_arm_censoring_excluded(reverse):
    base, cand = np.array([1., 2.]), np.array([np.nan, 2.])
    if reverse:
        base, cand = cand, base
    a = pl.DataFrame({'episode_id': ['a', 'b'], 'cluster_id': ['a', 'b'], 't_dec': [T0, T0]})
    inp = A.PanelInputs(a, base, np.array([True, False]), np.ones(2), {'f': np.ones(2)}, cand_R=cand)
    inp.validate()
    d, _, _ = A.candidate_diffs(inp, A.Candidate('c', 'f', A.RuleSpec('all', 'gt_c', 0.)), 0., np.arange(2))
    panel = A._panel(inp, np.arange(2), {'c': d}, 'f')
    assert panel.mask.tolist() == [False, True]
    assert panel.diffs.tolist() == [[0.], [0.]]


@pytest.mark.parametrize('value', [None, np.nan, np.inf, -np.inf])
@pytest.mark.parametrize('arm', ['base', 'candidate'])
def test_S12_nonfinite_under_common_mask_rejected(value, arm):
    a = pl.DataFrame({'episode_id': ['a'], 'cluster_id': ['a'], 't_dec': [T0]})
    base, cand = np.ones(1), np.ones(1)
    if arm == 'base':
        base = np.array([value])
    else:
        cand = np.array([value])
    with pytest.raises(A.PanelInvalid):
        A.PanelInputs(a, base, np.zeros(1, dtype=bool), np.ones(1), {}, cand_R=cand).validate()


@pytest.mark.parametrize('entry_plan,tp_plan', [(True, False), (False, True), (True, True), (False, False)])
def test_S14_probe_E_mixed_sources_three_leg_G2_parity(entry_plan, tp_plan):
    from quant_lab.market.contract import build_request, resolve_policy
    from quant_lab.market.execution import simulate_batch
    from tests.research.test_g2_parity import _resolver
    row = S.fake_episodes(1, span_days=2, seed=3, censor_frac=0).row(0, named=True)
    plan = row['order_plan']
    for key, given in [('entries', entry_plan), ('tps', tp_plan)]:
        template = plan[key][0]
        plan[key] = [dict(template, fraction=Decimal(v) if given else None) for v in ('0.2', '0.3', '0.5')]
    req = build_request(row, policy_version='base-v1', policy_hash=resolve_policy('base-v1').content_hash,
                        risk_budget=Decimal(100), market_manifest='m')
    fake = S.fake_execution(pl.DataFrame([row], schema_overrides={'order_plan': S.ORDER_PLAN_DTYPE}), policy_version='base-v1')
    real = simulate_batch([req], kernel='A', resolver=_resolver, strict=True)
    assert list(fake.schema.items()) == list(real.schema.items())
    for field in ('fraction_source', 'entry_fractions', 'tp_fractions'):
        assert fake[field].to_list() == real[field].to_list()
    assert fake['entry_fractions'].to_list()[0] == list(req.entry_fractions)
    assert fake['tp_fractions'].to_list()[0] == list(req.tp_fractions)
    assert fake['fraction_source'][0] == ('plan' if entry_plan and tp_plan else 'policy')


@pytest.mark.parametrize('key', ['entries', 'tps'])
def test_S14_partial_fraction_rejected(key):
    row = S.fake_episodes(1, span_days=2, seed=3).row(0, named=True)
    leg = row['order_plan'][key][0]
    row['order_plan'][key] = [dict(leg, fraction=Decimal('0.5')), dict(leg, fraction=None)]
    with pytest.raises(ValueError):
        S.fake_execution(pl.DataFrame([row], schema_overrides={'order_plan': S.ORDER_PLAN_DTYPE}))


@pytest.mark.parametrize('damage', ['cluster', 'instrument', 'missing', 'time', 'cross', 'nonfinite'])
@pytest.mark.parametrize('kind', ['null', 'power'])
def test_S10_probe_F_structure_faults_invalid_both_paths(damage, kind, monkeypatch):
    w = N.synth_world(N.WorldConfig(seed=1, n_clusters=900, n_candidates=2))
    bad = replace(w.inputs)
    if damage == 'cluster':
        bad.anchors = bad.anchors.with_columns(pl.Series('cluster_id', [f'unique{i}' for i in range(bad.n)]))
    elif damage == 'instrument':
        bad.anchors = bad.anchors.with_columns(pl.lit('ONE').alias('instrument_id'))
    elif damage == 'missing':
        bad.base_R = np.roll(bad.base_R, 1)
        bad.censored = np.roll(bad.censored, 1)
    else:
        original = N._dependence_diagnostics
        def diagnostics(inp, *args):
            ac, cross = original(inp, *args)
            if inp is bad:
                if damage == 'time':
                    return 0.95, cross
                if damage == 'cross':
                    return ac, [-0.95] * len(cross)
                return np.nan, cross
            return ac, cross
        monkeypatch.setattr(N, '_dependence_diagnostics', diagnostics)
    guard = N.assert_not_episode_shuffle(w.inputs, bad, w.day)
    assert guard['ok'] is False
    monkeypatch.setattr(N, 'synth_world', lambda cfg: w)
    monkeypatch.setattr(N, 'resample_null', lambda *a, **kw: bad)
    r = N.run_mc('common_shock', kind=kind, n_rep=1, world_cfg=w.cfg, pipe_cfg=A.PipelineConfig(B=100))
    assert r.verdict == 'invalid_null_model' and r.n_failed == 1
    assert r.diagnostics['n_invalid_null_model'] == 1 and r.worst_case_ci is not None
    assert any(v == 1 for v in r.diagnostics['guard_failures_by_check'].values())


def report_text():
    return Path('docs/adr/report-G3-null-model.md').read_text()


@pytest.mark.parametrize('damage', ['D1_false_power_pass', 'E_duplicate', 'G_guard_false', 'guard_count', 'invalid', 'all_T0',
                                   'short_run', 'arithmetic', 'tiers', 'limitation', 'unfinished', 'nonfinite_diagnostic'])
def test_S17_probe_D1_E_G_forged_reports_rejected(damage):
    text = report_text()
    start, end = text.rindex('```json') + 7, text.rindex('```')
    j = json.loads(text[start:end])
    r = j['results'][0]
    if damage == 'D1_false_power_pass':
        next(r for r in j['results'] if r['kind'] == 'power')['verdict'] = 'pass'
    elif damage == 'E_duplicate':
        bad = copy.deepcopy(r)
        bad.update(n_positive=900, verdict='fail')
        j['results'].insert(0, bad)
    elif damage == 'G_guard_false':
        r['diagnostics']['shuffle_guard']['ok'] = False
    elif damage == 'guard_count':
        r['diagnostics']['guard_failures_by_check']['icc'] = 1
    elif damage == 'invalid':
        r['diagnostics']['n_invalid_null_model'] = 1
    elif damage == 'nonfinite_diagnostic':
        r['diagnostics']['shuffle_guard']['sd']['null'] = float('nan')
    elif damage == 'all_T0':
        r['tiers'] = {'T0': r['n_done']}
    elif damage == 'short_run':
        r['n_done'] = r['n_planned'] = 100
    elif damage == 'arithmetic':
        r['searched_worst_ci'] = [0., 0.01]
    elif damage == 'tiers':
        r['tiers']['T1'] += 1
    elif damage == 'unfinished':
        r['n_done'] -= 1
    fake = text[:start] + json.dumps(j) + text[end:]
    if damage == 'limitation':
        fake = fake.replace('未检出', 'X').replace('合成', 'Y')
    # 内存替身读取伪造报告；独立进程运行与看板相同入口，退出码也必须失败。
    task = next(t for t in json.loads(Path('taskList.json').read_text())['modules']['research']['tasks'] if t['id'] == 'R-08')
    body = shlex.split(task['verify'].split(' && ')[-1])[-1]
    code = "import io,sys;from unittest.mock import patch;from quant_lab.research.nullmodel import verify_report_text;fake=sys.stdin.read()\nwith patch('builtins.open',return_value=io.StringIO(fake)):\n " + body
    result = subprocess.run([sys.executable, '-c', code], input=fake, text=True, capture_output=True)
    assert result.returncode != 0, result.stdout


def test_S17_real_low_power_truthful_fail_accepted():
    assert N.verify_report_text(report_text())['results']
    task = next(t for t in json.loads(Path('taskList.json').read_text())['modules']['research']['tasks'] if t['id'] == 'R-08')
    body = shlex.split(task['verify'].split(' && ')[-1])[-1]
    code = "import io,sys;from unittest.mock import patch;from quant_lab.research.nullmodel import verify_report_text;fake=sys.stdin.read()\nwith patch('builtins.open',return_value=io.StringIO(fake)):\n " + body
    result = subprocess.run([sys.executable, '-c', code], input=report_text(), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('text,ok', [('二审终裁：pass\n三审终裁：fail\n', False), ('二审终裁：fail\n三审终裁：pass\n', True)])
def test_S17_latest_review_verdict(text, ok):
    task = next(t for t in json.loads(Path('taskList.json').read_text())['modules']['research']['tasks'] if t['id'] == 'R-10')
    command = task['verify'].split(' && ', 1)[1].replace(' docs/adr/review-G3-P1.md', '')
    result = subprocess.run(['bash', '-c', command], input=text, text=True)
    assert (result.returncode == 0) == ok


@pytest.mark.parametrize('when', ['cold_publish', 'between_asts', 'hot_read'])
def test_S03_epoch_change_discards_real_snapshot(when, monkeypatch, lake_root):
    from tests.research.test_features import CLOSE, MEAN3, anchors, STEP
    a = anchors([T0 + 4 * STEP])
    bars = make_bars([float(i) for i in range(10)])
    state = {'epoch': 1}
    ctx = F.SnapshotContext(market_manifest='m', graph_version='g', derivation_hash='d', revocation_epoch=1,
                            consumable=lambda g, d, epoch: (g, d, epoch) == ('g', 'd', state['epoch']))
    if when == 'hot_read':
        F.feature_snapshot([CLOSE], a, bars=bars, ctx=ctx, cache=True)
        original = F._cache_read
        def read(*args):
            result = original(*args)
            state['epoch'] = 2
            return result
        monkeypatch.setattr(F, '_cache_read', read)
    elif when == 'between_asts':
        original = F._cache_write
        def write(*args):
            original(*args)
            state['epoch'] = 2
        monkeypatch.setattr(F, '_cache_write', write)
    else:
        backend = F.get_backend('polars')
        original = backend.compute
        def compute(*args):
            result = original(*args)
            state['epoch'] = 2
            return result
        monkeypatch.setattr(backend, 'compute', compute)
        monkeypatch.setattr(F, 'get_backend', lambda name: backend)
    with pytest.raises(F.SnapshotInvalid):
        F.feature_snapshot([CLOSE, MEAN3], a, bars=bars, ctx=ctx, cache=True)


def test_S08_selection_family_failure_closes_previous_attempt(monkeypatch):
    w = N.synth_world(N.WorldConfig(seed=1, n_clusters=900, n_candidates=2))
    led = MemoryLedger()
    monkeypatch.setattr(A, 'deff_from_panel', lambda *a, **kw: 1.)
    original = A.candidate_diffs
    calls = []
    def diffs(inp, candidate, *args):
        calls.append(candidate.cand_id)
        if len(set(calls)) > 1:
            raise RuntimeError('second candidate fails')
        return original(inp, candidate, *args)
    monkeypatch.setattr(A, 'candidate_diffs', diffs)
    with pytest.raises(RuntimeError, match='second candidate'):
        A.run_pipeline(w.inputs, N.default_candidates(2), A.PipelineConfig(B=100, config_cap=2), ledger=led)
    selection = [r for r in led.rows.values() if r['stage'] == 'selection']
    assert len(selection) == 2 and all(r['status'] == 'failed' for r in selection)
    assert all(r['status'] not in ('reserved', 'running') for r in led.rows.values())


def test_S12_explicit_arm_flags_must_match_values():
    a = pl.DataFrame({'episode_id': ['a'], 'cluster_id': ['a'], 't_dec': [T0]})
    inp = A.PanelInputs(a, np.ones(1), np.ones(1, dtype=bool), np.ones(1), {}, cand_R=np.array([np.nan]),
                        base_censored=np.zeros(1, dtype=bool), cand_censored=np.ones(1, dtype=bool))
    inp.validate()
    with pytest.raises(A.PanelInvalid):
        replace(inp, base_censored=np.ones(1, dtype=bool)).validate()
    with pytest.raises(A.PanelInvalid):
        replace(inp, cand_censored=np.zeros(1, dtype=bool)).validate()


def test_S14_policy_total_fraction_matches_G2():
    from quant_lab.market.contract import build_request, resolve_policy
    row = S.fake_episodes(1, span_days=2, seed=3).row(0, named=True)
    for key in ('entries', 'tps'):
        template = row['order_plan'][key][0]
        row['order_plan'][key] = [dict(template, fraction=None) for _ in range(3)]
    version = 'fixture-halftp-v1'
    req = build_request(row, policy_version=version, policy_hash=resolve_policy(version).content_hash,
                        risk_budget=Decimal(100), market_manifest='m')
    fake = S.fake_execution(pl.DataFrame([row], schema_overrides={'order_plan': S.ORDER_PLAN_DTYPE}), policy_version=version)
    assert fake['tp_fractions'].to_list()[0] == list(req.tp_fractions)
    assert sum(fake['tp_fractions'].to_list()[0]) == Decimal('0.5')
    assert fake['policy_hash'][0] == req.policy_hash


@pytest.mark.parametrize('damage', ['time', 'cross'])
def test_S10_actual_panel_correlation_destruction(damage):
    rng = np.random.default_rng(4)
    days = 240
    common = np.empty(days)
    common[0] = rng.normal()
    for i in range(1, days):
        common[i] = 0.97 * common[i - 1] + rng.normal()
    values = common[:, None] + rng.normal(0, 0.1, (days, 3))
    day = np.repeat(np.arange(days), 3)
    n = len(day)
    anchors = pl.DataFrame({'episode_id': [f'e{i}' for i in range(n)], 'cluster_id': [f'c{i // 3}' for i in range(n)],
                            'instrument_id': list('ABC') * days, 't_dec': [T0 + dt.timedelta(days=int(d)) for d in day]})
    orig = A.PanelInputs(anchors, values.reshape(-1), np.zeros(n, dtype=bool), np.full(n, 1 / 3), {})
    changed = values.copy()
    if damage == 'time':
        changed = values[rng.permutation(days)]
    else:
        for j in range(3):
            changed[:, j] = values[rng.permutation(days), j]
    bad = replace(orig, base_R=changed.reshape(-1))
    guard = N.assert_not_episode_shuffle(orig, bad, day, block_len_days=1)
    assert not guard['ok']
    assert guard['checks']['cluster_layout'] and guard['checks']['missing_layout']
    check = 'block_ac1' if damage == 'time' else 'cross_instrument'
    assert not guard['checks'][check]


@pytest.mark.parametrize('undefined', [False, True])
def test_S06_real_paired_target_dependence_tightens_gate(undefined):
    w = N.synth_world(N.WorldConfig(seed=1, n_clusters=1600, n_candidates=1, censor_frac=0))
    rng = np.random.default_rng(17)
    base = rng.normal(size=w.inputs.n)
    common = np.zeros(360)
    if not undefined:
        for i in range(1, 360):
            common[i] = 0.98 * common[i - 1] + rng.normal()
    inp = replace(w.inputs, base_R=base, cand_R=base + common[w.day], features={'f': np.ones(w.inputs.n)})
    candidate = A.Candidate('paired', 'f', A.RuleSpec('all', 'gt_c', 0.))
    report = A.run_pipeline(inp, [candidate], A.PipelineConfig(B=100, config_cap=1), ledger=MemoryLedger())
    assert report['candidate_pool']['n_admitted'] == 1
    gates = [f['candidate_dependency_gate']['paired'] for f in report['folds'] if f.get('candidate_dependency_gate')]
    assert gates and all(not g['admitted'] for g in gates)
    assert report['n_selected_folds'] == 0
    if undefined:
        assert all(g['DEFF'] is None for g in gates)
    else:
        assert all(g['DEFF'] > 2 for g in gates)


# ================================================================ B11（execution-interface §5.13）：caller 观察窗进配置身份
def test_B11_caller_horizon_end_enters_config_id(tmp_path, monkeypatch):
    """§5.13 B11：horizon_source=="caller" 时 horizon_end 进 config_id——换观察窗即新尝试（非 duplicate）；
    同值仍识别 duplicate（纳入本身不加重预算）；policy 推导窗不入（由 policy_hash 覆盖）。"""
    import datetime as dt
    import polars as pl
    from quant_lab.research.ledger import Ledger, config_id

    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(tmp_path / "lake"))
    h1 = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
    h2 = dt.datetime(2024, 3, 8, tzinfo=dt.UTC)
    base = dict(canonical_hash="ast-1", params={"rule": "gt_q70"}, objective="theta", rule_hash="rh")
    assert config_id(**base) == config_id(**base, caller_horizon_end=None)                 # policy 窗不入身份
    assert config_id(**base, caller_horizon_end=h1) != config_id(**base)                   # caller 窗改变身份
    assert config_id(**base, caller_horizon_end=h1) != config_id(**base, caller_horizon_end=h2)
    assert config_id(**base, caller_horizon_end=h1) == config_id(**base, caller_horizon_end=h1)

    led = Ledger()
    kw = dict(origin="human", canonical_hash="ast-1", params={"rule": "gt_q70"}, fold_id="wf000", visible_cutoff=h1,
              objective="theta", data_manifest="dm", seed=0, rule_hash="rh")
    a1 = led.reserve(**kw, caller_horizon_end=h1)
    led.mark(a1, "completed")
    a_same = led.reserve(**kw, caller_horizon_end=h1)
    assert led.status_of(a_same) == "duplicate"                                            # 同窗口 → 重复，不额外耗预算
    a_other = led.reserve(**kw, caller_horizon_end=h2)
    assert led.status_of(a_other) == "reserved"                                            # 换窗口 → 独立尝试
    df = led.read()
    ids = set(df.filter(pl.col("status") != "duplicate")["config_id"].to_list())
    assert len(ids) == 2 and df.filter(pl.col("attempt_id") == a_other)["caller_horizon_end"][0] == h2.isoformat()


# ================================================================ A27（research-schema §9.10.17）：带抽样方差的门必须"仍会触发"
def test_A27_structure_gate_fires_on_injected_break_and_is_quiet_on_normal():
    """A27 判别：光证明假警报消失不够——注入一次真实结构破坏，门必须仍然触发。
    正常生成器（误拒率）与破坏生成器（检出率）同时断言，二者缺一都无法把'口径修正'与'调宽到永不触发'区分开。"""
    import numpy as np
    from quant_lab.research import nullmodel as NM
    from quant_lab.research.api import PipelineConfig

    w = NM.synth_world(NM.WorldConfig(mechanism="common_shock", seed=2, n_clusters=1600))
    m = NM.fit_residual_model(w, block_len_days=3, train_end_day=180)
    fitted = m.grid[:m.train_days[1]]

    # (a) 正常生成器：逐 replicate 结构门不得因噪声触发
    normal_fail = 0
    for i in range(20):
        x = NM.resample_null(w, m, np.random.default_rng(4242 + i))
        g = NM.assert_not_episode_shuffle(w.inputs, x, w.day, block_len_days=3, fitted_grid=fitted)
        normal_fail += (not g["ok"])
    assert normal_fail <= 1, f"正常生成器误拒 {normal_fail}/20（门在惩罚估计噪声）"

    # (b) 注入真实破坏（各品种独立重排冲击块 → 摧毁跨品种结构）：门必须每次触发
    def break_cross(x, rng):
        grid = x.shock_grid.copy()
        nb = grid.shape[0] // 3
        for j in range(grid.shape[1]):
            grid[:nb * 3, j] = grid[:nb * 3, j].reshape(nb, 3)[rng.permutation(nb)].reshape(-1)
        return NM.replace(x, shock_grid=grid)

    # (b) 注入真实破坏（各品种独立重排冲击块 → 摧毁跨品种结构）。
    # 逐 replicate 判据是 Fisher-z 的 4σ，而该破坏的幅度约 3.1σ → 单次检出只有约 40%，这是刻意的保守取值：
    # 单次门用于抓粗大异常，**系统性**破坏由整轮总体检查（均值 vs 拟合格点）决定，见 (c)。
    # 因此这里断言"检出率显著高于正常误拒率"，而不是要求单次必中——后者只能靠把 k 调松换来，那会把假警报请回来。
    broken_caught = 0
    for i in range(20):
        rng = np.random.default_rng(9000 + i)
        g = NM.assert_not_episode_shuffle(w.inputs, break_cross(NM.resample_null(w, m, rng), rng), w.day, block_len_days=3, fitted_grid=fitted)
        broken_caught += (not g["ok"]) and (not g["checks"]["cross_instrument"])
    assert broken_caught >= 5 and broken_caught > 4 * max(normal_fail, 1), f"注入破坏检出 {broken_caught}/20，正常误拒 {normal_fail}/20（门已失效）"

    # (c) 整轮判定：破坏生成器必须判 invalid_null_model，正常的不得判
    orig_rs = NM.resample_null
    try:
        NM.resample_null = lambda world, model, rng, **kw: break_cross(orig_rs(world, model, rng, **kw), rng)
        r_bad = NM.run_mc("common_shock", kind="null", n_rep=8, seed0=2, world_cfg=NM.WorldConfig(n_clusters=1600), pipe_cfg=PipelineConfig(B=100))
    finally:
        NM.resample_null = orig_rs
    assert r_bad.verdict == "invalid_null_model" and r_bad.diagnostics["invalid_reason"]
    assert r_bad.diagnostics["grid_dependence"]["ok"] is False        # 决定性的是总体检查：均值 vs 拟合格点
    r_ok = NM.run_mc("common_shock", kind="null", n_rep=8, seed0=2, world_cfg=NM.WorldConfig(n_clusters=1600), pipe_cfg=PipelineConfig(B=100))
    assert r_ok.verdict != "invalid_null_model" and not r_ok.diagnostics["invalid_reason"]
    assert r_ok.diagnostics["grid_dependence"]["ok"] is True


def test_B19_liquidation_accounting_is_g2_owned_not_reimplemented():
    """B19 哨兵：强平记账由 G2 出唯一函数，G3 只调用不自算。
    当前 G3 不做任何强平计算（只镜像 G2 的 coverage_mask.liquidation_unmodeled 诊断标志）；
    若将来需要强平数值，必须从 quant_lab.market 导入，本测试会在自算时变红。"""
    import ast as pyast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant_lab" / "research"
    offenders = []
    for f in sorted(root.rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        if "liquidation" not in src and "maintenance_margin" not in src:
            continue
        tree = pyast.parse(src)
        imported = {n.module for n in pyast.walk(tree) if isinstance(n, pyast.ImportFrom) and (n.module or "").startswith("quant_lab.market")}
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)) and ("liquidation" in node.name or "margin" in node.name):
                offenders.append(f"{f.name}:{node.name} 自定义强平/保证金函数")
        for node in pyast.walk(tree):   # 赋值给 liquidation_* 的算式（非布尔标志镜像）也算自算
            if isinstance(node, pyast.Assign):
                for t in node.targets:
                    name = getattr(t, "id", None) or getattr(t, "attr", None)
                    if name and "liquidation" in name and not isinstance(node.value, (pyast.Constant, pyast.Name, pyast.Attribute, pyast.Subscript)):
                        offenders.append(f"{f.name}: 对 {name} 的自算赋值")
        assert imported or "liquidation_unmodeled" in src, f"{f.name} 使用 liquidation 但既不 import quant_lab.market 也不是标志镜像"
    assert not offenders, offenders


# ================================================================ A31（research-schema §9.10.18）：制品不得旧于判它的门
def test_A31_report_must_be_generated_by_current_code():
    """A31：受门判定的制品必须由不旧于门代码的版本生成，且该关系由 verify 机械断言（内嵌哈希变体）。"""
    import json as _json
    from pathlib import Path as _Path
    from quant_lab.research.nullmodel import research_code_digest, verify_report_text

    text = _Path("docs/adr/report-G3-null-model.md").read_text(encoding="utf-8")
    digest = research_code_digest()
    assert f"研究代码 sha256：`{digest}`" in text, "报告正文未内嵌生成它的代码哈希"
    payload = _json.loads(text[text.rindex("```json") + 7:text.rindex("```")])
    assert payload["meta"]["research_code_sha256"] == digest
    verify_report_text(text)                                   # 新鲜制品：通过

    stale = text.replace(digest, "0" * 64)                     # 篡改为旧版哈希：必须被拒
    with pytest.raises(ValueError, match="制品陈旧"):
        verify_report_text(stale)
    assert research_code_digest() == digest                    # 哈希稳定（同一份源码重复计算一致）
