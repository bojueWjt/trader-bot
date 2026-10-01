"""Offline mutation checks in isolated source copies, with fresh pytest processes.

Run: .venv-g2/bin/python tests/market/close_stop_mutations.py
The working tree source is never mutated. Evidence is written beside this script.
"""

from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DATA = "tests/data/test_close_stop_mapping.py::"
MARKET = "tests/market/test_close_stop.py::"
CASES = [
    (
        "close-semantics",
        "data/close_stop.py",
        "if not _CLOSE.search(condition):",
        "if False:",
        DATA + "test_unmapped_conditions_remain_unsupported",
    ),
    (
        "price-required",
        "data/close_stop.py",
        "price is None or",
        "False or",
        DATA + "test_unmapped_conditions_remain_unsupported",
    ),
    (
        "finite-price",
        "data/close_stop.py",
        "not price.is_finite() or ",
        "",
        DATA + "test_unmapped_conditions_remain_unsupported",
    ),
    (
        "positive-price",
        "data/close_stop.py",
        "or price <= 0",
        "",
        DATA + "test_unmapped_conditions_remain_unsupported",
    ),
    (
        "unique-period",
        "data/close_stop.py",
        "if len(periods) != 1:",
        "if not periods:",
        DATA + "test_unmapped_conditions_remain_unsupported",
    ),
    (
        "canonical-level",
        "data/validate.py",
        'stop = close_stop["level"]',
        "stop = None",
        DATA + "test_v2_canonical_and_lifecycle_preserve_condition_over_chart",
    ),
    (
        "canonical-metadata",
        "data/validate.py",
        'checks["stop_trigger"] =',
        'checks["wrong_trigger"] =',
        DATA + "test_v2_canonical_and_lifecycle_preserve_condition_over_chart",
    ),
    (
        "canonical-eligibility",
        "data/validate.py",
        'stop = close_stop["level"]',
        'mapping_issues.append({"field":"stop","reason":"mutant"})\n                stop = close_stop["level"]',
        DATA + "test_v2_canonical_and_lifecycle_preserve_condition_over_chart",
    ),
    (
        "canonical-chart-no-fallback",
        "data/validate.py",
        "                stop = None\n            else:",
        "                stop = Decimal(77)\n            else:",
        DATA + "test_v2_unsupported_does_not_fall_back_to_chart",
    ),
    (
        "lifecycle-close-plan",
        "data/lifecycle.py",
        '"trigger": "close", "timeframe": close_stop["timeframe"]',
        '"trigger": "mark", "timeframe": close_stop["timeframe"]',
        DATA + "test_v2_canonical_and_lifecycle_preserve_condition_over_chart",
    ),
    (
        "lifecycle-chart-no-fallback",
        "data/lifecycle.py",
        "            stop_plan = None\n            if close_stop",
        '            stop_plan = {"price":77,"trigger":"mark"}\n            if close_stop',
        DATA + "test_v2_unsupported_does_not_fall_back_to_chart",
    ),
    (
        "close-timeframe-required",
        "market/contract.py",
        'if self.trigger == "close" and self.timeframe is None:',
        "if False:",
        MARKET + "test_stop_timeframe_contract_rejects_invalid_combinations",
    ),
    (
        "mark-timeframe-forbidden",
        "market/contract.py",
        'if self.trigger == "mark" and self.timeframe is not None:',
        "if False:",
        MARKET + "test_stop_timeframe_contract_rejects_invalid_combinations",
    ),
    (
        "timeframe-enum",
        "market/contract.py",
        '"1d", "1w"] | None = None',
        '"1d", "1w", "5m"] | None = None',
        MARKET + "test_stop_timeframe_contract_rejects_invalid_combinations",
    ),
    (
        "event-close-only-stop",
        "market/contract.py",
        'e.trigger_basis in ("mark", "close")',
        'e.trigger_basis == "mark"',
        MARKET + "test_close_basis_is_only_allowed_on_stop_event",
    ),
    (
        "no-mark-fallback",
        "market/kernel_a.py",
        'self.plan.stop.trigger != "mark" or ',
        "",
        MARKET + "test_wicks_and_equal_close_do_not_stop",
    ),
    (
        "entry-required",
        "market/kernel_a.py",
        'stop.trigger != "close" or self.pos == 0 or',
        'stop.trigger != "close" or',
        MARKET + "test_close_hit_requires_position_and_exact_one_minute_evidence",
    ),
    (
        "only-close-point",
        "market/kernel_a.py",
        'or p.path_step != "C"',
        "",
        MARKET + "test_close_hit_requires_position_and_exact_one_minute_evidence",
    ),
    (
        "closed-timestamp",
        "market/kernel_a.py",
        "if p.ts != end - US:",
        "if False:",
        MARKET + "test_close_hit_requires_position_and_exact_one_minute_evidence",
    ),
    (
        "one-minute-bars",
        "market/kernel_a.py",
        "if b.interval_s == 60}",
        "if True}",
        MARKET + "test_close_hit_requires_position_and_exact_one_minute_evidence",
    ),
    (
        "bar-evidence",
        "market/kernel_a.py",
        "if (opened, p.price) not in self.close_bars:",
        "if False:",
        MARKET + "test_explicit_c_label_is_not_bar_close_evidence",
    ),
    (
        "weekly-monday",
        "market/kernel_a.py",
        "end.weekday() == 0",
        "end.weekday() == 1",
        MARKET + "test_four_hour_and_week_are_utc_aligned",
    ),
    (
        "utc-origin",
        "market/kernel_a.py",
        "(end - epoch).total_seconds()",
        "(end - epoch + dt.timedelta(hours=1)).total_seconds()",
        MARKET + "test_all_utc_periods_and_week_monday",
    ),
    (
        "period-boundary",
        "market/kernel_a.py",
        "if not aligned:",
        "if False:",
        MARKET + "test_all_utc_periods_and_week_monday",
    ),
    (
        "strict-long",
        "market/kernel_a.py",
        "return p.price < stop.price if",
        "return p.price <= stop.price if",
        MARKET + "test_wicks_and_equal_close_do_not_stop",
    ),
    (
        "strict-short",
        "market/kernel_a.py",
        "else p.price > stop.price",
        "else p.price >= stop.price",
        MARKET + "test_wicks_and_equal_close_do_not_stop",
    ),
    (
        "long-direction",
        "market/kernel_a.py",
        "return p.price < stop.price if",
        "return p.price > stop.price if",
        MARKET + "test_daily_close_strict_direction_next_last_and_r",
    ),
    (
        "short-direction",
        "market/kernel_a.py",
        "else p.price > stop.price",
        "else p.price < stop.price",
        MARKET + "test_daily_close_strict_direction_next_last_and_r",
    ),
    (
        "next-last-fill",
        "market/kernel_a.py",
        "                    self.match_point(ts, p)\n                    if self.close_stop_hit(p):\n                        close_point = p",
        '                    if self.close_stop_hit(p):\n                        self.trigger_stop(ts,p.bar_open_time,p.path_step,basis="close",price=p.price)\n                    self.match_point(ts,p)',
        MARKET + "test_daily_close_strict_direction_next_last_and_r",
    ),
    (
        "trigger-price-last",
        "market/kernel_a.py",
        'basis="close", price=close_point.price',
        'basis="close", price=self.mark',
        MARKET + "test_daily_close_strict_direction_next_last_and_r",
    ),
    (
        "trigger-basis-close",
        "market/kernel_a.py",
        'basis="close", price=close_point.price',
        'basis="mark", price=close_point.price',
        MARKET + "test_daily_close_strict_direction_next_last_and_r",
    ),
    (
        "pending-entry-cancel",
        "market/kernel_a.py",
        "                close_point = None",
        '                if self.plan.stop.trigger == "close":\n                    self.set_exit_latch = lambda ts: None\n                close_point = None',
        MARKET + "test_close_trigger_cancels_pending_entry_and_tp",
    ),
    (
        "pending-tp-cancel",
        "market/kernel_a.py",
        '            self.cancel(ts, o, "sl_triggered")',
        '            if self.plan.stop.trigger == "mark":\n                self.cancel(ts, o, "sl_triggered")',
        MARKET + "test_close_trigger_cancels_pending_entry_and_tp",
    ),
    (
        "b-reject",
        "market/nautilus_adapter.py",
        'if req.order_plan.stop.trigger == "close":',
        "if False:",
        MARKET + "test_b_explicitly_rejects_close",
    ),
    (
        "persisted-timeframe",
        "data/lifecycle.py",
        '"trigger": pl.String, "timeframe": pl.String',
        '"trigger": pl.String',
        DATA + "test_persisted_graph_through_l0_request_and_execution",
    ),
    (
        "validate-version",
        "data/validate.py",
        "tg45-validate-v0.6",
        "tg45-validate-v0.5",
        DATA + "test_canonical_and_graph_rules_invalidate_previous_artifacts",
    ),
    (
        "lifecycle-version",
        "data/lifecycle.py",
        "tg-lifecycle-v0.6",
        "tg-lifecycle-v0.5",
        DATA + "test_canonical_and_graph_rules_invalidate_previous_artifacts",
    ),
    (
        "kernel-version",
        "market/kernel_a.py",
        "kernel-a-v0.3",
        "kernel-a-v0.2",
        MARKET + "test_kernel_build_identity_changes_from_pre_close_rules",
    ),
]
for tf in ("15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"):
    CASES.append(
        (
            "parser-period-" + tf,
            "data/close_stop.py",
            f'"{tf}": re.compile',
            f'"{tf}": re.compile',
            DATA + "test_parse_known_close_timeframes",
        )
    )
    # Pattern disabling is performed below so the regex source stays readable.


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    sources = ROOT / "src/quant_lab"
    before = {str(p.relative_to(ROOT)): sha(p) for p in sources.rglob("*.py")}
    results = []
    with tempfile.TemporaryDirectory(
        prefix="close-stop-mutations-", dir="/tmp"
    ) as temporary:
        copied = Path(temporary) / "quant_lab"
        shutil.copytree(sources, copied, ignore=shutil.ignore_patterns("__pycache__"))
        for label, module, old, new, test in CASES:
            path = copied / module
            original = path.read_text()
            if label.startswith("parser-period-"):
                tf = label.removeprefix("parser-period-")
                old = "_PERIODS = {tf: re.compile(pattern, re.I) for tf, pattern in _PATTERNS.items()}"
                new = old + f'\n_PERIODS["{tf}"] = re.compile(r"(?!)")'
            assert original.count(old) == 1, (label, original.count(old))
            path.write_text(original.replace(old, new))
            executable = ROOT / (
                ".venv-g1/bin/python"
                if test.startswith(DATA)
                else ".venv-g2/bin/python"
            )
            # Confirm import origin before pytest; override repository pythonpath.
            code = (
                "import quant_lab,pytest; "
                f"assert quant_lab.__file__.startswith({str(copied)!r}),quant_lab.__file__; "
                f'raise SystemExit(pytest.main(["-o",{("pythonpath="+temporary)!r},"-q","-p","no:cacheprovider",{test!r}]))'
            )
            try:
                run = subprocess.run(
                    [str(executable), "-B", "-c", code],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "PYTHONPATH": temporary,
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=120,
                )
                summary = [
                    line
                    for line in run.stdout.splitlines()
                    if "failed" in line or line.startswith("FAILED ")
                ]
                results.append(
                    dict(
                        rule=label,
                        module=module,
                        test=test,
                        exit_code=run.returncode,
                        killed=run.returncode == 1
                        and any(" failed" in line for line in summary),
                        summary=summary,
                    )
                )
                print(
                    label, run.returncode, summary[-1:] or run.stdout[-300:], flush=True
                )
            finally:
                path.write_text(original)
    after = {str(p.relative_to(ROOT)): sha(p) for p in sources.rglob("*.py")}
    report = dict(
        method="isolated source-copy mutation; fresh subprocess and import-origin assertion",
        results=results,
        working_sources_unchanged=before == after,
    )
    (ROOT / "tests/market/close_stop_mutation_results.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    assert before == after, "Working source changed during audit"
    assert all(
        result["killed"] for result in results
    ), "Surviving mutant or infrastructure error"
    print(f"{len(results)}/{len(results)} mutants killed; working sources unchanged")


if __name__ == "__main__":
    main()
