"""Offline mutation evidence. Only relevant synthetic test selectors are executed.

Run with .venv-g2/bin/python tests/market/live_profile_mutations.py.
Each mutant runs in a fresh interpreter against a temporary source copy; the
working source is never edited. No SYNTHETIC_ONLY override or network is used.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
UNIT = "tests/market/test_live_profile.py::"
FLOW = "tests/market/test_live_replay.py::"
PROFILE = "market/live_profile.py"
CASES = [
    ("entry-concession-missing", PROFILE, 'factor = Decimal("1.001") if long else Decimal("0.999")',
     'factor = Decimal(1)', UNIT + "test_entry_concession_only_for_fuzzy_wording"),
    ("entry-direction-reversed", PROFILE, 'factor = Decimal("1.001") if long else Decimal("0.999")',
     'factor = Decimal("0.999") if long else Decimal("1.001")', UNIT + "test_entry_concession_only_for_fuzzy_wording"),
    ("entry-exact-also-shifted", PROFILE, 'if flags["entry_fuzzy"]:\n        factor',
     'if True:\n        factor', UNIT + "test_entry_concession_only_for_fuzzy_wording"),
    ("wording-unanchored", PROFILE, 'prices.issubset(numbers)', 'True', UNIT + "test_unresolved_or_unrelated_wording_is_exact"),
    ("wording-ambiguity-ignored", PROFILE, 'entry_candidates[0] if len(entry_candidates) == 1 else None',
     'entry_candidates[0] if entry_candidates else None', UNIT + "test_unresolved_or_unrelated_wording_is_exact"),
    ("wording-other-symbol", PROFILE, 'if tokens and not tokens.issubset({symbol, base_symbol}):',
     'if False:', UNIT + "test_opposite_opening_does_not_enable_entry_concession"),
    ("stop-breakout-missing", PROFILE, 'if flags["stop_breakout"]:', 'if False:', UNIT + "test_stop_breakout_then_widening"),
    ("stop-exact-also-widened", PROFILE, 'if flags["stop_fuzzy"]:', 'if True:', UNIT + "test_stop_breakout_then_widening"),
    ("tp-exact-also-shifted", PROFILE, '        if fuzzy:\n', '        if True:\n', UNIT + "test_take_profit_concession"),
    ("stop-breakout-direction", PROFILE, 'Decimal("0.997") if long else Decimal("1.003")',
     'Decimal("1.003") if long else Decimal("0.997")', UNIT + "test_stop_breakout_then_widening"),
    ("stop-widening-missing", PROFILE, 'stop *= Decimal("0.999") if long else Decimal("1.001")',
     'stop *= Decimal(1)', UNIT + "test_stop_breakout_then_widening"),
    ("stop-widening-direction", PROFILE, 'stop *= Decimal("0.999") if long else Decimal("1.001")',
     'stop *= Decimal("1.001") if long else Decimal("0.999")', UNIT + "test_stop_breakout_then_widening"),
    ("tp-concession-missing", PROFILE, 'D(tp["level"]) * (Decimal("0.999") if long else Decimal("1.001"))',
     'D(tp["level"])', UNIT + "test_take_profit_concession"),
    ("tp-direction-reversed", PROFILE, 'D(tp["level"]) * (Decimal("0.999") if long else Decimal("1.001"))',
     'D(tp["level"]) * (Decimal("1.001") if long else Decimal("0.999"))', UNIT + "test_take_profit_concession"),
    ("tick-direction-reversed", PROFILE, 'ROUND_CEILING if upward else ROUND_FLOOR',
     'ROUND_FLOOR if upward else ROUND_CEILING', UNIT + "test_tick_rounding_toward_fill_and_later_stop"),
    ("zone-depth-wrong", PROFILE, 'Decimal("0.85")', 'Decimal("1")', UNIT + "test_zone_depth_order_and_risk_shares"),
    ("zone-near-deep-risk-swapped", PROFILE,
     '("t1_near", Decimal("0"), Decimal("0.55"))', '("t1_near", Decimal("0"), Decimal("0.15"))',
     UNIT + "test_zone_depth_order_and_risk_shares"),
    ("zone-risk-as-quantity", PROFILE, 'a["risk_share"] / distance', 'a["risk_share"]',
     UNIT + "test_zone_depth_order_and_risk_shares"),
    ("pair-equal-quantity", PROFILE, 'Decimal(1) / D(e["price_lo"])', 'Decimal(1)', UNIT + "test_explicit_pair_equal_notional"),
    ("disabled-transform-applied", PROFILE, 'if not enabled:', 'if False:', UNIT + "test_market_reference_and_disabled_are_unchanged"),
    ("market-ref-shifted", PROFILE, 'if entry["kind"] == "market_ref":', 'if False:',
     UNIT + "test_market_reference_and_disabled_are_unchanged"),
    ("root-version-not-filtered", "data/api.py", 'pl.col("source_version_id").is_in(sorted(ids))', 'pl.lit(True)',
     FLOW + "test_bronze_accessor_uses_exact_root_versions_and_rejects_ambiguity"),
    ("pipeline-plan-not-transformed", "market/l0_replay.py", 'return dict(row, order_plan=plan), audit', 'return row, audit',
     FLOW + "test_four_live_policies_construct_and_run_kernel_a"),
    ("summary-rule-counts-zero", "market/l0_replay.py", 'sum(a["rule_counts"][rule] for a in live_records.values())', '0',
     FLOW + "test_l0_live_audit_and_summary_use_root_version_without_changing_source"),
    ("live-hash-flag-omitted", "market/contract.py", 'if self.live_execution_profile:', 'if False:',
     FLOW + "test_old_hashes_and_disabled_policy_bytes_are_pinned"),
]


def main():
    evidence = {"command": ".venv-g2/bin/python tests/market/live_profile_mutations.py", "mutations": []}
    with tempfile.TemporaryDirectory(prefix="quant-live-mutations-") as tmp:
        base = Path(tmp)
        for name, relative, before, after, selector in CASES:
            isolated = base / name
            shutil.copytree(ROOT / "src", isolated / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            path = isolated / "src" / "quant_lab" / relative
            source = path.read_text()
            assert source.count(before) == 1, (name, source.count(before))
            # Swap both shares so this mutant tests tranche order, not sum != 1.
            mutated = source.replace(before, after)
            if name == "zone-near-deep-risk-swapped":
                mutated = mutated.replace('("t3_deep", Decimal("0.85"), Decimal("0.15"))',
                                          '("t3_deep", Decimal("0.85"), Decimal("0.55"))')
            path.write_text(mutated)
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(isolated / "src"), str(ROOT))), PYTHONDONTWRITEBYTECODE="1")
            assert env.get("QUANT_LAB_SYNTHETIC_ONLY") != "1"
            # pytest config's pythonpath must point to this mutant, not the working tree.
            runner = isolated / "run.py"
            runner.write_text('import pathlib, quant_lab, pytest, sys\n'
                'assert pathlib.Path(quant_lab.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]).resolve())\n'
                'raise SystemExit(pytest.main(sys.argv[2:]))\n')
            args = [str(ROOT / ".venv-g2/bin/python"), str(runner), str(isolated / "src"),
                    "-c", str(ROOT / "pyproject.toml"), "-o", "pythonpath=" + str(isolated / "src"),
                    "-q", "--tb=short", str(ROOT / selector)]
            result = subprocess.run(args, cwd=isolated, env=env, capture_output=True, text=True, timeout=90)
            killed = result.returncode == 1 and " failed" in result.stdout and "ERROR collecting" not in result.stdout
            record = {"name": name, "file": relative, "test": selector, "before": before, "after": after,
                      "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "returncode": result.returncode,
                      "killed": killed, "output": result.stdout + result.stderr}
            evidence["mutations"].append(record)
            print(f"{name}: {'KILLED' if killed else 'FAILED_CHECK'}", flush=True)
        evidence["killed"] = sum(item["killed"] for item in evidence["mutations"])
        evidence["total"] = len(CASES)
    target = Path(__file__).with_name("live-profile-mutation-evidence.json")
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(f"{evidence['killed']}/{evidence['total']} mutants killed; {target.name}")
    return 0 if evidence["killed"] == evidence["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
