"""A29/A33 disk mutations, isolated imports/collection, SHA256 restoration, A26 overlays.

Only tests/market holds temporary copies and evidence. No source import rewriting,
pytest monkeypatch, or collection-time function substitution is used.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'tests/market/s45-a36-mutation-evidence.json'
GROUPS = ('time', 'numeric', 'empty', 'equivalence')
CHILD = r'''
import hashlib, importlib, json, os, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
expected = json.loads(sys.argv[2])
sys.path[:0] = [str(root / 'src'), str(root)]
def verify():
    for relative, sha in expected.items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == sha
    for name in ('contract', 'execution', 'kernel_a', 'single_source', 'partition_check', 'vision'):
        module = importlib.import_module('quant_lab.market.' + name)
        assert Path(module.__file__).resolve() == root / 'src/quant_lab/market' / (name + '.py')
    print('ISOLATION_OK pid=' + str(os.getpid()) + ' root=' + str(root), flush=True)
verify()
import pytest
outcomes = {}
class Evidence:
    def pytest_collection_finish(self, session):
        verify()
        for item in session.items:
            assert Path(item.path).resolve().is_relative_to(root / 'tests/market')
        print('COLLECTION_ORIGINS_OK items=' + str(len(session.items)), flush=True)
    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or report.failed:
            outcomes[report.nodeid] = report.outcome
code = pytest.main(['-c', str(root / 'pytest.ini'), '--confcutdir=' + str(root),
    'tests/market/test_boundary_discrimination.py',
    'tests/market/test_single_source.py::test_differential_partition_grid',
    '-q', '-p', 'no:cacheprovider'], plugins=[Evidence()])
print('OUTCOMES_JSON=' + json.dumps(outcomes), flush=True)
sys.exit(code)
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(root):
    return {str(p.relative_to(root)): sha(p.read_bytes())
            for parent in ('src', 'tests') for p in sorted((root / parent).rglob('*')) if p.is_file()}


def run(root):
    hashes = snapshot(root)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'PYTEST'))}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    # A33: child checks the exact on-disk files the parent hashed, twice.
    watched = {k: v for k, v in hashes.items() if k.startswith('src/quant_lab/market/') and k.endswith('.py')}
    result = subprocess.run([sys.executable, '-B', '-c', CHILD, str(root), json.dumps(watched)],
                            cwd=root, env=env, capture_output=True, text=True, timeout=180)
    output = result.stdout + result.stderr
    print(output, flush=True)
    assert 'ISOLATION_OK' in output and 'COLLECTION_ORIGINS_OK items=15' in output, output
    assert snapshot(root) == hashes, 'child changed disk contents'
    outcomes = json.loads(next(line.removeprefix('OUTCOMES_JSON=') for line in output.splitlines()
                               if line.startswith('OUTCOMES_JSON=')))
    pid = int(output.split('ISOLATION_OK pid=')[1].split()[0])
    return {'pid': pid, 'exit_code': result.returncode, 'outcomes': outcomes, 'output': output}


def matrix(run_result):
    return {group: run_result['outcomes']['tests/market/test_boundary_discrimination.py::test_boundary_' + group]
            for group in GROUPS}


def mutate(path, before, after):
    source = path.read_text()
    assert source.count(before) == 1, (path, before)
    path.write_text(source.replace(before, after))


def main():
    assert os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'
    records = []
    with tempfile.TemporaryDirectory(prefix='.s45-a36-', dir=ROOT / 'tests/market') as name:
        root = Path(name).resolve()
        ignore = shutil.ignore_patterns('__pycache__', '.s45-a36-*', '*.log', '*evidence.json')
        shutil.copytree(ROOT / 'src', root / 'src', ignore=ignore)
        shutil.copytree(ROOT / 'tests/market', root / 'tests/market', ignore=ignore)
        (root / 'tests/__init__.py').write_text('')
        (root / 'pytest.ini').write_text('[pytest]\nfilterwarnings = ignore::DeprecationWarning\n')
        control = ('partition_check.py', 'rep.expected_rows = exp_n', 'rep.expected_rows = exp_n + 1')
        cases = [
            ('known_red_control', *control, False),
            ('S45_time_forward_tolerance', 'partition_check.py', 'first_grid_point(t, sec) == t',
             '(first_grid_point(t, sec) - t <= dt.timedelta(microseconds=1))', 'time'),
            ('A36_numeric_float_validation', 'contract.py', 'check_decimal(self.risk_budget, "risk_budget")',
             'check_decimal(Decimal(float(self.risk_budget)), "risk_budget")', 'numeric'),
            ('A36_empty_falsy_fallback', 'contract.py',
             '"entry_fractions": ef if data.get("entry_fractions") is None else data["entry_fractions"]',
             '"entry_fractions": ef if not data.get("entry_fractions") else data["entry_fractions"]', 'empty'),
            ('A36_equivalence_explicit_start', 'contract.py',
             'return self.t_start if self.t_start is not None else derived_t_start(self.t_dec, policy)',
             'return self.t_start + dt.timedelta(microseconds=1) if self.t_start is not None else derived_t_start(self.t_dec, policy)',
             'equivalence'),
            ('A37_label_before_evidence', 'contract.py',
             'CENSOR_PRIORITY = ("SYMBOL_TIME_INVALID", "RULE_HISTORY_MISSING", "BAR_GAP", "MARK_STALE", "FUNDING_SCHEDULE_GAP", "LABEL_RIGHT_CENSORED")',
             'CENSOR_PRIORITY = ("LABEL_RIGHT_CENSORED", "SYMBOL_TIME_INVALID", "RULE_HISTORY_MISSING", "BAR_GAP", "MARK_STALE", "FUNDING_SCHEDULE_GAP")', False),
        ]
        for label, filename, before, after, group in cases:
            target = root / 'src/quant_lab/market' / filename
            control_path = root / 'src/quant_lab/market' / control[0]
            originals = {path: path.read_bytes() for path in (target, control_path)}
            tree_before = snapshot(root)
            entry = {'case': label, 'target': str(target.relative_to(root)), 'replacement': [before, after],
                     'before_sha256': sha(originals[target]), 'tree_before_sha256': sha(json.dumps(tree_before, sort_keys=True).encode())}
            print('CASE ' + label + ' BASELINE', flush=True)
            entry['baseline'] = run(root)
            assert entry['baseline']['exit_code'] == 0
            try:
                mutate(target, before, after)
                entry['injected_sha256'] = sha(target.read_bytes())
                assert entry['injected_sha256'] != entry['before_sha256']
                print('CASE ' + label + ' INJECTED', flush=True)
                entry['injected'] = run(root)
                if group:
                    # A26: before trusting any off-diagonal GREEN, break the shared
                    # property too. All four input groups must now fail behaviorally.
                    mutate(control_path, control[1], control[2])
                    entry['overlay_sha256'] = sha(control_path.read_bytes())
                    print('CASE ' + label + ' A26 OVERLAY', flush=True)
                    entry['overlay'] = run(root)
                    assert set(matrix(entry['overlay']).values()) == {'failed'}
            finally:
                for path, content in originals.items():
                    path.write_bytes(content)
                # Restoration is content identity, never inferred from an exit code.
                entry['restored_sha256'] = sha(target.read_bytes())
                assert snapshot(root) == tree_before
                assert entry['restored_sha256'] == entry['before_sha256']
            print('CASE ' + label + ' RESTORED SHA256=' + entry['restored_sha256'], flush=True)
            entry['restored'] = run(root)
            records.append(entry)
            OUTPUT.write_text(json.dumps(records, indent=2, ensure_ascii=False) + '\n')
            assert entry['restored']['exit_code'] == 0
            assert entry['injected']['exit_code'] == 1
            assert len({entry[stage]['pid'] for stage in ('baseline', 'injected', 'restored')}) == 3
            if group:
                assert matrix(entry['injected']) == {g: 'failed' if g == group else 'passed' for g in GROUPS}
            if label == 'known_red_control':
                assert set(matrix(entry['injected']).values()) == {'failed'}
            if label.startswith('S45'):
                assert entry['injected']['outcomes']['tests/market/test_single_source.py::test_differential_partition_grid'] == 'failed'
            if label.startswith('A37'):
                failures = [node for node, outcome in entry['injected']['outcomes'].items()
                            if 'test_a37_' in node and outcome == 'failed']
                assert len(failures) == 10
        print('BATCH_VALID: same-batch control RED; four diagonal RED / off-diagonal GREEN; A26 overlays RED; SHA256 restored', flush=True)


if __name__ == '__main__':
    main()
