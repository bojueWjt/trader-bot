"""前台执行，逐次恢复并校验文件字节；证据写入同目录。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
TARGET = Path('src/quant_lab/market/single_source.py')
TEST = 'tests/market/test_single_source.py::'
REPORT = Path('tests/market/p11_mutation_results.json')
rows = []


def run_case(label, test, transform, overlay=None, target=TARGET):
    raw = target.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    def run():
        result = subprocess.run([sys.executable, '-m', 'pytest', test, '-q', '-p', 'no:cacheprovider'], text=True, capture_output=True)
        return {'exit': result.returncode, 'output': result.stdout + result.stderr}
    row = {'case': label, 'test': test, 'sha256_before': sha}
    try:
        modified = transform(raw.decode())
        assert modified != raw.decode(), label
        target.write_text(modified)
        row['injected'] = run()
        if row['injected']['exit'] == 0:
            assert overlay is not None, 'A26 overlay required: ' + label
            target.write_text(overlay(modified))
            row['overlay'] = run()
            assert row['overlay']['exit'] == 1, row
            row['classification'] = 'A23 非行为证据、不可达防御；A26 叠加破坏性质后 RED'
        else:
            assert row['injected']['exit'] == 1, row
            row['classification'] = 'RED'
    finally:
        target.write_bytes(raw)
        restored = hashlib.sha256(target.read_bytes()).hexdigest()
        row['sha256_after'] = restored
        row['SHA256_EQUAL'] = restored == sha
        assert restored == sha, 'RESTORATION CORRUPTION'
    row['restored'] = run()
    rows.append(row)
    REPORT.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    print(label, 'INJECTED', row['injected']['exit'], 'OVERLAY', row.get('overlay', {}).get('exit'), 'SHA256_EQUAL', row['SHA256_EQUAL'], 'GREEN', row['restored']['exit'], flush=True)
    assert row['restored']['exit'] == 0, row


def disable(pattern):
    return lambda s: s.replace('self._hit("' + pattern + '", node)', 'pass # injected ' + pattern)


if __name__ == '__main__':
    patterns = [
        ('P1_inline_latency', 'latency'), ('P2_int_total_seconds', 'int-seconds'),
        ('P3_duration_div', 'duration-div'), ('P4_t_start_or_t_dec', 'start-or'),
        ('P5_inline_entry_ttl', 'expiry-add'), ('P6_inline_grid_comparison', 'middle-grid'),
        ('P6_inline_grid_comparison', 'tail-grid'), ('P7_inline_ttl_resolution', 'ttl-expression'),
        ('P7_inline_ttl_resolution', 'ttl-branches'),
    ]
    for pattern, ident in patterns:
        run_case(pattern + ':' + ident, TEST + 'test_lint_gate_fails_on_injected_violation[' + ident + ']', disable(pattern))
    for ident in ['annotated', 'augmented', 'named', 'return', 'comprehension', 'return-field']:
        primary = lambda s: s.replace('    def visit_IfExp(self, node):', '    def unused_IfExp(self, node):')
        if ident == 'annotated':
            primary = lambda s: s.replace('visit_Assign = visit_AnnAssign = visit_NamedExpr', 'visit_Assign = visit_NamedExpr')
        if ident == 'augmented':
            primary = lambda s: s.replace('    def visit_AugAssign(self, node):', '    def unused_AugAssign(self, node):')
        if ident in ['named', 'comprehension']:
            primary = lambda s: s.replace('visit_Assign = visit_AnnAssign = visit_NamedExpr', 'visit_Assign = visit_AnnAssign')
        if ident == 'return-field':
            primary = lambda s: s.replace('    def visit_Return(self, node):', '    def unused_Return(self, node):')
        run_case('P7:' + ident, TEST + 'test_ttl_standard_nodes[' + ident + ']', primary, disable('P7_inline_ttl_resolution'))
    for assignment in ['delta = o - prev', 'delta: int = o - prev', 'delta -= prev']:
        run_case('P6:' + assignment, TEST + 'test_grid_local_definition[' + assignment + ']', lambda s: s.replace('value = self.local_defs.get(value.id, value)', 'value = value'))
    for pattern, ident in [('P3_duration_div', 'duration-augmented'), ('P5_inline_entry_ttl', 'expiry-augmented')]:
        run_case(pattern + ':AugAssign', TEST + 'test_augmented_binary_patterns[' + ident + ']', disable(pattern))
    run_case('S40-count', TEST + 'test_single_source_use_counts_match_registry',
             lambda s: s.replace('if grid_points_between(prev + iv, o, interval_s) > 0:', 'delta = o - prev\n            if delta > iv:'),
             target=Path('src/quant_lab/market/kernel_a.py'))

    metadata_test = 'tests/market/test_review_p1_round2.py::test_c3_s12_b_rejects_stale_policy_and_registry_pins_hash'
    run_case('B20-placeholder', metadata_test,
             lambda s: s.replace('"_placeholder_fields": []', '"_placeholder_fields": ["research_horizon_s"]'),
             target=Path('src/quant_lab/market/policy_hashes.json'))
    run_case('B21-provenance', metadata_test,
             lambda s: s.replace('contracts.version', 'policy_hash'),
             target=Path('src/quant_lab/market/policy_hashes.json'))
