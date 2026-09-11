"""修复方串行源码突变取证工具；显式运行，非 pytest 自动收集。

仅改 market 生产源码，finally 按原字节还原；全树 SHA256 复核后才继续。
不将退出码用作还原证据。每个选择器的全部用例都必须 RED，再全部 GREEN。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/quant_lab/market"
EVIDENCE = ROOT / "tests/market/mutation_evidence.json"
R10 = "tests/market/test_review_p1_round10.py::"
GATE = "tests/market/test_single_source.py::"
CASES = []


def case(name, file, old, new, tests):
    CASES.append(dict(name=name, file=file, old=old, new=new, tests=tests))


case("S29-first-gap", "partition_check.py",
     'first_gap = bool(df.height and grid_points_between(cal_from, df[key][0], sec) > 0)',
     'first_gap = bool(df.height and df[key][0] > cal_from)',
     [R10 + "test_s29_subsecond_calendar_start_has_no_false_first_gap"])
case("S29-range-only", "partition_check.py", 'present = df.filter(legal)[key]',
     'present = df.filter((pl.col(key) >= cal_from) & (pl.col(key) < cal_to))[key]',
     [R10 + "test_s29_off_grid_never_covers_calendar"])
case("S29-truncated-count", "partition_check.py", 'exp_n = grid_points_between(cal_from, cal_to, sec)',
     'exp_n = int((cal_to - cal_from).total_seconds()) // sec',
     [R10 + "test_s29_subsecond_end_includes_last_grid_point"])
case("S31-remove-output-gate", "execution.py", '    check_policy_hash_consistency(df)\n', '',
     ["tests/market/test_outcome_kind.py::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes"])
case("S31-sanitized-copy", "execution.py", '    check_policy_hash_consistency(df)\n',
     '    check_policy_hash_consistency(df.with_columns(pl.lit("sanitized").alias("policy_hash")))\n',
     ["tests/market/test_outcome_kind.py::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes"])
case("S31-redacted-version", "execution.py", "{r['policy_version']} →", "REDACTED →",
     ["tests/market/test_outcome_kind.py::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes"])
case("S38-remove-domain", "contract.py", 'if value < 0:', 'if False:',
     [R10 + "test_s38_policy_rejects_negative_latency"])
case("S38-explicit-only", "contract.py", 'if exp_t_start < self.t_dec:',
     'if self.t_start is not None and self.t_start < self.t_dec:',
     [R10 + "test_s38_resolved_start_checked_for_both_spellings"])
expected = 'expected = grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS["1m"])'
case("S39-discard-return", "execution.py", expected,
     'grid_points_between(a, min(b, dt.datetime.now(dt.UTC)), INTERVAL_SECONDS["1m"])\n        expected = 0',
     [R10 + "test_s39_loader_grid_return_controls_coverage"])
case("S39-inline-count", "execution.py", expected,
     'expected = int((min(b, dt.datetime.now(dt.UTC)) - a).total_seconds() // INTERVAL_SECONDS["1m"])',
     [R10 + "test_s39_loader_grid_return_controls_coverage"])
case("S39-wrong-helper-value", "contract.py", 'return max(0, last_idx - first_idx + 1)',
     'return max(0, last_idx - first_idx + 1) + 7',
     [R10 + "test_s39_loader_grid_return_controls_coverage"])
case("S33-wrong-expected", "vision.py", 'return grid_points_between(a, b, INTERVAL_SECONDS[interval])',
     'return grid_points_between(a, b, INTERVAL_SECONDS[interval]) + 1',
     [R10 + "test_s33_aligned_calendar_equivalence"])
case("modified-existing-first-point-sentinel", "kernel_a.py",
     'first_expected = first_grid_point(self.t_start, bars[0].interval_s)',
     'first_expected = self.t_start',
     ["tests/market/test_constants_effective.py::test_grid_math_single_source_and_exact_to_microsecond"])
case("A24-real-caller-missing", "vision.py", 'return grid_points_between(a, b, INTERVAL_SECONDS[interval])',
     'return int((b - a).total_seconds() // INTERVAL_SECONDS[interval])',
     [GATE + "test_single_source_call_sites_match_registry"])
case("A24-real-caller-extra", "vision.py", None,
     '\ndef _unregistered_grid_probe():\n    return grid_points_between(a, b, 60)\n',
     [GATE + "test_single_source_call_sites_match_registry"])
case("A24-gate-missing-disabled", "single_source.py", '        if missing:\n', '        if False:\n',
     [GATE + "test_registry_gate_fails_when_mutated[missing]"])
case("A24-gate-extra-disabled", "single_source.py", '        if extra:\n', '        if False:\n',
     [GATE + "test_registry_gate_fails_when_mutated[extra]"])
patterns = [
    ("P1_inline_latency", "latency", "return t + dt.timedelta(seconds=policy.latency_s)"),
    ("P2_int_total_seconds", "int-seconds", "return int((b - a).total_seconds())"),
    ("P3_duration_div", "duration-div", "return (b - a).total_seconds() // interval_s"),
    ("P4_t_start_or_t_dec", "start-or", "return req.t_start or req.t_dec"),
    ("P5_inline_entry_ttl", "expiry-add", "return t + dt.timedelta(seconds=req.entry_ttl_s)"),
    ("P6_inline_grid_comparison", "middle-grid", "return o - prev > iv"),
    ("P6_inline_grid_comparison", "tail-grid", "return prev + iv < end"),
    ("P7_inline_ttl_resolution", "ttl-expression", "ttl = plan.expiry.entry_ttl_s if plan.expiry.entry_ttl_s is not None else policy.entry_ttl_s"),
    ("P7_inline_ttl_resolution", "ttl-branches", "ttl = plan.expiry.entry_ttl_s\n    if ttl is None:\n        ttl = policy.entry_ttl_s"),
]
for pattern, label, snippet in patterns:
    case("A24-real-inline-" + label, "vision.py", None, '\ndef _inline_probe():\n    ' + snippet + '\n',
         [GATE + "test_no_inline_reexpression_of_single_sources_anywhere_in_src"])
    case("A24-disable-pattern-" + label, "single_source.py", f'self._hit("{pattern}", node)', 'pass',
         [GATE + f"test_lint_gate_fails_on_injected_violation[{label}]"])
case("A24-foreign-freeze", "single_source.py", '        out.extend(lint.hits)',
     '        out.extend(hit for hit in lint.hits if not hit[2].startswith("research/"))',
     [GATE + "test_foreign_hits_registry_is_exact"])

# 归一后的每个真实调用方亦验证绕过会被登记/禁令发现。
case("S32-build-start-bypass", "contract.py",
     'horizon_end = derived_t_start(t_dec, policy) + dt.timedelta(seconds=derived_window_s(plan, policy, ttl))',
     'horizon_end = t_dec + dt.timedelta(seconds=policy.latency_s + derived_window_s(plan, policy, ttl))',
     [GATE + "test_single_source_call_sites_match_registry", GATE + "test_no_inline_reexpression_of_single_sources_anywhere_in_src"])
for label, old, new in [
    ("before", '"entry_ttl_s": resolve_entry_ttl_s(plan, pol)', '"entry_ttl_s": pol.entry_ttl_s'),
    ("after", 'exp_ttl = resolve_entry_ttl_s(self.order_plan, pol)', 'exp_ttl = pol.entry_ttl_s'),
    ("builder", 'ttl = resolve_entry_ttl_s(plan, policy)', 'ttl = policy.entry_ttl_s'),
]:
    case("S35-ttl-" + label, "contract.py", old, new, [GATE + "test_single_source_call_sites_match_registry"])
for label, following in [("timeline", "if deadline <= end:"), ("entries", "for i, (e, p, q) in enumerate(legs):")]:
    original = 'deadline = entry_expiry_at(self.t_start, self.req.entry_ttl_s)\n        ' + following
    inline = 'deadline = self.t_start + dt.timedelta(seconds=self.req.entry_ttl_s)\n        ' + following
    case("S35-expiry-A-" + label, "kernel_a.py", original, inline,
         [GATE + "test_single_source_call_sites_match_registry", GATE + "test_no_inline_reexpression_of_single_sources_anywhere_in_src"])
case("S35-expiry-B", "nautilus_adapter.py", 'deadline = entry_expiry_at(t_start, req.entry_ttl_s)',
     'deadline = t_start + dt.timedelta(seconds=req.entry_ttl_s)',
     [GATE + "test_single_source_call_sites_match_registry", GATE + "test_no_inline_reexpression_of_single_sources_anywhere_in_src"])
for label, old, new in [
    ("middle", 'if grid_points_between(prev + iv, o, interval_s) > 0:', 'if o - prev > iv:'),
    ("tail", 'if grid_points_between(prev + iv, end, interval_s) > 0:', 'if prev + iv < end:'),
]:
    case("S37-" + label, "kernel_a.py", old, new,
         [GATE + "test_no_inline_reexpression_of_single_sources_anywhere_in_src"])


def digest():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(SOURCE.rglob("*.py"))}


def run_tests(selectors):
    with tempfile.TemporaryDirectory(prefix="g2-mutation-") as directory:
        report = Path(directory) / "results.xml"
        command = [sys.executable, "-m", "pytest", *selectors, "-q", "-p", "no:cacheprovider", "--junitxml=" + str(report)]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH="src")
        result = subprocess.run(command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        nodes = []
        if report.exists():
            for node in ET.parse(report).iter("testcase"):
                status = "passed"
                if node.find("failure") is not None:
                    status = "failed"
                if node.find("error") is not None:
                    status = "error"
                nodes.append({"name": node.attrib["name"], "class": node.attrib["classname"], "status": status})
        return {"command": command, "exit_code": result.returncode, "tests": nodes, "output": result.stdout}


def main():
    results = []
    selected = CASES
    if len(sys.argv) > 1:
        selected = [c for c in CASES if c["name"] in sys.argv[1:]]
    for item in selected:
        path = SOURCE / item["file"]
        original = path.read_bytes()
        before = digest()
        source = original.decode()
        if item["old"] is None:
            mutated = source + item["new"]
        else:
            assert source.count(item["old"]) == 1, item["name"]
            mutated = source.replace(item["old"], item["new"])
        try:
            path.write_text(mutated)
            red = run_tests(item["tests"])
        finally:
            path.write_bytes(original)
            restored = digest()
            assert before == restored, "源码 SHA256 不一致：禁止继续"
        green = run_tests(item["tests"])
        entry = {**item, "red": red, "green": green, "sha256_before": before,
                 "sha256_restored": restored, "sha256_equal": before == restored}
        results.append(entry)
        EVIDENCE.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        all_red = red["tests"] and all(n["status"] == "failed" for n in red["tests"])
        all_green = green["tests"] and all(n["status"] == "passed" for n in green["tests"])
        print(f'{item["name"]}: RED rc={red["exit_code"]} ({len(red["tests"])} tests); GREEN rc={green["exit_code"]}; SHA256={before == restored}', flush=True)
        if red["exit_code"] != 1 or not all_red or green["exit_code"] != 0 or not all_green:
            print(red["output"] + green["output"], flush=True)
            raise SystemExit("突变未通过：停止，源码已按 SHA256 复核还原")


if __name__ == "__main__":
    main()
