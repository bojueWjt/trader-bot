"""A28/A29：在真实 market 源文件写盘突变，每阶段新进程，finally 写回并核 SHA256。

仅定向运行语义差分；原静态门不参与杀死突变。每次导入均断言 __file__ 是被写盘文件。
若仍绿，先还原并设计同路径的性质破坏叠加突变（A26），不把绿直接判为合格/不合格。
运行：PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python tests/market/a28_mutations.py
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MARKET = ROOT / 'src/quant_lab/market'
OUT = ROOT / 'tests/market/a28_mutation_results.json'
RECEIPT = ROOT / 'tests/market/a28_mutation_receipt.md'
TEST = 'tests/market/test_single_source.py::test_differential_'

# (ID, source module, target test, unique old text, mutant)
CASES = [
    ('partition-count', 'partition_check', 'partition_grid',
     'exp_n = grid_points_between(cal_from, cal_to, sec)',
     'exp_n = max(0, int((cal_to - cal_from).total_seconds()) // sec)'),
    ('partition-first', 'partition_check', 'partition_grid',
     'first_grid_point(t, sec) == t', 'int(t.timestamp()) % sec == 0'),
    ('partition-middle', 'partition_check', 'partition_grid',
     'grid_points_between(prev[i] + step, df[key][i], sec) > 0',
     'grid_points_between(prev[i] + step, df[key][i], sec) >= 0'),
    ('vision-count', 'vision', 'vision_count',
     'return grid_points_between(a, b, INTERVAL_SECONDS[interval])',
     'return int((b - a).total_seconds()) // INTERVAL_SECONDS[interval] + 1'),
    ('kernel-first', 'kernel_a', 'kernel_grid',
     'first_expected = first_grid_point(self.t_start, bars[0].interval_s)',
     'first_expected = dt.datetime.fromtimestamp(-(-int(self.t_start.timestamp()) // interval_s) * interval_s, dt.UTC)'),
    ('kernel-middle', 'kernel_a', 'kernel_grid',
     'if grid_points_between(prev + iv, o, interval_s) > 0:',
     'if grid_points_between(prev + iv, o, interval_s) >= 0:'),
    ('kernel-tail', 'kernel_a', 'kernel_grid',
     'if grid_points_between(prev + iv, end, interval_s) > 0:',
     'if grid_points_between(prev + iv, end, interval_s) >= 0:'),
    ('lake-count', 'execution', 'lake_grid',
     'expected = grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS["1m"])',
     'expected = max(0, int((min(b, dt.datetime.now(dt.UTC)) - a).total_seconds()) // INTERVAL_SECONDS["1m"])'),
    ('lake-start', 'execution', 'start',
     'a = req.resolved_t_start(_rp(req.policy_version)) - dt.timedelta(seconds=window_before_s)',
     'a = (req.t_start or req.t_dec) - dt.timedelta(seconds=window_before_s)'),
    ('start-validator', 'contract', 'start',
     'exp_t_start = derived_t_start(self.t_dec, pol)',
     'exp_t_start = (self.t_dec + dt.timedelta(seconds=pol.latency_s)).replace(microsecond=0)'),
    ('start-resolver', 'contract', 'start',
     'return self.t_start if self.t_start is not None else derived_t_start(self.t_dec, policy)',
     'return self.t_start if self.t_start is not None else (self.t_dec + dt.timedelta(seconds=policy.latency_s)).replace(microsecond=0)'),
    ('start-builder', 'contract', 'start',
     'horizon_end = derived_t_start(t_dec, policy) + dt.timedelta(seconds=derived_window_s(plan, policy, ttl))',
     'horizon_end = (t_dec + dt.timedelta(seconds=policy.latency_s)).replace(microsecond=0) + dt.timedelta(seconds=derived_window_s(plan, policy, ttl))'),
    ('start-lower-bound', 'contract', 'start',
     'if self.horizon_end <= exp_t_start:', 'if self.horizon_end < exp_t_start:'),
    ('window-validator', 'contract', 'window',
     'window = self.horizon_end - exp_t_start',
     'window = dt.timedelta(seconds=int((self.horizon_end - exp_t_start).total_seconds()))'),
    ('window-builder', 'contract', 'window',
     'horizon_end = derived_t_start(t_dec, policy) + dt.timedelta(seconds=derived_window_s(plan, policy, ttl))',
     'horizon_end = derived_t_start(t_dec, policy) + dt.timedelta(seconds=ttl + (plan.expiry.max_holding_s or policy.max_horizon_s))'),
    ('window-derived-validator', 'contract', 'window',
     'derived_s = derived_window_s(self.order_plan, pol, exp_ttl)',
     'derived_s = exp_ttl + (self.order_plan.expiry.max_holding_s or pol.max_horizon_s)'),
    ('ttl-before', 'contract', 'ttl',
     '"entry_ttl_s": resolve_entry_ttl_s(plan, pol)', '"entry_ttl_s": pol.entry_ttl_s'),
    ('ttl-validator', 'contract', 'ttl',
     'exp_ttl = resolve_entry_ttl_s(self.order_plan, pol)', 'exp_ttl = pol.entry_ttl_s'),
    ('ttl-builder', 'contract', 'ttl',
     'ttl = resolve_entry_ttl_s(plan, policy)', 'ttl = policy.entry_ttl_s'),
    ('expiry-timeline', 'kernel_a', 'expiry_timeline',
     'deadline = entry_expiry_at(self.t_start, self.req.entry_ttl_s)\n        if deadline <= end:',
     'deadline = (self.t_start + dt.timedelta(seconds=self.req.entry_ttl_s)).replace(microsecond=0)\n        if deadline <= end:'),
    ('expiry-timeline-bound', 'kernel_a', 'expiry_timeline',
     'if deadline <= end:', 'if deadline < end:'),
    ('expiry-orders', 'kernel_a', 'expiry_orders',
     'deadline = entry_expiry_at(self.t_start, self.req.entry_ttl_s)\n        for i, (e, p, q) in enumerate(legs):',
     'deadline = (self.t_start + dt.timedelta(seconds=self.req.entry_ttl_s)).replace(microsecond=0)\n        for i, (e, p, q) in enumerate(legs):'),
    ('expiry-b', 'nautilus_adapter', 'expiry_b',
     'deadline = entry_expiry_at(t_start, req.entry_ttl_s)',
     'deadline = (t_start + dt.timedelta(seconds=req.entry_ttl_s)).replace(microsecond=0)'),
    ('expiry-b-bound', 'nautilus_adapter', 'expiry_b',
     'TimeInForce.GTD if deadline <= end else TimeInForce.GTC',
     'TimeInForce.GTD if deadline < end else TimeInForce.GTC'),
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(module, test):
    path = MARKET / f'{module}.py'
    code = (
        'import importlib,pathlib,pytest,sys; '
        f'm=importlib.import_module("quant_lab.market.{module}"); '
        f'assert pathlib.Path(m.__file__).resolve()==pathlib.Path({str(path)!r}).resolve(),m.__file__; '
        'print("IMPORTED",m.__file__,flush=True); '
        f'sys.exit(pytest.main([{TEST + test!r},"-q","-p","no:cacheprovider","--tb=short"]))'
    )
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(ROOT / 'src')}
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    return {'sha256': sha(path), 'exit_code': result.returncode, 'output': result.stdout}


def persist(records):
    OUT.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n')
    text = ['# A28 真实树突变回执', '',
            '每阶段新 Python 进程，导入路径断言指向真实被写盘文件；只运行差分测试。', '',
            '| 注入 | 差分 | 基线/注入/还原 exit | 还原 SHA256 相等 |', '|---|---|---|---|']
    for row in records:
        codes = '/'.join(str(row[phase]['exit_code']) for phase in ('baseline', 'mutant', 'restored'))
        text.append(f"| {row['id']} | {row['test']} | {codes} | {row['sha_equal']} |")
    for row in records:
        text.extend(['', f"## {row['id']} — {row['file']}", '', '真实替换：',
                     '```python', row['old'], '# →', row['new'], '```'])
        for phase in ('baseline', 'mutant', 'overlay', 'restored'):
            if phase not in row:
                continue
            item = row[phase]
            text.extend(['', f"### {phase}（exit {item['exit_code']}）", '',
                         f"SHA256 `{item['sha256']}`", '', '```text', item['output'].rstrip(), '```'])
        text.append(f"\n内容还原相等：{row['sha_equal']}；裁定：{row['verdict']}。")
    RECEIPT.write_text('\n'.join(text) + '\n')


def main():
    # 本轮证据固定于当前真实树；失败也保留原始输出，不覆写为成功。
    records = []
    policy = MARKET / 'policy_hashes.json'
    policy_before = (sha(policy), policy.stat().st_mtime_ns)
    for name, module, test, old, new in CASES:
        path = MARKET / f'{module}.py'
        original = path.read_bytes()
        source = original.decode()
        assert source.count(old) == 1, (name, source.count(old))
        row = {'id': name, 'file': str(path.relative_to(ROOT)), 'test': TEST + test,
               'old': old, 'new': new, 'test_sha256': sha(ROOT / 'tests/market/test_single_source.py')}
        row['baseline'] = run(module, test)
        assert row['baseline']['exit_code'] == 0, row
        try:
            path.write_text(source.replace(old, new))
            row['mutant'] = run(module, test)
            # A26：仍绿不能直接裁定。保留证据并停在已还原状态，先设计同性质叠加突变。
            # 不用任意 raise / 语法错误作为叠加，那不证明性质检查有效。
        finally:
            path.write_bytes(original)
            assert sha(path) == hashlib.sha256(original).hexdigest(), name
        row['restored'] = run(module, test)
        row['sha_equal'] = row['baseline']['sha256'] == row['restored']['sha256']
        row['verdict'] = '非行为证据：需排查运行错误'
        if row['mutant']['exit_code'] == 1 and 'FAILED' in row['mutant']['output']:
            row['verdict'] = 'RED→内容还原一致→GREEN'
        if row['mutant']['exit_code'] == 0:
            row['verdict'] = '待 A26 同性质叠加；尚不裁定合格或不合格'
        records.append(row)
        persist(records)
        print(name, 'baseline=', row['baseline']['exit_code'], 'mutant=', row['mutant']['exit_code'],
              'restored=', row['restored']['exit_code'], 'sha_equal=', row['sha_equal'], flush=True)
        assert policy_before == (sha(policy), policy.stat().st_mtime_ns)
        assert row['restored']['exit_code'] == 0, row
        if row['mutant']['exit_code'] == 0:
            return 2
        assert row['mutant']['exit_code'] == 1 and 'FAILED' in row['mutant']['output'], row
    return 0


if __name__ == '__main__':
    sys.exit(main())
