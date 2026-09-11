"""A29/A33 real-disk mutation evidence. Run with .venv-g2/bin/python; no pytest mocks.

Copies are confined to tests/market and deleted after the batch. The child checks
module origins before pytest and again after conftest/collection. Every mutant
gets fresh baseline / injected / restored processes; restoration uses SHA256.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "tests/market/s42-s43-mutation-evidence.json"
CHILD = r'''
import importlib
import os
from pathlib import Path
import sys
root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root / 'src'), str(root)]
modules = ('contract', 'execution', 'kernel_a', 'single_source', 'partition_check', 'vision')
def verify():
    for name in modules:
        module = importlib.import_module('quant_lab.market.' + name)
        actual = Path(module.__file__).resolve()
        expected = root / 'src/quant_lab/market' / (name + '.py')
        assert actual == expected, (actual, expected)
    print('ISOLATION_OK pid=' + str(os.getpid()) + ' root=' + str(root), flush=True)
verify()
import pytest
class Origins:
    def pytest_collection_finish(self, session):
        verify()
        for item in session.items:
            assert Path(item.path).resolve().is_relative_to(root / 'tests/market')
        print('COLLECTION_ORIGINS_OK items=' + str(len(session.items)), flush=True)
sys.exit(pytest.main(['-c', str(root / 'pytest.ini'), '--confcutdir=' + str(root),
    'tests/market/test_single_source.py', '-q', '-p', 'no:cacheprovider'], plugins=[Origins()]))
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(root):
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(('PYTHON', 'PYTEST')):
            env.pop(key)
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    result = subprocess.run([sys.executable, '-B', '-c', CHILD, str(root)], cwd=root,
                            env=env, capture_output=True, text=True, timeout=180)
    output = result.stdout + result.stderr
    assert 'ISOLATION_OK' in output and 'COLLECTION_ORIGINS_OK' in output, output
    print(output, flush=True)
    return {'exit_code': result.returncode, 'output': output}


def main():
    assert os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'
    # No copied pyproject, inherited pytest options, bytecode, or installed-package fallback.
    with tempfile.TemporaryDirectory(prefix='.s42-s43-', dir=ROOT / 'tests/market') as name:
        root = Path(name).resolve()
        ignore = shutil.ignore_patterns('__pycache__', '.s42-s43-*', 's42-s43-mutation-evidence.json')
        shutil.copytree(ROOT / 'src', root / 'src', ignore=ignore)
        shutil.copytree(ROOT / 'tests/market', root / 'tests/market', ignore=ignore)
        (root / 'tests/__init__.py').write_text('')
        (root / 'pytest.ini').write_text('[pytest]\nfilterwarnings = ignore::DeprecationWarning\n')
        kernel = root / 'src/quant_lab/market/kernel_a.py'
        loader = root / 'src/quant_lab/market/execution.py'
        cases = [
            ('known_red_control', kernel, 'if opened != expected:', 'if opened == expected:',
             'test_differential_kernel_grid'),
            ('S42_original_seconds_truncation', kernel,
             'opens = sorted(b.open_time for b in bars',
             'opens = sorted(b.open_time.replace(microsecond=0) for b in bars',
             'test_differential_kernel_public_interior_bar'),
            ('loader_seconds_truncation', loader, 'opened = bar.open_time',
             'opened = bar.open_time.replace(microsecond=0)',
             'test_differential_lake_public_interior_bar'),
        ]
        records = []
        for label, target, before, after, required_failure in cases:
            pristine = target.read_bytes()
            source = pristine.decode()
            assert source.count(before) == 1, label
            entry = {'case': label, 'target': str(target.relative_to(root)),
                     'before_sha256': digest(pristine), 'replacement': [before, after]}
            print('\nCASE ' + label + ' BASELINE', flush=True)
            entry['baseline'] = run(root)
            assert entry['baseline']['exit_code'] == 0
            try:
                target.write_text(source.replace(before, after))
                entry['injected_sha256'] = digest(target.read_bytes())
                assert entry['injected_sha256'] != entry['before_sha256']
                print('CASE ' + label + ' INJECTED', flush=True)
                entry['injected'] = run(root)
            finally:
                target.write_bytes(pristine)
                entry['restored_sha256'] = digest(target.read_bytes())
                assert entry['restored_sha256'] == entry['before_sha256']
            print('CASE ' + label + ' RESTORED sha256=' + entry['restored_sha256'], flush=True)
            entry['restored'] = run(root)
            records.append(entry)
            OUTPUT.write_text(json.dumps(records, indent=2, ensure_ascii=False) + '\n')
            assert entry['restored']['exit_code'] == 0
            # A26: a green injection is NOT accepted; stop here for an overlay investigation.
            assert entry['injected']['exit_code'] == 1, 'GREEN: requires A26 overlay: ' + label
            assert 'FAILED tests/market/test_single_source.py::' + required_failure in entry['injected']['output']
        assert records[0]['case'] == 'known_red_control'
        print('BATCH_VALID: control RED; all mutations behaviorally RED; all SHA256 restored', flush=True)
        print('Evidence: ' + str(OUTPUT), flush=True)


if __name__ == '__main__':
    main()
